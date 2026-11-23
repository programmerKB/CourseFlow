"""Registration rules and mutations. Call mutations inside one caller-owned transaction.

Lock order: term (share for students / update for administrators), student_term,
selection plan (batch only), sections by ID. These transactions perform database operations exclusively.
"""
import hashlib
import json
from datetime import timezone
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func, and_, or_, exists

from .catalog_data import normalize_name
from .config import settings
from .errors import DomainError
from .models import (Term, RegistrationPeriod, StudentTerm, Section, Course, CourseAlias, CourseTopic, Topic, School,
                     Instructor, Department, SectionMeeting, SectionPrerequisite,
                     CourseCompletion, Enrollment, Application, AllocationRun,
                     AuditEvent, IdempotencyKey, NotificationOutbox, Student,
                     SelectionPlan, SelectionPlanItem)

REASONS = {
    'SECTION_UNAVAILABLE': '此班別目前未開放選課。',
    'ALREADY_ENROLLED': '已選上同一課程，不能跨班重複修課。',
    'APPLICATION_EXISTS': '已登記同課程的班別，請先取消原登記。',
    'PREREQUISITE_MISSING': '尚未在先前學期通過全部先修科目。',
    'TIME_CONFLICT': '此班別與目前正式課表的上課時間重疊。',
    'CREDIT_LIMIT_EXCEEDED': '加選後總學分超過本學期學分上限。',
    'SECTION_FULL': '此班別已額滿。',
    'REGISTRATION_CLOSED': '目前不在可操作的選課階段。',
}


def now(db):
    return db.scalar(select(func.clock_timestamp()))


def active_terms_query():
    return select(Term).where(Term.archived_at.is_(None))


def public_terms_query():
    stmt = active_terms_query().where(Term.catalog_published_at.is_not(None))
    if settings.public_initial_term_id:
        stmt = stmt.where(Term.id == settings.public_initial_term_id)
    return stmt


def get_term(db, term_id, lock=None, public=True):
    stmt = (public_terms_query() if public else active_terms_query()).where(Term.id == term_id)
    if lock:
        stmt = stmt.with_for_update(read=(lock == 'share'))
    term = db.scalar(stmt.execution_options(populate_existing=True))
    if term is None:
        raise DomainError(404, 'TERM_NOT_FOUND', '找不到公開學期。')
    return term


def phase(db, term, at=None):
    periods = list(db.scalars(select(RegistrationPeriod).where(RegistrationPeriod.term_id == term.id)))
    published = db.scalar(select(AllocationRun.id).where(AllocationRun.term_id == term.id)) is not None
    return phase_from_rows(term, at or now(db), periods, published)


def phase_from_rows(term, at, periods, published):
    by_kind = {period.kind: period for period in periods}
    initial, add_drop = by_kind.get('INITIAL'), by_kind.get('ADD_DROP')
    initial_only = term.id == settings.public_initial_term_id
    if not term.catalog_published_at or not initial or (not initial_only and not add_drop):
        return 'UPCOMING'
    if not initial_only and at >= add_drop.ends_at:
        return 'CLOSED'
    if at < initial.starts_at:
        return 'UPCOMING'
    if at < initial.ends_at:
        return 'INITIAL'
    if not published:
        return 'AWAITING_ALLOCATION'
    if initial_only or at < add_drop.starts_at:
        return 'RESULTS'
    return 'ADD_DROP'


def registration_status(db, term):
    periods = list(db.scalars(select(RegistrationPeriod).where(RegistrationPeriod.term_id == term.id).order_by(RegistrationPeriod.starts_at)))
    if term.id == settings.public_initial_term_id:
        periods = [period for period in periods if period.kind == 'INITIAL']
    run = db.scalar(select(AllocationRun).where(AllocationRun.term_id == term.id))
    at = now(db)
    return {'term_id': term.id, 'phase': phase_from_rows(term, at, periods, run is not None), 'as_of': at,
            'periods': [{'kind': p.kind, 'starts_at': p.starts_at, 'ends_at': p.ends_at}
                        for p in periods],
            'published_at': run.published_at if run else None,
            'allocation': run_dict(run) if run else None}


def section_rows(db, ids):
    if not ids:
        return []
    counts = select(Enrollment.section_id, func.count().label('n')).where(
        Enrollment.status == 'ENROLLED', Enrollment.section_id.in_(ids),
    ).group_by(Enrollment.section_id).subquery()
    rows = db.execute(select(Section, Course, Instructor, Department, School, func.coalesce(counts.c.n, 0))
                      .join(Course, Section.course_id == Course.id)
                      .join(Instructor, Section.instructor_id == Instructor.id)
                      .join(Department, Course.department_id == Department.id)
                      .join(School, Department.school_id == School.id)
                      .outerjoin(counts, Section.id == counts.c.section_id).where(Section.id.in_(ids))).all()
    meetings = {}
    for m in db.scalars(select(SectionMeeting).where(SectionMeeting.section_id.in_(ids)).order_by(SectionMeeting.weekday, SectionMeeting.start_time)):
        meetings.setdefault(m.section_id, []).append({'id': m.id, 'weekday': m.weekday, 'start_time': m.start_time,
                                                     'end_time': m.end_time, 'room': m.room})
    prerequisites = {}
    for sid, c in db.execute(select(SectionPrerequisite.section_id, Course).join(Course, SectionPrerequisite.required_course_id == Course.id).where(SectionPrerequisite.section_id.in_(ids))):
        prerequisites.setdefault(sid, []).append({'id': c.id, 'code': c.code, 'title': c.title})
    topics = {}
    course_ids = {course.id for _, course, *_ in rows}
    for cid, topic in db.execute(select(CourseTopic.course_id, Topic).join(Topic, CourseTopic.topic_id == Topic.id)
                                 .where(CourseTopic.course_id.in_(course_ids)).order_by(Topic.code)):
        topics.setdefault(cid, []).append({'code': topic.code, 'label': topic.label})
    at = now(db)
    result = {}
    for s, c, i, d, school, n in rows:
        result[s.id] = {'id': s.id, 'term_id': s.term_id, 'course_id': c.id, 'code': c.code, 'title': c.title,
                        'description': c.description, 'section_no': s.section_no, 'department_id': d.id,
                        'department_name': d.name, 'school_code': school.code, 'school_name': school.name,
                        'catalog_origin': c.catalog_origin, 'source_url': c.source_url, 'topics': topics.get(c.id, []),
                        'instructor_id': i.id, 'instructor_name': i.name,
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


def list_sections(db, term_id, q=None, department_id=None, instructor=None, weekday=None, page=1, page_size=20,
                  *, school_code=None, topic_code=None):
    stmt = select(Section.id).join(Course, Course.id == Section.course_id).join(Instructor, Instructor.id == Section.instructor_id).where(Section.term_id == term_id)
    if q and (q := q.strip()):
        stmt = stmt.where(or_(Course.code.icontains(q, autoescape=True), Course.title.icontains(q, autoescape=True),
                              exists(select(CourseAlias.course_id).where(
                                  CourseAlias.course_id == Course.id,
                                  CourseAlias.normalized_alias == normalize_name(q)))))
    if department_id:
        stmt = stmt.where(Course.department_id == department_id)
    if instructor and (instructor := instructor.strip()):
        stmt = stmt.where(Instructor.name.icontains(instructor, autoescape=True))
    if school_code:
        stmt = stmt.join(Department, Course.department_id == Department.id).join(
            School, School.id == Department.school_id).where(School.code == school_code)
    if topic_code:
        stmt = stmt.where(exists(select(CourseTopic.course_id).join(Topic, Topic.id == CourseTopic.topic_id)
                                 .where(CourseTopic.course_id == Course.id, Topic.code == topic_code)))
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
    return timetable_snapshot(db, student_term(db, student_id, term_id))


def timetable_snapshot(db, st):
    # Read both statuses together so publishing cannot move a section between reads.
    enrolled = select(Enrollment.section_id, Enrollment.status).where(
        Enrollment.student_id == st.student_id, Enrollment.term_id == st.term_id, Enrollment.status == 'ENROLLED')
    pending = select(Application.section_id, Application.status).where(
        Application.student_id == st.student_id, Application.term_id == st.term_id, Application.status == 'PENDING')
    selections = db.execute(enrolled.union_all(pending)).all()
    enrolled_ids = {section_id for section_id, status in selections if status == 'ENROLLED'}
    sections = section_rows(db, list({section_id for section_id, _ in selections}))
    sections.sort(key=lambda section: (section['code'], section['section_no']))
    items = [section for section in sections if section['id'] in enrolled_ids]
    pending_items = [section for section in sections if section['id'] not in enrolled_ids]
    return {'items': items, 'pending_items': pending_items,
            'total_credits': sum((s['credits'] for s in items), Decimal('0.0')),
            'credit_limit': st.credit_limit, 'as_of': now(db)}


@dataclass(frozen=True)
class AdmissionState:
    enrolled: list[dict]
    pending: list[dict]
    passed_courses: set[str]
    credit_limit: Decimal


def load_admission_state(db, st, term, *, include_pending=True):
    enrolled_ids = [row.section_id for row in active_enrollments(db, st.student_id, term.id)]
    pending_ids = list(db.scalars(select(Application.section_id).where(
        Application.student_id == st.student_id, Application.term_id == term.id,
        Application.status == 'PENDING',
    ))) if include_pending else []
    rows = {row['id']: row for row in section_rows(db, list(dict.fromkeys(enrolled_ids + pending_ids)))}
    passed_courses = set(db.scalars(select(CourseCompletion.course_id)
        .join(Term, Term.id == CourseCompletion.completed_term_id)
        .where(CourseCompletion.student_id == st.student_id, Term.end_date < term.start_date)))
    return AdmissionState([rows[sid] for sid in enrolled_ids], [rows[sid] for sid in pending_ids], passed_courses, st.credit_limit)


def baseline_credits(baseline, phase):
    rows = baseline.enrolled + (baseline.pending if phase == 'INITIAL' else [])
    return sum({row['id']: row['credits'] for row in rows}.values(), Decimal('0.0'))


def within_availability(meetings, availability):
    return not availability or all(any(
        meeting['weekday'] == slot.weekday and slot.start_time <= meeting['start_time'] and meeting['end_time'] <= slot.end_time
        for slot in availability
    ) for meeting in meetings)


def evaluate_admission(term_id, section, baseline, phase, planned=(), *, keep_existing_pending=False):
    """Use the same ordered constraints for one course, a draft, and a batch."""
    if section['term_id'] != term_id or not section['is_selectable']:
        return 'SECTION_UNAVAILABLE', None
    if any(row['course_id'] == section['course_id'] for row in baseline.enrolled):
        return 'ALREADY_ENROLLED', None
    pending = [row for row in baseline.pending if not keep_existing_pending or row['id'] != section['id']] if phase == 'INITIAL' else []
    if any(row['course_id'] == section['course_id'] for row in pending):
        return 'APPLICATION_EXISTS', None
    if any(row['course_id'] == section['course_id'] for row in planned):
        return ('APPLICATION_EXISTS' if phase == 'INITIAL' else 'ALREADY_ENROLLED'), None
    missing = {row['id'] for row in section['prerequisites']} - baseline.passed_courses
    if missing:
        return 'PREREQUISITE_MISSING', {'missing_course_ids': sorted(missing)}
    existing = {row['id']: row for row in [*baseline.enrolled, *pending, *planned]}
    conflicts = sorted({
        row['id'] for row in existing.values() for meeting in row['meetings'] for target in section['meetings']
        if target['weekday'] == meeting['weekday'] and target['start_time'] < meeting['end_time']
        and meeting['start_time'] < target['end_time']
    })
    if conflicts:
        return 'TIME_CONFLICT', {'conflicting_section_ids': conflicts}
    credits = section['credits'] + sum((row['credits'] for row in existing.values()), Decimal('0.0'))
    if credits > baseline.credit_limit:
        return 'CREDIT_LIMIT_EXCEEDED', {'total_credits': str(credits), 'credit_limit': str(baseline.credit_limit)}
    if phase != 'INITIAL' and section['remaining'] <= 0:
        return 'SECTION_FULL', None
    return None, None


def admission_error(db, st, term, section):
    """Check a formal enrollment under the rules used by allocation and add/drop."""
    if section.term_id != term.id or not section.is_selectable:
        return 'SECTION_UNAVAILABLE', None
    baseline = load_admission_state(db, st, term, include_pending=False)
    return evaluate_admission(term.id, section_dict(db, section), baseline, 'ADD_DROP')


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
    rows = list(db.scalars(select(Application).where(
        Application.student_id == student_id, Application.term_id == term_id, Application.status != 'CANCELLED',
    ).order_by(Application.submitted_at)))
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
    application = save_application(db, student_id, section, actor_user_id, now(db))
    return application_dict(db, application)


def save_application(db, student_id, section, actor_user_id, at, plan_id=None):
    term_id, section_id = section.term_id, section.id
    pending = db.scalar(select(Application).where(Application.student_id == student_id, Application.term_id == term_id,
                                                   Application.course_id == section.course_id, Application.status == 'PENDING'))
    if pending:
        if pending.section_id == section_id:
            if plan_id:
                audit(db, actor_user_id, term_id, 'APPLICATION_CONFIRMED', 'application', pending.id,
                      {'section_id': section_id, 'plan_id': plan_id})
            return pending
        raise DomainError(409, 'APPLICATION_EXISTS', '已登記同課程的其他班別，請先取消。')
    a = db.scalar(select(Application).where(Application.student_id == student_id, Application.term_id == term_id, Application.section_id == section_id))
    if a:
        a.status, a.submitted_at, a.decided_at, a.reason_code, a.allocation_run_id = 'PENDING', at, None, None, None
    else:
        a = Application(student_id=student_id, term_id=term_id, course_id=section.course_id, section_id=section_id,
                        status='PENDING', submitted_at=at)
        db.add(a)
    db.flush()
    payload = {'section_id': section_id, 'plan_id': plan_id} if plan_id else None
    audit(db, actor_user_id, term_id, 'APPLICATION_SUBMITTED', 'application', a.id, payload)
    return a


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


def request_fingerprint(method, path, body):
    payload = {'method': method, 'path': path, 'body': body}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def replay_response(db, student_id, term_id, request_key, fingerprint):
    old = db.get(IdempotencyKey, (student_id, term_id, request_key))
    if not old:
        return None
    if old.request_fingerprint != fingerprint:
        raise DomainError(409, 'IDEMPOTENCY_KEY_REUSED', '此重送鍵已用於另一個請求。')
    return old.response_status, old.response_body


def record_response(db, student_id, term_id, request_key, fingerprint, status, body):
    db.add(IdempotencyKey(student_id=student_id, term_id=term_id, request_key=request_key,
                          request_fingerprint=fingerprint, response_status=status, response_body=body))


def queue_timetable_email(db, student_id, term_id, user_id, request_key):
    term = get_term(db, term_id, 'share')
    st = student_term(db, student_id, term_id, True)
    fingerprint = request_fingerprint('POST', f'/api/v1/me/terms/{term_id}/timetable/email', {})
    replay = replay_response(db, student_id, term_id, request_key, fingerprint)
    if replay is not None:
        return replay

    timetable = timetable_snapshot(db, st)
    fields = ('code', 'title', 'section_no', 'credits', 'meetings')
    payload = {group: [{field: section[field] for field in fields} for section in timetable[group]]
               for group in ('items', 'pending_items')}
    payload.update(term_code=term.code, total_credits=timetable['total_credits'],
                   credit_limit=timetable['credit_limit'], url='/timetable?' + urlencode({'term': term_id}))
    notification_id = str(uuid4())
    db.add(NotificationOutbox(id=notification_id, event_key=f'timetable:{notification_id}', term_id=term_id,
                              recipient_user_id=user_id, type='TIMETABLE', payload=jsonable_encoder(payload)))
    body = {'notification_id': notification_id}
    record_response(db, student_id, term_id, request_key, fingerprint, 201, body)
    return 201, body


def audit_enrollment(db, actor_user_id, enrollment, action, plan_id=None, *, channel=None):
    payload = {'section_id': enrollment.section_id, 'source': 'ADD_DROP'}
    if plan_id:
        payload['plan_id'] = plan_id
    if channel:
        payload['channel'] = channel
    audit(db, actor_user_id, enrollment.term_id, action, 'enrollment', enrollment.id, payload)


def lock_add_drop_section(db, term, section_id):
    section = get_section(db, term.id, section_id, True)
    registration = registration_status(db, term)
    if registration['phase'] != 'ADD_DROP':
        raise DomainError(409, 'REGISTRATION_CLOSED', REASONS['REGISTRATION_CLOSED'],
                          {'registration': registration, 'section_id': section_id})
    return section


def add_enrollment(db, term, st, section_id, actor_user_id, *, channel=None):
    section = lock_add_drop_section(db, term, section_id)
    code, details = admission_error(db, st, term, section)
    if code:
        raise DomainError(409, code, REASONS[code], details)
    enrollment = enroll(db, st.student_id, term.id, section, 'ADD_DROP')
    audit_enrollment(db, actor_user_id, enrollment, 'ENROLLED', channel=channel)
    total_credits = db.scalar(select(func.coalesce(func.sum(Section.credits), Decimal('0.0')))
        .join(Enrollment, Enrollment.section_id == Section.id).where(
            Enrollment.student_id == st.student_id, Enrollment.term_id == term.id,
            Enrollment.status == 'ENROLLED'))
    return enrollment, section_dict(db, section), total_credits


def enrollment_change(db, student_id, term_id, section_id, actor_user_id, request_key, method, path):
    term = get_term(db, term_id, 'share')
    st = student_term(db, student_id, term_id, True)
    fingerprint = request_fingerprint(method, path, {'section_id': section_id} if method == 'POST' else {})
    replay = replay_response(db, student_id, term_id, request_key, fingerprint)
    if replay is not None:
        return replay
    if method == 'POST':
        e, details, total_credits = add_enrollment(db, term, st, section_id, actor_user_id)
        status = 201
        body = jsonable_encoder({'enrollment': {'id': e.id, 'section_id': section_id, 'status': e.status, 'source': e.source},
                                  'remaining': details['remaining'], 'total_credits': total_credits,
                                  'as_of': now(db)})
    else:
        lock_add_drop_section(db, term, section_id)
        e = db.scalar(select(Enrollment).where(Enrollment.student_id == student_id, Enrollment.term_id == term_id,
                                              Enrollment.section_id == section_id, Enrollment.status == 'ENROLLED'))
        if not e:
            raise DomainError(404, 'ENROLLMENT_NOT_FOUND', '找不到此班別的有效選課。')
        e.status, e.updated_at = 'DROPPED', now(db)
        db.flush()
        status, body = 204, None
        audit_enrollment(db, actor_user_id, e, 'DROPPED')
    record_response(db, student_id, term_id, request_key, fingerprint, status, body)
    db.flush()
    return status, body


def confirm_selection_plan(db, student_id, term_id, plan_id, section_ids, actor_user_id, request_key):
    """Revalidate and submit the selected part of an owned draft atomically."""
    try:
        term = get_term(db, term_id, 'share')
        st = student_term(db, student_id, term_id, True)
    except DomainError as error:
        if error.code in ('TERM_NOT_FOUND', 'TERM_INELIGIBLE'):
            raise DomainError(404, 'PLAN_NOT_FOUND', '找不到此學期的本人試排。') from None
        raise
    plan = db.scalar(select(SelectionPlan).where(
        SelectionPlan.id == plan_id, SelectionPlan.student_id == student_id, SelectionPlan.term_id == term_id,
    ).with_for_update().execution_options(populate_existing=True))
    if plan is None:
        raise DomainError(404, 'PLAN_NOT_FOUND', '找不到此學期的本人試排。')

    ordered_ids = sorted(section_ids)
    path = f'/api/v1/me/terms/{term_id}/selection-plans/{plan_id}/confirm'
    fingerprint = request_fingerprint('POST', path, {'section_ids': ordered_ids})
    replay = replay_response(db, student_id, term_id, request_key, fingerprint)
    if replay is not None:
        return replay
    if plan.status == 'SUBMITTED':
        raise DomainError(409, 'PLAN_ALREADY_SUBMITTED', '這份試排已確認。')
    items = list(db.scalars(select(SelectionPlanItem).where(
        SelectionPlanItem.plan_id == plan_id, SelectionPlanItem.section_id.in_(ordered_ids),
    ).order_by(SelectionPlanItem.position)))
    if len(items) != len(ordered_ids):
        raise DomainError(409, 'PLAN_SELECTION_MISMATCH', '只能確認試排中的班別。')

    # Lock in one consistent order across students. Validate and reply in plan order.
    sections = {section_id: get_section(db, term_id, section_id, True) for section_id in ordered_ids}
    at = now(db)
    current_phase = phase(db, term, at)
    if current_phase not in ('INITIAL', 'ADD_DROP'):
        raise DomainError(409, 'REGISTRATION_CLOSED', REASONS['REGISTRATION_CLOSED'])
    if current_phase != plan.phase or at >= plan.expires_at:
        raise DomainError(409, 'PLAN_EXPIRED', '試排已過期或選課階段已切換，請重新試排。')

    baseline = load_admission_state(db, st, term, include_pending=(current_phase == 'INITIAL'))
    details_by_id = {row['id']: row for row in section_rows(db, ordered_ids)}
    validated = []
    for item in items:
        details = details_by_id[item.section_id]
        code, details_error = evaluate_admission(
            term_id, details, baseline, current_phase, validated, keep_existing_pending=True,
        )
        if code:
            raise DomainError(409, code, REASONS[code], {'section_id': item.section_id, **(details_error or {})})
        validated.append(details)

    results = []
    for details in validated:
        section = sections[details['id']]
        if current_phase == 'INITIAL':
            application = save_application(db, student_id, section, actor_user_id, at, plan_id)
            status = application.status
        else:
            enrollment = enroll(db, student_id, term_id, section, 'ADD_DROP')
            audit_enrollment(db, actor_user_id, enrollment, 'ENROLLED', plan_id)
            status = enrollment.status
        results.append({'section_id': section.id, 'status': status})

    plan.status, plan.confirmed_at = 'SUBMITTED', at
    body = jsonable_encoder({'plan_id': plan_id, 'phase': current_phase, 'items': results, 'submitted_at': at})
    record_response(db, student_id, term_id, request_key, fingerprint, 200, body)
    db.flush()
    return 200, body


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
                                  type='ALLOCATION', payload={'term_code': term.code, 'run_id': run.id, 'results': results,
                                  'url': '/applications?' + urlencode({'term': term_id})}))
    # Use the actual final publication instant; all result rows become visible together at commit.
    run.published_at = now(db)
    audit(db, actor_user_id, term_id, 'ALLOCATION_PUBLISHED', 'allocation_run', run.id, run_dict(run))
    db.flush()
    return run_dict(run)
