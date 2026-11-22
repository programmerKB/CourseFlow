"""Provision a separate PostgreSQL test database using the migration role."""
import os
import re
import subprocess
import sys
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

os.chdir(Path(__file__).resolve().parent)
if not os.environ.get('DATABASE_URL'):
    raise RuntimeError('Set DATABASE_URL to the migration-role connection')
from app.config import settings

base = make_url(settings.database_url)
test_url = make_url(os.environ.get('TEST_DATABASE_URL') or base.set(database='courses_test').render_as_string(hide_password=False))
if not test_url.database or not re.fullmatch(r'[A-Za-z0-9_]+_test', test_url.database):
    raise RuntimeError('Test database name must end in _test')
with create_engine(base.set(database='postgres'), isolation_level='AUTOCOMMIT').connect() as db:
    if not db.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name':test_url.database}):
        db.exec_driver_sql('CREATE DATABASE "' + test_url.database + '"')
env = dict(os.environ)
# Both scratch URLs below already contain their respective role passwords.
env.pop('DATABASE_PASSWORD', None)
env.pop('PUBLIC_DATABASE_PASSWORD', None)
env['TEST_DATABASE_URL'] = test_url.render_as_string(hide_password=False)
public = make_url(settings.public_database_url) if env.get('PUBLIC_DATABASE_URL') else test_url.set(username='course_public', password=env.get('PUBLIC_DB_PASSWORD','course_public'))
env['TEST_PUBLIC_DATABASE_URL'] = public.set(database=test_url.database).render_as_string(hide_password=False)
env['PYTHONPATH'] = str(Path.cwd())
raise SystemExit(subprocess.call([sys.executable,'-m','pytest','-q',*sys.argv[1:]],env=env))
