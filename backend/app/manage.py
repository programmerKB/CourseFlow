"""Operator commands for the teacher-specification acceptance steps.

  python -m app.manage set-email --login student05 --email you@example.com
      Point a fictional demo account at an inbox you control before the AT-40 Gmail test.
  python -m app.manage verify-email [--send-to you@example.com]
      Check the configured transport (Gmail SMTP login, or the Gmail API token and scope) and
      optionally send one test message. verify-gmail is the same command.
  python -m app.manage credit-overloads
      List student terms whose active enrollments exceed the effective limit (min(limit, 18)).
  python -m app.manage retry-failed [--error GMAIL_NOT_CONFIGURED]
      Queue FAILED emails again, e.g. after Gmail credentials were added or replaced.
"""
import argparse
import json
import re
import smtplib
import sys
from contextlib import closing
from email.message import EmailMessage
from email.utils import make_msgid

import httpx
from sqlalchemy import select, text, update

from . import gmail, worker
from .config import settings
from .db import SessionLocal
from .models import NotificationOutbox, User
from .registration import now

ADDRESS = re.compile(r'[^@\s<>"]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')


def set_email(login, address):
    address = address.strip()
    if not ADDRESS.fullmatch(address) or len(address) > 320:
        sys.exit('The email address is not valid.')
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.login == login.strip().lower()))
        if user is None:
            sys.exit('No account uses that login.')
        user.email = address
    print(json.dumps({'login': user.login, 'role': user.role, 'email_updated': True}))


SETTING_HINTS = {
    'GMAIL_CLIENT_ID': 'Google Cloud Console → 憑證 → OAuth 用戶端（電腦版應用程式）的用戶端 ID，結尾是 .apps.googleusercontent.com',
    'GMAIL_CLIENT_SECRET': '同一個 OAuth 用戶端的密鑰（通常以 GOCSPX- 開頭），不要加引號或空白',
    'GMAIL_REFRESH_TOKEN': '執行 python3 scripts/gmail-oauth.py --client-id <ID> --manual 後印出的值（通常以 1// 開頭）',
    'GMAIL_SENDER': '授權時登入的 Gmail 帳號，例如 CourseFlow <you@gmail.com>',
}


SMTP_SETTING_HINTS = {
    'SMTP_HOST': 'Gmail 為 smtp.gmail.com',
    'SMTP_USER': '完整的 Gmail 地址，例如 you@gmail.com',
    'SMTP_PASSWORD': 'Google 帳戶 → 安全性 → 兩步驟驗證 → 應用程式密碼 產生的 16 碼密碼（不是 Gmail 登入密碼）',
    'SMTP_STARTTLS': 'Gmail 的 587 埠須設為 true（或改用 SMTP_PORT=465）',
}


def smtp_problems():
    """Names of missing or inconsistent SMTP_* settings for Gmail; values are never printed."""
    host = settings.smtp_host.strip().lower()
    if host != 'smtp.gmail.com':
        return ['SMTP_HOST'] if not settings.smtp_user.strip() else []
    missing = [name for name, value in (('SMTP_USER', settings.smtp_user.strip()),
                                        ('SMTP_PASSWORD', worker.smtp_password())) if not value]
    if settings.smtp_port == 587 and not settings.smtp_starttls:
        missing.append('SMTP_STARTTLS')
    return missing


def verify_smtp(send_to=None):
    problems = smtp_problems()
    preview = settings.smtp_host.strip().lower() != 'smtp.gmail.com' and not settings.smtp_user.strip()
    report = {'transport': 'smtp', 'host': settings.smtp_host, 'port': settings.smtp_port,
              'starttls': settings.smtp_starttls, 'implicit_tls': settings.smtp_port == 465, 'user': settings.smtp_user.strip() or None,
              'sender': gmail.sender(), 'mailpit_preview': preview, 'problems': problems}
    if preview:
        print('尚未設定 Gmail 帳號：通知信目前寄到 Mailpit 預覽信箱，不會寄到學生信箱。', file=sys.stderr)
    for name in problems:
        print(f'{name} 需要設定：{SMTP_SETTING_HINTS[name]}', file=sys.stderr)
    if problems:
        print('修改專案根目錄的 .env（不是 .env.example），再執行 sudo ./deploy.sh --skip-seed --no-build。', file=sys.stderr)
    message = None
    if send_to:
        message = EmailMessage()
        message['From'], message['To'] = gmail.sender(), send_to
        message['Subject'] = 'CourseFlow 寄信測試 Email test'
        message['Message-ID'] = make_msgid(domain='courseflow.example.test')
        message.set_content('這是 CourseFlow 的寄信測試。This test message was sent through Gmail SMTP.')
    try:
        if message is not None:
            worker.deliver_smtp(message, 'verify-email')
            report['message_id'] = 'smtp:' + message['Message-ID'].strip('<>')
        else:
            with closing(worker.open_smtp()) as smtp:
                smtp.quit()
        report['login'] = 'OK' if settings.smtp_user else 'NOT_USED'
    except worker.SMTPConfigurationError as error:
        report['error'] = error.code
    except smtplib.SMTPAuthenticationError:
        report['error'] = 'SMTP_AUTH_FAILED'
    except (OSError, smtplib.SMTPException):
        report['error'] = 'SMTP_UNREACHABLE'
    if 'error' in report:
        print(f'{report["error"]}：{worker.SMTP_HINTS.get(report["error"], "")}', file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if 'error' not in report and not problems and not preview else 1)


def verify_email(send_to=None):
    """Check the configured transport: Gmail SMTP (default) or the Gmail API."""
    if settings.gmail_transport == 'api':
        verify_gmail(send_to)
    else:
        verify_smtp(send_to)


def verify_gmail(send_to=None):
    missing, malformed = gmail.setting_problems()
    report = {'transport': settings.gmail_transport, 'configured': settings.gmail_configured,
              'sender': settings.gmail_sender or None, 'missing': missing, 'malformed': malformed}
    for name in dict.fromkeys(missing + malformed):
        state = '未設定' if name in missing else '格式不正確'
        print(f'{name} {state}：{SETTING_HINTS[name]}', file=sys.stderr)
    if missing or malformed:
        print('修改專案根目錄的 .env（不是 .env.example），再執行 sudo ./deploy.sh --skip-seed --no-build。'
              '確認容器收到的欄位：sudo docker compose exec api python -m app.manage verify-gmail', file=sys.stderr)
    try:
        with httpx.Client(timeout=10) as client:
            _, scope = gmail.access_token(client, force=True)
        report['token'] = 'OK'
        report['gmail_send_scope'] = gmail.SEND_SCOPE in scope.split()
        if send_to:
            message = EmailMessage()
            message['From'], message['To'] = settings.gmail_sender, send_to
            message['Subject'] = 'CourseFlow Gmail API test 寄信測試'
            message.set_content('This test message was sent with users.messages.send.')
            report['message_id'] = gmail.send_message(message)
    except gmail.GmailError as error:
        report['error'] = error.code
    print(json.dumps(report, indent=2))
    sys.exit(0 if report.get('token') == 'OK' and report.get('gmail_send_scope') and 'error' not in report else 1)


def credit_overloads():
    with SessionLocal() as db:
        rows = db.execute(text('SELECT term_code, student_no, credit_limit, effective_credit_limit, enrolled_credits '
                               'FROM v_credit_limit_overloads ORDER BY term_code, student_no')).mappings().all()
    print(json.dumps([{key: str(value) for key, value in row.items()} for row in rows], ensure_ascii=False, indent=2))


def retry_failed(error=None):
    """Configuration errors are not retried automatically; requeue them once the cause is fixed."""
    with SessionLocal.begin() as db:
        stmt = update(NotificationOutbox).where(NotificationOutbox.status == 'FAILED')
        if error:
            stmt = stmt.where(NotificationOutbox.last_error == error)
        count = db.execute(stmt.values(status='PENDING', attempt_count=0, next_attempt_at=now(db), last_error=None,
                                       claim_token=None, lease_until=None)).rowcount
    print(json.dumps({'requeued': count, 'error_filter': error}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    email_command = commands.add_parser('set-email')
    email_command.add_argument('--login', required=True)
    email_command.add_argument('--email', required=True)
    for name in ('verify-email', 'verify-gmail'):  # verify-gmail kept for earlier instructions
        commands.add_parser(name).add_argument('--send-to')
    commands.add_parser('credit-overloads')
    retry_command = commands.add_parser('retry-failed')
    retry_command.add_argument('--error', help='Only requeue rows with this last_error code.')
    args = parser.parse_args()
    if args.command == 'set-email':
        set_email(args.login, args.email)
    elif args.command in ('verify-email', 'verify-gmail'):
        verify_email(args.send_to)
    elif args.command == 'retry-failed':
        retry_failed(args.error)
    else:
        credit_overloads()


if __name__ == '__main__':
    main()
