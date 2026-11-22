"""Gemini Developer API transport for the assistant and its verification command."""
import asyncio
import re

import httpx

from .config import settings


class GeminiHTTPError(Exception):
    def __init__(self, status_code: int, payload: object):
        self.code = status_code
        if isinstance(payload, list) and len(payload) == 1:
            payload = payload[0]
        body = payload.get('error', payload) if isinstance(payload, dict) else {}
        self.details = body if isinstance(body, dict) else {}
        self.status = self.details.get('status')
        self.message = self.details.get('message')
        super().__init__(f'Gemini API returned HTTP {status_code}')


def response_text(payload: object) -> str:
    """Accept only complete visible text from a finished candidate."""
    try:
        candidate = payload['candidates'][0]
        if candidate['finishReason'] != 'STOP':
            raise ValueError('Gemini did not finish generating a response')
        parts = candidate['content']['parts']
        text = ''.join(part['text'] for part in parts if 'text' in part and not part.get('thought'))
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ValueError('Gemini returned an invalid response') from None
    if not text.strip():
        raise ValueError('Gemini returned no response text')
    return text


async def generate_content(message: str, *, system_instruction: str | None = None,
                           response_schema: dict | None = None) -> str:
    model = settings.gemini_model.removeprefix('models/')
    if not re.fullmatch(r'gemini-[A-Za-z0-9._-]{1,80}', model):
        raise GeminiHTTPError(404, {})
    body = {'contents': [{'role': 'user', 'parts': [{'text': message}]}]}
    if system_instruction is not None:
        body['systemInstruction'] = {'parts': [{'text': system_instruction}]}
    if response_schema is not None:
        body['generationConfig'] = {
            'responseMimeType': 'application/json',
            'responseJsonSchema': response_schema,
        }

    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    async with asyncio.timeout(11):
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(url, headers={'x-goog-api-key': settings.gemini_api_key.strip()}, json=body)
    if not response.is_success:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        raise GeminiHTTPError(response.status_code, payload)
    try:
        payload = response.json()
    except ValueError:
        raise ValueError('Gemini returned an invalid response') from None
    return response_text(payload)
