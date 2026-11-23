"""Resolve explicit student commands, then submit through the common enrollment service."""
import hashlib
import re
import time
from dataclasses import dataclass
from unicodedata import normalize
from urllib.parse import urlencode

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from . import registration as reg
from .catalog_data import department_names, normalize_name, school_prefix
from .chat_sources import source
from .db import SessionLocal
from .errors import DomainError
from .models import Course, CourseAlias, Department, School, Section


COMMAND = re.compile(r'^(?:我(?:要|想)|請(?:幫我)?|幫我|麻煩(?:幫我)?)\s*加選\s*(.*)$')
AMBIGUOUS = re.compile(r'["\'「」『』“”‘’?,，、;；\n]|(?:可以|能不能|可不可以|是否|不要|不用|不想|不必|別加選|取消|退選|和幫我|以及|然後|還有)|(?:嗎|呢|好嗎|行嗎)$')
# Accept a separated class number or the common unseparated "A班" or "01班".
CLASS_NUMBER = re.compile(r'(?:第([^第()]{1,32})班|班號\s*([a-z0-9_-]{1,32})|[\s(]+([a-z0-9_-]{1,32})班\)?|([a-z]|\d{1,8})班)$')


@dataclass(frozen=True)
class Command:
    target: str
    school_code: str | None
    section_no: str | None


def parse_command(message):
    normalized = normalize('NFKC', message).casefold().strip().rstrip('。.!！ ')
    match = COMMAND.fullmatch(normalized)
    if not match:
        return None
    target = match[1].strip()
    if not target or AMBIGUOUS.search(target) or target in ('這個', '這門課', '這門', '那門課', '那門', '該課', '它'):
        return Command('', None, None)

    class_number = CLASS_NUMBER.search(target)
    number = None
    if class_number:
        raw_number = next(value for value in class_number.groups() if value is not None)
        prefix = target[:class_number.start()].rstrip(' (')
        # The digits at the end of a course code belong to that code.
        if prefix and not prefix.endswith('-'):
            target, number = prefix, normalize_name(raw_number)
    target, school = school_prefix(normalize_name(target))
    return Command(target, school, number)


def fingerprint(term_id, channel, content, section_id=None):
    path = '/api/v1/chat/voice' if channel == 'CHAT_VOICE' else '/api/v1/chat'
    return reg.request_fingerprint('POST', path, {
        'term_id': term_id, 'sha256': hashlib.sha256(content).hexdigest(), 'section_id': section_id,
    })


def existing_response(user, term_id, request_key, digest):
    if term_id and request_key:
        with SessionLocal() as db:
            reg.get_term(db, term_id)
            replay = reg.replay_response(db, user['student_id'], term_id, request_key, digest)
            if replay is not None:
                return replay[1]
    return None


def section_matches(db, term, command):
    stmt = (select(Section.id, Section.section_no, Course, Department, School.code)
            .join(Course, Course.id == Section.course_id)
            .join(Department, Department.id == Course.department_id)
            .join(School, School.id == Department.school_id)
            .where(Section.term_id == term.id).order_by(Course.code, Section.section_no, Section.id))
    if command.school_code:
        stmt = stmt.where(School.code == command.school_code)
    rows = db.execute(stmt).all()
    # A recognized prefix is a constraint, even if it leaves zero matching courses.
    all_departments = db.execute(select(Department, School.code).join(School, School.id == Department.school_id)).all()
    qualifiers = {name for dept, school in all_departments for name in department_names(dept.code, dept.name, school)
                  if name.endswith(('系', '所', '班'))}
    qualifier = next((name for name in sorted(qualifiers, key=lambda name: (-len(name), name))
                      if command.target.startswith(name) and command.target != name), None)
    target = command.target[len(qualifier):].removeprefix('的') if qualifier else command.target
    if not target:
        return [], []
    known_aliases = set(db.scalars(select(CourseAlias.course_id).where(CourseAlias.normalized_alias == target)))
    alias_matches = set(db.scalars(select(CourseAlias.course_id).where(
        CourseAlias.normalized_alias.contains(target, autoescape=True))))
    exact, partial = [], []
    for section_id, number, course, department, school in rows:
        if qualifier and qualifier not in department_names(department.code, department.name, school):
            continue
        normalized_number = normalize_name(number)
        if command.section_no is not None and normalized_number != command.section_no:
            if not (normalized_number.isdecimal() and command.section_no.isdecimal()
                    and int(normalized_number) == int(command.section_no)):
                continue
        code, title = normalize_name(course.code), normalize_name(course.title)
        if target in (code, title) or course.id in known_aliases:
            exact.append(section_id)
        elif target in code or target in title or course.id in alias_matches:
            partial.append(section_id)
    return exact, partial


def clarification(db, message, request_id, term=None, choices=None):
    result = {'mode': 'clarify', 'answer': message, 'request_id': request_id, 'as_of': reg.now(db),
              'sources': [source(term.id if term else None, title='選擇學期並查課' if term is None else '課程查詢')]}
    if choices is not None:
        registration = reg.registration_status(db, term)
        result.update(
            choices=[{**{key: row[key] for key in (
                'id', 'code', 'title', 'school_code', 'school_name', 'department_name', 'section_no',
                'meetings', 'remaining', 'is_selectable',
            )}, 'section_id': row['id']} for row in reg.section_rows(db, choices[:5])],
            choice_total=len(choices), registration=registration,
        )
    return result


def handle_command(user, command, term_id, request_key, digest, request_id, channel, selected_section=None, deadline=None):
    """The transaction persists both the result and its replay record."""
    try:
        with SessionLocal.begin() as db:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.execute(text("SET LOCAL statement_timeout = '2s'"))
            if term_id is None:
                return clarification(db, '請先選擇要加選的學期。', request_id)
            term = reg.get_term(db, term_id, 'share')
            if not command.target:
                return clarification(db, '請一次指定一門要加選的課，提供完整課名或課號。', request_id, term)
            if not request_key:
                raise DomainError(422, 'IDEMPOTENCY_KEY_REQUIRED', '直接加選需要 Idempotency-Key。')
            st = reg.student_term(db, user['student_id'], term_id, True)
            replay = reg.replay_response(db, st.student_id, term_id, request_key, digest)
            if replay is not None:
                return replay[1]

            exact, partial = section_matches(db, term, command)
            candidates = exact or partial
            if selected_section is not None:
                if selected_section not in candidates[:5]:
                    raise DomainError(409, 'CHAT_CHOICE_INVALID', '班別已變動，請重新查詢並選擇。')
                section_id = selected_section
            elif len(exact) == 1:
                section_id = exact[0]
            else:
                message = ('找到多個同名班別，請選擇校系及班號。' if len(exact) > 1 else
                           '請從搜尋結果指定完整班別。' if partial else '這個學期查不到符合指令的班別。')
                return clarification(db, message, request_id, term, candidates)

            try:
                enrollment, section, credits = reg.add_enrollment(db, term, st, section_id, user['id'], channel=channel)
            except DomainError as error:
                details = {**(error.details or {}), 'section_id': section_id,
                           'sources': [source(term_id, section_id, '查看班別與選課入口')]}
                raise DomainError(error.status, error.code, error.message, details) from None
            submitted_at = reg.now(db)
            description = f'{section["school_name"]} {section["department_name"]} 的 {section["title"]} {section["section_no"]} 班'
            response = jsonable_encoder({
                'mode': 'add_course', 'answer': f'已加選 {description}。', 'as_of': submitted_at, 'request_id': request_id,
                'sources': [source(term_id, section_id, section['title']),
                            source(term_id, title='我的課表', url='/timetable?' + urlencode({'term': term_id}))],
                'action': {
                    'status': 'ENROLLED', 'term_id': term_id, 'enrollment_id': enrollment.id, 'section_id': section_id,
                    'school_code': section['school_code'], 'school_name': section['school_name'],
                    'department_name': section['department_name'], 'course_title': section['title'],
                    'section_no': section['section_no'], 'total_credits': str(credits),
                    'remaining': section['remaining'], 'submitted_at': submitted_at,
                },
                **({'transcript': f'我要加選 {description}', 'transcript_status': 'CLEAR'} if channel == 'CHAT_VOICE' else {}),
            })
            reg.record_response(db, st.student_id, term_id, request_key, digest, 200, response)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            return response
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) not in ('55P03', '57014'):
            raise
        raise DomainError(503, 'ENROLLMENT_BUSY', '選課服務正在處理其他請求，請重送同一操作。') from None
