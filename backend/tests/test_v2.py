"""V2 acceptance regressions against PostgreSQL and real audio decoding."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import json
from shutil import which
import subprocess
from threading import Barrier
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import registration, voice
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import Application, AuditEvent, Enrollment, NotificationOutbox, SectionMeeting, SelectionPlan, StudentTerm, Term
from app.roadmap_data import TRACKS
from app.seed import seed_university_catalog, uid
from conftest import FIXED, signin


TERM = uid('term/university-catalog')
INIT = uid('term/initial')
STUDENT = uid('student/student01')


def section(code, number='A'):
    return uid(f'section/university-catalog/{code}/{number}')


@pytest.fixture(autouse=True)
def university_catalog(fresh_db):
    with SessionLocal.begin() as db:
        seed_university_catalog(db, FIXED, db.get(Term, uid('term/history')), separate_term=True)


def ask(client, message, *, term=TERM, key='chat-test', **kwargs):
    return client.post('/api/v1/chat', json={'message': message, 'term_id': term, **kwargs},
                       headers={'Idempotency-Key': key} if key else {})


def voice_request(client, audio, *, key='voice-test', media_type='audio/webm', term=TERM, track_code=None):
    fields = {'term_id': term}
    if track_code is not None:
        fields['track_code'] = track_code
    return client.post('/api/v1/chat/voice', data=fields,
                       files={'audio': ('fixture', audio, media_type)}, headers={'Idempotency-Key': key})


def transaction_counts():
    with SessionLocal() as db:
        return tuple(db.scalar(select(func.count()).select_from(model).where(model.term_id == TERM))
                     for model in (Enrollment, AuditEvent, NotificationOutbox, SelectionPlan))


def test_at20_three_university_catalog_filters_and_sources(client):
    schools = {row['code']: row for row in client.get('/api/v1/schools').json()}
    assert set(schools) == {'NTU', 'NTNU', 'NTUST'}
    items = client.get(f'/api/v1/terms/{TERM}/sections', params={'page_size': 50}).json()['items']
    assert len({r['course_id'] for r in items}) == 40
    assert {code: len({r['course_id'] for r in items if r['school_code'] == code})
            for code in schools} == {'NTU': 15, 'NTNU': 11, 'NTUST': 14}
    assert all(r['catalog_origin'] == 'UNIVERSITY_REFERENCE' and r['source_url'].startswith('https://')
               and r['topics'] and r['meetings'] for r in items)
    assert all(r['instructor_name'] == f'{schools[r["school_code"]]["name"]} - 示範教師' for r in items)
    electronics = next(d['id'] for d in schools['NTUST']['departments'] if d['name'] == '電子工程系')
    uri = f'/api/v1/terms/{TERM}/sections'
    response = client.get(uri, params={'school_code': 'NTUST', 'department_id': electronics,
                                       'topic_code': 'memory_systems', 'q': '高等計算機結構'}).json()
    assert [r['code'] for r in response['items']] == ['NTUST-ET-001']
    assert client.get(uri, params={'q': '高等計算機架構'}).json()['total'] == 3
    assert client.get(uri, params={'q': '%_'}).json()['total'] == 0
    detail = client.get(f'{uri}/{section("NTUST-ET-001")}').json()
    assert detail['school_code'] == 'NTUST' and detail['source_url'] == response['items'][0]['source_url']
    signin(client)
    query = ask(client, '請推薦記憶體系統相關課。').json()['recommendation']['candidates']
    assert {c['school_code'] for c in query} == set(schools)


def test_at21_text_enrollment_without_gemini_and_replay_after_window(client, monkeypatch):
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', '')
    assert ask(client, '我要加選 NTUST-ET-001').status_code == 401
    signin(client)
    before = transaction_counts()
    message = '我要加選台科大電子系的高等計算機結構。'
    assert ask(client, message, term=None).json()['mode'] == 'clarify'
    assert ask(client, message, key=None).status_code == 422
    response = ask(client, message)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mode'] == 'add_course' and data['action']['section_id'] == section('NTUST-ET-001')
    assert data['action']['remaining'] == 0 and data['action']['total_credits'] == '3.0'
    assert transaction_counts() == (before[0] + 1, before[1] + 1, *before[2:])
    assert ask(client, '我要加選 ＮＴＵＳＴ－ＥＴ－００２ Ａ班', key='code').json()['mode'] == 'add_course'
    timetable = client.get(f'/api/v1/me/terms/{TERM}/timetable').json()
    assert {row['id'] for row in timetable['items']} == {section('NTUST-ET-001'), section('NTUST-ET-002')}
    monkeypatch.setattr(registration, 'now', lambda db: FIXED + timedelta(days=31))
    assert ask(client, message).json() == data
    assert ask(client, '我要加選 NTUST-ET-002').json()['code'] == 'IDEMPOTENCY_KEY_REUSED'


def test_at22_candidates_and_non_commands_preserve_timetable(client):
    signin(client)
    before = transaction_counts()
    ambiguous = '我要加選高等計算機結構'
    same_course = '請加選適應控制系統'
    by_title = ask(client, ambiguous).json()
    assert by_title['mode'] == 'clarify'
    assert [choice['code'] for choice in by_title['choices']] == ['NTNU-CS-002', 'NTUST-CS-002', 'NTUST-ET-001']
    assert by_title['choice_total'] == 3
    assert all(row['school_name'] and row['meetings'] and row['remaining'] for row in by_title['choices'])
    assert [choice['section_no'] for choice in ask(client, same_course).json()['choices']] == ['A', 'B']
    assert ask(client, '幫我加選高等計算').json()['mode'] == 'clarify'
    for text in ('我可以加選資料庫設計嗎？', '我不要加選資料庫設計', '「我要加選資料庫設計」',
                 '我要加選資料庫設計嗎', '我要加選這門課', '我要加選 NTUST-ET-001、NTUST-ET-002'):
        assert ask(client, text).json()['mode'] != 'add_course'
    invalid = ask(client, ambiguous, section_id=section('NTU-EE-002'))
    assert invalid.status_code == 409 and invalid.json()['code'] == 'CHAT_CHOICE_INVALID'
    assert transaction_counts() == before
    assert ask(client, ambiguous, section_id=section('NTNU-CS-002')).json()['action']['school_code'] == 'NTNU'
    data = ask(client, same_course + '第B班', key='specific')
    assert data.json()['action']['section_id'] == section('NTU-EE-001', 'B')


def test_eligibility_question_remains_read_only_without_model(client, monkeypatch):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', '')
    for question in ('我可以加選資料庫設計嗎？', '這門課可以加選嗎？', '我不要加選資料庫設計'):
        response = ask(client, question)
        assert response.status_code == 200 and response.json()['mode'] == 'student_query'
    assert transaction_counts() == (0, 0, 0, 0)


@pytest.mark.parametrize(('code', 'expected'), [
    ('NTUST-ET-006', 'SECTION_FULL'), ('NTU-CS-002', 'PREREQUISITE_MISSING'),
    ('NTU-EE-002', 'TIME_CONFLICT'), ('NTU-EE-003', 'CREDIT_LIMIT_EXCEEDED'),
    ('NTU-EE-001', 'ALREADY_ENROLLED'),
])
def test_at23_direct_enrollment_reuses_business_rules(client, code, expected):
    student = uid('student/student02')
    with SessionLocal.begin() as db:
        if expected == 'CREDIT_LIMIT_EXCEEDED':
            db.get(StudentTerm, (student, TERM)).credit_limit = Decimal('2.0')
        if expected in ('TIME_CONFLICT', 'ALREADY_ENROLLED'):
            existing = section('NTU-EE-001')
            db.add(Enrollment(student_id=student, term_id=TERM, course_id=uid('course/NTU-EE-001'),
                              section_id=existing, status='ENROLLED', source='ADD_DROP'))
            if expected == 'TIME_CONFLICT':
                a, b = [db.scalar(select(SectionMeeting).where(SectionMeeting.section_id == sid))
                        for sid in (existing, section(code))]
                b.weekday, b.start_time, b.end_time = a.weekday, a.start_time, a.end_time
    signin(client, 'student02')
    before = transaction_counts()
    response = ask(client, f'我要加選 {code} 第A班')
    assert response.status_code == 409 and response.json()['code'] == expected, response.text
    assert transaction_counts() == before


def test_at23_initial_offers_registration_and_closed_shows_schedule(client, monkeypatch):
    signin(client)
    response = ask(client, '我要加選 CS301', term=INIT)
    assert response.status_code == 200
    preview = response.json()
    assert preview['mode'] == 'clarify' and preview['registration']['phase'] == 'INITIAL'
    assert [entry['section_id'] for entry in preview['choices']] == [uid('section/initial/CS301/A')]
    assert not client.get(f'/api/v1/me/terms/{INIT}/applications').json()['items']
    assert ask(client, '我要登記初選 NTUST-ET-001').json()['code'] == 'REGISTRATION_CLOSED'
    monkeypatch.setattr(registration, 'now', lambda db: FIXED + timedelta(days=31))
    response = ask(client, '請加選 NTUST-ET-001')
    assert response.json()['details']['registration']['phase'] == 'CLOSED'
    assert not transaction_counts()[0]


def encode_audio(seconds=0.6, frequency=440, codec='libopus', container='webm', silent=False, output=None):
    if not which('ffmpeg'):
        pytest.skip('FFmpeg is required for the audio integration tests')
    source = 'anullsrc=r=48000:cl=mono' if silent else f'sine=frequency={frequency}:sample_rate=48000'
    flags = ['-movflags', 'empty_moov+frag_keyframe'] if container == 'mp4' else []
    destination = str(output) if output is not None else 'pipe:1'
    result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-t', str(seconds),
                             '-c:a', codec, *flags, '-f', container, destination],
                            check=True, capture_output=True, timeout=10)
    return output.read_bytes() if output is not None else result.stdout


@pytest.fixture
def audio():
    return encode_audio()


def test_voice_decodes_mp4_with_sample_index_after_audio(tmp_path):
    if not which('ffmpeg'):
        pytest.skip('FFmpeg is required for the audio integration tests')
    clip = tmp_path / 'capture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-t', '8', '-c:a', 'aac', '-b:a', '192k', str(clip)],
                   check=True, capture_output=True, timeout=10)
    wav = asyncio.run(voice.decode_audio(clip.read_bytes(), 'audio/mp4'))
    with wave.open(BytesIO(wav), 'rb') as audio_file:
        assert audio_file.getnchannels() == 1 and audio_file.getframerate() == 16_000
        assert 7.9 <= audio_file.getnframes() / audio_file.getframerate() <= 8.1


@pytest.mark.parametrize(('codec', 'container', 'media_type'), [
    ('libopus', 'webm', 'audio/webm'), ('aac', 'mp4', 'audio/mp4'), ('libopus', 'ogg', 'audio/ogg'),
    ('pcm_s16le', 'wav', 'audio/wav'), ('libmp3lame', 'mp3', 'audio/mpeg'),
])
def test_at24_voice_direct_addition_with_recording_and_upload_formats(client, monkeypatch, tmp_path,
                                                                    codec, container, media_type):
    calls = []

    async def transcribe(wav):
        calls.append(wav)
        with wave.open(BytesIO(wav)) as decoded:
            assert decoded.getnchannels() == 1 and decoded.getframerate() == 16000
        return voice.Transcript(status='CLEAR', transcript='我要加選臺科大電子系的高等計算機結構')

    monkeypatch.setattr(voice, 'transcribe', transcribe)
    signin(client)
    output = tmp_path / 'voice.wav' if container == 'wav' else None
    audio = encode_audio(codec=codec, container=container, output=output)
    response = voice_request(client, audio, media_type=media_type)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mode'] == 'add_course' and data['action']['section_id'] == section('NTUST-ET-001')
    assert '高等計算機結構' in data['transcript'] and data['transcript_status'] == 'CLEAR'
    assert voice_request(client, audio, media_type=media_type).json() == data and len(calls) == 1
    output = tmp_path / 'changed.wav' if container == 'wav' else None
    changed_audio = encode_audio(frequency=500, codec=codec, container=container, output=output)
    assert voice_request(client, changed_audio, media_type=media_type).json()['code'] == 'IDEMPOTENCY_KEY_REUSED'
    with SessionLocal() as db:
        audit = db.scalar(select(AuditEvent).where(AuditEvent.term_id == TERM))
        assert audit.payload['channel'] == 'CHAT_VOICE'
        assert set(audit.payload) == {'section_id', 'source', 'channel'}


def test_voice_initial_choice_flows_to_pending_registration(client, monkeypatch, audio):
    async def transcribe(_wav):
        return voice.Transcript(status='CLEAR', transcript='我想要加選，CS101 A班。')

    monkeypatch.setattr(voice, 'transcribe', transcribe)
    signin(client)
    result = voice_request(client, audio, term=INIT)
    assert result.status_code == 200, result.text
    preview = result.json()
    choice = uid('section/initial/CS101/A')
    assert preview['transcript'] == '我想要加選，CS101 A班。'
    assert preview['registration']['phase'] == 'INITIAL'
    assert [section['section_id'] for section in preview['choices']] == [choice]
    base = f'/api/v1/me/terms/{INIT}'
    assert client.get(f'{base}/applications').json()['items'] == []
    result = client.put(f'{base}/applications/{choice}')
    assert result.status_code == 200 and result.json()['status'] == 'PENDING'
    timetable = client.get(f'{base}/timetable').json()
    assert [row['id'] for row in timetable['pending_items']] == [choice]
    assert timetable['items'] == []


def test_voice_trial_plan_keeps_the_selected_research_track(client, monkeypatch, audio):
    async def transcribe(_wav):
        return voice.Transcript(status='CLEAR', transcript='請試排這個方向的本期課程。')

    monkeypatch.setattr(voice, 'transcribe', transcribe)
    signin(client, 'student08')
    roadmap = ask(client, '類比與混合訊號 IC').json()['recommendation']['roadmap']
    response = voice_request(client, audio, track_code=roadmap['track_code'])
    assert response.status_code == 200, response.text
    recommendation = response.json()['recommendation']
    assert recommendation['roadmap']['track_code'] == roadmap['track_code']
    assert recommendation['plan'] is not None


def test_at25_malformed_silent_oversized_and_unavailable_voice(client, monkeypatch, audio, caplog):
    signin(client)
    cases = [
        (b'different file', 'audio/ogg', 415, 'VOICE_UNSUPPORTED_FORMAT'),
        (encode_audio(silent=True), 'audio/webm', 422, 'VOICE_UNCLEAR'),
        (b'x' * (voice.MAX_AUDIO_BYTES + 1), 'audio/webm', 413, 'VOICE_TOO_LARGE'),
        (encode_audio(seconds=21), 'audio/webm', 413, 'VOICE_TOO_LONG'),
    ]
    for content, mime, status, code in cases:
        result = voice_request(client, content, media_type=mime)
        assert result.status_code == status and result.json()['code'] == code, result.text

    async def uncertain(wav):
        return voice.Transcript(status='UNCLEAR', transcript='我要加選臺科大')

    monkeypatch.setattr(voice, 'transcribe', uncertain)
    result = voice_request(client, audio)
    assert result.status_code == 422 and result.json()['details']['transcript_status'] == 'UNCLEAR'

    async def timeout(wav):
        raise TimeoutError('sensitive fixture detail')

    monkeypatch.setattr(voice, 'transcribe', timeout)
    result = voice_request(client, audio)
    assert result.status_code == 503 and result.json()['code'] == 'VOICE_UNAVAILABLE'
    assert 'sensitive fixture detail' not in caplog.text and '我要加選臺科大' not in caplog.text
    assert transaction_counts() == (0, 0, 0, 0)


def test_audio_provider_receives_wav_and_incomplete_response_cannot_write(client, monkeypatch, audio):
    monkeypatch.setattr(settings, 'gemini_api_key', 'test-key')
    monkeypatch.setattr(settings, 'gemini_audio_model', 'gemini-audio-fixture')
    reasons = iter(('MAX_TOKENS', 'STOP'))

    async def send(self, request, *args, **kwargs):
        assert request.url.host == 'generativelanguage.googleapis.com'
        assert '/models/gemini-audio-fixture:generateContent' in request.url.path
        payload = json.loads(request.content)
        assert payload['generationConfig']['responseMimeType'] == 'application/json'
        part = next(part['inlineData'] for content in payload['contents'] for part in content['parts'] if 'inlineData' in part)
        assert part['mimeType'] == 'audio/wav'
        with wave.open(BytesIO(base64.urlsafe_b64decode(part['data']))) as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
        return httpx.Response(200, request=request, json={'candidates': [{
            'finishReason': next(reasons), 'content': {'parts': [{'text': json.dumps({
                'status': 'CLEAR', 'transcript': '幫我加選 NTUST-ET-001',
            })}]},
        }]})

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    signin(client)
    response = voice_request(client, audio, key='provider-incomplete')
    assert response.status_code == 503 and response.json()['code'] == 'VOICE_UNAVAILABLE'
    assert transaction_counts() == (0, 0, 0, 0)
    response = voice_request(client, audio, key='provider-complete')
    assert response.status_code == 200 and response.json()['action']['section_id'] == section('NTUST-ET-001')


def test_at26_voice_text_manual_compete_for_one_seat(monkeypatch, audio):
    async def transcribe(wav):
        await asyncio.sleep(0.02)
        return voice.Transcript(status='CLEAR', transcript='幫我加選 NTUST-ET-001')

    monkeypatch.setattr(voice, 'transcribe', transcribe)
    barrier = Barrier(3)
    path = f'/api/v1/me/terms/{TERM}/enrollments'

    def execute(client, index):
        if index == 0:
            return ask(client, '請加選臺科大電子系高等計算機結構', key='last-seat')
        if index == 1:
            return voice_request(client, audio, key='last-seat')
        return client.post(path, json={'section_id': section('NTUST-ET-001')},
                           headers={'Idempotency-Key': 'last-seat'})

    def compete(index):
        with TestClient(app) as client:
            signin(client, f'student{index + 1:02}')
            barrier.wait(timeout=10)
            first = execute(client, index)
            if first.status_code in (200, 201):
                assert execute(client, index).json() == first.json()
            return first

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(compete, range(3)))
    assert sum(r.status_code in (200, 201) for r in responses) == 1
    assert [r.json()['code'] for r in responses if r.status_code == 409] == ['SECTION_FULL', 'SECTION_FULL']
    assert transaction_counts() == (1, 1, 0, 0)


def test_at32_all_stages_aliases_and_targeted_clarification(client, monkeypatch):
    signin(client)
    monkeypatch.setattr(settings, 'chat_stub', False)
    monkeypatch.setattr(settings, 'gemini_api_key', '')
    for code, track in TRACKS.items():
        for query in (track.title, track.aliases[0]):
            response = ask(client, query)
            assert response.status_code == 200, response.text
            rec = response.json()['recommendation']
            path = rec['roadmap']
            assert rec['plan'] is None and path['track_code'] == code
            assert [stage['stage'] for stage in path['stages']] == ['FOUNDATION', 'SPECIALIZATION', 'RESEARCH']
            assert all(path['stages'][2]['milestone'][key] for key in ('project', 'deliverable', 'metrics'))
    assert ask(client, '我想找進階課').json()['mode'] == 'clarify'
    assert transaction_counts() == (0, 0, 0, 0)


def test_at33_ic_and_nvm_choose_track(client):
    signin(client)
    for query, codes in [('IC 設計', {'digital_ic', 'analog_ic'}), ('NVM', {'memory_systems', 'semiconductor_process'})]:
        choices = ask(client, query).json()['track_choices']
        assert {c['track_code'] for c in choices} == codes
        for choice in choices:
            actual = ask(client, choice['title']).json()['recommendation']['roadmap']
            assert actual['track_code'] == choice['track_code']


def test_at34_roadmap_classifies_offerings_sources_and_same_names(client):
    signin(client)
    response = ask(client, '計算機結構與記憶體系統').json()
    courses = [course for stage in response['recommendation']['roadmap']['stages'] for course in stage['courses']]
    same_name = [row for row in courses if row['title'] == '高等計算機結構']
    assert len(same_name) == 3 and all(r['availability'] == 'OFFERED' for r in same_name)
    assert len({entry['sections'][0]['section_id'] for entry in same_name}) == 3
    assert all(entry['sections'][0]['source']['term_id'] == TERM for entry in same_name)
    assert any(entry['availability'] == 'NO_PUBLIC_SECTION' for entry in courses)
    response = ask(client, 'RF', term=None).json()
    rec = response['recommendation']
    assert rec['registration'] is None and rec['plan'] is None
    assert {course['availability'] for stage in rec['roadmap']['stages'] for course in stage['courses']} == {
        'TERM_NOT_SELECTED', 'CURRICULUM_REFERENCE',
    }
    assert '選擇學期' in response['answer']


def test_at35_actual_completion_controls_prerequisites(client):
    question = '我修過資料庫設計，想做 AI Agent，這學期先修什麼？'
    for number in (1, 2):
        signin(client, f'student{number:02}')
        rec = ask(client, question).json()['recommendation']
        assert rec['roadmap']['track_code'] == 'ai_agent'
        db_design = next(c for c in rec['roadmap']['stages'][0]['courses'] if c.get('course_code') == 'NTUST-ET-002')
        distributed = next(c for c in rec['candidates'] if c['course_code'] == 'NTU-CS-002')
        assert db_design['completed'] == (number == 1)
        assert distributed['eligibility']['reason_code'] == (None if number == 1 else 'PREREQUISITE_MISSING')


def test_at36_roadmap_consultation_then_preview_and_confirm(client):
    signin(client)
    result = ask(client, '我想做 RF，這學期先修什麼，以後能做什麼專題？').json()['recommendation']
    assert result['plan'] is None and result['roadmap']['stages'][2]['milestone']['metrics']
    assert transaction_counts() == (0, 0, 0, 0)
    result = ask(client, '請試排本期符合條件的班別', track_code='rf_microwave').json()['recommendation']
    plan = result['plan']
    assert plan['term_id'] == TERM and plan['section_ids']
    assert transaction_counts() == (0, 0, 0, 1)
    response = client.post(f'/api/v1/me/terms/{TERM}/selection-plans/{plan["plan_id"]}/confirm',
                           json={'section_ids': plan['section_ids']}, headers={'Idempotency-Key': 'plan'})
    assert response.status_code == 200 and all(row['status'] == 'ENROLLED' for row in response.json()['items'])
