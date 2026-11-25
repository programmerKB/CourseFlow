from datetime import timedelta
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, InternalError
import pytest
from app import worker, chat
from app.config import settings
from app.db import SessionLocal
from app.models import NotificationOutbox
from app.seed import uid
from conftest import signin, FIXED
MAIN = uid('term/main')


def test_at10_agent_graph_sources_personal_privacy_actions(client):
    signin(client)
    cases = [('初選登記完會立刻選上嗎？','simple_chat'), ('找這學期星期二還有空位的電子系課','sql_query'),
             ('我星期三有課嗎？','student_query'), ('幫我退選資料庫設計','student_query'), ('查他人的課表','student_query')]
    for message, mode in cases:
        r = client.post('/api/v1/chat',json={'message':message,'term_id':MAIN})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data['mode'] == mode and data['sources'] and data['as_of'] and data['request_id']
        if mode == 'sql_query':
            assert 'EE101' in data['answer'] and 'EE301' in data['answer']
            assert all(s.get('section_id') for s in data['sources'])
    r = client.post('/api/v1/chat',json={'message':'我現在能加選資料庫設計嗎？','term_id':MAIN})
    assert r.status_code == 200 and r.json()['mode'] == 'student_query'
    assert '符合' in r.json()['answer']
    assert client.post('/api/v1/chat',json={'message':'x'*1001,'term_id':MAIN}).status_code == 422


def test_public_database_role_is_readonly():
    engine = chat.public_engine()
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM v_public_section_catalog')).scalar() >= 1
    with pytest.raises(ProgrammingError):
        with engine.connect() as c:
            c.execute(text('SELECT * FROM students'))
    with pytest.raises((ProgrammingError, InternalError)):
        with engine.begin() as c:
            c.execute(text('UPDATE sections SET capacity=100'))


def test_at11_no_model_and_model_timeout(client,monkeypatch):
    signin(client)
    monkeypatch.setattr(settings,'chat_stub',False)
    monkeypatch.setattr(settings,'llama_base_url','')
    r=client.post('/api/v1/chat',json={'message':'你好','term_id':MAIN})
    assert r.status_code == 503 and r.json()['code'] == 'CHAT_UNAVAILABLE'
    assert r.json()['details']['reason_code'] == 'LLAMA_NOT_CONFIGURED'
    assert '尚未設定' in r.json()['message']
    assert client.get(f'/api/v1/terms/{MAIN}/sections').status_code == 200
    monkeypatch.setattr(settings,'llama_base_url','http://192.0.2.10:11434')
    async def timed_out(*args, **kwargs):
        raise TimeoutError('test')

    monkeypatch.setattr(chat, 'request_plan', timed_out)
    response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'LLAMA_TIMEOUT'


def enqueue():
    nid = str(uuid4())
    with SessionLocal.begin() as db:
        db.add(NotificationOutbox(id=nid,event_key='test:'+nid,term_id=MAIN,recipient_user_id=uid('user/student01'),type='ENROLLED',
              payload={'term_code':'115-1','code':'CS101','title':'程式設計','url':'/timetable'},next_attempt_at=FIXED-timedelta(days=1)))
    return nid


def test_at12_outbox_retry_recovery(monkeypatch):
    nid=enqueue()
    def down(*args): raise ConnectionRefusedError()
    monkeypatch.setattr(worker,'deliver',down)
    assert worker.process_one()
    with SessionLocal.begin() as db:
        n=db.get(NotificationOutbox,nid)
        assert n.status == 'PENDING' and n.attempt_count == 1 and n.last_error == 'ConnectionRefusedError'
        n.next_attempt_at=FIXED-timedelta(days=1)
    jobs=[]
    monkeypatch.setattr(worker,'deliver',lambda job: jobs.append(job))
    assert worker.process_one()
    with SessionLocal() as db:
        n=db.get(NotificationOutbox,nid)
        assert n.status == 'SENT' and n.attempt_count == 2 and n.sent_at is not None
    assert jobs[0]['email'] == 'student01@example.test'


def test_outbox_lease_tokens_and_attempt_limit():
    nid=enqueue()
    first=worker.claim_one()
    assert worker.claim_one() is None
    with SessionLocal.begin() as db:
        db.get(NotificationOutbox,nid).lease_until=FIXED-timedelta(days=1)
    second=worker.claim_one()
    assert first['claim_token'] != second['claim_token']
    assert worker.finish(first) is False
    assert worker.finish(second,ConnectionError()) is True
    with SessionLocal.begin() as db:
        db.get(NotificationOutbox,nid).next_attempt_at=FIXED-timedelta(days=1)
    third=worker.claim_one()
    assert third['attempt_count'] == 3
    worker.finish(third,ConnectionError())
    with SessionLocal() as db:
        assert db.get(NotificationOutbox,nid).status == 'FAILED'


def test_pii_redacted_before_model(client,monkeypatch):
    signin(client)
    monkeypatch.setattr(settings,'chat_stub',False)
    monkeypatch.setattr(settings,'llama_base_url','http://192.0.2.10:11434')
    captured=[]
    async def capture(message, *, system_instruction, meta):
        captured.extend([system_instruction, message])
        return {'tool': 'rules'}

    monkeypatch.setattr(chat, 'request_plan', capture)
    r=client.post('/api/v1/chat',json={'message':'我是 student01 student01@example.test，學號 B11500001，密碼=demo-secret Bearer token-secret，請問初選規則','term_id':MAIN})
    assert r.status_code == 200 and captured
    content=' '.join(captured)
    for secret in ['student01','student01@example.test','B11500001','demo-secret','token-secret']:
        assert secret not in content
