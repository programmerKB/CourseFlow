"""Assistant failures stay actionable without exposing provider response text."""
import logging
import json

import httpx
import pytest

from app import chat
from app.config import settings
from app.seed import seed, uid
from conftest import signin

MAIN = uid('term/main')
INITIAL = uid('term/initial')
PROVIDER_SECRET = 'provider-payload-never-log'


def mock_gemini_http(monkeypatch, response_for):
    async def send(self, request, *args, **kwargs):
        assert request.url.host == 'generativelanguage.googleapis.com'
        response = response_for(request)
        response.request = request
        return response

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)


@pytest.mark.parametrize(('status', 'provider_status', 'reason', 'attempts'), [
    (400, 'INVALID_ARGUMENT', 'GEMINI_REQUEST_REJECTED', 1),
    (401, 'UNAUTHENTICATED', 'GEMINI_AUTH_FAILED', 1),
    (402, 'FAILED_PRECONDITION', 'GEMINI_PAYMENT_REQUIRED', 1),
    (403, 'PERMISSION_DENIED', 'GEMINI_AUTH_FAILED', 1),
    (404, 'NOT_FOUND', 'GEMINI_MODEL_UNAVAILABLE', 1),
    (418, PROVIDER_SECRET, 'CHAT_FAILED', 1),
    (429, 'RESOURCE_EXHAUSTED', 'GEMINI_QUOTA_EXCEEDED', 1),
    (500, 'INTERNAL', 'GEMINI_PROVIDER_UNAVAILABLE', 2),
    (502, None, 'GEMINI_PROVIDER_UNAVAILABLE', 2),
    (503, 'UNAVAILABLE', 'GEMINI_PROVIDER_UNAVAILABLE', 2),
    (504, 'DEADLINE_EXCEEDED', 'GEMINI_TIMEOUT', 1),
])
def test_provider_failure_has_safe_diagnostic(client, monkeypatch, caplog, status, provider_status, reason, attempts):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-never-log')
    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {
            'code': status, 'status': provider_status, 'message': PROVIDER_SECRET,
        }})

    mock_gemini_http(monkeypatch, fail)
    with caplog.at_level(logging.INFO):
        response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    data = response.json()
    assert len(requests) == attempts
    assert response.status_code == 503 and data['code'] == 'CHAT_UNAVAILABLE'
    assert data['details']['reason_code'] == reason and len(data['details']['sources']) == 2
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == 'courses.agent' and record.levelno == logging.WARNING]
    diagnostic = next(event for event in events if event.get('mode') == 'unavailable')
    assert diagnostic['request_id'] == data['request_id']
    assert diagnostic['reason_code'] == reason
    assert diagnostic['provider_http_status'] == status
    if provider_status and provider_status != PROVIDER_SECRET:
        assert diagnostic['provider_status'] == provider_status
    else:
        assert 'provider_status' not in diagnostic
    assert PROVIDER_SECRET not in response.text + caplog.text
    assert settings.gemini_api_key not in response.text + caplog.text
    assert client.get(f'/api/v1/terms/{MAIN}/sections').status_code == 200


@pytest.mark.parametrize(('message', 'api_key'), [
    ('找星期二還有空位的電子系課', 'test-key-no-network'),
    ('請幫我查詢這學期週二仍有名額的臺科大電子工程系的課程。', ''),
])
def test_weekday_seat_search_queries_catalog_without_gemini(client, monkeypatch, message, api_key):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', api_key)

    async def unexpected(*args, **kwargs):
        pytest.fail('A weekday seat search must use the public catalog directly')

    monkeypatch.setattr(chat, 'generate_content', unexpected)

    response = client.post('/api/v1/chat', json={'message': message, 'term_id': INITIAL})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mode'] == 'sql_query'
    assert {item['title'].split()[0] for item in data['sources']} == {'EE101', 'EE301'}
    assert {item['term_id'] for item in data['sources']} == {INITIAL}


def test_temporary_gemini_failure_retries_the_same_course_query(client, monkeypatch, caplog):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-never-log')
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={'error': {
                'code': 503, 'status': 'UNAVAILABLE', 'message': PROVIDER_SECRET,
            }})
        return httpx.Response(200, json={'candidates': [{
            'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [{'text': json.dumps({
                'mode': 'sql_query', 'tool': 'courses', 'weekday': 2, 'has_seats': True,
                'department': '電子', 'sort': 'remaining',
            })}]},
        }]})

    mock_gemini_http(monkeypatch, respond)
    with caplog.at_level(logging.INFO):
        response = client.post('/api/v1/chat', json={
            'message': '找星期二還有空位的電子系課，依剩餘名額排序', 'term_id': INITIAL,
        })
    assert response.status_code == 200, response.text
    assert len(requests) == 2 and requests[0].content == requests[1].content
    assert requests[0].url == requests[1].url
    data = response.json()
    assert data['mode'] == 'sql_query'
    assert {source['title'].split()[0] for source in data['sources']} == {'EE101', 'EE301'}
    assert all(source['term_id'] == INITIAL for source in data['sources'])
    assert PROVIDER_SECRET not in response.text + caplog.text
    assert settings.gemini_api_key not in response.text + caplog.text


def test_blank_key_is_reported_without_calling_model(client, monkeypatch, caplog):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', '  ')

    async def unexpected(*args, **kwargs):
        pytest.fail('An empty key must not trigger any model call')

    monkeypatch.setattr(chat.graph, 'ainvoke', unexpected)
    with caplog.at_level(logging.WARNING, logger='courses.agent'):
        response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'GEMINI_NOT_CONFIGURED'
    assert '尚未設定' in response.json()['message']
    assert 'GEMINI_NOT_CONFIGURED' in caplog.text


def test_http_transport_timeout_has_timeout_message(client, monkeypatch):
    signin(client)
    async def fail(*args, **kwargs):
        raise httpx.ReadTimeout('private request URL must not be exposed')
    monkeypatch.setattr(chat.graph, 'ainvoke', fail)
    response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'GEMINI_TIMEOUT'
    assert '逾時' in response.json()['message']


def test_langchain_structured_request_returns_sourced_rules(client, monkeypatch):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    monkeypatch.setattr(settings, 'gemini_model', 'gemini-3.5-flash-lite')
    monkeypatch.setenv('GOOGLE_GEMINI_BASE_URL', 'https://other.invalid')
    captured = []

    def generate(request):
        captured.append(request)
        assert str(request.url) == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'
        assert request.headers['x-goog-api-key'] == settings.gemini_api_key
        assert 'x-server-timeout' not in request.headers and 'authorization' not in request.headers
        body = json.loads(request.content)
        assert body['contents'] == [{'role': 'user', 'parts': [{'text': '請問初選規則'}]}]
        assert '結構化查詢計畫' in body['systemInstruction']['parts'][0]['text']
        generation = body['generationConfig']
        assert generation['responseMimeType'] == 'application/json'
        assert generation['responseJsonSchema']['properties']['tool'] == chat.Plan.model_json_schema()['properties']['tool']
        assert {'temperature', 'candidateCount', 'topP', 'topK'}.isdisjoint(generation)
        return httpx.Response(200, json={'candidates': [{
            'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [
                {'text': 'private thought', 'thought': True}, {'text': '{"tool":'}, {'text': '"rules"}'},
            ]},
        }]})

    mock_gemini_http(monkeypatch, generate)
    response = client.post('/api/v1/chat', json={'message': '請問初選規則', 'term_id': MAIN})
    assert response.status_code == 200, response.text
    assert captured and '待分發' in response.json()['answer']
    assert response.json()['sources'][0]['url'] == '/rules'


@pytest.mark.parametrize('provider_response', [
    {'promptFeedback': {'blockReason': 'SAFETY'}},
    {'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': '{"tool":"rules"}'}]}}]},
    {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"tool":"courses","limit":21}'}]}}]},
    {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"tool":"courses"'}]}}]},
])
def test_incomplete_or_invalid_model_plan_is_not_executed(client, monkeypatch, provider_response):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')

    def respond(request):
        return httpx.Response(200, json=provider_response)

    mock_gemini_http(monkeypatch, respond)
    response = client.post('/api/v1/chat', json={'message': '請問初選規則', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'CHAT_FAILED'
    assert 'answer' not in response.json()


def test_verification_can_save_outside_readonly_app_directory(monkeypatch, tmp_path):
    from app import verify_gemini
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    seed(recommendation_demo=True)
    app_dir = tmp_path / 'app'
    app_dir.mkdir(mode=0o555)
    monkeypatch.chdir(app_dir)
    monkeypatch.setattr(verify_gemini.tempfile, 'gettempdir', lambda: str(tmp_path))

    async def intent(message, **kwargs):
        return chat.stub_plan(message).model_dump_json()

    monkeypatch.setattr(chat, 'generate_content', intent)
    try:
        verify_gemini.main()
        record = json.loads((tmp_path / 'gemini-verification.json').read_text())
        assert record['status'] == 'executed' and record['response']['mode'] == 'sql_query'
        assert record['recommendation']['mode'] == 'recommendation'
        assert {item['course_code'] for item in record['recommendation']['candidates']} == {
            'DEMO-501', 'DEMO-502', 'DEMO-503', 'DEMO-504', 'DEMO-505',
        }
        assert {'plan', 'baseline'}.isdisjoint(record['recommendation'])
        assert 'test-key-no-network' not in json.dumps(record)
    finally:
        app_dir.chmod(0o755)


def test_verification_rejects_rules_answer_for_course_query(monkeypatch, tmp_path):
    from app import verify_gemini
    seed(recommendation_demo=True)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    monkeypatch.setattr(verify_gemini.tempfile, 'gettempdir', lambda: str(tmp_path))

    async def rules(*args, **kwargs):
        return '{"tool":"rules"}'

    monkeypatch.setattr(chat, 'generate_content', rules)
    with pytest.raises(verify_gemini.VerificationError) as error:
        verify_gemini.main()
    assert error.value.code == 'VERIFY_COURSE_QUERY_MISMATCH'
    assert not (tmp_path / 'gemini-verification.json').exists()
