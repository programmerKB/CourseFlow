"""Run inside the API container: python < scripts/diagnose-gemini.py.

Send only fixed public text to Google's endpoint, bypassing the Gemini SDK.
Print the provider's message after removing the configured credential.
"""
import json
import os
import re
from urllib.parse import quote

import httpx

from app.config import settings


def redact_message(message, key):
    if not isinstance(message, str):
        return '[no provider message]'
    # Redact before truncation so long credentials cannot leave a partial value.
    variants = {key, key.strip()}
    variants.update(quote(value, safe='') for value in tuple(variants))
    variants.update(json.dumps(value)[1:-1] for value in tuple(variants))
    for value in sorted(variants, key=len, reverse=True):
        if value:
            message = message.replace(value, '[REDACTED]')
    message = re.sub(r'AIza[A-Za-z0-9_-]{20,}', '[REDACTED]', message)
    message = re.sub(r'AQ\.[A-Za-z0-9._-]{20,}', '[REDACTED]', message)
    message = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', message)
    return message[:2000]


def main():
    key = settings.gemini_api_key
    model = settings.gemini_model.removeprefix('models/')
    result = {'transport': 'direct_rest',
              'sdk_base_url_override_set': bool(os.environ.get('GOOGLE_GEMINI_BASE_URL'))}
    if not key.strip():
        print(json.dumps({**result, 'status': 'not_configured', 'hint': 'GEMINI_API_KEY 未設定。'}, ensure_ascii=False))
        return 1
    if not key.strip().isascii() or re.search(r'[\x00-\x20\x7f]', key.strip()):
        print(json.dumps({**result, 'status': 'invalid_key_format',
                          'hint': '金鑰包含空白、控制字元或非 ASCII 字元；請重新複製金鑰。'}, ensure_ascii=False))
        return 1
    if not re.fullmatch(r'gemini-[A-Za-z0-9._-]{1,80}', model):
        print(json.dumps({**result, 'status': 'invalid_model', 'hint': '請檢查 GEMINI_MODEL 名稱及空白。'}, ensure_ascii=False))
        return 1
    result.update(model=model, key_type=('authorization' if key.strip().startswith('AQ.') else
                                        'standard' if key.strip().startswith('AIza') else 'other'))
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                headers={'x-goog-api-key': key.strip()},
                json={'contents': [{'role': 'user', 'parts': [{'text': 'Reply with OK.'}]}]})
    except httpx.HTTPError:
        print(json.dumps({**result, 'status': 'transport_failed',
                          'hint': '無法取得 HTTP 回應；請檢查容器連外網路、代理與 TLS。'}, ensure_ascii=False))
        return 1
    result.update(status='ok' if response.is_success else 'failed', http_status=response.status_code)
    if not response.is_success:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, list) and len(body) == 1:
            body = body[0]
        error = body.get('error', body) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            error = {}
        result['provider_message'] = redact_message(error.get('message'), key)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if response.is_success else 1


if __name__ == '__main__':
    raise SystemExit(main())
