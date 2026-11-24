"""SMTP protocol negotiation tests: no external connection or email delivery."""
import json
import logging

import pytest

from app import worker
from app.config import settings


class FakeSMTP:
    def __init__(self, before=(), after=()):
        self.before = set(before)
        self.after = set(after)
        self.tls = False
        self.calls = []
        self.message = None

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

    def send_message(self, msg):
        self.calls.append('send')
        self.message = msg


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(settings, 'smtp_host', 'mailpit')
    monkeypatch.setattr(settings, 'smtp_port', 1025)
    monkeypatch.setattr(settings, 'smtp_starttls', False)
    monkeypatch.setattr(settings, 'smtp_user', '')
    monkeypatch.setattr(settings, 'smtp_password', '')
    return {'id': 'test-smtp-id', 'claim_token': 'test-claim', 'attempt_count': 1,
            'email': 'recipient@example.test', 'type': 'ENROLLED',
            'payload': {'term_code': '115-1', 'code': 'CS101', 'title': '程式設計'}}


def install_fake(monkeypatch, smtp):
    monkeypatch.setattr(worker.smtplib, 'SMTP', lambda *args, **kwargs: smtp)


def test_mailpit_without_credentials_sends(job, monkeypatch):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    worker.send_smtp(job)
    assert smtp.calls == ['ehlo', 'send']
    assert smtp.message['Message-ID'] == '<outbox-test-smtp-id@course-registration.example.test>'


def test_initial_receipt_renders_pending_status_and_every_course(job, monkeypatch):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    job['type'] = 'INITIAL_RECEIPT'
    job['payload'] = {'term_code': '115-1', 'url': '/applications?term=example-term', 'sections': [
        {'code': 'CS101', 'title': '程式設計', 'section_no': 'A'},
        {'code': 'EE101', 'title': '電路學', 'section_no': 'B'},
    ]}
    worker.send_smtp(job)
    assert str(smtp.message['Subject']) == '115-1 初選登記確認'
    body = smtp.message.get_content()
    assert '等待初選分發' in body
    assert 'CS101 程式設計（A 班）' in body and 'EE101 電路學（B 班）' in body
    assert '/applications?term=example-term' in body


def test_smtp_quit_failure_preserves_acceptance_and_old_term_link(job, monkeypatch, caplog):
    smtp = FakeSMTP()
    monkeypatch.setattr(smtp, 'quit', lambda: (421, b'temporarily unavailable'))
    install_fake(monkeypatch, smtp)
    job['term_id'] = 'example-term'
    job['payload']['url'] = '/timetable'
    monkeypatch.setattr(worker, 'claim_one', lambda: job)
    completed = []
    monkeypatch.setattr(worker, 'finish', lambda _, error, *provider: completed.append(error) or True)
    assert worker.process_one()
    assert completed == [None]
    assert '/timetable?term=example-term' in smtp.message.get_content()
    assert 'SMTP_QUIT_REJECTED' in caplog.text


def test_mailpit_with_credentials_reports_auth_unsupported(job, monkeypatch):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    monkeypatch.setattr(settings, 'smtp_password', 'test-private-password')
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.send_smtp(job)
    assert exc.value.code == 'SMTP_AUTH_UNSUPPORTED'
    assert smtp.calls == ['ehlo']


def test_starttls_unsupported_does_not_fall_back_to_plaintext(job, monkeypatch):
    smtp = FakeSMTP(before={'auth'})
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.send_smtp(job)
    assert exc.value.code == 'SMTP_STARTTLS_UNSUPPORTED'
    assert smtp.calls == ['ehlo']


def test_gmail_rechecks_auth_after_starttls(job, monkeypatch):
    smtp = FakeSMTP(before={'starttls'}, after={'auth'})
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    worker.send_smtp(job)
    assert smtp.calls == ['ehlo', 'starttls', 'ehlo', 'login', 'send']


def test_auth_must_be_advertised_after_tls(job, monkeypatch):
    smtp = FakeSMTP(before={'starttls', 'auth'}, after=set())
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    with pytest.raises(worker.SMTPConfigurationError) as exc:
        worker.send_smtp(job)
    assert exc.value.code == 'SMTP_AUTH_UNSUPPORTED'
    assert smtp.calls == ['ehlo', 'starttls', 'ehlo']


def test_logs_explain_configuration_failure_without_credentials(job, monkeypatch, caplog):
    smtp = FakeSMTP()
    install_fake(monkeypatch, smtp)
    monkeypatch.setattr(settings, 'smtp_user', 'sender@example.test')
    monkeypatch.setattr(settings, 'smtp_password', 'test-private-password')
    monkeypatch.setattr(worker, 'claim_one', lambda: job)
    monkeypatch.setattr(worker, 'finish', lambda *args: True)
    with caplog.at_level(logging.INFO, logger='courses.worker'):
        assert worker.process_one()
    record = json.loads(caplog.records[-1].message)
    assert record['error_code'] == 'SMTP_AUTH_UNSUPPORTED'
    assert 'Mailpit' in record['hint']
    assert 'sender@example.test' not in caplog.text
    assert 'recipient@example.test' not in caplog.text
    assert 'test-private-password' not in caplog.text
