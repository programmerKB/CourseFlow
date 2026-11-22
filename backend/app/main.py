import hashlib
import logging
import time
from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Depends, Response, Query, Header
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import get_db
from .errors import DomainError
from .auth import current_user, student_user, admin_user, COOKIE_NAME, login
from .models import Term, Department, Instructor, Course, AuthSession, NotificationOutbox
from . import registration as reg, admin
from .admin import UUIDStr, StrictModel

log = logging.getLogger('courses')
logging.basicConfig(level=logging.INFO, format='%(message)s')
app = FastAPI(title='大學選課與課程推薦系統', version='2.0.0')
prefix = '/api/v1'


@app.middleware('http')
async def request_context(request, call_next):
    request.state.request_id = str(uuid4())
    begin = time.monotonic()
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        if (origin is not None and origin not in settings.browser_origins) or request.headers.get('sec-fetch-site') == 'cross-site':
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
    return [{'id': d.id, 'code': d.code, 'name': d.name} for d in db.scalars(select(Department).order_by(Department.code))]


@app.get(prefix + '/terms/{term_id}/registration')
def registration(term_id: UUIDStr, db=Depends(get_db)):
    return reg.registration_status(db, reg.get_term(db, term_id))


@app.get(prefix + '/terms/{term_id}/sections')
def sections(term_id: UUIDStr, q: str | None = Query(None, max_length=100), department_id: UUIDStr | None = None,
             instructor: str | None = Query(None, max_length=100), weekday: int | None = Query(None, ge=1, le=7),
             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50), db=Depends(get_db)):
    reg.get_term(db, term_id)
    return reg.list_sections(db, term_id, q, department_id, instructor, weekday, page, page_size)


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
            'instructors': [{'id': i.id, 'name': i.name, 'department_id': i.department_id} for i in db.scalars(select(Instructor).order_by(Instructor.name))]}


@app.get(prefix + '/admin/terms')
def admin_terms(user=Depends(admin_user), db=Depends(get_db)):
    return [admin.term_dict(t) for t in db.scalars(select(Term).order_by(Term.start_date.desc(), Term.code))]


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
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=50),
                   user=Depends(admin_user), db=Depends(get_db)):
    reg.get_term(db, term_id, public=False)
    return reg.list_sections(db, term_id, q, department_id, instructor, weekday, page, page_size)


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
                       'next_attempt_at': n.next_attempt_at, 'sent_at': n.sent_at, 'last_error': n.last_error}
                      for n in db.scalars(select(NotificationOutbox).where(NotificationOutbox.term_id == term_id).order_by(NotificationOutbox.next_attempt_at.desc(), NotificationOutbox.id).offset((page - 1) * page_size).limit(page_size))]}


class ChatInput(StrictModel):
    message: str = Field(min_length=1, max_length=1000)
    term_id: UUIDStr | None = None


@app.post(prefix + '/chat')
async def chat(body: ChatInput, request: Request, user=Depends(student_user), db=Depends(get_db)):
    from .chat import handle_chat
    return await handle_chat(db, user, body.message, body.term_id, request.state.request_id)
