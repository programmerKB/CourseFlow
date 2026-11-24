"""Bounded audio handling for the authenticated assistant endpoint."""
import asyncio
from array import array
from io import BytesIO
import logging
import sys
from typing import Literal
import wave

from google import genai
from google.genai import types
from httpx import HTTPError
from pydantic import BaseModel, Field, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .config import settings
from .errors import DomainError
from .gemini_diagnostics import safe_provider_diagnostic
from .gemini_rest import GeminiHTTPError


MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_AUDIO_BYTES + 16 * 1024
RATE = 16_000
MAX_AUDIO_SECONDS = 20
FORM_SCHEMA = {
    'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {
        'type': 'object', 'required': ['audio'], 'properties': {
            'audio': {'type': 'string', 'format': 'binary'},
            'term_id': {'type': 'string', 'format': 'uuid'},
            'track_code': {'type': 'string', 'maxLength': 64},
        },
    }}}},
}
FORMATS = {
    'audio/webm': ('matroska', b'\x1a\x45\xdf\xa3', 0),
    'video/webm': ('matroska', b'\x1a\x45\xdf\xa3', 0),
    'audio/mp4': ('mov', b'ftyp', 4),
    'video/mp4': ('mov', b'ftyp', 4),
    'audio/ogg': ('ogg', b'OggS', 0),
    'application/ogg': ('ogg', b'OggS', 0),
    'audio/wav': ('wav', b'RIFF', 0),
    'audio/mpeg': ('mp3', b'ID3', 0),
}
log = logging.getLogger('courses.voice')


class Transcript(BaseModel):
    status: Literal['CLEAR', 'UNCLEAR', 'SILENT']
    transcript: str = Field(max_length=1000)


class VoiceFormParser(MultiPartParser):
    # Keep the bounded audio upload in memory until conversion finishes.
    max_file_size = MAX_AUDIO_BYTES + 1
    spool_max_size = MAX_AUDIO_BYTES + 1


def unavailable():
    return DomainError(503, 'VOICE_UNAVAILABLE', '語音辨識暫時無法使用。你可以重新錄音或改用文字輸入。')


def unclear(transcript='', status='UNCLEAR'):
    return DomainError(422, 'VOICE_UNCLEAR', '沒有聽清楚。請重新錄音或改用文字輸入。',
                       {'transcript': transcript, 'transcript_status': status})


async def receive_audio(request):
    content_type = request.headers.get('content-type', '')
    if content_type.split(';', 1)[0].strip().lower() != 'multipart/form-data':
        raise DomainError(415, 'VOICE_UNSUPPORTED_FORMAT', '請使用錄音表單上傳音訊。')
    content_length = request.headers.get('content-length', '')
    if content_length.isdecimal() and int(content_length) > MAX_MULTIPART_BYTES:
        raise DomainError(413, 'VOICE_TOO_LARGE', '每段錄音最多 5 MiB。')

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_MULTIPART_BYTES:
            raise DomainError(413, 'VOICE_TOO_LARGE', '每段錄音最多 5 MiB。')
        body.extend(chunk)

    async def parts():
        yield bytes(body)

    try:
        form = await VoiceFormParser(request.headers, parts(), max_files=1, max_fields=2).parse()
    except (MultiPartException, MultipartParseError, ValueError):
        raise DomainError(422, 'INVALID_VOICE_FORM', '錄音表單無法讀取，請重錄。') from None
    try:
        audio, term, track = form.get('audio'), form.get('term_id'), form.get('track_code')
        if not isinstance(audio, UploadFile) or term is not None and not isinstance(term, str):
            raise DomainError(422, 'INVALID_VOICE_FORM', '錄音表單至少需包含一段錄音。')
        if track is not None and (not isinstance(track, str) or not 1 <= len(track) <= 64):
            raise DomainError(422, 'VALIDATION_ERROR', '研究方向識別碼格式不正確。')
        content = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(content) > MAX_AUDIO_BYTES:
            raise DomainError(413, 'VOICE_TOO_LARGE', '每段錄音最多 5 MiB。')
        if not content:
            raise unclear(status='SILENT')
        return content, (audio.content_type or '').split(';', 1)[0].strip().lower(), term, track
    finally:
        await form.close()


async def decode_audio(content, media_type):
    entry = FORMATS.get(media_type)
    if entry is None:
        raise DomainError(415, 'VOICE_UNSUPPORTED_FORMAT', '請使用 WebM、M4A、MP4、Ogg、WAV 或 MP3 音檔。')
    demuxer, signature, position = entry
    matches = content[position:position + len(signature)] == signature
    if demuxer == 'wav':
        matches = matches and content[8:12] == b'WAVE'
    elif demuxer == 'mp3':
        matches = matches or (len(content) >= 2 and content[0] == 0xff and (content[1] & 0xe0) == 0xe0)
    if not matches:
        raise DomainError(415, 'VOICE_UNSUPPORTED_FORMAT', '錄音內容與所選格式不符。')
    input_options = ['-protocol_whitelist', 'pipe']
    input_source = 'pipe:0'
    if demuxer == 'mov':
        # The sample index can follow the media data. Cache the bounded input so FFmpeg can seek back.
        input_options = ['-protocol_whitelist', 'cache,pipe', '-read_ahead_limit', '-1']
        input_source = 'cache:pipe:0'
    try:
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-threads', '1',
            *input_options, '-f', demuxer, '-i', input_source,
            '-map', '0:a:0', '-vn', '-sn', '-dn', '-ac', '1', '-ar', str(RATE),
            '-af', 'asetpts=N/SR/TB', '-t', str(MAX_AUDIO_SECONDS + 0.1),
            '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise unavailable() from None
    try:
        async with asyncio.timeout(5):
            pcm, _ = await process.communicate(content)
    except TimeoutError:
        raise unavailable() from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode:
        raise DomainError(415, 'VOICE_UNSUPPORTED_FORMAT', '無法讀取錄音格式，請重新錄音。')
    if len(pcm) > 2 * RATE * MAX_AUDIO_SECONDS:
        raise DomainError(413, 'VOICE_TOO_LONG', '每段錄音最多 20 秒。')
    samples = array('h', pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    if max(map(abs, samples), default=0) < 128:
        raise unclear(status='SILENT')

    buffer = BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(pcm)
    return buffer.getvalue()


async def transcribe(wav):
    if not settings.gemini_api_key.strip() or not settings.gemini_audio_model.strip():
        raise unavailable()
    http_options = types.HttpOptions(base_url='https://generativelanguage.googleapis.com/',
                                     retry_options=types.HttpRetryOptions(attempts=1))
    try:
        async with genai.Client(api_key=settings.gemini_api_key.strip(), vertexai=False,
                                http_options=http_options).aio as client:
            result = await client.models.generate_content(
                model=settings.gemini_audio_model.strip(),
                contents=[
                    '逐字轉寫這段華語錄音，使用繁體中文並保留英文縮寫、課號與班號。'
                    '只報告聽見的話，請勿回答錄音的問題、補齊課名或改寫成指令。'
                    '若不能肯定全部文字，狀態設為 UNCLEAR 並保留能聽清的文字。無人聲時設為 SILENT。',
                    types.Part.from_bytes(data=wav, mime_type='audio/wav'),
                ],
                config=types.GenerateContentConfig(temperature=0, max_output_tokens=1200,
                                                   response_mime_type='application/json', response_schema=Transcript),
            )
    except genai.errors.APIError as error:
        raise GeminiHTTPError(error.code, error.details) from None
    if not result.candidates or result.candidates[0].finish_reason != types.FinishReason.STOP:
        raise ValueError('Gemini returned an incomplete transcription')
    return Transcript.model_validate_json(result.text or '')


async def recognize(content, media_type, request_id):
    wav = await decode_audio(content, media_type)
    try:
        async with asyncio.timeout(20):
            transcription = await transcribe(wav)
    except DomainError:
        raise
    except (ValidationError, ValueError, RuntimeError, TimeoutError, GeminiHTTPError, HTTPError, OSError) as error:
        log.warning('voice_unavailable request_id=%s diagnostic=%s', request_id, safe_provider_diagnostic(error))
        raise unavailable() from None
    transcript = transcription.transcript.strip()
    if transcription.status != 'CLEAR' or not transcript:
        raise unclear(transcript, transcription.status if transcription.status != 'CLEAR' else 'UNCLEAR')
    return transcript
