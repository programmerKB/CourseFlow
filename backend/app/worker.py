"""Transactional outbox: leases committed before SMTP, token guarded completion."""
import argparse
import json
import logging
import signal
import smtplib
import ssl
import threading
from datetime import timedelta
from email.message import EmailMessage
from uuid import uuid4
from sqlalchemy import select, update, or_, and_
from .db import SessionLocal
from .config import settings
from .models import NotificationOutbox, User
from .registration import now

log = logging.getLogger('courses.worker')


SMTP_HINTS = {
    'SMTP_STARTTLS_UNSUPPORTED': '伺服器不支援 STARTTLS；請核對 SMTP_HOST、SMTP_PORT 與 SMTP_STARTTLS。Gmail 使用 smtp.gmail.com:587 並開啟 STARTTLS。',
    'SMTP_AUTH_UNSUPPORTED': '伺服器不支援帳密登入；若目前連到 Mailpit，寄送真實郵件需改成 Gmail 等 SMTP 服務。Mailpit 測試模式請清空 SMTP_USER 與 SMTP_PASSWORD。',
}


class SMTPConfigurationError(smtplib.SMTPNotSupportedError):
    """Only predefined codes/hints are stored; provider responses may contain private data."""
    def __init__(self, code):
        self.code = code
        super().__init__(SMTP_HINTS[code])


def smtp_error_code(error):
    return error.code if isinstance(error, SMTPConfigurationError) else type(error).__name__


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
        return {'id': row.id, 'claim_token': row.claim_token, 'attempt_count': row.attempt_count,
                'email': user.email, 'type': row.type, 'payload': row.payload}


def send_smtp(job):
    p = job['payload']
    labels = {'ENROLLED': '加選成功', 'DROPPED': '退選成功', 'ALLOCATION': '初選分發結果'}
    label = labels.get(job['type'], '選課通知')
    msg = EmailMessage()
    msg['From'], msg['To'] = settings.smtp_from, job['email']
    msg['Subject'] = f'{p.get("term_code", "")} {label}'
    msg['Message-ID'] = f'<outbox-{job["id"]}@course-registration.example.test>'
    content = [f'{p.get("term_code", "")} {label}']
    if job['type'] == 'ALLOCATION':
        for result in p.get('results', []):
            content.append(f'{result["code"]} {result["title"]}：{result["status"]} {result.get("reason") or ""}')
    else:
        content.append(f'{p.get("code", "")} {p.get("title", "")}：{label}')
    content.append('查看結果：' + settings.app_origin.rstrip('/') + p.get('url', '/applications'))
    msg.set_content('\n'.join(content))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.ehlo_or_helo_if_needed()
        if settings.smtp_starttls:
            if not smtp.has_extn('starttls'):
                raise SMTPConfigurationError('SMTP_STARTTLS_UNSUPPORTED')
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if settings.smtp_user:
            if not smtp.has_extn('auth'):
                raise SMTPConfigurationError('SMTP_AUTH_UNSUPPORTED')
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def finish(job, error=None):
    with SessionLocal.begin() as db:
        at = now(db)
        values = {'claim_token': None, 'lease_until': None}
        if error is None:
            values.update(status='SENT', sent_at=at, last_error=None)
        else:
            values.update(status='FAILED' if job['attempt_count'] >= 3 else 'PENDING', last_error=smtp_error_code(error)[:120])
            if job['attempt_count'] < 3:
                values['next_attempt_at'] = at + timedelta(minutes=1 if job['attempt_count'] == 1 else 5)
        return db.execute(update(NotificationOutbox).where(NotificationOutbox.id == job['id'],
            NotificationOutbox.claim_token == job['claim_token'], NotificationOutbox.status == 'PROCESSING').values(**values)).rowcount == 1


def process_one():
    job = claim_one()
    if not job:
        return False
    if job.get('terminal'):
        return True
    error = None
    try:
        send_smtp(job)
    except Exception as exc:
        error = exc
    owned = finish(job, error)
    log.info(json.dumps({'event_id': job['id'], 'attempt': job['attempt_count'], 'owned': owned,
                         'error_class': type(error).__name__ if error else None,
                         'error_code': smtp_error_code(error) if error else None,
                         'hint': SMTP_HINTS.get(smtp_error_code(error)) if error else None}, ensure_ascii=False))
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
