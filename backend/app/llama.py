"""Client for the local Llama 3.2 3B model used by both assistants.

The English registration agent (FR-22) reads intents with it, and the Chinese assistant turns
free-form questions into query plans with it. No cloud language model is used.

Two wire formats are supported so the same deployment can use a local Ollama service or a
remote OpenAI-compatible server (vLLM, llama.cpp server, hosted Llama 3.2 3B Instruct):

* ``ollama``: ``POST {LLAMA_BASE_URL}/api/chat`` with a JSON-schema ``format``.
* ``openai``: ``POST {LLAMA_BASE_URL}/v1/chat/completions`` with a JSON response format.

Only the student's message and fixed instructions are sent. Errors are reduced to fixed
reason codes so provider responses never reach logs or students.
"""
import json
import re

import httpx

from .config import settings

# Ollama's tag for Llama 3.2 3B Instruct is llama3.2:3b; hosted names look like
# meta-llama/Llama-3.2-3B-Instruct. Both satisfy the course specification.
COMPLIANT_MODEL = re.compile(r'llama[-_ .]?3\.2.*\b3b\b', re.I)


class LlamaError(Exception):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def model_is_compliant(name=None):
    return bool(COMPLIANT_MODEL.search(name or settings.llama_model))


def endpoint():
    base = settings.llama_base_url
    if not base:
        raise LlamaError('LLAMA_NOT_CONFIGURED')
    return base + ('/api/chat' if settings.llama_api_style == 'ollama' else '/v1/chat/completions')


def request_body(system, message, schema, max_tokens=160):
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': message}]
    if settings.llama_api_style == 'ollama':
        return {'model': settings.llama_model, 'messages': messages, 'stream': False, 'format': schema,
                'keep_alive': '30m', 'options': {'temperature': 0, 'num_predict': max_tokens}}
    return {'model': settings.llama_model, 'messages': messages, 'temperature': 0, 'max_tokens': max_tokens,
            'response_format': {'type': 'json_object'}}


def response_text(data):
    try:
        if settings.llama_api_style == 'ollama':
            return data['message']['content']
        return data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        raise LlamaError('LLAMA_INVALID_OUTPUT') from None


def auth_headers():
    headers = {'Accept': 'application/json'}
    key = settings.llama_api_key.get_secret_value().strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    return headers


def server_evidence(data):
    """Values only the model server can produce: its model name, token counts and timings."""
    if not isinstance(data, dict):
        return {}
    if settings.llama_api_style == 'ollama':
        ms = lambda key: round(data[key] / 1e6) if isinstance(data.get(key), (int, float)) else None  # noqa: E731
        evidence = {'server_model': data.get('model'), 'prompt_tokens': data.get('prompt_eval_count'),
                    'output_tokens': data.get('eval_count'), 'total_ms': ms('total_duration'),
                    'load_ms': ms('load_duration'), 'created_at': data.get('created_at')}
    else:
        usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
        evidence = {'server_model': data.get('model'), 'prompt_tokens': usage.get('prompt_tokens'),
                    'output_tokens': usage.get('completion_tokens'), 'response_id': data.get('id')}
    return {key: value for key, value in evidence.items() if value is not None}


async def chat_json(system, message, schema, *, transport=None, meta=None, max_tokens=160):
    """Return the model's JSON object. Raise LlamaError with a fixed reason code.

    When ``meta`` is a dict it receives the server's own evidence of the inference.
    """
    url = endpoint()
    body = request_body(system, message, schema, max_tokens)
    try:
        async with httpx.AsyncClient(timeout=settings.llama_timeout_seconds, transport=transport) as client:
            response = await client.post(url, json=body, headers=auth_headers())
            if (response.status_code in (400, 500) and settings.llama_api_style == 'ollama'
                    and isinstance(body.get('format'), dict)):
                # Ollama builds before 0.5 reject JSON-schema formats; JSON mode still returns an
                # object, and the agent validates it against the same schema afterwards.
                response = await client.post(url, json={**body, 'format': 'json'}, headers=auth_headers())
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Wrong address, a firewall, or a server that only listens on 127.0.0.1.
        raise LlamaError('LLAMA_UNREACHABLE') from None
    except httpx.TimeoutException:
        raise LlamaError('LLAMA_TIMEOUT') from None
    except httpx.HTTPError:
        raise LlamaError('LLAMA_UNAVAILABLE') from None
    if response.status_code == 404:
        raise LlamaError('LLAMA_MODEL_UNAVAILABLE')
    if response.status_code in (401, 403):
        raise LlamaError('LLAMA_AUTH_FAILED')
    if response.status_code == 429:
        raise LlamaError('LLAMA_RATE_LIMITED')
    if response.status_code >= 400:
        raise LlamaError('LLAMA_UNAVAILABLE')
    try:
        data = response.json()
        text = response_text(data)
        value = json.loads(text)
    except (ValueError, TypeError):
        raise LlamaError('LLAMA_INVALID_OUTPUT') from None
    if not isinstance(value, dict):
        raise LlamaError('LLAMA_INVALID_OUTPUT')
    if meta is not None:
        meta.update(server_evidence(data))
    return value


def version_tuple(value):
    return tuple(int(part) for part in re.findall(r'\d+', value or '')[:3])


async def server_status(*, transport=None):
    """Check that the model server answers and has the configured model, without running inference."""
    status = {'endpoint': settings.llama_base_url or None, 'api_style': settings.llama_api_style,
              'model': settings.llama_model, 'model_is_llama_3_2_3b': model_is_compliant()}
    if not settings.llama_base_url:
        return {**status, 'ok': False, 'reason_code': 'LLAMA_NOT_CONFIGURED'}
    base = settings.llama_base_url
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), transport=transport) as client:
            if settings.llama_api_style == 'ollama':
                version = (await client.get(base + '/api/version')).json().get('version')
                response = await client.get(base + '/api/tags')
                names = [row['name'] for row in response.json().get('models', [])]
                status['server_version'] = version
            else:
                response = await client.get(base + '/v1/models', headers=auth_headers())
                if response.status_code in (401, 403):
                    return {**status, 'ok': False, 'reason_code': 'LLAMA_AUTH_FAILED'}
                names = [row['id'] for row in response.json().get('data', [])]
    except httpx.ConnectTimeout:
        return {**status, 'ok': False, 'reason_code': 'LLAMA_CONNECT_TIMEOUT'}
    except httpx.ConnectError:
        return {**status, 'ok': False, 'reason_code': 'LLAMA_CONNECTION_REFUSED'}
    except httpx.TimeoutException:
        return {**status, 'ok': False, 'reason_code': 'LLAMA_TIMEOUT'}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return {**status, 'ok': False, 'reason_code': 'LLAMA_NOT_OLLAMA'}
    status['installed_models'] = names[:20]
    if settings.llama_model not in names:
        return {**status, 'ok': False, 'reason_code': 'LLAMA_MODEL_MISSING'}
    if settings.llama_api_style == 'ollama' and version_tuple(status.get('server_version')) < (0, 5, 0):
        status['warning'] = 'OLLAMA_BEFORE_0_5_USES_JSON_MODE'
    return {**status, 'ok': True}
