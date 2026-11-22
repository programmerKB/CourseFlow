"""The standalone probe exposes useful errors without credential or content leaks."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'diagnose-gemini.py'
    if not path.is_file():
        pytest.skip('The host-side probe is outside the backend Docker build context')
    spec = importlib.util.spec_from_file_location('gemini_rest_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('variant', ['raw', 'stripped', 'url', 'json'])
def test_redaction_removes_raw_stripped_encoded_and_escaped_credentials(probe, variant):
    key = ' \tAQ.fake-credential/with+symbols?"and\\unicode雪\n '
    representations = {
        'raw': key,
        'stripped': key.strip(),
        'url': quote(key, safe=''),
        'json': json.dumps(key)[1:-1],
    }
    value = representations[variant]
    message = probe.redact_message(f'Google rejected {value}; inspect the project.', key)
    assert message == 'Google rejected [REDACTED]; inspect the project.'
    assert value not in message and key.strip() not in message


def test_redaction_happens_before_truncation_and_bounds_provider_message(probe):
    key = 'private-credential-' + 'x' * 2200
    assert probe.redact_message(f'Failed for {key}; request rejected.', key) == (
        'Failed for [REDACTED]; request rejected.')
    assert len(probe.redact_message('a' * 3000, key)) == 2000


def test_redaction_also_removes_other_standard_and_authorization_keys(probe):
    message = probe.redact_message('AIza' + 'a' * 35 + ' and AQ.' + 'b' * 40, 'configured-fake-key')
    assert message == '[REDACTED] and [REDACTED]'
    assert probe.redact_message({'message': 'malformed value'}, 'configured-fake-key') == '[no provider message]'


def mock_transport(probe, monkeypatch, handler):
    original_client = httpx.Client

    def client(**kwargs):
        assert kwargs['timeout'] == 10.0
        assert kwargs['follow_redirects'] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(probe.httpx, 'Client', client)


def configure(probe, monkeypatch):
    monkeypatch.setattr(probe.settings, 'gemini_api_key', 'AQ.fake-key-only-never-network-1234567890')
    monkeypatch.setattr(probe.settings, 'gemini_model', 'models/gemini-3.5-flash-lite')


def test_success_uses_pinned_rest_endpoint_and_synthetic_body_without_echoing_response(probe, monkeypatch, capsys):
    configure(probe, monkeypatch)
    monkeypatch.setenv('GOOGLE_GEMINI_BASE_URL', 'https://private.invalid/not-the-google-endpoint')
    monkeypatch.setenv('GOOGLE_API_KEY', 'another-fake-key-that-must-not-be-used')
    captured = []
    response_secret = 'model-response-content-must-not-be-printed'

    def respond(request):
        captured.append(request)
        assert str(request.url) == (
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent')
        assert request.headers['x-goog-api-key'] == probe.settings.gemini_api_key
        assert 'authorization' not in request.headers
        assert json.loads(request.content) == {
            'contents': [{'role': 'user', 'parts': [{'text': 'Reply with OK.'}]}],
        }
        return httpx.Response(200, json={'candidates': [{'content': {
            'parts': [{'text': response_secret + probe.settings.gemini_api_key}],
        }}]})

    mock_transport(probe, monkeypatch, respond)
    assert probe.main() == 0
    output = capsys.readouterr()
    diagnostic = json.loads(output.out)
    assert len(captured) == 1
    assert diagnostic['transport'] == 'direct_rest'
    assert diagnostic['status'] == 'ok' and diagnostic['http_status'] == 200
    assert diagnostic['sdk_base_url_override_set'] is True
    assert diagnostic['model'] == 'gemini-3.5-flash-lite'
    assert diagnostic['key_type'] == 'authorization'
    assert 'provider_message' not in diagnostic
    assert response_secret not in output.out and probe.settings.gemini_api_key not in output.out
    assert not output.err


@pytest.mark.parametrize('envelope', ['nested', 'flat', 'array'])
def test_unknown_http400_surfaces_redacted_message_without_dumping_details(probe, monkeypatch, capsys, envelope):
    configure(probe, monkeypatch)
    metadata_secret = 'private-project-metadata-must-not-be-dumped'

    def respond(request):
        error = {
            'status': 'INVALID_ARGUMENT',
            'message': f'Unrecognized project prerequisite for {probe.settings.gemini_api_key}.',
            'details': [{'metadata': metadata_secret}],
        }
        body = error if envelope == 'flat' else [{'error': error}] if envelope == 'array' else {'error': error}
        return httpx.Response(400, json=body)

    mock_transport(probe, monkeypatch, respond)
    assert probe.main() == 1
    output = capsys.readouterr()
    diagnostic = json.loads(output.out)
    assert diagnostic['http_status'] == 400 and diagnostic['status'] == 'failed'
    assert diagnostic['provider_message'] == 'Unrecognized project prerequisite for [REDACTED].'
    assert metadata_secret not in output.out and probe.settings.gemini_api_key not in output.out
    assert not output.err


def test_transport_exception_does_not_disclose_illegal_header_value(probe, monkeypatch, capsys):
    configure(probe, monkeypatch)

    def fail(request):
        raise httpx.LocalProtocolError(f'Illegal header value b"{probe.settings.gemini_api_key}"')

    mock_transport(probe, monkeypatch, fail)
    assert probe.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['status'] == 'transport_failed'
    assert probe.settings.gemini_api_key not in output.out
    assert 'Illegal header' not in output.out and 'Traceback' not in output.out
    assert not output.err


@pytest.mark.parametrize('body', ['not-json-with-private-provider-payload', '[]', 'null', '"private-provider-text"',
                                 '{"error": "private-error-value"}', '{"error":{"message":["private-value"]}}'])
def test_malformed_or_nonobject_error_body_is_not_echoed_or_raised(probe, monkeypatch, capsys, body):
    configure(probe, monkeypatch)
    mock_transport(probe, monkeypatch, lambda request: httpx.Response(400, text=body))
    assert probe.main() == 1
    output = capsys.readouterr()
    diagnostic = json.loads(output.out)
    assert diagnostic['provider_message'] == '[no provider message]'
    assert 'private-' not in output.out and 'Traceback' not in output.out
    assert not output.err


@pytest.mark.parametrize(('key', 'model', 'status'), [
    ('', 'gemini-3.5-flash-lite', 'not_configured'),
    (' \t\n ', 'gemini-3.5-flash-lite', 'not_configured'),
    ('fake-key\nprivate-secret', 'gemini-3.5-flash-lite', 'invalid_key_format'),
    ('fake-key private-secret', 'gemini-3.5-flash-lite', 'invalid_key_format'),
    ('fake-key雪private-secret', 'gemini-3.5-flash-lite', 'invalid_key_format'),
    ('fake-offline-key', '', 'invalid_model'),
    ('fake-offline-key', ' ', 'invalid_model'),
    ('fake-offline-key', 'gemini-3.5-flash-lite?key=private-secret', 'invalid_model'),
    ('fake-offline-key', 'https://private.invalid/models/gemini-3.5-flash-lite', 'invalid_model'),
])
def test_blank_key_or_invalid_model_returns_before_creating_http_client(probe, monkeypatch, capsys, key, model, status):
    monkeypatch.setattr(probe.settings, 'gemini_api_key', key)
    monkeypatch.setattr(probe.settings, 'gemini_model', model)

    def forbidden_client(**kwargs):
        pytest.fail('Invalid configuration must not create an HTTP client')

    monkeypatch.setattr(probe.httpx, 'Client', forbidden_client)
    assert probe.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['status'] == status
    assert 'private' not in output.out and 'Traceback' not in output.out
    assert not output.err
