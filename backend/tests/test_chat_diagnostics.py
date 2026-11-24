"""The Chinese assistant plans with the local Llama model; failures stay actionable and private."""
import json
import logging

import httpx
import pytest

from app import chat
from app.config import settings
from app.seed import seed, uid
from conftest import signin

MAIN = uid('term/main')
INITIAL = uid('term/initial')
LLAMA_URL = 'http://192.0.2.10:11434'
PROVIDER_SECRET = 'provider-payload-never-log'


def use_local_model(monkeypatch):
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'llama_base_url', LLAMA_URL)
    monkeypatch.setattr(settings, 'llama_api_style', 'ollama')
    monkeypatch.setattr(settings, 'llama_model', 'llama3.2:3b')


def mock_ollama(monkeypatch, response_for):
    async def send(self, request, *args, **kwargs):
        assert str(request.url) == LLAMA_URL + '/api/chat'
        response = response_for(request)
        response.request = request
        return response

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)


def ollama_reply(plan, **extra):
    return httpx.Response(200, json={
        'model': 'llama3.2:3b', 'created_at': '2026-11-25T02:00:00Z', 'done': True,
        'message': {'role': 'assistant', 'content': json.dumps(plan, ensure_ascii=False)},
        'prompt_eval_count': 1240, 'eval_count': 60, 'total_duration': 812_000_000, **extra,
    })


def full_plan(**fields):
    return {'tool': 'rules', 'keyword': None, 'department': None, 'school_code': None, 'weekday': None,
            'has_seats': False, 'sort': 'code', 'topic': None, 'track_code': None, 'target_count': 3,
            'available_times': [], **fields}


@pytest.mark.parametrize(('status', 'reason'), [
    (401, 'LLAMA_AUTH_FAILED'), (403, 'LLAMA_AUTH_FAILED'), (404, 'LLAMA_MODEL_UNAVAILABLE'),
    (429, 'LLAMA_RATE_LIMITED'), (500, 'LLAMA_UNAVAILABLE'), (503, 'LLAMA_UNAVAILABLE'),
])
def test_model_server_failure_has_safe_diagnostic(client, monkeypatch, caplog, status, reason):
    signin(client)
    use_local_model(monkeypatch)
    mock_ollama(monkeypatch, lambda request: httpx.Response(status, json={'error': PROVIDER_SECRET}))
    with caplog.at_level(logging.INFO):
        response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    data = response.json()
    assert response.status_code == 503 and data['code'] == 'CHAT_UNAVAILABLE'
    assert data['details']['reason_code'] == reason and len(data['details']['sources']) == 2
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == 'courses.agent' and record.levelno == logging.WARNING]
    diagnostic = next(event for event in events if event.get('mode') == 'unavailable')
    assert diagnostic['request_id'] == data['request_id'] and diagnostic['reason_code'] == reason
    assert PROVIDER_SECRET not in response.text + caplog.text
    assert client.get(f'/api/v1/terms/{MAIN}/sections').status_code == 200


@pytest.mark.parametrize(('error', 'reason'), [
    (httpx.ConnectError('refused'), 'LLAMA_UNREACHABLE'), (httpx.ConnectTimeout('no route'), 'LLAMA_UNREACHABLE'),
    (httpx.ReadTimeout('slow'), 'LLAMA_TIMEOUT'),
])
def test_unreachable_or_slow_model_server(client, monkeypatch, error, reason):
    signin(client)
    use_local_model(monkeypatch)

    async def send(self, request, *args, **kwargs):
        raise error

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    assert response.status_code == 503 and response.json()['details']['reason_code'] == reason
    assert '請先使用課程查詢或我的課表' in response.json()['message']


@pytest.mark.parametrize(('message', 'base_url'), [
    ('找星期二還有空位的電子系課', LLAMA_URL),
    ('請幫我查詢這學期週二仍有名額的臺科大電子工程系的課程。', ''),
])
def test_weekday_seat_search_queries_catalog_without_model(client, monkeypatch, message, base_url):
    signin(client)
    use_local_model(monkeypatch)
    monkeypatch.setattr(settings, 'llama_base_url', base_url)

    async def unexpected(*args, **kwargs):
        pytest.fail('A weekday seat search must use the public catalog directly')

    monkeypatch.setattr(chat, 'request_plan', unexpected)
    response = client.post('/api/v1/chat', json={'message': message, 'term_id': INITIAL})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mode'] == 'sql_query'
    assert {item['title'].split()[0] for item in data['sources']} == {'EE101', 'EE301'}
    assert {item['term_id'] for item in data['sources']} == {INITIAL}
    assert data['agent']['model'] == 'local rules' and data['agent']['tools'][0] == 'rules.plan_query'


def test_old_ollama_json_mode_fallback_still_runs_the_course_query(client, monkeypatch):
    signin(client)
    use_local_model(monkeypatch)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={'error': 'invalid format'})
        return ollama_reply(full_plan(tool='courses', department='電子系', weekday=2, has_seats=True,
                                      sort='remaining'))

    mock_ollama(monkeypatch, respond)
    response = client.post('/api/v1/chat', json={
        'message': '找星期二還有空位的電子系課，依剩餘名額排序', 'term_id': INITIAL,
    })
    assert response.status_code == 200, response.text
    assert isinstance(requests[0]['format'], dict) and requests[1]['format'] == 'json'
    data = response.json()
    assert data['mode'] == 'sql_query'
    assert {source['title'].split()[0] for source in data['sources']} == {'EE101', 'EE301'}


def test_blank_model_url_is_reported_without_calling_model(client, monkeypatch, caplog):
    signin(client)
    use_local_model(monkeypatch)
    monkeypatch.setattr(settings, 'llama_base_url', '')

    async def unexpected(*args, **kwargs):
        pytest.fail('An unconfigured model must not be called')

    monkeypatch.setattr(chat.graph, 'ainvoke', unexpected)
    with caplog.at_level(logging.WARNING, logger='courses.agent'):
        response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'LLAMA_NOT_CONFIGURED'
    assert '尚未設定' in response.json()['message'] and 'LLAMA_NOT_CONFIGURED' in caplog.text


def test_http_transport_timeout_has_timeout_message(client, monkeypatch):
    signin(client)

    async def fail(*args, **kwargs):
        raise httpx.ReadTimeout('private request URL must not be exposed')

    monkeypatch.setattr(chat.graph, 'ainvoke', fail)
    response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'LLAMA_TIMEOUT'
    assert '逾時' in response.json()['message']


def test_local_model_request_returns_sourced_rules_with_server_evidence(client, monkeypatch, caplog):
    signin(client)
    use_local_model(monkeypatch)
    captured = []

    def generate(request):
        body = json.loads(request.content)
        captured.append(body)
        return ollama_reply(full_plan(tool='rules'))

    mock_ollama(monkeypatch, generate)
    with caplog.at_level(logging.INFO, logger='courses.agent'):
        response = client.post('/api/v1/chat', json={'message': '請問初選規則', 'term_id': MAIN})
    assert response.status_code == 200, response.text
    body = captured[0]
    assert body['model'] == 'llama3.2:3b' and body['stream'] is False and body['format'] == chat.PLAN_SCHEMA
    assert body['options']['temperature'] == 0 and body['options']['num_predict'] == 320
    assert body['messages'][0]['role'] == 'system' and 'query plan' in body['messages'][0]['content']
    assert body['messages'][1] == {'role': 'user', 'content': '請問初選規則'}
    data = response.json()
    assert '待分發' in data['answer'] and data['sources'][0]['url'] == '/rules'
    agent = data['agent']
    assert agent['graph'] == 'course_assistant' and agent['model'] == 'llama3.2:3b' and agent['intent'] == 'rules'
    assert agent['tools'] == ['llama.plan_query']
    assert agent['llm']['server_model'] == 'llama3.2:3b' and agent['llm']['prompt_tokens'] == 1240
    assert agent['llm']['total_ms'] == 812
    logged = next(json.loads(r.getMessage()) for r in caplog.records if '"graph": "course_assistant"' in r.getMessage())
    assert logged['llm']['output_tokens'] == 60 and '初選規則' not in json.dumps(logged, ensure_ascii=False)


@pytest.mark.parametrize('content', [
    '{"tool":"rules"',               # truncated JSON
    '["rules"]',                     # not an object
    '{"tool":"drop_everything"}',    # not a known tool
])
def test_invalid_model_plan_is_not_executed(client, monkeypatch, content):
    signin(client)
    use_local_model(monkeypatch)
    mock_ollama(monkeypatch, lambda request: httpx.Response(200, json={
        'model': 'llama3.2:3b', 'message': {'role': 'assistant', 'content': content}}))
    response = client.post('/api/v1/chat', json={'message': '請問初選規則', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'LLAMA_INVALID_OUTPUT'
    assert 'answer' not in response.json()


def test_fields_the_student_did_not_say_are_dropped():
    # Llama 3.2 3B often fills in a school, a department or a day of its own.
    plan = chat.ground_plan(full_plan(tool='courses', keyword='資料結構', department='資訊工程學系碩士班',
                                      school_code='NTU', weekday=4, has_seats=True, sort='remaining'),
                            '資料結構還有名額嗎')
    assert (plan.keyword, plan.department, plan.school_code, plan.weekday) == ('資料結構', None, None, None)
    assert plan.has_seats is True and plan.sort == 'code' and plan.mode == 'sql_query'
    plan = chat.ground_plan(full_plan(tool='courses', keyword='臺大電機所', department='電機所', school_code='NTU'),
                            '有哪些台大電機所的課')
    assert (plan.keyword, plan.department, plan.school_code) == (None, '電機所', 'NTU')
    # The department went into the keyword slot and the model invented another one.
    plan = chat.ground_plan(full_plan(tool='courses', keyword='電機所', department='電子系', school_code='NTU'),
                            '有哪些台大電機所的課')
    assert (plan.keyword, plan.department, plan.school_code) == (None, '電機所', 'NTU')
    plan = chat.ground_plan(full_plan(tool='recommend', topic='影像處理', target_count=2, track_code='ml_vision',
                                      available_times=[{'weekday': 4, 'start_time': '00:00', 'end_time': '23:59'}]),
                            '我的研究方向是影像處理，推薦3門課')
    assert plan.topic == '影像處理' and plan.track_code == 'ml_vision' and plan.target_count == 3
    assert plan.available_times == []
    plan = chat.ground_plan(full_plan(tool='recommend', topic='Edge AI', target_count=2, track_code='edge_ai',
                                      available_times=[{'weekday': 3, 'start_time': '08:00', 'end_time': '12:00'}]),
                            'My research topic is Edge AI, 2 courses only, Wednesday')
    assert plan.target_count == 2 and [(w.weekday, str(w.start_time), str(w.end_time)) for w in plan.available_times] == [
        (3, '00:00:00', '23:59:00')]
    plan = chat.ground_plan(full_plan(tool='recommend', topic='機器學習', target_count=2,
                                      available_times=[{'weekday': 2, 'start_time': '13:00', 'end_time': '17:00'}]),
                            '推薦兩門週二下午的機器學習相關課程')
    assert plan.target_count == 2 and [(w.weekday, str(w.start_time), str(w.end_time)) for w in plan.available_times] == [
        (2, '13:00:00', '18:00:00')]
    plan = chat.ground_plan(full_plan(tool='recommend', target_count=1, available_times=[
        {'weekday': 2, 'start_time': '15:00', 'end_time': '18:00'}, {'weekday': 2, 'start_time': '09:00', 'end_time': '12:00'}]),
        '只選週二 15:00 至 18:00 的 1 門課')
    assert plan.target_count == 1 and [(str(w.start_time), str(w.end_time)) for w in plan.available_times] == [
        ('15:00:00', '18:00:00')]


def test_wrongly_typed_model_fields_are_ignored_not_fatal():
    # OpenAI-compatible servers do not enforce the JSON schema.
    plan = chat.ground_plan({'tool': 'recommend', 'weekday': [2], 'target_count': {'n': 2}, 'track_code': ['llm'],
                             'sort': 7, 'keyword': 42, 'topic': ['x'], 'has_seats': 'yes',
                             'available_times': [{'weekday': [2], 'start_time': 9, 'end_time': None}, 'Tuesday']},
                            '推薦兩門週二的課')
    assert (plan.weekday, plan.target_count, plan.track_code, plan.sort) == (2, 3, None, 'code')
    assert (plan.keyword, plan.topic, plan.has_seats, plan.available_times) == (None, None, False, [])
    assert chat.ground_plan({'tool': 'courses', 'weekday': True}, '有什麼課').weekday is None
    with pytest.raises(chat.llama.LlamaError):
        chat.ground_plan({'tool': ['rules']}, '你好')


def test_english_research_question_uses_the_roadmap_planner(client, monkeypatch):
    signin(client)
    use_local_model(monkeypatch)
    seen = []

    async def plan(message, *, system_instruction, meta):
        seen.append(system_instruction)
        meta.update({'server_model': 'llama3.2:3b', 'prompt_tokens': 1250, 'output_tokens': 70, 'total_ms': 790})
        return full_plan(tool='recommend', topic='Edge AI', track_code='edge_ai', target_count=2,
                         available_times=[{'weekday': 3, 'start_time': '08:00', 'end_time': '12:00'}])

    monkeypatch.setattr(chat, 'request_plan', plan)
    response = client.post('/api/v1/chat', json={
        'message': 'My research topic is Edge AI, 2 courses only, Wednesday', 'term_id': MAIN})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mode'] == 'recommendation' and data['recommendation']['roadmap']['track_code'] == 'edge_ai'
    assert data['agent']['graph'] == 'course_assistant' and data['agent']['tools'][0] == 'llama.plan_query'
    assert '"selected_track_code": "edge_ai"' in seen[0]


def test_verification_saves_record_outside_readonly_app_directory(monkeypatch, tmp_path):
    from app import verify_assistant
    use_local_model(monkeypatch)
    seed(recommendation_demo=True)
    app_dir = tmp_path / 'app'
    app_dir.mkdir(mode=0o555)
    monkeypatch.chdir(app_dir)
    monkeypatch.setattr(verify_assistant.tempfile, 'gettempdir', lambda: str(tmp_path))

    async def plan(message, *, system_instruction, meta):
        meta.update({'server_model': 'llama3.2:3b', 'prompt_tokens': 1240, 'output_tokens': 60, 'total_ms': 800})
        if '電子系' in message:
            return full_plan(tool='courses', department='電子系', weekday=2, has_seats=True)
        return full_plan(tool='recommend', topic='大語言模型', track_code='llm', target_count=3)

    monkeypatch.setattr(chat, 'request_plan', plan)
    try:
        verify_assistant.main()
        record = json.loads((tmp_path / 'assistant-verification.json').read_text())
        assert record['status'] == 'executed' and record['response']['mode'] == 'sql_query'
        assert record['query_agent']['llm']['server_model'] == 'llama3.2:3b'
        assert record['recommendation']['mode'] == 'recommendation' and record['recommendation']['track_code'] == 'llm'
        assert {'plan', 'baseline'}.isdisjoint(record['recommendation'])
    finally:
        app_dir.chmod(0o755)


def test_verification_rejects_rules_answer_for_course_query(monkeypatch, tmp_path):
    from app import verify_assistant
    seed(recommendation_demo=True)
    use_local_model(monkeypatch)
    monkeypatch.setattr(verify_assistant.tempfile, 'gettempdir', lambda: str(tmp_path))

    async def rules(message, *, system_instruction, meta):
        meta.update({'server_model': 'llama3.2:3b'})
        return full_plan(tool='rules')

    monkeypatch.setattr(chat, 'request_plan', rules)
    with pytest.raises(verify_assistant.VerificationError) as error:
        verify_assistant.main()
    assert error.value.code == 'VERIFY_COURSE_QUERY_MISMATCH'
    assert not (tmp_path / 'assistant-verification.json').exists()
