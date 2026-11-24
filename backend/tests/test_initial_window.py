from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, settings
from app.configure_initial import configure_initial_window
from app.db import SessionLocal
from app.models import AllocationRun, Application, AuditEvent, RegistrationPeriod
from app.seed import uid
from conftest import FIXED, signin


MAIN = uid('term/main')


@pytest.mark.parametrize('dates', [
    {'initial_start_at': '2026-11-24T09:00:00+08:00', 'initial_end_at': ''},
    {'initial_start_at': '2026-11-24T09:00:00', 'initial_end_at': '2026-11-25T09:00:00+08:00'},
    {'initial_start_at': '2026-11-25T09:00:00+08:00', 'initial_end_at': '2026-11-24T09:00:00+08:00'},
])
def test_initial_window_rejects_missing_naive_or_reversed_dates(dates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **dates)


def test_initial_window_update_keeps_applications_and_is_idempotent(client, monkeypatch):
    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    old_start, old_end = FIXED - timedelta(days=2), FIXED + timedelta(days=1)
    start, end = FIXED - timedelta(days=1), FIXED + timedelta(days=20)
    monkeypatch.setattr(settings, 'initial_start_at', start)
    monkeypatch.setattr(settings, 'initial_end_at', end)
    with SessionLocal.begin() as db:
        db.delete(db.scalar(select(AllocationRun).where(AllocationRun.term_id == MAIN)))
        period = db.get(RegistrationPeriod, (MAIN, 'INITIAL'))
        period.starts_at, period.ends_at = old_start, old_end

    signin(client)
    base = f'/api/v1/me/terms/{MAIN}'
    section = uid('section/main/CS101/A')
    response = client.put(f'{base}/applications/{section}')
    assert response.status_code == 200
    application_id = response.json()['application_id']
    assert not configure_initial_window(check_only=True)
    with SessionLocal() as db:
        assert db.get(RegistrationPeriod, (MAIN, 'INITIAL')).ends_at == old_end
    assert configure_initial_window()
    assert not configure_initial_window()
    with SessionLocal() as db:
        assert db.get(Application, application_id).status == 'PENDING'
        assert (db.get(RegistrationPeriod, (MAIN, 'INITIAL')).starts_at,
                db.get(RegistrationPeriod, (MAIN, 'INITIAL')).ends_at) == (start, end)
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'INITIAL_PERIOD_CONFIGURED')))) == 1
    status = client.get(f'/api/v1/terms/{MAIN}/registration').json()
    assert status['phase'] == 'INITIAL' and [p['kind'] for p in status['periods']] == ['INITIAL']
    monkeypatch.setattr(settings, 'public_initial_term_id', '')
    with pytest.raises(ValueError, match='加退選時段'):
        configure_initial_window(check_only=True)


def test_initial_window_rejects_changes_after_allocation_or_conflicting_add_drop(monkeypatch):
    monkeypatch.setattr(settings, 'initial_start_at', FIXED - timedelta(days=5))
    monkeypatch.setattr(settings, 'initial_end_at', FIXED + timedelta(days=2))
    with pytest.raises(ValueError, match='已公布分發結果'):
        configure_initial_window(check_only=True)
    with SessionLocal.begin() as db:
        db.delete(db.scalar(select(AllocationRun).where(AllocationRun.term_id == MAIN)))
    with pytest.raises(ValueError, match='加退選時段'):
        configure_initial_window()
