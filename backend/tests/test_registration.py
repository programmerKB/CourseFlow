from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.db import SessionLocal
from app.models import (Application, Enrollment, AuditEvent, NotificationOutbox, IdempotencyKey,
                        AllocationRun, RegistrationPeriod, Section, StudentTerm, Term)
from app import registration as reg
from app.seed import uid, seed
from conftest import signin, FIXED

MAIN = uid('term/main')
INITIAL = uid('term/initial')


def sec(code, sno='A', slug='main'):
    return uid(f'section/{slug}/{code}/{sno}')


def add(c, section, key=None, term=MAIN):
    return c.post(f'/api/v1/me/terms/{term}/enrollments', json={'section_id': section}, headers={'Idempotency-Key': key or str(uuid4())})


def count(model):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_at01_auth_roles_sessions_origin(client):
    assert client.get(f'/api/v1/terms/{MAIN}/sections').status_code == 200
    assert client.get(f'/api/v1/me/terms/{MAIN}/timetable').status_code == 401
    signin(client)
    assert client.get('/api/v1/auth/me').json()['login'] == 'student01'
    assert client.get('/api/v1/admin/catalog').status_code == 403
    assert client.post('/api/v1/auth/logout', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/v1/auth/logout').status_code == 204
    assert client.get('/api/v1/auth/me').status_code == 401


def test_at02_catalog_filters_multimeeting_and_counts(client):
    r = client.get(f'/api/v1/terms/{MAIN}/sections', params={'q': '程式', 'instructor': '陳', 'weekday': 3}).json()
    assert r['total'] == 1 and len(r['items'][0]['meetings']) == 2
    s = r['items'][0]
    assert s['remaining'] == s['capacity'] - s['enrolled_count']
    assert client.get(f'/api/v1/terms/{INITIAL}/sections/{s["id"]}').status_code == 404
    assert client.get(f'/api/v1/terms/{MAIN}/sections?page_size=51').status_code == 422
    assert client.get('/api/v1/terms/not-a-uuid/sections').status_code == 422


def test_at03_initial_boundaries_replay_cancel(client, monkeypatch):
    signin(client)
    path = f'/api/v1/me/terms/{INITIAL}/applications/'
    a, b = sec('CS101', slug='initial'), sec('CS101', 'B', 'initial')
    with SessionLocal() as db:
        start = db.get(RegistrationPeriod, (INITIAL, 'INITIAL')).starts_at
        end = db.get(RegistrationPeriod, (INITIAL, 'INITIAL')).ends_at
    monkeypatch.setattr(reg, 'now', lambda db: start)
    first = client.put(path + a)
    assert first.status_code == 200
    assert first.json()['status'] == 'PENDING'
    assert client.put(path + a).json()['application_id'] == first.json()['application_id']
    assert client.put(path + b).json()['code'] == 'APPLICATION_EXISTS'
    assert client.delete(path + a).status_code == 204
    assert client.put(path + b).status_code == 200
    monkeypatch.setattr(reg, 'now', lambda db: end)
    assert client.put(path + a).json()['code'] == 'REGISTRATION_CLOSED'


def prepare_allocation():
    with SessionLocal.begin() as db:
        initial = db.get(RegistrationPeriod, (INITIAL, 'INITIAL'))
        initial.ends_at = FIXED - timedelta(seconds=1)
        for n in (1, 2):
            db.add(Application(student_id=uid(f'student/student{n:02}'), term_id=INITIAL, course_id=uid('course/CS303'),
                               section_id=sec('CS303', slug='initial'), status='PENDING'))


def test_at04_deterministic_allocation_and_replay(client):
    prepare_allocation()
    signin(client, 'admin')
    path = f'/api/v1/admin/terms/{INITIAL}/allocation'
    first = client.post(path)
    assert first.status_code == 200, first.text
    assert first.json()['accepted_count'] == 1 and first.json()['rejected_count'] == 1
    before = [count(m) for m in (Enrollment, NotificationOutbox, AuditEvent)]
    assert client.post(path).json() == first.json()
    assert before == [count(m) for m in (Enrollment, NotificationOutbox, AuditEvent)]
    expected = min((uid(f'student/student{n:02}') for n in (1, 2)), key=lambda sid: hashlib.sha256(f'v1|ntust-2026-fixed-seed|{sid}|{sec("CS303",slug="initial")}'.encode()).hexdigest())
    with SessionLocal() as db:
        assert db.scalar(select(Application.student_id).where(Application.term_id == INITIAL, Application.status == 'ACCEPTED')) == expected
        assert db.scalar(select(Application.reason_code).where(Application.term_id == INITIAL, Application.status == 'REJECTED')) == 'SECTION_FULL'


def test_at05_batch_rolls_back_on_database_error(client, monkeypatch):
    prepare_allocation()
    signin(client, 'admin')
    original = reg.enroll
    def broken(db, *args):
        original(db, *args)
        db.execute(text('SELECT 1/0'))
    monkeypatch.setattr(reg, 'enroll', broken)
    before = [count(m) for m in (Enrollment, NotificationOutbox, AuditEvent, AllocationRun)]
    with pytest.raises(Exception):
        client.post(f'/api/v1/admin/terms/{INITIAL}/allocation')
    assert before == [count(m) for m in (Enrollment, NotificationOutbox, AuditEvent, AllocationRun)]
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Application).where(Application.status == 'PENDING')))) == 2
    monkeypatch.setattr(reg, 'enroll', original)
    assert client.post(f'/api/v1/admin/terms/{INITIAL}/allocation').status_code == 200


def test_at06_twenty_students_last_seat():
    clients = [signin(TestClient(app), f'student{n:02}') for n in range(1, 21)]
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(lambda c: add(c, sec('CS303')), clients))
        assert sum(r.status_code == 201 for r in responses) == 1
        assert [r.json()['code'] for r in responses if r.status_code != 201] == ['SECTION_FULL'] * 19
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.section_id == sec('CS303'), Enrollment.status == 'ENROLLED')) == 1
    finally:
        for c in clients:
            c.close()


@pytest.mark.parametrize('codes,student,expected', [
    ([('CS101','A'),('CS302','A')], 'student01', 'TIME_CONFLICT'),
    ([('CS101','A'),('CS101','B')], 'student01', 'ALREADY_ENROLLED'),
    ([('CS101','A'),('EE101','A')], 'student20', 'CREDIT_LIMIT_EXCEEDED'),
    ([('CS101','A'),('CS201','A')], 'student01', None),
])
def test_at07_same_student_races(codes, student, expected):
    a, b = signin(TestClient(app), student), signin(TestClient(app), student)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda x: add(x[0], sec(*x[1])), [(a,codes[0]),(b,codes[1])]))
        assert sum(r.status_code == 201 for r in results) == (1 if expected else 2)
        if expected:
            assert next(r.json()['code'] for r in results if r.status_code != 201) == expected
    finally:
        a.close(); b.close()


def test_at08_prerequisite_drop_release_capacity(client):
    signin(client, 'student11')
    assert add(client, sec('CS301')).json()['code'] == 'PREREQUISITE_MISSING'
    signin(client, 'student19')
    assert client.delete(f'/api/v1/me/terms/{MAIN}/enrollments/{sec("CS304")}', headers={'Idempotency-Key': 'drop'}).status_code == 204
    signin(client, 'student01')
    assert add(client, sec('CS304')).status_code == 201
    signin(client, 'admin')
    r = client.patch(f'/api/v1/admin/terms/{MAIN}/sections/{sec("CS304")}', json={'capacity': 0})
    assert r.json()['code'] == 'CAPACITY_BELOW_ENROLLED'


def test_at09_idempotency_replay_after_close_and_conflict(client, monkeypatch):
    signin(client)
    first = add(client, sec('CS101'), 'retry-key')
    assert first.status_code == 201
    before = [count(m) for m in (Enrollment, AuditEvent, NotificationOutbox, IdempotencyKey)]
    monkeypatch.setattr(reg, 'now', lambda db: FIXED + timedelta(days=100))
    second = add(client, sec('CS101'), 'retry-key')
    assert second.status_code == first.status_code and second.json() == first.json()
    assert before == [count(m) for m in (Enrollment, AuditEvent, NotificationOutbox, IdempotencyKey)]
    assert add(client, sec('EE101'), 'retry-key').json()['code'] == 'IDEMPOTENCY_KEY_REUSED'


def test_failed_write_has_no_side_effects_and_missing_key(client):
    signin(client)
    before = [count(m) for m in (Enrollment, AuditEvent, NotificationOutbox, IdempotencyKey)]
    assert add(client, sec('CS304')).json()['code'] == 'SECTION_FULL'
    assert before == [count(m) for m in (Enrollment, AuditEvent, NotificationOutbox, IdempotencyKey)]
    assert client.post(f'/api/v1/me/terms/{MAIN}/enrollments', json={'section_id': sec('CS101')}).status_code == 422


def test_personal_isolation_and_drop_replay(client):
    signin(client)
    assert add(client, sec('CS101')).status_code == 201
    signin(client, 'student02')
    assert client.get(f'/api/v1/me/terms/{MAIN}/timetable').json()['items'] == []
    assert client.delete(f'/api/v1/me/terms/{MAIN}/enrollments/{sec("CS101")}', headers={'Idempotency-Key':'d'}).status_code == 404
    signin(client)
    path = f'/api/v1/me/terms/{MAIN}/enrollments/{sec("CS101")}'
    assert client.delete(path, headers={'Idempotency-Key':'drop-key'}).status_code == 204
    before = count(NotificationOutbox)
    assert client.delete(path, headers={'Idempotency-Key':'drop-key'}).status_code == 204
    assert count(NotificationOutbox) == before
    assert add(client, sec('CS101')).status_code == 201


def test_seed_preserves_existing_records(client):
    signin(client)
    assert add(client, sec('CS101')).status_code == 201
    before = [count(m) for m in (Enrollment, AuditEvent, NotificationOutbox)]
    seed('initial')
    assert before == [count(m) for m in (Enrollment, AuditEvent, NotificationOutbox)]
    assert client.get(f'/api/v1/terms/{MAIN}/registration').json()['phase'] == 'ADD_DROP'


def test_composite_foreign_keys_and_partial_indexes():
    with pytest.raises(IntegrityError):
        with SessionLocal.begin() as db:
            db.add(Enrollment(student_id=uid('student/student01'), term_id=MAIN, course_id=uid('course/EE101'),
                              section_id=sec('CS101'), status='ENROLLED', source='ADD_DROP'))


def test_draft_admin_lifecycle(client):
    signin(client, 'admin')
    r = client.post('/api/v1/admin/terms', json={'code':'116-1','start_date':'2027-09-01','end_date':'2028-01-31','allocation_seed':'seed'})
    assert r.status_code == 201
    tid = r.json()['id']
    assert client.get(f'/api/v1/terms/{tid}/sections').status_code == 404
    periods = {'periods':[{'kind':'INITIAL','starts_at':'2027-08-01T00:00:00+08:00','ends_at':'2027-08-02T00:00:00+08:00'},
                          {'kind':'ADD_DROP','starts_at':'2027-08-03T00:00:00+08:00','ends_at':'2027-08-10T00:00:00+08:00'}]}
    assert client.put(f'/api/v1/admin/terms/{tid}/registration-periods',json=periods).status_code == 200
    data = {'course_id':uid('course/CS101'),'instructor_id':uid('teacher/chen'),'section_no':'C','credits':'2.5','capacity':3,
            'meetings':[{'weekday':1,'start_time':'09:00','end_time':'10:00','room':'T1'}],'prerequisite_course_ids':[]}
    r = client.post(f'/api/v1/admin/terms/{tid}/sections',json=data)
    assert r.status_code == 201, r.text
    sid = r.json()['id']
    assert client.post(f'/api/v1/admin/terms/{tid}/publish').status_code == 200
    assert client.get(f'/api/v1/terms/{tid}/sections/{sid}').status_code == 200
    assert client.patch(f'/api/v1/admin/terms/{tid}/sections/{sid}',json={'credits':'3.0'}).json()['code'] == 'CATALOG_FROZEN'
    assert client.put(f'/api/v1/admin/terms/{tid}/registration-periods',json=periods).json()['code'] == 'CATALOG_FROZEN'


@pytest.mark.parametrize('offset,expected', [(-2,'UPCOMING'),(-1,'INITIAL'),(7,'AWAITING_ALLOCATION'),(30,'CLOSED')])
def test_phase_half_open_boundaries(offset,expected,client,monkeypatch):
    monkeypatch.setattr(reg,'now',lambda db: FIXED+timedelta(days=offset))
    assert client.get(f'/api/v1/terms/{INITIAL}/registration').json()['phase']==expected


def test_fractional_credits_exact_limit_and_multi_slot_conflict(client):
    with SessionLocal.begin() as db:
        db.get(Section,sec('CS101')).credits='1.5'
        db.get(Section,sec('CS201')).credits='1.5'
        db.get(StudentTerm,(uid('student/student01'),MAIN)).credit_limit='3.0'
    signin(client)
    assert add(client,sec('CS101')).status_code==201
    assert add(client,sec('CS201')).status_code==201
    assert float(client.get(f'/api/v1/me/terms/{MAIN}/timetable').json()['total_credits'])==3.0


def test_initial_allocation_too_early_and_zero_pending(client):
    signin(client,'admin')
    assert client.post(f'/api/v1/admin/terms/{INITIAL}/allocation').json()['code']=='ALLOCATION_NOT_READY'
    with SessionLocal.begin() as db:
        db.get(RegistrationPeriod,(INITIAL,'INITIAL')).ends_at=FIXED-timedelta(seconds=1)
    result=client.post(f'/api/v1/admin/terms/{INITIAL}/allocation')
    assert result.status_code==200 and result.json()['accepted_count']==0 and result.json()['rejected_count']==0


def test_admin_rejects_overlapping_meetings_and_periods(client):
    signin(client,'admin')
    t=client.post('/api/v1/admin/terms',json={'code':'bad-slots','start_date':'2027-09-01','end_date':'2028-01-31','allocation_seed':'fixed'}).json()['id']
    body={'course_id':uid('course/CS101'),'instructor_id':uid('teacher/chen'),'section_no':'A','credits':'3','capacity':5,
          'meetings':[{'weekday':1,'start_time':'09:00','end_time':'11:00'},{'weekday':1,'start_time':'10:00','end_time':'12:00'}]}
    assert client.post(f'/api/v1/admin/terms/{t}/sections',json=body).json()['code']=='MEETING_OVERLAP'
    periods={'periods':[{'kind':'INITIAL','starts_at':'2027-08-01T00:00:00Z','ends_at':'2027-08-03T00:00:00Z'},
                        {'kind':'ADD_DROP','starts_at':'2027-08-02T00:00:00Z','ends_at':'2027-08-10T00:00:00Z'}]}
    assert client.put(f'/api/v1/admin/terms/{t}/registration-periods',json=periods).status_code==422
