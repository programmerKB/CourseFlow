from datetime import timedelta

from conftest import FIXED, signin
from app import chat, voice
from app.config import settings
from app.db import SessionLocal
from app.models import Application, RegistrationPeriod
from app.seed import uid


MAIN = uid('term/main')
OTHER = uid('term/initial')
MAIN_SECTION = uid('section/main/CS101/A')
OTHER_SECTION = uid('section/initial/CS101/A')


def test_scoped_term_is_the_only_public_term_and_defaults_chat(client, monkeypatch):
    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    assert [term['id'] for term in client.get('/api/v1/terms').json()] == [MAIN]
    for path in ('registration', 'sections', f'sections/{OTHER_SECTION}'):
        assert client.get(f'/api/v1/terms/{OTHER}/{path}').status_code == 404

    signin(client)
    r = client.post('/api/v1/chat', json={'message': '找星期二還有空位的電子系課'})
    assert r.status_code == 200
    assert r.json()['mode'] == 'sql_query'
    assert {s['term_id'] for s in r.json()['sources']} == {MAIN}
    rejected = client.post('/api/v1/chat', json={'message': '請問初選規則', 'term_id': OTHER})
    assert rejected.status_code == 404
    assert rejected.json()['code'] == 'TERM_NOT_FOUND'
    assert client.put(f'/api/v1/me/terms/{OTHER}/applications/{OTHER_SECTION}').status_code == 404

    signin(client, 'admin')
    assert {MAIN, OTHER} <= {term['id'] for term in client.get('/api/v1/admin/terms').json()}
    archived = client.get(f'/api/v1/admin/terms/{OTHER}/sections', params={'q': 'CS101', 'weekday': 1})
    assert archived.status_code == 200
    assert [item['id'] for item in archived.json()['items']] == [OTHER_SECTION]
    assert client.get(f'/api/v1/admin/terms/{OTHER}/sections/{OTHER_SECTION}').status_code == 200


def test_scoped_term_accepts_initial_applications_and_disables_add_drop(client, monkeypatch):
    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    signin(client)
    registration = f'/api/v1/terms/{MAIN}/registration'
    # The fixture has a published result and would be in ADD_DROP without the scope.
    status = client.get(registration).json()
    assert status['phase'] == 'RESULTS'
    assert [period['kind'] for period in status['periods']] == ['INITIAL']
    add = client.post(f'/api/v1/me/terms/{MAIN}/enrollments',
                      json={'section_id': MAIN_SECTION}, headers={'Idempotency-Key': 'scope-no-add-drop'})
    assert add.status_code == 409
    assert add.json()['code'] == 'REGISTRATION_CLOSED'

    with SessionLocal.begin() as db:
        period = db.get(RegistrationPeriod, (MAIN, 'INITIAL'))
        period.starts_at = FIXED - timedelta(days=1)
        period.ends_at = FIXED + timedelta(days=1)
    assert client.get(registration).json()['phase'] == 'INITIAL'
    message = '我要登記初選 CS101 A班'
    text = client.post('/api/v1/chat', json={'message': message}, headers={'Idempotency-Key': 'scope-text'})
    assert text.status_code == 200, text.text
    assert [choice['section_id'] for choice in text.json()['choices']] == [MAIN_SECTION]

    async def recognize(*args):
        return message

    monkeypatch.setattr(voice, 'recognize', recognize)
    audio = client.post('/api/v1/chat/voice', files={'audio': ('fixture.webm', b'fixture', 'audio/webm')},
                        headers={'Idempotency-Key': 'scope-voice'})
    assert audio.status_code == 200, audio.text
    assert [choice['section_id'] for choice in audio.json()['choices']] == [MAIN_SECTION]

    apply = client.put(f'/api/v1/me/terms/{MAIN}/applications/{MAIN_SECTION}')
    assert apply.status_code == 200 and apply.json()['status'] == 'PENDING'


def test_department_advice_after_confirmation_uses_current_registrations_without_model(client, monkeypatch):
    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'llama_base_url', '')

    async def unexpected(*args, **kwargs):
        raise AssertionError('This query must use the local database')

    monkeypatch.setattr(chat, 'request_plan', unexpected)
    with SessionLocal.begin() as db:
        period = db.get(RegistrationPeriod, (MAIN, 'INITIAL'))
        period.starts_at, period.ends_at = FIXED - timedelta(days=1), FIXED + timedelta(days=1)
        db.add(Application(student_id=uid('student/student02'), term_id=MAIN, status='PENDING',
                           course_id=uid('course/CS101'), section_id=MAIN_SECTION))

    signin(client)
    question = {'message': '我是電子系學生 給我修課建議'}
    response = client.post('/api/v1/chat', json=question)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mode'] == 'recommendation'
    first = data['recommendation']
    assert first['topic'] == '電子工程系'
    assert {item['course_code'] for item in first['candidates']} == {'EE101', 'EE201', 'EE301', 'EE302'}
    assert all(item['match_reason'] == '開課系所：電子工程系。' for item in first['candidates'])
    assert all(link['term_id'] == MAIN for link in data['sources'])

    chosen = uid('section/main/EE101/A')
    plan_id = first['plan']['plan_id']
    response = client.post(f'/api/v1/me/terms/{MAIN}/selection-plans/{plan_id}/confirm',
                           json={'section_ids': [chosen]}, headers={'Idempotency-Key': 'department-confirm'})
    assert response.status_code == 200, response.text
    assert response.json()['items'] == [{'section_id': chosen, 'status': 'PENDING'}]

    response = client.post('/api/v1/chat', json=question)
    assert response.status_code == 200, response.text
    followup = response.json()['recommendation']
    assert {item['id'] for item in followup['baseline']['pending']} == {chosen}
    assert next(item for item in followup['candidates'] if item['section_id'] == chosen)['reason_code'] == 'APPLICATION_EXISTS'
    assert chosen not in followup['plan']['section_ids']

    response = client.post('/api/v1/chat', json={'message': '我剛剛選了什麼？'})
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary['mode'] == 'student_query'
    assert '初選待分發' in summary['answer'] and 'EE101 電路學' in summary['answer']
    assert 'CS101' not in summary['answer']
    assert [item['title'] for item in summary['sources']] == ['我的選課', '我的課表']
    assert len(client.get(f'/api/v1/me/terms/{MAIN}/applications').json()['items']) == 1
