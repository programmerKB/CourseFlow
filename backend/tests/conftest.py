"""Run only against an explicitly named disposable PostgreSQL *_test database."""
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text, select

url = os.environ.get('TEST_DATABASE_URL')
if not url or not url.rsplit('/', 1)[-1].endswith('_test'):
    raise RuntimeError('Set TEST_DATABASE_URL to a disposable PostgreSQL database ending in _test')
os.environ['DATABASE_URL'] = url
os.environ['PUBLIC_DATABASE_URL'] = os.environ.get('TEST_PUBLIC_DATABASE_URL', url.replace('course_migrator', 'course_public'))
os.environ['CHAT_STUB'] = 'true'

from app.db import engine, SessionLocal
from app.main import app
from app.seed import seed, uid
from app.models import RegistrationPeriod
from app import registration
from fastapi.testclient import TestClient

FIXED = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope='session', autouse=True)
def migrations():
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=Path(__file__).resolve().parents[1], check=True)


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    with engine.begin() as conn:
        conn.execute(text('TRUNCATE users, departments, terms, courses RESTART IDENTITY CASCADE'))
    seed('add-drop', include_demo_terms=True)
    with SessionLocal.begin() as db:
        for slug, kind, start, end in [('main','INITIAL',-7,-2), ('main','ADD_DROP',-1,30),
                                      ('initial','INITIAL',-1,7), ('initial','ADD_DROP',8,30)]:
            row = db.get(RegistrationPeriod, (uid('term/' + slug), kind))
            row.starts_at, row.ends_at = FIXED + timedelta(days=start), FIXED + timedelta(days=end)
    monkeypatch.setattr(registration, 'now', lambda db: FIXED)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def signin(c, name='student01'):
    r = c.post('/api/v1/auth/login', json={'login': name, 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return c
