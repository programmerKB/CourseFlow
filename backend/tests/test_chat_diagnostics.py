"""Assistant failures stay actionable without exposing provider response text."""
import logging
import json

import httpx
import pytest

from app import chat
from app.config import settings
from app.seed import uid
from conftest import signin

MAIN = uid('term/main')


@pytest.mark.parametrize(('status', 'reason'), [
    (400, 'GEMINI_REQUEST_REJECTED'),
    (401, 'GEMINI_AUTH_FAILED'),
    (403, 'GEMINI_AUTH_FAILED'),
    (404, 'GEMINI_MODEL_UNAVAILABLE'),
    (429, 'GEMINI_QUOTA_EXCEEDED'),
    (500, 'CHAT_FAILED'),
    (504, 'GEMINI_TIMEOUT'),
])
def test_provider_failure_has_safe_diagnostic(client, monkeypatch, caplog, status, reason):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-never-log')
    secret = 'provider-payload-never-log'

    async def fail(*args, **kwargs):
        return httpx.Response(status, json={'error': {'message': secret}})

    monkeypatch.setattr(httpx.AsyncClient, 'post', fail)
    with caplog.at_level(logging.WARNING, logger='courses.agent'):
        response = client.post('/api/v1/chat', json={'message': '你好', 'term_id': MAIN})
    data = response.json()
    assert response.status_code == 503 and data['code'] == 'CHAT_UNAVAILABLE'
    assert data['details']['reason_code'] == reason and len(data['details']['sources']) == 2
    assert reason in caplog.text
    assert secret not in response.text + caplog.text
    assert settings.gemini_api_key not in response.text + caplog.text
    assert client.get(f'/api/v1/terms/{MAIN}/sections').status_code == 200


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


def test_rest_structured_request_returns_sourced_rules(client, monkeypatch):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    monkeypatch.setattr(settings, 'gemini_model', 'gemini-3.5-flash-lite')
    captured = []

    def generate(request):
        captured.append(request)
        assert str(request.url) == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'
        assert request.headers['x-goog-api-key'] == settings.gemini_api_key
        assert 'x-server-timeout' not in request.headers and 'authorization' not in request.headers
        body = json.loads(request.content)
        assert body['contents'] == [{'role': 'user', 'parts': [{'text': '請問初選規則'}]}]
        assert '唯讀選課助理' in body['systemInstruction']['parts'][0]['text']
        assert body['generationConfig'] == {
            'responseMimeType': 'application/json', 'responseJsonSchema': chat.Plan.model_json_schema(),
        }
        return httpx.Response(200, json={'candidates': [{
            'finishReason': 'STOP', 'content': {'parts': [
                {'text': 'private thought', 'thought': True}, {'text': '{"tool":'}, {'text': '"rules"}'},
            ]},
        }]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(generate), **kw))
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

    async def respond(*args, **kwargs):
        return httpx.Response(200, json=provider_response)

    monkeypatch.setattr(httpx.AsyncClient, 'post', respond)
    response = client.post('/api/v1/chat', json={'message': '請問初選規則', 'term_id': MAIN})
    assert response.status_code == 503
    assert response.json()['details']['reason_code'] == 'CHAT_FAILED'
    assert 'answer' not in response.json()


def test_verification_can_save_outside_readonly_app_directory(monkeypatch, tmp_path):
    from app import verify_gemini
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    app_dir = tmp_path / 'app'
    app_dir.mkdir(mode=0o555)
    monkeypatch.chdir(app_dir)
    monkeypatch.setattr(verify_gemini.tempfile, 'gettempdir', lambda: str(tmp_path))

    async def result(*args):
        return {'mode': 'sql_query', 'answer': '公開課程結果', 'sources': []}

    monkeypatch.setattr(verify_gemini, 'handle_chat', result)
    try:
        verify_gemini.main()
        record = json.loads((tmp_path / 'gemini-verification.json').read_text())
        assert record['status'] == 'executed' and record['response']['mode'] == 'sql_query'
        assert 'test-key-no-network' not in json.dumps(record)
    finally:
        app_dir.chmod(0o755)
