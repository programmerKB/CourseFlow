"""Transactional outbox: leases committed before delivery, token guarded completion.

Every email (registration confirmations, timetables and allocation
results) uses one transport chosen by GMAIL_TRANSPORT:

* ``smtp`` (default): Gmail SMTP with an app password (smtp.gmail.com:587, STARTTLS). With the
  default SMTP_HOST=mailpit and no account the messages land in the bundled Mailpit inbox.
* ``api``: the Gmail API (``users.messages.send``), the transport named by the teacher specification.
"""
import argparse
import json
import logging
import signal
import smtplib
import ssl
import threading
from contextlib import closing
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import urlencode
from uuid import uuid4
from sqlalchemy import select, update, or_, and_
from .db import SessionLocal
from .config import settings
from .gmail import GMAIL_HINTS, GmailError, build_confirmation, sender, send_message
from .models import NotificationOutbox, User
from .registration import CONFIRMATION_TYPE, now

log = logging.getLogger('courses.worker')

SMTP_HINTS = {
    'SMTP_NOT_CONFIGURED': 'SMTP_HOST 是 smtp.gmail.com，但 SMTP_USER 或 SMTP_PASSWORD 未填。填入 Gmail 地址與 16 碼'
                           '應用程式密碼後重建 api 與 worker，再執行 manage retry-failed 重寄。',
    'SMTP_AUTH_FAILED': 'Gmail 拒絕登入：SMTP_USER 須是完整 Gmail 地址，SMTP_PASSWORD 須是 16 碼「應用程式密碼」'
                        '（帳戶要先開啟兩步驟驗證），不是 Gmail 登入密碼。修正後執行 manage retry-failed 重寄。',
    'SMTP_UNREACHABLE': '連不到 SMTP 伺服器：核對 SMTP_HOST、SMTP_PORT（Gmail 為 smtp.gmail.com、587）與主機對外網路。',
    'SMTP_STARTTLS_UNSUPPORTED': '伺服器不支援 STARTTLS；請核對 SMTP_HOST、SMTP_PORT 與 SMTP_STARTTLS。Gmail 使用 smtp.gmail.com:587 並開啟 STARTTLS。',
    'SMTP_AUTH_UNSUPPORTED': '伺服器不支援帳密登入；若目前連到 Mailpit，寄送真實郵件需改成 smtp.gmail.com。Mailpit 預覽請清空 SMTP_USER 與 SMTP_PASSWORD。',
}


class SMTPConfigurationError(smtplib.SMTPNotSupportedError):
    """Only predefined codes/hints are stored; provider responses may contain private data."""
    def __init__(self, code, *, permanent=True):
        self.code = code
        self.permanent = permanent
        super().__init__(SMTP_HINTS[code])


def error_code(error):
    return error.code if isinstance(error, (SMTPConfigurationError, GmailError)) else type(error).__name__


def smtp_password():
    # Google shows app passwords in four groups ("abcd efgh ijkl mnop"); the spaces are not part of it.
    return settings.smtp_password.get_secret_value().replace(' ', '').strip()


def claim_one():
    with SessionLocal.begin() as db:
        at = now(db)
        row = db.scalar(select(NotificationOutbox).where(or_(
            and_(NotificationOutbox.status == 'PENDING', NotificationOutbox.next_attempt_at <= at),
            and_(NotificationOutbox.status == 'PROCESSING', NotificationOutbox.lease_until <= at)))
            .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        if row.attempt_count >= 3:
            row.status, row.claim_token, row.lease_until, row.last_error = 'FAILED', None, None, 'LEASE_EXPIRED'
            return {'terminal': True}
        row.status, row.attempt_count = 'PROCESSING', row.attempt_count + 1
        row.claim_token, row.lease_until = str(uuid4()), at + timedelta(seconds=60)
        user = db.get(User, row.recipient_user_id)
        return {'id': row.id, 'claim_token': row.claim_token, 'attempt_count': row.attempt_count, 'term_id': row.term_id,
                'email': user.email, 'type': row.type, 'payload': row.payload}


def timetable_email_lines(payload):
    lines = ['上課時間以臺北時間顯示。']
    groups = (
        (f'正式選課（{payload["total_credits"]} 學分，上限 {payload["credit_limit"]} 學分）', payload['items']),
        ('初選待分發', payload['pending_items']),
    )
    for heading, sections in groups:
        lines.extend(('', heading))
        if not sections:
            lines.append('目前沒有課程。')
        for section in sections:
            lines.append(f'• {section["code"]} {section["title"]}（{section["section_no"]} 班，{section["credits"]} 學分）')
            if not section['meetings']:
                lines.append('  上課時間尚未安排。')
            for meeting in section['meetings']:
                day = '一二三四五六日'[meeting['weekday'] - 1]
                start, end = meeting['start_time'][:5], meeting['end_time'][:5]
                lines.append(f'  週{day} {start}–{end} {meeting["room"]}')
    return lines


NOTICE_LABELS = {'ENROLLED': '加選成功', 'DROPPED': '退選成功', 'ALLOCATION': '初選分發結果',
                 'INITIAL_RECEIPT': '初選登記確認', 'TIMETABLE': '每週課表'}


def build_notice(job):
    """Render a timetable, initial-registration receipt or allocation notice."""
    p = job['payload']
    notice_type = job['type']
    label = NOTICE_LABELS.get(notice_type, '選課通知')
    msg = EmailMessage()
    msg['From'], msg['To'] = sender(), job['email']
    msg['Subject'] = f'{p.get("term_code", "")} {label}'
    msg['Message-ID'] = make_msgid(idstring=f'outbox-{job["id"]}', domain='courseflow.example.test')
    content = [f'{p.get("term_code", "")} {label}']
    if notice_type == 'ALLOCATION':
        for result in p.get('results', []):
            content.append(f'{result["code"]} {result["title"]}：{result["status"]} {result.get("reason") or ""}')
    elif notice_type == 'INITIAL_RECEIPT':
        content.append('以下班別已登記，等待初選分發。正式錄取以分發公布為準。')
        for section in p['sections']:
            content.append(f'{section["code"]} {section["title"]}（{section["section_no"]} 班）')
    elif notice_type == 'TIMETABLE':
        content.extend(timetable_email_lines(p))
    else:
        content.append(f'{p.get("code", "")} {p.get("title", "")}：{label}')
    path = p.get('url', '/applications')
    if job.get('term_id') and path in ('/applications', '/timetable'):
        path += '?' + urlencode({'term': job['term_id']})
    link = settings.app_origin.rstrip('/') + path
    content.append(f'\n查看我的課表 {link}' if notice_type == 'TIMETABLE' else '查看結果：' + link)
    content.append('\n此信由 CourseFlow 選課系統寄出。')
    msg.set_content('\n'.join(content))
    return msg


def build_message(job):
    return build_confirmation(job) if job['type'] == CONFIRMATION_TYPE else build_notice(job)


def open_smtp():
    """Connect, upgrade to TLS and log in. Never falls back to plaintext login."""
    if settings.smtp_host.strip().lower() == 'smtp.gmail.com' and not (settings.smtp_user.strip() and smtp_password()):
        raise SMTPConfigurationError('SMTP_NOT_CONFIGURED')
    implicit_tls = settings.smtp_port == 465  # Gmail also offers SSL on 465 instead of STARTTLS on 587
    try:
        if implicit_tls:
            smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    except OSError:
        raise SMTPConfigurationError('SMTP_UNREACHABLE', permanent=False) from None
    try:
        smtp.ehlo_or_helo_if_needed()
        if settings.smtp_starttls and not implicit_tls:
            if not smtp.has_extn('starttls'):
                raise SMTPConfigurationError('SMTP_STARTTLS_UNSUPPORTED')
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if settings.smtp_user:
            if not smtp.has_extn('auth'):
                raise SMTPConfigurationError('SMTP_AUTH_UNSUPPORTED')
            try:
                smtp.login(settings.smtp_user.strip(), smtp_password())
            except smtplib.SMTPAuthenticationError:
                raise SMTPConfigurationError('SMTP_AUTH_FAILED') from None
    except BaseException:
        smtp.close()
        raise
    return smtp


def deliver_smtp(msg, event_id):
    with closing(open_smtp()) as smtp:
        smtp.send_message(msg)
        # A successful DATA reply means the server has accepted this message.
        # Failure to end the session must not cause the outbox to resend it.
        try:
            status, _ = smtp.quit()
            if status != 221:
                log.warning('SMTP_QUIT_REJECTED event_id=%s status=%s', event_id, status)
        except OSError as error:
            log.warning('SMTP_QUIT_FAILED event_id=%s error_class=%s', event_id, type(error).__name__)


def deliver(job, *, transport=None):
    """Send one outbox event and return the message ID kept as delivery evidence."""
    message = build_message(job)
    if settings.gmail_transport == 'api':
        return send_message(message, transport=transport)
    deliver_smtp(message, job['id'])
    return 'smtp:' + message['Message-ID'].strip('<>')


def finish(job, error=None, provider_message_id=None):
    with SessionLocal.begin() as db:
        at = now(db)
        values = {'claim_token': None, 'lease_until': None}
        if error is None:
            values.update(status='SENT', sent_at=at, last_error=None, provider_message_id=provider_message_id)
        else:
            # Configuration and authorization errors are recorded at once instead of retried.
            final = job['attempt_count'] >= 3 or getattr(error, 'permanent', False)
            values.update(status='FAILED' if final else 'PENDING', last_error=error_code(error)[:120])
            if not final:
                values['next_attempt_at'] = at + timedelta(minutes=1 if job['attempt_count'] == 1 else 5)
        return db.execute(update(NotificationOutbox).where(NotificationOutbox.id == job['id'],
            NotificationOutbox.claim_token == job['claim_token'], NotificationOutbox.status == 'PROCESSING').values(**values)).rowcount == 1


def process_one():
    job = claim_one()
    if not job:
        return False
    if job.get('terminal'):
        return True
    error = provider_message_id = None
    try:
        provider_message_id = deliver(job)
    except Exception as exc:
        error = exc
    owned = finish(job, error, provider_message_id)
    log.info(json.dumps({'event_id': job['id'], 'type': job['type'], 'attempt': job['attempt_count'], 'owned': owned,
                         'transport': 'gmail_api' if settings.gmail_transport == 'api' else 'smtp',
                         'error_class': type(error).__name__ if error else None,
                         'error_code': error_code(error) if error else None,
                         'hint': (GMAIL_HINTS.get(error_code(error)) or SMTP_HINTS.get(error_code(error)))
                         if error else None}, ensure_ascii=False))
    return True


def run_once(limit=100):
    for _ in range(limit):
        if not process_one():
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if args.once:
        run_once()
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            run_once()
        except Exception as error:
            log.error(json.dumps({'error_class': type(error).__name__}))
        stop.wait(2)


if __name__ == '__main__':
    main()
