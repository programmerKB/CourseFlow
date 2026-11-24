from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app import chat, registration as reg
from app.config import settings
from app.db import SessionLocal
from app.models import (AllocationRun, Application, AuditEvent, Course, CourseCompletion, Enrollment,
                        RegistrationPeriod, Section, StudentTerm, Term)
from app.seed import seed, uid
from conftest import FIXED, signin


MAIN = uid('term/main')
MARKER = uid('run/main')


def empty_database():
    # conftest permits only a disposable *_test database.
    with SessionLocal.begin() as db:
        db.execute(text('TRUNCATE users, departments, terms, courses RESTART IDENTITY CASCADE'))


def windows(db):
    return {p.kind: (p.starts_at, p.ends_at) for p in db.scalars(select(RegistrationPeriod).where(
        RegistrationPeriod.term_id == MAIN))}


@pytest.mark.parametrize('public_term', [MAIN, ''])
def test_fresh_seed_publishes_full_catalog_only_in_main(client, monkeypatch, public_term):
    empty_database()
    monkeypatch.setattr(settings, 'public_initial_term_id', public_term)
    result = seed('initial', recommendation_demo=True, university_demo=True)
    assert result['university_term_id'] == MAIN
    with SessionLocal() as db:
        terms = {term.id: term for term in db.scalars(select(Term))}
        history_id = uid('term/history')
        assert set(terms) == {MAIN, history_id}
        assert terms[MAIN].code.startswith('115-1') and terms[MAIN].catalog_published_at
        assert terms[history_id].archived_at is not None
        assert db.get(CourseCompletion, (uid('student/student01'), uid('course/CS201'), history_id))
        assert db.scalar(select(AllocationRun).where(AllocationRun.term_id == MAIN)) is None
        start, end = windows(db)['INITIAL']
        assert start <= datetime.now(timezone.utc) < end
        courses = list(db.scalars(select(Course.code).join(Section, Section.course_id == Course.id)
                                  .where(Section.term_id == MAIN)))
        assert {code for code in courses if code.startswith('DEMO-')} == {f'DEMO-{n}' for n in range(501, 506)}
        assert courses.count('CS101') == 2
        # Adding recommendation sections cannot overwrite a student's main-term limit or enroll them.
        assert db.get(StudentTerm, (uid('student/student01'), MAIN)).credit_limit == 18
        assert db.scalar(select(Enrollment).where(Enrollment.student_id == uid('student/student01'))) is None

    assert [row['id'] for row in client.get('/api/v1/terms').json()] == [MAIN]
    path = f'/api/v1/terms/{MAIN}/sections'
    assert client.get(path).json()['total'] == 58
    for school, count in (('NTU', 15), ('NTNU', 11), ('NTUST', 14)):
        items = client.get(path, params={'school_code': school, 'page_size': 50}).json()['items']
        references = {row['course_id'] for row in items if row['catalog_origin'] == 'UNIVERSITY_REFERENCE'}
        assert len(references) == count
        assert all(row['term_id'] == MAIN and row['school_code'] == school for row in items)
    signin(client, 'student11')
    advice = client.post('/api/v1/chat', json={'message': '我是電子系學生，給我修課建議', 'term_id': MAIN}).json()
    assert advice['mode'] == 'recommendation'
    assert {row['school_code'] for row in advice['recommendation']['candidates']} == {'NTUST'}
    signin(client, 'admin')
    assert [row['id'] for row in client.get('/api/v1/admin/terms').json()] == [MAIN]


def test_archived_terms_are_unavailable_to_active_views_and_routes(client):
    from app.chat import public_engine

    archived = uid('term/initial')
    with SessionLocal.begin() as db:
        db.get(Term, archived).archived_at = FIXED
    assert [row['id'] for row in client.get('/api/v1/terms').json()] == [MAIN]
    assert client.get(f'/api/v1/terms/{archived}/sections').status_code == 404
    signin(client, 'admin')
    assert [row['id'] for row in client.get('/api/v1/admin/terms').json()] == [MAIN]
    assert client.get(f'/api/v1/admin/terms/{archived}/registration').status_code == 404
    with public_engine().connect() as db:
        for view in ('v_section_availability', 'v_public_section_catalog', 'v_public_section_meetings',
                     'v_public_section_topics'):
            assert db.scalar(text(f'SELECT count(*) FROM {view} WHERE term_id = :term_id'),
                             {'term_id': archived}) == 0


@pytest.mark.parametrize(('scenario', 'phase', 'follow_up', 'status', 'timetable_group'), [
    ('initial', 'INITIAL', '請試排這個方向的本期課程。', 'PENDING', 'pending_items'),
    ('add-drop', 'ADD_DROP', '請幫我自動選課。', 'ENROLLED', 'items'),
])
def test_roadmap_follow_up_selects_current_courses_with_local_model(
        client, monkeypatch, scenario, phase, follow_up, status, timetable_group):
    empty_database()
    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN if phase == 'INITIAL' else '')
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'llama_base_url', 'http://192.0.2.10:11434')
    monkeypatch.setattr(reg, 'now', lambda db: datetime.now(timezone.utc))
    seed(scenario, recommendation_demo=True, university_demo=True)
    signin(client)

    question = '我想走嵌入式系統，請給我修課路徑。'
    constraint = '請試排這個方向的課，只選臺科大週二 15:00 至 18:00 的 1 門課。'
    model_messages = []

    async def intent(message, **kwargs):
        model_messages.append(message)
        if message == question:
            return {'tool': 'recommend', 'topic': '嵌入式系統', 'track_code': 'embedded_systems'}
        if message == constraint:
            assert '"selected_track_code": "embedded_systems"' in kwargs['system_instruction']
            return {'tool': 'recommend', 'school_code': 'NTUST', 'target_count': 1,
                    'available_times': [{'weekday': 2, 'start_time': '15:00', 'end_time': '18:00'}]}
        return {'tool': 'clarify'}

    monkeypatch.setattr(chat, 'request_plan', intent)
    consult = client.post('/api/v1/chat', json={'term_id': MAIN, 'message': question})
    assert consult.status_code == 200, consult.text
    advice = consult.json()['recommendation']
    assert advice['plan'] is None

    reply = client.post('/api/v1/chat', json={
        'term_id': MAIN, 'message': follow_up, 'track_code': advice['roadmap']['track_code'],
    })
    assert reply.status_code == 200 and reply.json()['mode'] == 'recommendation', reply.text
    assert model_messages == [question]
    plan = reply.json()['recommendation']['plan']
    assert plan['phase'] == phase and plan['term_id'] == MAIN and plan['section_ids']
    filtered = client.post('/api/v1/chat', json={
        'term_id': MAIN, 'message': constraint, 'track_code': advice['roadmap']['track_code'],
    })
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()['recommendation']['plan']['section_ids'] == [uid('section/main/EE301/A')]
    assert model_messages == [question, constraint]
    timetable_url = f'/api/v1/me/terms/{MAIN}/timetable'
    before = client.get(timetable_url).json()
    assert before['items'] == before['pending_items'] == []
    confirm = client.post(f'/api/v1/me/terms/{MAIN}/selection-plans/{plan["plan_id"]}/confirm',
                          json={'section_ids': plan['section_ids']}, headers={'Idempotency-Key': 'roadmap-confirm'})
    assert confirm.status_code == 200, confirm.text
    assert {item['status'] for item in confirm.json()['items']} == {status}
    assert {row['id'] for row in client.get(timetable_url).json()[timetable_group]} == set(plan['section_ids'])


def test_scoped_seed_reopens_old_demo_once_and_preserves_records(monkeypatch):
    empty_database()
    seed('add-drop', recommendation_demo=True)
    student = uid('student/student01')
    enrolled_id = uid('test/real-enrollment')
    application_id = uid('test/pending-application')
    with SessionLocal.begin() as db:
        before = windows(db)
        assert db.get(AllocationRun, MARKER) is not None
        term_ids = set(db.scalars(select(Term.id)))
        db.add(Enrollment(id=enrolled_id, student_id=student, term_id=MAIN, course_id=uid('course/CS101'),
                          section_id=uid('section/main/CS101/A'), status='ENROLLED', source='ADD_DROP'))
        db.add(Application(id=application_id, student_id=student, term_id=MAIN, course_id=uid('course/CS303'),
                           section_id=uid('section/main/CS303/A'), status='PENDING'))

    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    assert seed('initial', recommendation_demo=True)['main_initial_reopened'] is True
    with SessionLocal() as db:
        after = windows(db)
        start, end = after['INITIAL']
        assert start <= datetime.now(timezone.utc) < end
        assert end - start == timedelta(days=7)
        assert end <= after['ADD_DROP'][0]
        assert after != before
        assert db.get(AllocationRun, MARKER) is None
        assert db.get(Enrollment, enrolled_id).status == 'ENROLLED'
        assert db.get(Application, application_id).status == 'PENDING'
        assert set(db.scalars(select(Term.id))) == term_ids
        assert db.get(Section, uid('section/main/DEMO-501/A')) is not None
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'DEMO_INITIAL_REOPENED'))
        assert event.target_id == MARKER and event.actor_user_id is None
        assert event.payload['previous_periods'][0]['kind'] == 'INITIAL'
        assert datetime.fromisoformat(event.payload['previous_periods'][0]['ends_at']) == before['INITIAL'][1]

    assert seed('initial', recommendation_demo=True)['main_initial_reopened'] is False
    with SessionLocal() as db:
        assert windows(db) == after
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'DEMO_INITIAL_REOPENED')))) == 1

    monkeypatch.setattr(reg, 'now', lambda db: end + timedelta(seconds=1))
    with SessionLocal.begin() as db:
        term = db.get(Term, MAIN)
        assert reg.phase(db, term) == 'AWAITING_ALLOCATION'
        result = reg.allocate(db, MAIN, uid('user/admin'))
        assert result['run_id'] != MARKER
        assert result['accepted_count'] == 1
        assert db.get(Application, application_id).status == 'ACCEPTED'
        assert db.get(Enrollment, enrolled_id).status == 'ENROLLED'
        assert reg.phase(db, term) == 'RESULTS'


@pytest.mark.parametrize('evidence', ['result', 'publication', 'changed_schedule'])
def test_scoped_seed_does_not_reset_real_or_modified_allocation(monkeypatch, evidence):
    empty_database()
    seed('add-drop')
    with SessionLocal.begin() as db:
        if evidence == 'result':
            db.add(Application(student_id=uid('student/student01'), term_id=MAIN, course_id=uid('course/CS101'),
                               section_id=uid('section/main/CS101/A'), status='ACCEPTED', allocation_run_id=MARKER))
        elif evidence == 'publication':
            db.add(AuditEvent(actor_user_id=uid('user/admin'), term_id=MAIN, action='ALLOCATION_PUBLISHED',
                              target_type='allocation_run', target_id=MARKER))
        else:
            period = db.get(RegistrationPeriod, (MAIN, 'INITIAL'))
            period.ends_at += timedelta(hours=1)
        db.flush()
        before = windows(db)

    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    assert seed('initial')['main_initial_reopened'] is False
    with SessionLocal() as db:
        assert db.get(AllocationRun, MARKER) is not None
        assert windows(db) == before
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'DEMO_INITIAL_REOPENED')) is None
