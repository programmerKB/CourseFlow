"""Operator commands for the teacher-specification acceptance steps.

  python -m app.manage set-email --login student05 --email you@example.com
      Point a fictional demo account at an inbox you control before the AT-40 Gmail test.
  python -m app.manage verify-gmail [--send-to you@example.com]
      Refresh the OAuth token, check the gmail.send scope, optionally send one test message.
  python -m app.manage credit-overloads
      List student terms whose active enrollments exceed the effective limit (min(limit, 18)).
"""
import argparse
import json
import re
import sys
from email.message import EmailMessage

import httpx
from sqlalchemy import select, text

from . import gmail
from .config import settings
from .db import SessionLocal
from .models import User

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


def verify_gmail(send_to=None):
    report = {'transport': settings.gmail_transport, 'configured': settings.gmail_configured,
              'sender': settings.gmail_sender or None}
    try:
        with httpx.Client(timeout=10) as client:
            _, scope = gmail.access_token(client, force=True)
        report['token'] = 'OK'
        report['gmail_send_scope'] = gmail.SEND_SCOPE in scope.split()
        if send_to:
            message = EmailMessage()
            message['From'], message['To'] = settings.gmail_sender, send_to
            message['Subject'] = 'CourseFlow Gmail API test'
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    email_command = commands.add_parser('set-email')
    email_command.add_argument('--login', required=True)
    email_command.add_argument('--email', required=True)
    gmail_command = commands.add_parser('verify-gmail')
    gmail_command.add_argument('--send-to')
    commands.add_parser('credit-overloads')
    args = parser.parse_args()
    if args.command == 'set-email':
        set_email(args.login, args.email)
    elif args.command == 'verify-gmail':
        verify_gmail(args.send_to)
    else:
        credit_overloads()


if __name__ == '__main__':
    main()
