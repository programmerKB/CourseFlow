import asyncio
import hashlib
import logging
import threading
import time
from datetime import datetime
from ipaddress import ip_address, ip_network
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request, Depends, Response, Query, Header
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from decimal import Decimal
from pydantic import Field, model_validator
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from .config import normalize_origin, settings
from .db import get_db
from .errors import DomainError
from .auth import current_user, student_user, admin_user, instructor_user, COOKIE_NAME, login
from .models import Term, School, Department, Instructor, Course, AuthSession, NotificationOutbox, Topic
from . import registration as reg, admin, voice, instructor as teaching
from .admin import UUIDStr, StrictModel

log = logging.getLogger('courses')
logging.basicConfig(level=logging.INFO, format='%(message)s')
if settings.chat_stub:
    log.warning('CHAT_STUB=true: the assistants use fixed test rules and do not call the local Llama model.')
app = FastAPI(title='大學選課與課程推薦系統', version='3.0.1')


@app.on_event('startup')
def preload_speech_model():
    # Load Whisper in the background so the first recording does not wait for it.
    if settings.whisper_preload:
        threading.Thread(target=voice.preload, name='whisper-preload', daemon=True).start()
prefix = '/api/v1'
LAN_NETWORKS = tuple(ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def matches_local_origin(request, origin):
    host = request.headers.get('host')
    if not host:
        return False
    scheme = request.url.scheme
    if origin.startswith('https://') and request.headers.get('sec-fetch-site') == 'same-origin':
        # A browser can confirm the frontend's HTTPS origin when TLS ends at Nginx.
        scheme = 'https'
    try:
        target_origin = normalize_origin(f'{scheme}://{host}')
        if origin != target_origin:
            return False
        hostname = urlsplit(target_origin).hostname
        if hostname == 'localhost':
            return True
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in LAN_NETWORKS)


@app.middleware('http')
async def request_context(request, call_next):
    request.state.request_id = str(uuid4())
    begin = time.monotonic()
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        if (request.headers.get('sec-fetch-site') == 'cross-site' or
                origin is not None and origin not in settings.browser_origins and not matches_local_origin(request, origin)):
            return JSONResponse(status_code=403, content={'code': 'ORIGIN_FORBIDDEN', 'message': '請由同源網站送出請求。', 'request_id': request.state.request_id})
    response = await call_next(request)
    response.headers['X-Request-ID'] = request.state.request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    log.info(__import__('json').dumps({'request_id': request.state.request_id, 'method': request.method, 'path': request.url.path,
                                     'status': response.status_code, 'elapsed_ms': round((time.monotonic() - begin) * 1000, 2)}))
    return response


@app.exception_handler(DomainError)
async def domain_error(request, exc):
    body = {'code': exc.code, 'message': exc.message, 'request_id': request.state.request_id}
    if exc.details is not None:
        body['details'] = exc.details
    return JSONResponse(status_code=exc.status, content=jsonable_encoder(body))


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse(status_code=422, content={'code': 'VALIDATION_ERROR', 'message': '輸入欄位格式不正確。',
        'request_id': request.state.request_id, 'details': [{'field': '.'.join(str(x) for x in e['loc']), 'message': e['msg']} for e in exc.errors()]})


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    log.error('database_constraint request_id=%s class=%s', request.state.request_id, type(exc.orig).__name__)
    return JSONResponse(status_code=409, content={'code': 'DATA_CONFLICT', 'message': '資料狀態已改變，請重新查詢。', 'request_id': request.state.request_id})


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse(status_code=exc.status_code, content={'code': 'HTTP_ERROR', 'message': str(exc.detail),
                        'request_id': request.state.request_id})


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    log.error('unexpected_error request_id=%s error_class=%s', request.state.request_id, type(exc).__name__)
    return JSONResponse(status_code=500, content={'code': 'INTERNAL_ERROR', 'message': '系統暫時無法完成此操作，請稍後重試。',
                        'request_id': request.state.request_id})


@app.get('/health/live')
@app.get(prefix + '/health/live')
def live():
    return {'status': 'ok'}


@app.get('/health/ready')
@app.get(prefix + '/health/ready')
def ready(db=Depends(get_db)):
    try:
        db.execute(text('SELECT 1'))
        return {'status': 'ready'}
    except Exception:
        raise DomainError(503, 'DATABASE_UNAVAILABLE', '資料庫尚未就緒。')


class LoginInput(StrictModel):
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=200)


@app.post(prefix + '/auth/login')
def auth_login(body: LoginInput, response: Response, db=Depends(get_db)):
    with db.begin():
        token, user = login(db, body.login, body.password)
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_hours * 3600, httponly=True,
                        secure=settings.cookie_secure, samesite='lax', path='/')
    return user


@app.get(prefix + '/auth/me')
def auth_me(user=Depends(current_user)):
    return user


@app.post(prefix + '/auth/logout', status_code=204)
def auth_logout(request: Request, response: Response, user=Depends(current_user), db=Depends(get_db)):
    with db.begin():
        session = db.get(AuthSession, hashlib.sha256(request.cookies[COOKIE_NAME].encode()).hexdigest())
        session.revoked_at = reg.now(db)
    response.delete_cookie(COOKIE_NAME, path='/', httponly=True, samesite='lax', secure=settings.cookie_secure)


@app.get(prefix + '/terms')
def terms(db=Depends(get_db)):
    return [admin.term_dict(t) for t in db.scalars(reg.public_terms_query().order_by(Term.start_date.desc(), Term.code))]


@app.get(prefix + '/departments')
def departments(db=Depends(get_db)):
    return [{'id': d.id, 'code': d.code, 'name': d.name, 'school_code': s.code, 'school_name': s.name}
            for d, s in db.execute(select(Department, School).join(School, School.id == Department.school_id)
                                   .order_by(School.code, Department.code))]


@app.get(prefix + '/schools')
def schools(db=Depends(get_db)):
    rows = db.execute(select(School, Department).outerjoin(Department, Department.school_id == School.id)
                      .order_by(School.code, Department.code))
    result = {}
    for school, department in rows:
        entry = result.setdefault(school.code, {'id': school.id, 'code': school.code, 'name': school.name,
                                               'departments': []})
        if department:
            entry['departments'].append({'id': department.id, 'code': department.code, 'name': department.name})
    return list(result.values())


@app.get(prefix + '/topics')
def topics(db=Depends(get_db)):
    return [{'code': topic.code, 'label': topic.label} for topic in db.scalars(select(Topic).order_by(Topic.code))]


@app.get(prefix + '/terms/{term_id}/registration')
def registration(term_id: UUIDStr, db=Depends(get_db)):
    return reg.registration_status(db, reg.get_term(db, term_id))


@app.get(prefix + '/terms/{term_id}/sections')
def sections(term_id: UUIDStr, q: str | None = Query(None, max_length=100), department_id: UUIDStr | None = None,
             instructor: str | None = Query(None, max_length=100), weekday: int | None = Query(None, ge=1, le=7),
             school_code: str | None = Query(None, max_length=16), topic_code: str | None = Query(None, max_length=64),
             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50), db=Depends(get_db)):
    reg.get_term(db, term_id)
    return reg.list_sections(db, term_id, q, department_id, instructor, weekday, page, page_size,
                             school_code=school_code.strip().upper() if school_code else None,
                             topic_code=topic_code.strip().lower() if topic_code else None)


@app.get(prefix + '/terms/{term_id}/sections/{section_id}')
def section_detail(term_id: UUIDStr, section_id: UUIDStr, db=Depends(get_db)):
    reg.get_term(db, term_id)
    return reg.section_dict(db, reg.get_section(db, term_id, section_id))


@app.put(prefix + '/me/terms/{term_id}/applications/{section_id}')
def application_add(term_id: UUIDStr, section_id: UUIDStr, user=Depends(student_user), db=Depends(get_db)):
    with db.begin():
        return reg.apply(db, user['student_id'], term_id, section_id, user['id'])


@app.delete(prefix + '/me/terms/{term_id}/applications/{section_id}', status_code=204)
def application_cancel(term_id: UUIDStr, section_id: UUIDStr, user=Depends(student_user), db=Depends(get_db)):
    with db.begin():
        reg.cancel_application(db, user['student_id'], term_id, section_id, user['id'])


@app.get(prefix + '/me/terms/{term_id}/applications')
def application_list(term_id: UUIDStr, user=Depends(student_user), db=Depends(get_db)):
    return reg.applications(db, user['student_id'], term_id)


class EnrollmentInput(StrictModel):
    section_id: UUIDStr


RequestKey = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200)]
OptionalRequestKey = Annotated[str | None, Header(alias='Idempotency-Key', min_length=1, max_length=200)]


class PlanConfirmInput(StrictModel):
    section_ids: list[UUIDStr] = Field(min_length=1, max_length=5)

    @model_validator(mode='after')
    def distinct_sections(self):
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError('確認的班別不得重複。')
        return self


class PlanConfirmationItem(StrictModel):
    section_id: UUIDStr
    status: Literal['PENDING', 'ENROLLED']


class PlanConfirmation(StrictModel):
    plan_id: UUIDStr
    phase: Literal['INITIAL', 'ADD_DROP']
    items: list[PlanConfirmationItem]
    submitted_at: datetime
    notification_id: UUIDStr | None = Field(default=None, description='ADD_DROP 提交建立的選課確認信事件。')


@app.post(prefix + '/me/terms/{term_id}/selection-plans/{plan_id}/confirm', response_model=PlanConfirmation)
def plan_confirm(term_id: UUIDStr, plan_id: UUIDStr, body: PlanConfirmInput, request_key: RequestKey,
                 user=Depends(student_user), db=Depends(get_db)):
    with db.begin():
        status, data = reg.confirm_selection_plan(db, user['student_id'], term_id, plan_id,
                                                  body.section_ids, user['id'], request_key)
    return JSONResponse(status_code=status, content=data)


@app.post(prefix + '/me/terms/{term_id}/enrollments')
def enrollment_add(term_id: UUIDStr, body: EnrollmentInput, request: Request, request_key: RequestKey,
                   user=Depends(student_user), db=Depends(get_db)):
    with db.begin():
        status, data = reg.enrollment_change(db, user['student_id'], term_id, body.section_id, user['id'], request_key, 'POST', request.url.path)
    return JSONResponse(status_code=status, content=data)


@app.delete(prefix + '/me/terms/{term_id}/enrollments/{section_id}')
def enrollment_drop(term_id: UUIDStr, section_id: UUIDStr, request: Request, request_key: RequestKey,
                    user=Depends(student_user), db=Depends(get_db)):
    with db.begin():
        status, data = reg.enrollment_change(db, user['student_id'], term_id, section_id, user['id'], request_key, 'DELETE', request.url.path)
    return Response(status_code=status) if status == 204 else JSONResponse(status_code=status, content=data)


@app.get(prefix + '/me/terms/{term_id}/timetable')
def timetable(term_id: UUIDStr, user=Depends(student_user), db=Depends(get_db)):
    return reg.get_timetable(db, user['student_id'], term_id)


@app.post(prefix + '/me/terms/{term_id}/timetable/email', status_code=201)
def email_timetable(term_id: UUIDStr, request_key: RequestKey, user=Depends(student_user), db=Depends(get_db)):
    with db.begin():
        status, data = reg.queue_timetable_email(db, user['student_id'], term_id, user['id'], request_key)
    return JSONResponse(status_code=status, content=data)


@app.get(prefix + '/me/terms/{term_id}/sections/{section_id}/eligibility')
def eligibility(term_id: UUIDStr, section_id: UUIDStr, user=Depends(student_user), db=Depends(get_db)):
    return reg.eligibility(db, user['student_id'], term_id, section_id)


@app.get(prefix + '/admin/catalog')
def catalog(user=Depends(admin_user), db=Depends(get_db)):
    return {'courses': [{'id': c.id, 'code': c.code, 'title': c.title, 'department_id': c.department_id} for c in db.scalars(select(Course).order_by(Course.code))],
            'departments': departments(db),
            'instructors': [{'id': i.id, 'name': i.name, 'department_id': i.department_id}
                            for i in db.scalars(select(Instructor).order_by(Instructor.name, Instructor.department_id, Instructor.id))]}


@app.get(prefix + '/admin/terms')
def admin_terms(user=Depends(admin_user), db=Depends(get_db)):
    return [admin.term_dict(t) for t in db.scalars(reg.active_terms_query().order_by(Term.start_date.desc(), Term.code))]


@app.post(prefix + '/admin/terms', status_code=201)
def term_create(body: admin.TermInput, user=Depends(admin_user), db=Depends(get_db)):
    with db.begin():
        return admin.create_term(db, body, user['id'])


@app.get(prefix + '/admin/terms/{term_id}/registration')
def admin_registration(term_id: UUIDStr, user=Depends(admin_user), db=Depends(get_db)):
    return reg.registration_status(db, reg.get_term(db, term_id, public=False))


@app.put(prefix + '/admin/terms/{term_id}/registration-periods')
def period_update(term_id: UUIDStr, body: admin.PeriodsInput, user=Depends(admin_user), db=Depends(get_db)):
    with db.begin():
        return admin.set_periods(db, term_id, body, user['id'])


@app.get(prefix + '/admin/terms/{term_id}/sections')
def admin_sections(term_id: UUIDStr, q: str | None = Query(None, max_length=100), department_id: UUIDStr | None = None,
                   instructor: str | None = Query(None, max_length=100), weekday: int | None = Query(None, ge=1, le=7),
                   school_code: str | None = Query(None, max_length=16), topic_code: str | None = Query(None, max_length=64),
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=50),
                   user=Depends(admin_user), db=Depends(get_db)):
    reg.get_term(db, term_id, public=False)
    return reg.list_sections(db, term_id, q, department_id, instructor, weekday, page, page_size,
                             school_code=school_code.strip().upper() if school_code else None,
                             topic_code=topic_code.strip().lower() if topic_code else None)


@app.post(prefix + '/admin/terms/{term_id}/sections', status_code=201)
def section_create(term_id: UUIDStr, body: admin.SectionInput, user=Depends(admin_user), db=Depends(get_db)):
    with db.begin():
        return admin.save_section(db, term_id, body, user['id'])


@app.get(prefix + '/admin/terms/{term_id}/sections/{section_id}')
def admin_section_detail(term_id: UUIDStr, section_id: UUIDStr, user=Depends(admin_user), db=Depends(get_db)):
    reg.get_term(db, term_id, public=False)
    return reg.section_dict(db, reg.get_section(db, term_id, section_id))


@app.patch(prefix + '/admin/terms/{term_id}/sections/{section_id}')
def section_patch(term_id: UUIDStr, section_id: UUIDStr, body: admin.SectionPatch, user=Depends(admin_user), db=Depends(get_db)):
    with db.begin():
        return admin.save_section(db, term_id, body, user['id'], section_id)


@app.post(prefix + '/admin/terms/{term_id}/publish')
def term_publish(term_id: UUIDStr, user=Depends(admin_user), db=Depends(get_db)):
    with db.begin():
        return admin.publish(db, term_id, user['id'])


@app.post(prefix + '/admin/terms/{term_id}/allocation')
def allocation(term_id: UUIDStr, user=Depends(admin_user), db=Depends(get_db)):
    with db.begin():
        return reg.allocate(db, term_id, user['id'])


@app.get(prefix + '/admin/terms/{term_id}/notifications')
def notifications(term_id: UUIDStr, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), user=Depends(admin_user), db=Depends(get_db)):
    reg.get_term(db, term_id, public=False)
    return {'items': [{'id': n.id, 'type': n.type, 'status': n.status, 'attempt_count': n.attempt_count,
                       'next_attempt_at': n.next_attempt_at, 'sent_at': n.sent_at, 'last_error': n.last_error,
                       'provider_message_id': n.provider_message_id}
                      for n in db.scalars(select(NotificationOutbox).where(NotificationOutbox.term_id == term_id).order_by(NotificationOutbox.next_attempt_at.desc(), NotificationOutbox.id).offset((page - 1) * page_size).limit(page_size))]}


class ChatInput(StrictModel):
    message: str = Field(min_length=1, max_length=1000)
    term_id: UUIDStr | None = None
    section_id: UUIDStr | None = Field(default=None, description='學生明確點選的候選班別，須附原加選指令。')
    track_code: str | None = Field(default=None, max_length=64, description='先前選定的研究方向，用於接續試排。')


class ChatSource(StrictModel):
    title: str
    url: str
    term_id: UUIDStr | None = None
    section_id: UUIDStr | None = None


class ChatEnrollment(StrictModel):
    status: Literal['ENROLLED', 'DROPPED']
    term_id: UUIDStr
    enrollment_id: UUIDStr
    section_id: UUIDStr
    course_code: str | None = None
    school_code: str
    school_name: str
    department_name: str
    course_title: str
    section_no: str
    total_credits: str
    remaining: int
    submitted_at: datetime
    notification_id: UUIDStr | None = Field(default=None, description='正式加選建立的確認信事件；退選為空。')


class ChatAgent(StrictModel):
    graph: str
    model: str
    intent: str
    tools: list[str]
    llm: dict | None = Field(default=None, description='本機模型伺服器回報的模型、token 數與耗時；CHAT_STUB 時為 {"stub": true}；'
                                                       '完整句型由本機規則判讀、未呼叫模型時為 {"local_rules": true}。')


class ChatOutput(StrictModel):
    mode: Literal['simple_chat', 'sql_query', 'student_query', 'recommendation', 'clarify', 'add_course', 'drop_course']
    answer: str
    sources: list[ChatSource]
    as_of: datetime
    request_id: str
    recommendation: dict | None = None
    action: ChatEnrollment | None = None
    registration: dict | None = None
    choices: list[dict] | None = None
    choice_total: int | None = None
    choice_action: Literal['add', 'drop', 'apply'] | None = None
    agent: ChatAgent | None = Field(default=None, description='LangGraph 流程（英文加退選或中文查詢）的模型、執行的工具與模型伺服器證據。')
    track_choices: list[dict] | None = None
    transcript: str | None = None
    transcript_status: Literal['CLEAR', 'UNCLEAR', 'SILENT'] | None = None


@app.post(prefix + '/chat', response_model=ChatOutput, response_model_exclude_unset=True)
async def chat(body: ChatInput, request: Request, request_key: OptionalRequestKey = None,
               user=Depends(student_user), db=Depends(get_db)):
    from .chat import handle_chat
    return await handle_chat(db, user, body.message, body.term_id, request.state.request_id, request_key=request_key,
                             selected_section=body.section_id, track_context=body.track_code)


@app.post(prefix + '/chat/voice', openapi_extra=voice.FORM_SCHEMA,
          response_model=ChatOutput, response_model_exclude_unset=True)
async def chat_voice(request: Request, request_key: RequestKey, user=Depends(student_user), db=Depends(get_db)):
    from .chat import handle_chat, resolve_term_id
    from .chat_enrollment import existing_response, fingerprint

    deadline = time.monotonic() + 30
    transcript = ''
    try:
        async with asyncio.timeout(30):
            audio, media_type, term_id, track_code = await voice.receive_audio(request)
            try:
                term_id = TypeAdapter(UUIDStr | None).validate_python(term_id)
            except ValidationError:
                raise DomainError(422, 'VALIDATION_ERROR', '學期識別碼格式不正確。') from None
            term_id = resolve_term_id(term_id)
            digest = fingerprint(term_id, 'CHAT_VOICE', audio)
            replay = await run_in_threadpool(existing_response, user, term_id, request_key, digest)
            if replay is not None:
                return replay
            transcript = await voice.recognize(audio, media_type, request.state.request_id)
            del audio
            result = await handle_chat(db, user, transcript, term_id, request.state.request_id,
                                       request_key=request_key, digest=digest, channel='CHAT_VOICE',
                                       track_context=track_code, deadline=deadline)
            return {'transcript': transcript, 'transcript_status': 'CLEAR', **result}
    except TimeoutError:
        if not transcript:
            raise voice.unavailable() from None
        # Speech was recognised; the assistant (usually the local model's first load) ran out of time.
        from .chat import UNAVAILABLE_MESSAGES
        raise DomainError(503, 'CHAT_UNAVAILABLE', UNAVAILABLE_MESSAGES['LLAMA_TIMEOUT'] + '辨識文字已保留，可稍後重送。',
                          {'mode': 'unavailable', 'reason_code': 'LLAMA_TIMEOUT', 'transcript': transcript,
                           'transcript_status': 'CLEAR'}) from None
    except DomainError as error:
        if not transcript:
            raise
        raise DomainError(error.status, error.code, error.message,
                          {'transcript': transcript, 'transcript_status': 'CLEAR', **(error.details or {})}) from None


class GradeInput(StrictModel):
    score: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2, description='0–100，最多兩位小數。')


@app.get(prefix + '/instructor/terms/{term_id}/sections')
def instructor_sections(term_id: UUIDStr, user=Depends(instructor_user), db=Depends(get_db)):
    return teaching.teaching_sections(db, user['instructor_id'], term_id)


@app.get(prefix + '/instructor/terms/{term_id}/sections/{section_id}/roster')
def instructor_roster(term_id: UUIDStr, section_id: UUIDStr, user=Depends(instructor_user), db=Depends(get_db)):
    return teaching.roster(db, user['instructor_id'], term_id, section_id)


@app.put(prefix + '/instructor/terms/{term_id}/sections/{section_id}/roster/{enrollment_id}/grade')
def instructor_grade(term_id: UUIDStr, section_id: UUIDStr, enrollment_id: UUIDStr, body: GradeInput,
                     user=Depends(instructor_user), db=Depends(get_db)):
    with db.begin():
        return teaching.set_grade(db, user, term_id, section_id, enrollment_id, body.score)
