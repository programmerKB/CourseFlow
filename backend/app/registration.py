"""Registration rules and mutations. Call mutations inside one caller-owned transaction.

Lock order: term (share for students / update for administrators), student_term,
section. No network I/O is performed inside any of these transactions.
"""
import hashlib
import json
from datetime import timezone
from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func, and_, or_, exists

from .errors import DomainError
from .models import (Term, RegistrationPeriod, StudentTerm, Section, Course,
                     Instructor, Department, SectionMeeting, SectionPrerequisite,
                     CourseCompletion, Enrollment, Application, AllocationRun,
                     AuditEvent, IdempotencyKey, NotificationOutbox, Student)

REASONS = {
    'SECTION_UNAVAILABLE': '此班別目前未開放選課。',
    'ALREADY_ENROLLED': '已選上同一課程，不能跨班重複修課。',
    'PREREQUISITE_MISSING': '尚未在先前學期通過全部先修科目。',
    'TIME_CONFLICT': '此班別與目前正式課表的上課時間重疊。',
    'CREDIT_LIMIT_EXCEEDED': '加選後總學分超過本學期學分上限。',
    'SECTION_FULL': '此班別已額滿。',
    'REGISTRATION_CLOSED': '目前不在可操作的選課階段。',
}


def now(db):
    return db.scalar(select(func.clock_timestamp()))


def get_term(db, term_id, lock=None, public=True):
    stmt = select(Term).where(Term.id == term_id)
    if lock:
        stmt = stmt.with_for_update(read=(lock == 'share'))
    term = db.scalar(stmt.execution_options(populate_existing=True))
    if not term or (public and not term.catalog_published_at):
        raise DomainError(404, 'TERM_NOT_FOUND', '找不到公開學期。')
    return term


def phase(db, term, at=None):
    at = at or now(db)
    periods = {p.kind: p for p in db.scalars(select(RegistrationPeriod).where(RegistrationPeriod.term_id == term.id))}
    initial, add_drop = periods.get('INITIAL'), periods.get('ADD_DROP')
    if not term.catalog_published_at or not initial or not add_drop:
        return 'UPCOMING'
    published = db.scalar(select(AllocationRun.id).where(AllocationRun.term_id == term.id)) is not None
    if at >= add_drop.ends_at:
        return 'CLOSED'
    if at < initial.starts_at:
        return 'UPCOMING'
    if at < initial.ends_at:
        return 'INITIAL'
    if not published:
        return 'AWAITING_ALLOCATION'
    if at < add_drop.starts_at:
        return 'RESULTS'
    return 'ADD_DROP'


def registration_status(db, term):
    at = now(db)
    run = db.scalar(select(AllocationRun).where(AllocationRun.term_id == term.id))
    return {'term_id': term.id, 'phase': phase(db, term, at), 'as_of': at,
            'periods': [{'kind': p.kind, 'starts_at': p.starts_at, 'ends_at': p.ends_at}
                        for p in db.scalars(select(RegistrationPeriod).where(RegistrationPeriod.term_id == term.id).order_by(RegistrationPeriod.starts_at))],
            'published_at': run.published_at if run else None,
            'allocation': run_dict(run) if run else None}


def section_rows(db, ids):
    if not ids:
        return []
    counts = select(Enrollment.section_id, func.count().label('n')).where(Enrollment.status == 'ENROLLED').group_by(Enrollment.section_id).subquery()
    rows = db.execute(select(Section, Course, Instructor, Department, func.coalesce(counts.c.n, 0))
                      .join(Course, Section.course_id == Course.id)
                      .join(Instructor, Section.instructor_id == Instructor.id)
                      .join(Department, Course.department_id == Department.id)
                      .outerjoin(counts, Section.id == counts.c.section_id).where(Section.id.in_(ids))).all()
    meetings = {}
    for m in db.scalars(select(SectionMeeting).where(SectionMeeting.section_id.in_(ids)).order_by(SectionMeeting.weekday, SectionMeeting.start_time)):
        meetings.setdefault(m.section_id, []).append({'id': m.id, 'weekday': m.weekday, 'start_time': m.start_time,
                                                     'end_time': m.end_time, 'room': m.room})
    prerequisites = {}
    for sid, c in db.execute(select(SectionPrerequisite.section_id, Course).join(Course, SectionPrerequisite.required_course_id == Course.id).where(SectionPrerequisite.section_id.in_(ids))):
        prerequisites.setdefault(sid, []).append({'id': c.id, 'code': c.code, 'title': c.title})
    at = now(db)
    result = {}
    for s, c, i, d, n in rows:
        result[s.id] = {'id': s.id, 'term_id': s.term_id, 'course_id': c.id, 'code': c.code, 'title': c.title,
                        'description': c.description, 'section_no': s.section_no, 'department_id': d.id,
                        'department_name': d.name, 'instructor_id': i.id, 'instructor_name': i.name,
                        'credits': s.credits, 'capacity': s.capacity, 'enrolled_count': n,
                        'remaining': s.capacity - n, 'is_selectable': s.is_selectable,
                        'meetings': meetings.get(s.id, []), 'prerequisites': prerequisites.get(s.id, []), 'as_of': at}
    return [result[sid] for sid in ids if sid in result]


def get_section(db, term_id, section_id, lock=False):
    stmt = select(Section).where(Section.id == section_id, Section.term_id == term_id)
    if lock:
        stmt = stmt.with_for_update()
    section = db.scalar(stmt.execution_options(populate_existing=True))
    if not section:
        raise DomainError(404, 'SECTION_NOT_FOUND', '找不到此學期的班別。')
    return section


def section_dict(db, section):
    return section_rows(db, [section.id])[0]


def list_sections(db, term_id, q=None, department_id=None, instructor=None, weekday=None, page=1, page_size=20):
    stmt = select(Section.id).join(Course, Course.id == Section.course_id).join(Instructor, Instructor.id == Section.instructor_id).where(Section.term_id == term_id)
    if q:
        stmt = stmt.where(or_(Course.code.ilike(f'%{q}%'), Course.title.ilike(f'%{q}%')))
    if department_id:
        stmt = stmt.where(Course.department_id == department_id)
    if instructor:
        stmt = stmt.where(Instructor.name.ilike(f'%{instructor}%'))
    if weekday:
        stmt = stmt.where(exists(select(SectionMeeting.id).where(SectionMeeting.section_id == Section.id, SectionMeeting.weekday == weekday)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    ids = list(db.scalars(stmt.order_by(Course.code, Section.section_no, Section.id).offset((page - 1) * page_size).limit(page_size)))
    return {'items': section_rows(db, ids), 'total': total, 'page': page, 'page_size': page_size, 'as_of': now(db)}


def student_term(db, student_id, term_id, lock=False):
    stmt = select(StudentTerm).where(StudentTerm.student_id == student_id, StudentTerm.term_id == term_id)
    if lock:
        stmt = stmt.with_for_update()
    st = db.scalar(stmt.execution_options(populate_existing=True))
    if not st:
        raise DomainError(403, 'TERM_INELIGIBLE', '尚無此學期的修課資格。')
    return st


def active_enrollments(db, student_id, term_id):
    return list(db.scalars(select(Enrollment).where(Enrollment.student_id == student_id, Enrollment.term_id == term_id, Enrollment.status == 'ENROLLED')))


def get_timetable(db, student_id, term_id):
    get_term(db, term_id)
    st = student_term(db, student_id, term_id)
    active = active_enrollments(db, student_id, term_id)
    items = section_rows(db, [e.section_id for e in active])
    return {'items': items, 'total_credits': sum((s['credits'] for s in items), Decimal('0.0')),
            'credit_limit': st.credit_limit, 'as_of': now(db)}


def admission_error(db, st, term, section):
    """Return first rule failure in the exact SRS order. No writes."""
    if section.term_id != term.id or not section.is_selectable:
        return 'SECTION_UNAVAILABLE', None
    active = active_enrollments(db, st.student_id, term.id)
    if any(e.course_id == section.course_id for e in active):
        return 'ALREADY_ENROLLED', None
    required = set(db.scalars(select(SectionPrerequisite.required_course_id).where(SectionPrerequisite.section_id == section.id)))
    passed = set(db.scalars(select(CourseCompletion.course_id).join(Term, Term.id == CourseCompletion.completed_term_id)
                            .where(CourseCompletion.student_id == st.student_id, Term.end_date < term.start_date)))
    if required - passed:
        return 'PREREQUISITE_MISSING', {'missing_course_ids': sorted(required - passed)}
    target = list(db.scalars(select(SectionMeeting).where(SectionMeeting.section_id == section.id)))
    existing = list(db.scalars(select(SectionMeeting).where(SectionMeeting.section_id.in_([e.section_id for e in active])))) if active else []
    conflicts = sorted({m.section_id for t in target for m in existing if t.weekday == m.weekday and t.start_time < m.end_time and m.start_time < t.end_time})
    if conflicts:
        return 'TIME_CONFLICT', {'conflicting_section_ids': conflicts}
    credits = db.scalar(select(func.coalesce(func.sum(Section.credits), 0)).join(Enrollment, Enrollment.section_id == Section.id)
                        .where(Enrollment.student_id == st.student_id, Enrollment.term_id == term.id, Enrollment.status == 'ENROLLED'))
    if credits + section.credits > st.credit_limit:
        return 'CREDIT_LIMIT_EXCEEDED', {'total_credits': str(credits + section.credits), 'credit_limit': str(st.credit_limit)}
    n = db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.section_id == section.id, Enrollment.status == 'ENROLLED'))
    if n >= section.capacity:
        return 'SECTION_FULL', None
    return None, None


def eligibility(db, student_id, term_id, section_id):
    term = get_term(db, term_id)
    st = student_term(db, student_id, term_id)
    section = get_section(db, term_id, section_id)
    at = now(db)
    current_phase = phase(db, term, at)
    code, details = admission_error(db, st, term, section) if current_phase == 'ADD_DROP' else ('REGISTRATION_CLOSED', None)
    return {'phase': current_phase, 'eligible': code is None, 'reason_code': code,
            'message': REASONS.get(code, '目前符合加選條件，提交時仍須重新檢查。'), 'details': details, 'as_of': at}


def audit(db, actor_user_id, term_id, action, target_type, target_id, payload=None):
    event = AuditEvent(actor_user_id=actor_user_id, term_id=term_id, action=action,
                       target_type=target_type, target_id=target_id, payload=jsonable_encoder(payload or {}))
    db.add(event)
    db.flush()
    return event


def application_dict(db, a):
    return {**section_dict(db, get_section(db, a.term_id, a.section_id)), 'application_id': a.id,
            'status': a.status, 'reason_code': a.reason_code, 'reason': REASONS.get(a.reason_code),
            'submitted_at': a.submitted_at, 'decided_at': a.decided_at}


def applications(db, student_id, term_id):
    get_term(db, term_id)
    student_term(db, student_id, term_id)
    rows = list(db.scalars(select(Application).where(Application.student_id == student_id, Application.term_id == term_id).order_by(Application.submitted_at)))
    sections = {s['id']: s for s in section_rows(db, [a.section_id for a in rows])}
    return {'items': [{**sections[a.section_id], 'application_id': a.id, 'status': a.status,
                       'reason_code': a.reason_code, 'reason': REASONS.get(a.reason_code),
                       'submitted_at': a.submitted_at, 'decided_at': a.decided_at} for a in rows], 'as_of': now(db)}


def apply(db, student_id, term_id, section_id, actor_user_id):
    term = get_term(db, term_id, 'share')
    student_term(db, student_id, term_id, True)
    section = get_section(db, term_id, section_id, True)
    if phase(db, term) != 'INITIAL':
        raise DomainError(409, 'REGISTRATION_CLOSED', REASONS['REGISTRATION_CLOSED'])
    if not section.is_selectable:
        raise DomainError(409, 'SECTION_UNAVAILABLE', REASONS['SECTION_UNAVAILABLE'])
    pending = db.scalar(select(Application).where(Application.student_id == student_id, Application.term_id == term_id,
                                                   Application.course_id == section.course_id, Application.status == 'PENDING'))
    if pending:
        if pending.section_id == section_id:
            return application_dict(db, pending)
        raise DomainError(409, 'APPLICATION_EXISTS', '已登記同課程的其他班別，請先取消。')
    a = db.scalar(select(Application).where(Application.student_id == student_id, Application.term_id == term_id, Application.section_id == section_id))
    if a:
        a.status, a.submitted_at, a.decided_at, a.reason_code, a.allocation_run_id = 'PENDING', now(db), None, None, None
    else:
        a = Application(student_id=student_id, term_id=term_id, course_id=section.course_id, section_id=section_id, status='PENDING')
        db.add(a)
    db.flush()
    audit(db, actor_user_id, term_id, 'APPLICATION_SUBMITTED', 'application', a.id)
    return application_dict(db, a)


def cancel_application(db, student_id, term_id, section_id, actor_user_id):
    term = get_term(db, term_id, 'share')
    student_term(db, student_id, term_id, True)
    get_section(db, term_id, section_id, True)
    if phase(db, term) != 'INITIAL':
        raise DomainError(409, 'REGISTRATION_CLOSED', REASONS['REGISTRATION_CLOSED'])
    a = db.scalar(select(Application).where(Application.student_id == student_id, Application.term_id == term_id, Application.section_id == section_id, Application.status == 'PENDING'))
    if a:
        a.status = 'CANCELLED'
        audit(db, actor_user_id, term_id, 'APPLICATION_CANCELLED', 'application', a.id)


def enroll(db, student_id, term_id, section, source):
    e = db.scalar(select(Enrollment).where(Enrollment.student_id == student_id, Enrollment.term_id == term_id, Enrollment.section_id == section.id))
    if e:
        e.status, e.source, e.updated_at = 'ENROLLED', source, now(db)
    else:
        e = Enrollment(student_id=student_id, term_id=term_id, course_id=section.course_id,
                       section_id=section.id, status='ENROLLED', source=source)
        db.add(e)
    db.flush()
    return e


def enrollment_change(db, student_id, term_id, section_id, actor_user_id, request_key, method, path):
    term = get_term(db, term_id, 'share')
    st = student_term(db, student_id, term_id, True)
    fingerprint = hashlib.sha256(json.dumps({'method': method, 'path': path, 'body': {'section_id': section_id} if method == 'POST' else {}}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    old = db.get(IdempotencyKey, (student_id, term_id, request_key))
    if old:
        if old.request_fingerprint != fingerprint:
            raise DomainError(409, 'IDEMPOTENCY_KEY_REUSED', '此重送鍵已用於另一個請求。')
        return old.response_status, old.response_body
    section = get_section(db, term_id, section_id, True)
    if phase(db, term) != 'ADD_DROP':
        raise DomainError(409, 'REGISTRATION_CLOSED', REASONS['REGISTRATION_CLOSED'])
    if method == 'POST':
        code, details = admission_error(db, st, term, section)
        if code:
            raise DomainError(409, code, REASONS[code], details)
        e = enroll(db, student_id, term_id, section, 'ADD_DROP')
        status = 201
        action = 'ENROLLED'
    else:
        e = db.scalar(select(Enrollment).where(Enrollment.student_id == student_id, Enrollment.term_id == term_id,
                                              Enrollment.section_id == section_id, Enrollment.status == 'ENROLLED'))
        if not e:
            raise DomainError(404, 'ENROLLMENT_NOT_FOUND', '找不到此班別的有效選課。')
        e.status, e.updated_at = 'DROPPED', now(db)
        db.flush()
        status, action = 204, 'DROPPED'
    event = audit(db, actor_user_id, term_id, action, 'enrollment', e.id, {'section_id': section_id, 'source': 'ADD_DROP'})
    details = section_dict(db, section)
    db.add(NotificationOutbox(event_key=f'enrollment:{event.id}', term_id=term_id, recipient_user_id=actor_user_id,
                              type=action, payload=jsonable_encoder({'term_code': term.code, 'title': details['title'],
                              'code': details['code'], 'action': action, 'section_id': section_id, 'url': '/timetable'})))
    body = jsonable_encoder({'enrollment': {'id': e.id, 'section_id': section_id, 'status': e.status, 'source': e.source},
                              'remaining': details['remaining'], 'total_credits': get_timetable(db, student_id, term_id)['total_credits'],
                              'as_of': now(db)}) if status == 201 else None
    db.add(IdempotencyKey(student_id=student_id, term_id=term_id, request_key=request_key,
                          request_fingerprint=fingerprint, response_status=status, response_body=body))
    db.flush()
    return status, body


def run_dict(run):
    return {'run_id': run.id, 'published_at': run.published_at.astimezone(timezone.utc), 'accepted_count': run.accepted_count, 'rejected_count': run.rejected_count}


def allocate(db, term_id, actor_user_id):
    term = get_term(db, term_id, 'update')
    previous = db.scalar(select(AllocationRun).where(AllocationRun.term_id == term_id))
    if previous:
        return run_dict(previous)
    initial = db.get(RegistrationPeriod, (term_id, 'INITIAL'))
    if not initial or now(db) < initial.ends_at:
        raise DomainError(409, 'ALLOCATION_NOT_READY', '初選尚未截止。')
    pending = list(db.scalars(select(Application).where(Application.term_id == term_id, Application.status == 'PENDING')))
    pending.sort(key=lambda a: (hashlib.sha256(f'v1|{term.allocation_seed}|{a.student_id}|{a.section_id}'.encode('utf-8')).hexdigest(), a.student_id, a.section_id))
    run = AllocationRun(term_id=term_id, algorithm_version='v1', seed=term.allocation_seed, published_at=now(db), accepted_count=0, rejected_count=0)
    db.add(run)
    db.flush()
    summaries = {}
    for a in pending:
        st = student_term(db, a.student_id, term_id, True)
        section = get_section(db, term_id, a.section_id, True)
        code, _ = admission_error(db, st, term, section)
        a.status, a.reason_code, a.decided_at, a.allocation_run_id = ('REJECTED' if code else 'ACCEPTED'), code, now(db), run.id
        if code:
            run.rejected_count += 1
        else:
            run.accepted_count += 1
            enroll(db, a.student_id, term_id, section, 'INITIAL')
        course = db.get(Course, section.course_id)
        summaries.setdefault(a.student_id, []).append({'section_id': a.section_id, 'code': course.code, 'title': course.title,
                                                       'status': a.status, 'reason': REASONS.get(code)})
        db.flush()
    for sid, results in summaries.items():
        student = db.get(Student, sid)
        db.add(NotificationOutbox(event_key=f'allocation:{run.id}:{sid}', term_id=term_id, recipient_user_id=student.user_id,
                                  type='ALLOCATION', payload={'term_code': term.code, 'run_id': run.id, 'results': results, 'url': '/applications'}))
    audit(db, actor_user_id, term_id, 'ALLOCATION_PUBLISHED', 'allocation_run', run.id, run_dict(run))
    # Use the actual final publication instant; all result rows become visible together at commit.
    run.published_at = now(db)
    db.flush()
    return run_dict(run)
