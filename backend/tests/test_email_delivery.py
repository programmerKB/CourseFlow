"""Email rendering and Gmail SMTP delivery: no external connection or delivery.

The default transport is Gmail SMTP with an app password; the Gmail API option is covered in
test_teacher_spec.py (AT-40/41).
"""
import json
import logging
import smtplib

import pytest

from app import gmail, worker
from app.config import settings


class FakeSMTP:
    def __init__(self, before=(), after=(), login_error=None):
        self.before = set(before)
        self.after = set(after)
        self.tls = False
        self.calls = []
        self.message = None
        self.credentials = None
        self.login_error = login_error

    def quit(self):
        return 221, b'bye'

    def close(self):
        pass

    def ehlo_or_helo_if_needed(self):
        self.ehlo()

    def ehlo(self):
        self.calls.append('ehlo')

    def has_extn(self, name):
        return name in (self.after if self.tls else self.before)

    def starttls(self, *, context):
        self.calls.append('starttls')
        assert context.check_hostname
        self.tls = True

    def login(self, user, password):
        assert self.has_extn('auth')
        self.calls.append('login')
        self.credentials = (user, password)
        if self.login_error:
            raise self.login_error

    def send_message(self, msg):
        self.calls.append('send')
        self.message = msg


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(settings, 'gmail_transport', 'smtp')
    monkeypatch.setattr(settings, 'smtp_host', 'mailpit')
    monkeypatch.setattr(settings, 'smtp_port', 1025)
    monkeypatch.setattr(settings, 'smtp_starttls', False)
    monkeypatch.setattr(settings, 'smtp_user', '')
    monkeypatch.setattr(settings, 'smtp_password', type(settings.smtp_password)(''))
    monkeypatch.setattr(settings, 'smtp_from', '')
    return {'id': 'test-email-id', 'claim_token': 'test-claim', 'attempt_count': 1,
            'email': 'recipient@example.test', 'type': 'ENROLLED',
            'payload': {'term_code': '115-1', 'code': 'CS101', 'title': '程式設計'}}


def gmail_smtp(monkeypatch, password='abcd efgh ijkl mnop'):
    monkeypatch.setattr(settings, 'smtp_host', 'smtp.gmail.com')
    monkeypatch.setattr(settings, 'smtp_port', 587)
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@gmail.com')
    monkeypatch.setattr(settings, 'smtp_password', type(settings.smtp_password)(password))


def install_fake(monkeypatch, smtp):
    monkeypatch.setattr(worker.smtplib, 'SMTP', lambda *args, **kwargs: smtp)


def test_mailpit_without_credentials_sends(job, monkeypatch):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    provider_id = worker.deliver(job)
    assert smtp.calls == ['ehlo', 'send']
    assert provider_id == 'smtp:' + smtp.message['Message-ID'].strip('<>') and 'outbox-test-email-id' in provider_id
    assert smtp.message['From'] == gmail.PREVIEW_SENDER


def test_gmail_smtp_uses_tls_app_password_without_spaces_and_the_account_as_sender(job, monkeypatch):
    gmail_smtp(monkeypatch)
    smtp = FakeSMTP(before={'starttls'}, after={'auth'})
    install_fake(monkeypatch, smtp)
    worker.deliver(job)
    assert smtp.calls == ['ehlo', 'starttls', 'ehlo', 'login', 'send']
    assert smtp.credentials == ('sender@gmail.com', 'abcdefghijklmnop')
    assert smtp.message['From'] == 'sender@gmail.com'
    monkeypatch.setattr(settings, 'smtp_from', 'CourseFlow <sender@gmail.com>')
    smtp = FakeSMTP(before={'starttls'}, after={'auth'})  # one connection per message
    install_fake(monkeypatch, smtp)
    worker.deliver(job)
    assert smtp.message['From'] == 'CourseFlow <sender@gmail.com>'


def test_gmail_host_without_account_is_reported_not_attempted(job, monkeypatch):
    gmail_smtp(monkeypatch, password='')
    monkeypatch.setattr(worker.smtplib, 'SMTP', lambda *a, **k: pytest.fail('must not connect without an account'))
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.deliver(job)
    assert exc.value.code == 'SMTP_NOT_CONFIGURED' and exc.value.permanent


def test_rejected_app_password_is_a_permanent_fixed_code(job, monkeypatch):
    gmail_smtp(monkeypatch)
    install_fake(monkeypatch, FakeSMTP(before={'starttls'}, after={'auth'},
                                       login_error=smtplib.SMTPAuthenticationError(535, b'private provider text')))
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.deliver(job)
    assert exc.value.code == 'SMTP_AUTH_FAILED' and exc.value.permanent


def test_unreachable_server_is_retried(job, monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(worker.smtplib, 'SMTP', refuse)
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.deliver(job)
    assert exc.value.code == 'SMTP_UNREACHABLE' and not exc.value.permanent


def test_mailpit_with_credentials_reports_auth_unsupported(job, monkeypatch):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    monkeypatch.setattr(settings, 'smtp_password', type(settings.smtp_password)('test-private-password'))
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.deliver(job)
    assert exc.value.code == 'SMTP_AUTH_UNSUPPORTED'
    assert smtp.calls == ['ehlo']


def test_starttls_unsupported_does_not_fall_back_to_plaintext(job, monkeypatch):
    smtp = FakeSMTP(before={'auth'})
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.deliver(job)
    assert exc.value.code == 'SMTP_STARTTLS_UNSUPPORTED'
    assert smtp.calls == ['ehlo']


def test_auth_must_be_advertised_after_tls(job, monkeypatch):
    smtp = FakeSMTP(before={'starttls', 'auth'}, after=set())
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.deliver(job)
    assert exc.value.code == 'SMTP_AUTH_UNSUPPORTED'
    assert smtp.calls == ['ehlo', 'starttls', 'ehlo']


def test_initial_receipt_renders_pending_status_and_every_course(job):
    job['type'] = 'INITIAL_RECEIPT'
    job['payload'] = {'term_code': '115-1', 'url': '/applications?term=example-term', 'sections': [
        {'code': 'CS101', 'title': '程式設計', 'section_no': 'A'},
        {'code': 'EE101', 'title': '電路學', 'section_no': 'B'},
    ]}
    message = worker.build_message(job)
    assert str(message['Subject']) == '115-1 初選登記確認'
    body = message.get_content()
    assert '等待初選分發' in body and 'CourseFlow 選課系統寄出' in body
    assert 'CS101 程式設計（A 班）' in body and 'EE101 電路學（B 班）' in body
    assert '/applications?term=example-term' in body


def test_timetable_notice_renders_every_course(job):
    job['type'] = 'TIMETABLE'
    job['payload'] = {'term_code': '115-1', 'total_credits': '3.0', 'credit_limit': '18.0', 'pending_items': [],
                      'url': '/timetable', 'items': [{'code': 'CS101', 'title': '程式設計', 'section_no': 'A',
                                                      'credits': '3.0', 'meetings': []}]}
    message = worker.build_message(job)
    assert '每週課表' in str(message['Subject']) and 'CS101 程式設計（A 班，3.0 學分）' in message.get_content()


def test_quit_failure_preserves_acceptance_and_old_term_link(job, monkeypatch, caplog):
    smtp = FakeSMTP()
    monkeypatch.setattr(smtp, 'quit', lambda: (421, b'temporarily unavailable'))
    install_fake(monkeypatch, smtp)
    job['term_id'] = 'example-term'
    job['payload']['url'] = '/timetable'
    monkeypatch.setattr(worker, 'claim_one', lambda: job)
    completed = []
    monkeypatch.setattr(worker, 'finish', lambda _, error, *provider: completed.append((error, provider)) or True)
    assert worker.process_one()
    assert completed[0][0] is None and completed[0][1][0].startswith('smtp:')
    assert '/timetable?term=example-term' in smtp.message.get_content()
    assert 'SMTP_QUIT_REJECTED' in caplog.text


def test_logs_explain_configuration_failure_without_credentials(job, monkeypatch, caplog):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    monkeypatch.setattr(settings, 'smtp_password', type(settings.smtp_password)('test-private-password'))
    monkeypatch.setattr(worker, 'claim_one', lambda: job)
    monkeypatch.setattr(worker, 'finish', lambda *args: True)
    with caplog.at_level(logging.INFO, logger='courses.worker'):
        assert worker.process_one()
    record = json.loads(caplog.records[-1].message)
    assert record['transport'] == 'smtp' and record['error_code'] == 'SMTP_AUTH_UNSUPPORTED'
    assert 'Mailpit' in record['hint']
    for secret in ('sender@example.test', 'recipient@example.test', 'test-private-password'):
        assert secret not in caplog.text


def test_legacy_dev_smtp_value_means_smtp():
    assert type(settings).legacy_transport('dev-smtp') == 'smtp'
    assert type(settings).legacy_transport(' API ') == 'api'


def test_confirmation_shows_submission_time_in_taipei(job):
    job.update(type='REGISTRATION_CONFIRMATION', term_id='example-term', payload={
        'term_code': '115-1', 'operation': 'ADD', 'channel': 'WEB', 'total_credits': '18.0', 'credit_limit': '18.0',
        'submitted_at': '2026-11-25T02:05:09.123456+00:00', 'url': '/timetable?term=example-term',
        'items': [{'code': 'CS101', 'title': '程式設計', 'section_no': 'A', 'credits': '3.0', 'school_name': '臺科大',
                   'department_name': '資工系', 'meetings': [{'weekday': 1, 'start_time': '09:00:00',
                                                              'end_time': '10:30:00', 'room': 'TR-211'}]}]})
    body = worker.build_message(job).get_content()
    assert '2026/11/25 10:05（臺北時間 Taipei time）' in body and '+00:00' not in body
    assert '週一 09:00–10:30 TR-211' in body and '18.0 of 18.0 credits' in body


def test_verify_email_names_missing_gmail_smtp_settings(job, monkeypatch, capsys):
    from app import manage
    gmail_smtp(monkeypatch, password='')
    with pytest.raises(SystemExit) as exit_code:
        manage.verify_email()
    assert exit_code.value.code == 1
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report['transport'] == 'smtp' and report['problems'] == ['SMTP_PASSWORD']
    assert report['error'] == 'SMTP_NOT_CONFIGURED' and 'SMTP_PASSWORD 需要設定' in err


def test_verify_email_logs_in_and_sends_a_test_message(job, monkeypatch, capsys):
    from app import manage
    gmail_smtp(monkeypatch, password='private-app-password')
    smtp = FakeSMTP(before={'starttls'}, after={'auth'})
    install_fake(monkeypatch, smtp)
    with pytest.raises(SystemExit) as exit_code:
        manage.verify_email('you@example.test')
    assert exit_code.value.code == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report['login'] == 'OK' and report['message_id'].startswith('smtp:')
    assert smtp.calls == ['ehlo', 'starttls', 'ehlo', 'login', 'send']
    assert 'private-app-password' not in out + err


def test_verify_gmail_api_names_missing_and_malformed_settings_without_values(monkeypatch, capsys):
    from app import manage
    monkeypatch.setattr(settings, 'gmail_transport', 'api')
    monkeypatch.setattr(settings, 'gmail_client_id', 'not-a-google-client-id')
    monkeypatch.setattr(settings, 'gmail_client_secret', type(settings.gmail_client_secret)('GOCSPX-private-value'))
    monkeypatch.setattr(settings, 'gmail_refresh_token', type(settings.gmail_refresh_token)(''))
    monkeypatch.setattr(settings, 'gmail_sender', 'CourseFlow <sender@gmail.com>')
    gmail.clear_token_cache()
    with pytest.raises(SystemExit) as exit_code:
        manage.verify_email()
    assert exit_code.value.code == 1
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report['missing'] == ['GMAIL_REFRESH_TOKEN'] and report['malformed'] == ['GMAIL_CLIENT_ID']
    assert report['error'] == 'GMAIL_NOT_CONFIGURED'
    assert 'GMAIL_REFRESH_TOKEN 未設定' in err and 'GMAIL_CLIENT_ID 格式不正確' in err and '.env.example' in err
    assert 'GOCSPX-private-value' not in out + err and 'not-a-google-client-id' not in out + err


def test_gmail_ssl_port_465_uses_implicit_tls(job, monkeypatch):
    gmail_smtp(monkeypatch)
    monkeypatch.setattr(settings, 'smtp_port', 465)
    smtp = FakeSMTP(before={'auth'})
    opened = []
    monkeypatch.setattr(worker.smtplib, 'SMTP', lambda *a, **k: pytest.fail('465 must not use plain SMTP'))
    monkeypatch.setattr(worker.smtplib, 'SMTP_SSL', lambda host, port, **kwargs: opened.append((host, port)) or smtp)
    worker.deliver(job)
    assert opened == [('smtp.gmail.com', 465)] and smtp.calls == ['ehlo', 'login', 'send']


def test_spaces_around_env_values_are_ignored():
    stripped = type(settings).strip_text('  smtp.gmail.com \n')
    assert stripped == 'smtp.gmail.com' and type(settings).strip_text(None) is None
