"""Operator troubleshooting must remain useful without exposing provider payloads."""
import asyncio
import json

import httpx
import pytest

from app import chat, verify_gemini
from app.config import settings
from app.errors import DomainError
from app.gemini_diagnostics import safe_provider_diagnostic
from app.gemini_rest import GeminiHTTPError


SECRET = 'private-provider-payload-never-disclose'


def provider_error(*, status='INVALID_ARGUMENT', reason='API_KEY_INVALID', message=SECRET, details=None):
    return GeminiHTTPError(400, {'error': {
        'status': status, 'message': message,
        'details': details if details is not None else [{
            '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
            'reason': reason, 'domain': SECRET, 'metadata': {'api_key': SECRET},
        }],
    }})


def raise_wrapped(error):
    try:
        raise error
    except GeminiHTTPError as cause:
        raise RuntimeError(SECRET) from cause


def test_diagnostic_recovers_http400_key_failure_through_suppressed_domain_context(monkeypatch, caplog):
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')

    async def fail(*args, **kwargs):
        raise_wrapped(provider_error())

    monkeypatch.setattr(chat.graph, 'ainvoke', fail)
    with pytest.raises(DomainError) as caught:
        asyncio.run(chat.handle_chat(None, {}, '公開選課規則', None, 'operator-diagnostic-test'))

    error = caught.value
    assert error.__cause__ is None and error.__suppress_context__
    assert isinstance(error.__context__, RuntimeError)
    diagnostic = safe_provider_diagnostic(error)
    assert diagnostic['http_status'] == 400
    assert diagnostic['provider_status'] == 'INVALID_ARGUMENT'
    assert diagnostic['provider_reasons'] == ['API_KEY_INVALID']
    assert '金鑰無效' in diagnostic['hint']
    assert SECRET not in json.dumps(diagnostic, ensure_ascii=False) + caplog.text


@pytest.mark.parametrize('details', [
    [{'reason': SECRET, 'fieldViolations': [{'field': f'contents["{SECRET}"]', 'description': SECRET}]}],
    {'reason': SECRET},
    [SECRET, None, {'reason': {'secret': SECRET}, 'fieldViolations': [SECRET, None]}],
])
def test_unknown_provider_status_reason_fields_and_metadata_are_never_returned(details):
    diagnostic = safe_provider_diagnostic(provider_error(status=SECRET, reason=SECRET, details=details))
    assert diagnostic['http_status'] == 400
    assert 'provider_status' not in diagnostic
    assert 'provider_reasons' not in diagnostic
    assert 'request_fields' not in diagnostic
    assert SECRET not in json.dumps(diagnostic, ensure_ascii=False)


def test_request_field_diagnostics_return_only_canonical_names_without_values():
    diagnostic = safe_provider_diagnostic(provider_error(reason=SECRET,
        message=f'candidateCount={SECRET}; temperature={SECRET}; topP={SECRET}; topK={SECRET}',
        details=[{'fieldViolations': [
            {'field': f'generationConfig.responseJsonSchema.properties["{SECRET}"]', 'description': SECRET},
            {'field': f'generation_config.thinking_config[{SECRET}]'},
        ]}]))
    assert diagnostic['request_fields'] == [
        'candidate_count', 'response_json_schema', 'temperature', 'thinking_config', 'top_k', 'top_p',
    ]
    assert 'provider_reasons' not in diagnostic
    assert SECRET not in json.dumps(diagnostic, ensure_ascii=False)


@pytest.mark.parametrize(('message', 'reason'), [
    (f'API key not valid. {SECRET}', 'API_KEY_INVALID'),
    (f'API key has expired. {SECRET}', 'API_KEY_EXPIRED'),
    (f'Your API key was reported as leaked. {SECRET}', 'API_KEY_LEAKED'),
    (f'User location is not supported. {SECRET}', 'USER_LOCATION_UNSUPPORTED'),
])
def test_fixed_provider_message_signatures_are_classified_without_disclosing_text(message, reason):
    diagnostic = safe_provider_diagnostic(provider_error(reason=SECRET, message=message))
    assert diagnostic['provider_reasons'] == [reason]
    assert SECRET not in json.dumps(diagnostic, ensure_ascii=False)


def test_cli_failure_emits_safe_json_and_exit1_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    monkeypatch.setattr(settings, 'gemini_model', 'gemini-3.5-flash-lite')

    def fail(connection_only=False):
        try:
            raise_wrapped(provider_error())
        except Exception:
            raise chat.unavailable(None, 'GEMINI_REQUEST_REJECTED') from None

    monkeypatch.setattr(verify_gemini, 'main', fail)
    with pytest.raises(SystemExit) as caught:
        verify_gemini.cli([])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert not output.out
    diagnostic = json.loads(output.err)
    assert diagnostic['stage'] == 'course_query'
    assert diagnostic['model'] == 'gemini-3.5-flash-lite'
    assert diagnostic['reason_code'] == 'GEMINI_REQUEST_REJECTED'
    assert diagnostic['provider_reasons'] == ['API_KEY_INVALID']
    assert 'Traceback' not in output.err
    assert SECRET not in output.err and settings.gemini_api_key not in output.err


def test_cli_does_not_echo_malformed_model_or_unrelated_exception(monkeypatch, capsys):
    monkeypatch.setattr(settings, 'gemini_model', f'https://private.invalid/{SECRET}')

    def fail(connection_only=False):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(verify_gemini, 'main', fail)
    with pytest.raises(SystemExit) as caught:
        verify_gemini.cli(['--connection-only'])
    assert caught.value.code == 1
    output = capsys.readouterr()
    diagnostic = json.loads(output.err)
    assert diagnostic['stage'] == 'connection'
    assert diagnostic['model'] == '[invalid model name]'
    assert diagnostic['reason_code'] == 'CHAT_FAILED'
    assert SECRET not in output.err and 'Traceback' not in output.err


def test_connection_only_serializes_minimal_bounded_request_without_database_or_artifact(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key-no-network')
    monkeypatch.setattr(settings, 'gemini_model', 'gemini-3.5-flash-lite')
    monkeypatch.setattr(verify_gemini.tempfile, 'gettempdir', lambda: str(tmp_path))
    artifact = tmp_path / 'gemini-verification.json'
    artifact.write_text('existing full verification must remain intact\n')

    def no_database():
        pytest.fail('The connection check must not access the database')

    monkeypatch.setattr(verify_gemini, 'SessionLocal', no_database)
    captured = []

    def request(req):
        body = json.loads(req.content)
        captured.append(body)
        assert req.method == 'POST'
        assert str(req.url) == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'
        assert req.headers['x-goog-api-key'] == settings.gemini_api_key
        assert 'x-server-timeout' not in req.headers
        assert body == {'contents': [{'parts': [{'text': 'Reply with OK.'}], 'role': 'user'}]}
        assert settings.gemini_api_key not in json.dumps(body)
        return httpx.Response(200, json={'candidates': [{
            'content': {'role': 'model', 'parts': [{'text': 'OK'}]}, 'finishReason': 'STOP',
        }]})

    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs['timeout'] == 10.0
        assert kwargs['follow_redirects'] is False
        return real_client(transport=httpx.MockTransport(request), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    verify_gemini.cli(['--connection-only'])
    output = capsys.readouterr()
    assert len(captured) == 1
    assert 'connection succeeded' in output.out
    assert 'have not been verified' in output.out
    assert not output.err
    assert artifact.read_text() == 'existing full verification must remain intact\n'
    assert settings.gemini_api_key not in output.out
