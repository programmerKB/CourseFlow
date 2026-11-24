"""Bounded audio handling for the authenticated assistant endpoint.

Speech is transcribed inside the API process with faster-whisper (an OpenAI Whisper model
converted for CTranslate2). Audio never leaves the server.
"""
import asyncio
from array import array
from io import BytesIO
import logging
import re
import sys
import threading
from typing import Literal
import wave

from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .config import settings
from .errors import DomainError


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
# Whisper follows the language and vocabulary of its prompt: Traditional Chinese and the words
# students use with the assistant, so course codes such as "CS101 A 班" come out intact.
WHISPER_PROMPT = ('以下是臺灣學生對選課助理說的話，使用繁體中文。常見用語：加選、退選、試排、修課路徑、研究方向、'
                  '課程、班別、空位、星期二、臺科大、電子系、邊緣 AI、CS101 A 班。')
# Whisper's own threshold for a low-confidence decode (average log probability per token).
MIN_AVG_LOGPROB = -1.0
MAX_COMPRESSION_RATIO = 2.4
_model = None
_model_lock = threading.Lock()
# One transcription at a time keeps the CPU and memory use of a small VM predictable.
_decode_lock = threading.Lock()
_converter = None


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


def load_model():
    """Load the Whisper model once per process (from WHISPER_MODEL_DIR when it was prepared there)."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                                  compute_type=settings.whisper_compute_type, cpu_threads=settings.whisper_cpu_threads,
                                  download_root=settings.whisper_model_dir or None)
        return _model


def preload():
    try:
        load_model()
        log.info('whisper_ready model=%s device=%s', settings.whisper_model, settings.whisper_device)
    except Exception as error:
        log.warning('whisper_preload_failed model=%s error_class=%s', settings.whisper_model, type(error).__name__)


def to_traditional(text):
    """Whisper sometimes switches to Simplified characters mid-sentence; Traditional text is unchanged."""
    global _converter
    if _converter is None:
        from opencc import OpenCC
        _converter = OpenCC('s2tw')
    return _converter.convert(text)


def assess(segments, text):
    if not segments or not text:
        return 'SILENT'
    seconds = [max(segment.end - segment.start, 0.1) for segment in segments]
    logprob = sum(segment.avg_logprob * length for segment, length in zip(segments, seconds)) / sum(seconds)
    echoed = re.sub(r'[\s，。、,.]', '', text) in re.sub(r'[\s，。、,.]', '', WHISPER_PROMPT)
    if logprob < MIN_AVG_LOGPROB or max(s.compression_ratio for s in segments) > MAX_COMPRESSION_RATIO or echoed:
        return 'UNCLEAR'
    return 'CLEAR'


def run_whisper(wav):
    import numpy as np

    with wave.open(BytesIO(wav), 'rb') as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
    model = load_model()
    with _decode_lock:
        segments, _ = model.transcribe(samples, language='zh', task='transcribe', beam_size=5, temperature=0,
                                       vad_filter=True, condition_on_previous_text=False,
                                       initial_prompt=WHISPER_PROMPT, without_timestamps=True)
        segments = list(segments)
    text = to_traditional(''.join(segment.text for segment in segments)).strip()[:1000]
    return Transcript(status=assess(segments, text), transcript=text)


async def transcribe(wav):
    return await run_in_threadpool(run_whisper, wav)


async def recognize(content, media_type, request_id):
    wav = await decode_audio(content, media_type)
    try:
        async with asyncio.timeout(20):
            transcription = await transcribe(wav)
    except DomainError:
        raise
    except Exception as error:
        # Only the error class is logged: no audio, transcript or file paths.
        log.warning('voice_unavailable request_id=%s error_class=%s', request_id, type(error).__name__)
        raise unavailable() from None
    transcript = transcription.transcript.strip()
    if transcription.status != 'CLEAR' or not transcript:
        raise unclear(transcript, transcription.status if transcription.status != 'CLEAR' else 'UNCLEAR')
    return transcript
