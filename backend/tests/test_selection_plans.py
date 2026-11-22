"""Recommendation and atomic confirmation acceptance tests on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError

from app import chat, registration as reg
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import (AllocationRun, Application, AuditEvent, Enrollment, IdempotencyKey,
                        NotificationOutbox, Section, SectionMeeting, SelectionPlan, SelectionPlanItem, StudentTerm)
from app.seed import seed_recommendation, uid
from conftest import FIXED, signin


ADD_DROP = uid('term/recommendation-add-drop')
INITIAL = uid('term/recommendation-initial')
STUDENT = uid('student/student01')
TOPIC = '我的研究方向是大語言模型，請推薦並幫我選三門。'


def section(code, term=ADD_DROP):
    slug = 'recommendation-add-drop' if term == ADD_DROP else 'recommendation-initial'
    return uid(f'section/{slug}/{code}/A')


@pytest.fixture(autouse=True)
def research_demo(fresh_db):
    with SessionLocal.begin() as db:
        seed_recommendation(db, FIXED, uid('dep/CS'))


def ask(client, term=ADD_DROP, message=TOPIC):
    response = client.post('/api/v1/chat', json={'term_id': term, 'message': message})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, plan, ids, key='confirm-demo'):
    path = f'/api/v1/me/terms/{plan["term_id"]}/selection-plans/{plan["plan_id"]}/confirm'
    return client.post(path, json={'section_ids': ids}, headers={'Idempotency-Key': key})


def row_counts(term):
    with SessionLocal() as db:
        return tuple(db.scalar(select(func.count()).select_from(model).where(model.term_id == term))
                     for model in (Application, Enrollment, AuditEvent, NotificationOutbox, IdempotencyKey))


def get_status(plan):
    with SessionLocal() as db:
        return db.get(SelectionPlan, plan['plan_id']).status


def test_at14_known_synonyms_recommend_only_real_sections_and_explain_rejections(client):
    signin(client)
    before = row_counts(ADD_DROP)
    first = ask(client)
    second = ask(client, message='我的研究方向是 LLM，請推薦並幫我選三門。')
    a, b = first['recommendation'], second['recommendation']
    assert first['mode'] == second['mode'] == 'recommendation'
    assert a['topic'] == b['topic'] == '大語言模型'
    assert a['candidates'] == b['candidates']
    assert [row['course_code'] for row in a['candidates']] == [f'DEMO-{index}' for index in range(501, 506)]
    assert [(row['score'], row['remaining'], row['reason_code']) for row in a['candidates']] == [
        (7, 4, None), (4, 6, None), (4, 2, None), (4, 3, 'TIME_CONFLICT'), (4, 0, 'SECTION_FULL'),
    ]
    ids = {row['section_id'] for row in a['candidates']}
    assert {item['section_id'] for item in first['sources']} == ids
    assert 1 <= len(a['learning_extensions']) <= 2
    assert all(set(item['section_ids']) <= ids for item in a['learning_extensions'])
    assert a['baseline']['considered_credits'] == '9.0'
    assert a['plan']['projected_credits'] == '18.0'
    assert a['plan']['section_ids'] == [section(f'DEMO-{index}') for index in (501, 502, 503)]
    assert row_counts(ADD_DROP) == before
    with SessionLocal() as db:
        draft = db.get(SelectionPlan, a['plan']['plan_id'])
        assert draft.student_id == STUDENT and draft.status == 'DRAFT'
        assert draft.expires_at - draft.created_at == timedelta(minutes=15)
        item_rows = db.scalars(select(SelectionPlanItem).where(SelectionPlanItem.plan_id == draft.id)
                               .order_by(SelectionPlanItem.position))
        assert [item.section_id for item in item_rows] == a['plan']['section_ids']
    unrelated = ask(client, message='我的研究方向是量子重力，請推薦相關課。')
    assert unrelated['recommendation']['candidates'] == [] and unrelated['recommendation']['plan'] is None
    assert ask(client, message='請給我修課建議')['mode'] == 'clarify'


def test_at14_prerequisites_credits_unselectable_and_all_meetings(client, monkeypatch):
    with SessionLocal.begin() as db:
        db.get(StudentTerm, (uid('student/student11'), ADD_DROP)).credit_limit = Decimal('3.0')
        db.get(Section, section('DEMO-503')).is_selectable = False
        # The first meeting fits Tuesday's preference, but the second does not.
        db.add(SectionMeeting(section_id=section('DEMO-504'), weekday=2,
                              start_time=time(9), end_time=time(10), room='AUDIT'))
    signin(client, 'student11')
    result = ask(client)['recommendation']
    reasons = {item['course_code']: item['reason_code'] for item in result['candidates']}
    assert reasons['DEMO-501'] == 'PREREQUISITE_MISSING'
    assert reasons['DEMO-503'] == 'SECTION_UNAVAILABLE'
    assert reasons['DEMO-504'] == 'CREDIT_LIMIT_EXCEEDED'
    assert result['plan']['section_ids'] == [section('DEMO-502')]

    signin(client, 'student02')
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'fake-for-structured-plan')

    async def intent(*args, **kwargs):
        return '''{"mode":"recommendation","tool":"recommend","topic":"LLM",
            "available_times":[{"weekday":2,"start_time":"08:00","end_time":"18:00"}]}'''

    monkeypatch.setattr(chat, 'generate_content', intent)
    restricted = ask(client)['recommendation']
    result_by_code = {item['course_code']: item for item in restricted['candidates']}
    assert result_by_code['DEMO-504']['reason_code'] == 'TIME_PREFERENCE_MISMATCH'
    assert len(result_by_code['DEMO-504']['meetings']) == 2
    assert restricted['plan']['section_ids'] == [section('DEMO-501')]


@pytest.mark.parametrize(('term', 'phase', 'status'), [
    (ADD_DROP, 'ADD_DROP', 'ENROLLED'), (INITIAL, 'INITIAL', 'PENDING'),
])
def test_at15_only_confirmed_subset_writes_and_initial_goes_to_allocation(client, monkeypatch, term, phase, status):
    signin(client)
    before = row_counts(term)
    plan = ask(client, term=term)['recommendation']['plan']
    assert len(plan['section_ids']) == 3
    assert row_counts(term) == before
    selected = [plan['section_ids'][0], plan['section_ids'][2]]
    response = confirm(client, plan, selected[::-1])
    assert response.status_code == 200, response.text
    assert response.json()['phase'] == phase and response.json()['items'] == [
        {'section_id': section_id, 'status': status} for section_id in selected
    ]
    increments = (0, 2, 2, 0, 1) if phase == 'ADD_DROP' else (2, 0, 2, 0, 1)
    assert row_counts(term) == tuple(count + diff for count, diff in zip(before, increments))
    assert get_status(plan) == 'SUBMITTED'
    timetable = client.get(f'/api/v1/me/terms/{term}/timetable').json()
    group = 'pending_items' if phase == 'INITIAL' else 'items'
    assert {row['id'] for row in timetable[group]}.intersection(plan['section_ids']) == set(selected)
    with SessionLocal() as db:
        payloads = [event.payload for event in db.scalars(select(AuditEvent).where(AuditEvent.term_id == term))]
        assert {payload['section_id'] for payload in payloads if payload.get('plan_id') == plan['plan_id']} == set(selected)
    replay = confirm(client, plan, selected)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert row_counts(term) == tuple(count + diff for count, diff in zip(before, increments))
    if phase == 'INITIAL':
        monkeypatch.setattr(reg, 'now', lambda db: FIXED + timedelta(days=8))
        signin(client, 'admin')
        result = client.post(f'/api/v1/admin/terms/{term}/allocation')
        assert result.status_code == 200 and result.json()['accepted_count'] == 2
        with SessionLocal() as db:
            assert {e.section_id for e in db.scalars(select(Enrollment).where(Enrollment.term_id == term))} == set(selected)
            notice = db.scalar(select(NotificationOutbox).where(NotificationOutbox.term_id == term,
                                                                NotificationOutbox.type == 'ALLOCATION'))
            assert notice.payload['url'] == f'/applications?term={term}'
        signin(client)
        timetable = client.get(f'/api/v1/me/terms/{term}/timetable').json()
        assert timetable['pending_items'] == []
        assert {row['id'] for row in timetable['items']} == set(selected)


def test_at16_seat_taken_after_preview_leaves_all_rows_and_draft_unchanged(client):
    contested = section('DEMO-503')
    with SessionLocal.begin() as db:
        db.get(Section, contested).capacity = 1
    signin(client)
    plan = ask(client)['recommendation']['plan']
    with TestClient(app) as other:
        signin(other, 'student02')
        response = other.post(f'/api/v1/me/terms/{ADD_DROP}/enrollments', json={'section_id': contested},
                              headers={'Idempotency-Key': 'last-seat'})
        assert response.status_code == 201, response.text
    before = row_counts(ADD_DROP)
    response = confirm(client, plan, [plan['section_ids'][0], contested])
    assert response.status_code == 409
    assert response.json()['code'] == 'SECTION_FULL' and response.json()['details']['section_id'] == contested
    assert row_counts(ADD_DROP) == before and get_status(plan) == 'DRAFT'


def test_at16_database_error_after_first_write_rolls_back_whole_batch(client, monkeypatch):
    signin(client)
    plan = ask(client)['recommendation']['plan']
    selected = plan['section_ids'][:2]
    before = row_counts(ADD_DROP)
    original = reg.enroll

    def enroll(db, student_id, term_id, section_row, source):
        if section_row.id == selected[1]:
            db.execute(text('SELECT 1/0'))
        return original(db, student_id, term_id, section_row, source)

    with monkeypatch.context() as patch:
        patch.setattr(reg, 'enroll', enroll)
        with TestClient(app, raise_server_exceptions=False) as checked:
            signin(checked)
            response = confirm(checked, plan, selected)
            assert response.status_code == 500
    assert row_counts(ADD_DROP) == before and get_status(plan) == 'DRAFT'
    assert confirm(client, plan, selected).status_code == 200


def test_at17_replay_uses_original_set_even_after_expiry_but_obeys_ownership(client, monkeypatch):
    signin(client)
    plan = ask(client)['recommendation']['plan']
    selected = plan['section_ids'][:2]
    result = confirm(client, plan, selected)
    assert result.status_code == 200, result.text
    before = row_counts(ADD_DROP)
    monkeypatch.setattr(reg, 'now', lambda db: FIXED + timedelta(days=31))
    replay = confirm(client, plan, selected[::-1])
    assert replay.status_code == 200 and replay.json() == result.json()
    assert confirm(client, plan, [selected[0]]).json()['code'] == 'IDEMPOTENCY_KEY_REUSED'
    assert confirm(client, plan, selected, 'different-key').json()['code'] == 'PLAN_ALREADY_SUBMITTED'
    signin(client, 'student02')
    forbidden = confirm(client, plan, selected)
    assert forbidden.status_code == 404 and forbidden.json()['code'] == 'PLAN_NOT_FOUND'
    assert row_counts(ADD_DROP) == before


def test_at17_competing_batches_with_opposite_input_order_preserve_capacity():
    ordered = [section('DEMO-502'), section('DEMO-503')]
    with SessionLocal.begin() as db:
        for section_id in ordered:
            db.get(Section, section_id).capacity = 1
    barrier = Barrier(2)

    def request(login, ids):
        with TestClient(app) as concurrent_client:
            signin(concurrent_client, login)
            plan = ask(concurrent_client)['recommendation']['plan']
            assert set(ids) <= set(plan['section_ids'])
            barrier.wait(timeout=8)
            return confirm(concurrent_client, plan, ids)

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(request, 'student01', ordered)
        b = executor.submit(request, 'student02', ordered[::-1])
        responses = [a.result(), b.result()]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert next(response for response in responses if response.status_code == 409).json()['code'] == 'SECTION_FULL'
    with SessionLocal() as db:
        for section_id in ordered:
            assert db.scalar(select(func.count()).select_from(Enrollment).where(
                Enrollment.section_id == section_id, Enrollment.status == 'ENROLLED')) == 1
    assert row_counts(ADD_DROP)[3:] == (0, 1)


def test_at18_rejects_expired_closed_and_outside_draft_without_writes(client, monkeypatch):
    signin(client)
    plan = ask(client)['recommendation']['plan']
    chosen = plan['section_ids'][0]
    before = row_counts(ADD_DROP)
    assert confirm(client, plan, [section('DEMO-505')]).json()['code'] == 'PLAN_SELECTION_MISMATCH'
    duplicate = confirm(client, plan, [chosen, chosen.upper()])
    assert duplicate.status_code == 422
    monkeypatch.setattr(reg, 'now', lambda db: FIXED + timedelta(minutes=15))
    assert confirm(client, plan, [chosen]).json()['code'] == 'PLAN_EXPIRED'
    monkeypatch.setattr(reg, 'now', lambda db: FIXED + timedelta(days=30))
    assert confirm(client, plan, [chosen]).json()['code'] == 'REGISTRATION_CLOSED'
    unavailable = ask(client)['recommendation']
    assert unavailable['plan'] is None and unavailable['plan_unavailable_reason'] == 'REGISTRATION_CLOSED'
    assert row_counts(ADD_DROP) == before and get_status(plan) == 'DRAFT'


def test_at18_stage_change_never_reinterprets_an_initial_draft(client, monkeypatch):
    signin(client)
    plan = ask(client, term=INITIAL)['recommendation']['plan']
    with SessionLocal.begin() as db:
        db.add(AllocationRun(term_id=INITIAL, seed='research-demo-v2', algorithm_version='v1',
                             published_at=FIXED + timedelta(days=7), accepted_count=0, rejected_count=0))
    before = row_counts(INITIAL)
    monkeypatch.setattr(reg, 'now', lambda db: FIXED + timedelta(days=8))
    response = confirm(client, plan, plan['section_ids'])
    assert response.status_code == 409 and response.json()['code'] == 'PLAN_EXPIRED'
    assert row_counts(INITIAL) == before


def test_public_topic_view_has_no_private_table_grants():
    with chat.public_engine().connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM v_public_section_topics')) == 10
        assert not connection.scalar(text("SELECT has_table_privilege(current_user, 'selection_plans', 'SELECT')"))
        assert not connection.scalar(text("SELECT has_table_privilege(current_user, 'course_topics', 'SELECT')"))
        with pytest.raises(ProgrammingError):
            connection.execute(text('SELECT * FROM selection_plan_items'))


def test_timetable_recognizes_spoken_weekday(client):
    signin(client)
    result = ask(client, message='我週四有課嗎？')
    assert result['mode'] == 'student_query'
    assert 'CS201' in result['answer'] and 'CS101' not in result['answer'] and 'CS302' not in result['answer']
