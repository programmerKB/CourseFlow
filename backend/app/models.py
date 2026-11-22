"""PostgreSQL schema: relational constraints remain active for every writer."""
from uuid import uuid4
from sqlalchemy import (Column, String, Text, Integer, Boolean, Numeric, Date, DateTime, Time,
                        ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Index, func, text)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .db import Base


def ident():
    return Column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))


def ref(table, **kw):
    return Column(UUID(as_uuid=False), ForeignKey(table + '.id', ondelete='RESTRICT'), nullable=False, **kw)


def stamp(**kw):
    return Column(DateTime(timezone=True), nullable=False, server_default=func.now(), **kw)


class User(Base):
    __tablename__ = 'users'
    id = ident()
    login = Column(String(255), unique=True, nullable=False)
    email = Column(String(320), nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(16), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text('true'))
    display_name = Column(String(255), nullable=False)
    __table_args__ = (CheckConstraint("login = lower(btrim(login)) AND login <> ''", name='ck_login_normalized'),
                      CheckConstraint("role IN ('STUDENT','ADMIN')", name='ck_user_role'))


class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    token_hash = Column(String(64), primary_key=True)
    user_id = ref('users', index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))


class Department(Base):
    __tablename__ = 'departments'
    id = ident()
    code = Column(String(64), unique=True, nullable=False)
    name = Column(String(255), nullable=False)


class Student(Base):
    __tablename__ = 'students'
    id = ident()
    user_id = ref('users', unique=True)
    student_no = Column(String(64), unique=True, nullable=False)
    department_id = ref('departments')
    name = Column(String(255), nullable=False)


class Instructor(Base):
    __tablename__ = 'instructors'
    id = ident()
    name = Column(String(255), nullable=False)
    department_id = ref('departments')


class Term(Base):
    __tablename__ = 'terms'
    id = ident()
    code = Column(String(64), unique=True, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    catalog_published_at = Column(DateTime(timezone=True))
    allocation_seed = Column(Text, nullable=False)
    __table_args__ = (CheckConstraint('start_date <= end_date', name='ck_term_dates'),)


class RegistrationPeriod(Base):
    __tablename__ = 'registration_periods'
    term_id = ref('terms', primary_key=True)
    kind = Column(String(16), primary_key=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("kind IN ('INITIAL','ADD_DROP')", name='ck_period_kind'),
                      CheckConstraint('starts_at < ends_at', name='ck_period_order'))


class StudentTerm(Base):
    __tablename__ = 'student_terms'
    student_id = ref('students', primary_key=True)
    term_id = ref('terms', primary_key=True)
    credit_limit = Column(Numeric(4, 1), nullable=False)
    __table_args__ = (CheckConstraint('credit_limit >= 0', name='ck_credit_limit'),)


class Course(Base):
    __tablename__ = 'courses'
    id = ident()
    code = Column(String(64), unique=True, nullable=False)
    department_id = ref('departments')
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False, server_default='')


class Topic(Base):
    __tablename__ = 'topics'
    id = ident()
    code = Column(String(64), nullable=False, unique=True)
    label = Column(String(100), nullable=False)


class CourseTopic(Base):
    __tablename__ = 'course_topics'
    course_id = ref('courses', primary_key=True)
    topic_id = ref('topics', primary_key=True)
    __table_args__ = (Index('ix_course_topics_topic_course', 'topic_id', 'course_id'),)


class Section(Base):
    __tablename__ = 'sections'
    id = ident()
    term_id = ref('terms')
    course_id = ref('courses')
    section_no = Column(String(32), nullable=False)
    instructor_id = ref('instructors')
    credits = Column(Numeric(4, 1), nullable=False)
    capacity = Column(Integer, nullable=False)
    is_selectable = Column(Boolean, nullable=False, server_default=text('true'))
    __table_args__ = (UniqueConstraint('term_id', 'course_id', 'section_no', name='uq_section_number'),
                      UniqueConstraint('id', 'term_id', 'course_id', name='uq_section_identity'),
                      CheckConstraint('credits > 0', name='ck_section_credits'),
                      CheckConstraint('capacity >= 0', name='ck_section_capacity'), Index('ix_sections_term', 'term_id'))


class SectionMeeting(Base):
    __tablename__ = 'section_meetings'
    id = ident()
    section_id = ref('sections')
    weekday = Column(Integer, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    room = Column(String(255), nullable=False, server_default='')
    __table_args__ = (UniqueConstraint('section_id', 'weekday', 'start_time', 'end_time', name='uq_meeting'),
                      CheckConstraint('weekday BETWEEN 1 AND 7', name='ck_meeting_day'),
                      CheckConstraint('start_time < end_time', name='ck_meeting_time'))


class SectionPrerequisite(Base):
    __tablename__ = 'section_prerequisites'
    section_id = ref('sections', primary_key=True)
    required_course_id = ref('courses', primary_key=True)


class CourseCompletion(Base):
    __tablename__ = 'course_completions'
    student_id = ref('students', primary_key=True)
    course_id = ref('courses', primary_key=True)
    completed_term_id = ref('terms', primary_key=True)


class AllocationRun(Base):
    __tablename__ = 'allocation_runs'
    id = ident()
    term_id = ref('terms', unique=True)
    algorithm_version = Column(String(64), nullable=False)
    seed = Column(Text, nullable=False)
    published_at = stamp()
    accepted_count = Column(Integer, nullable=False, server_default='0')
    rejected_count = Column(Integer, nullable=False, server_default='0')
    __table_args__ = (UniqueConstraint('term_id', 'id', name='uq_run_term'),
                      CheckConstraint('accepted_count >= 0 AND rejected_count >= 0', name='ck_run_counts'))


def student_section_constraints(name):
    return (ForeignKeyConstraint(['student_id', 'term_id'], ['student_terms.student_id', 'student_terms.term_id'], ondelete='RESTRICT', name=f'fk_{name}_student_term'),
            ForeignKeyConstraint(['section_id', 'term_id', 'course_id'], ['sections.id', 'sections.term_id', 'sections.course_id'], ondelete='RESTRICT', name=f'fk_{name}_section'),
            UniqueConstraint('student_id', 'term_id', 'section_id', name=f'uq_{name}_section'))


class Application(Base):
    __tablename__ = 'applications'
    id = ident()
    student_id = Column(UUID(as_uuid=False), nullable=False)
    term_id = Column(UUID(as_uuid=False), nullable=False)
    course_id = Column(UUID(as_uuid=False), nullable=False)
    section_id = Column(UUID(as_uuid=False), nullable=False)
    status = Column(String(16), nullable=False, server_default='PENDING')
    submitted_at = stamp()
    decided_at = Column(DateTime(timezone=True))
    reason_code = Column(String(64))
    allocation_run_id = Column(UUID(as_uuid=False))
    __table_args__ = (*student_section_constraints('application'),
                      ForeignKeyConstraint(['term_id', 'allocation_run_id'], ['allocation_runs.term_id', 'allocation_runs.id'], ondelete='RESTRICT', name='fk_application_run'),
                      CheckConstraint("status IN ('PENDING','CANCELLED','ACCEPTED','REJECTED')", name='ck_application_status'),
                      Index('uq_pending_course', 'student_id', 'term_id', 'course_id', unique=True, postgresql_where=text("status = 'PENDING'")),
                      Index('ix_pending_term', 'term_id', postgresql_where=text("status = 'PENDING'")))


class Enrollment(Base):
    __tablename__ = 'enrollments'
    id = ident()
    student_id = Column(UUID(as_uuid=False), nullable=False)
    term_id = Column(UUID(as_uuid=False), nullable=False)
    course_id = Column(UUID(as_uuid=False), nullable=False)
    section_id = Column(UUID(as_uuid=False), nullable=False)
    status = Column(String(16), nullable=False)
    source = Column(String(16), nullable=False)
    updated_at = stamp()
    __table_args__ = (*student_section_constraints('enrollment'),
                      CheckConstraint("status IN ('ENROLLED','DROPPED')", name='ck_enrollment_status'),
                      CheckConstraint("source IN ('INITIAL','ADD_DROP')", name='ck_enrollment_source'),
                      Index('uq_enrolled_course', 'student_id', 'term_id', 'course_id', unique=True, postgresql_where=text("status = 'ENROLLED'")),
                      Index('ix_enrolled_section', 'section_id', postgresql_where=text("status = 'ENROLLED'")))


class SelectionPlan(Base):
    __tablename__ = 'selection_plans'
    id = ident()
    student_id = Column(UUID(as_uuid=False), nullable=False)
    term_id = Column(UUID(as_uuid=False), nullable=False)
    phase = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, server_default='DRAFT')
    created_at = stamp()
    expires_at = Column(DateTime(timezone=True), nullable=False)
    confirmed_at = Column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['student_id', 'term_id'], ['student_terms.student_id', 'student_terms.term_id'],
                             ondelete='RESTRICT', name='fk_plan_student_term'),
        UniqueConstraint('id', 'term_id', name='uq_plan_term'),
        CheckConstraint("phase IN ('INITIAL','ADD_DROP')", name='ck_plan_phase'),
        CheckConstraint("status IN ('DRAFT','SUBMITTED')", name='ck_plan_status'),
        CheckConstraint('created_at < expires_at', name='ck_plan_expiry'),
        CheckConstraint("(status = 'SUBMITTED') = (confirmed_at IS NOT NULL)", name='ck_plan_confirmation'),
        Index('ix_plan_student_term', 'student_id', 'term_id'),
    )


class SelectionPlanItem(Base):
    __tablename__ = 'selection_plan_items'
    plan_id = Column(UUID(as_uuid=False), primary_key=True)
    section_id = Column(UUID(as_uuid=False), primary_key=True)
    term_id = Column(UUID(as_uuid=False), nullable=False)
    course_id = Column(UUID(as_uuid=False), nullable=False)
    position = Column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(['plan_id', 'term_id'], ['selection_plans.id', 'selection_plans.term_id'],
                             ondelete='RESTRICT', name='fk_plan_item_plan'),
        ForeignKeyConstraint(['section_id', 'term_id', 'course_id'], ['sections.id', 'sections.term_id', 'sections.course_id'],
                             ondelete='RESTRICT', name='fk_plan_item_section'),
        UniqueConstraint('plan_id', 'position', name='uq_plan_item_position'),
        UniqueConstraint('plan_id', 'course_id', name='uq_plan_item_course'),
        CheckConstraint('position BETWEEN 1 AND 5', name='ck_plan_item_position'),
    )


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id = ident()
    actor_user_id = Column(UUID(as_uuid=False), ForeignKey('users.id', ondelete='RESTRICT'))
    term_id = ref('terms')
    action = Column(String(128), nullable=False)
    target_type = Column(String(64), nullable=False)
    target_id = Column(UUID(as_uuid=False))
    occurred_at = stamp()
    payload = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class IdempotencyKey(Base):
    __tablename__ = 'idempotency_keys'
    student_id = Column(UUID(as_uuid=False), primary_key=True)
    term_id = Column(UUID(as_uuid=False), primary_key=True)
    request_key = Column(String(200), primary_key=True)
    request_fingerprint = Column(String(64), nullable=False)
    response_status = Column(Integer, nullable=False)
    response_body = Column(JSONB, nullable=False)
    created_at = stamp()
    __table_args__ = (ForeignKeyConstraint(['student_id', 'term_id'], ['student_terms.student_id', 'student_terms.term_id'], ondelete='RESTRICT'),
                      CheckConstraint('response_status IN (200,201,204)', name='ck_idempotency_success'))


class NotificationOutbox(Base):
    __tablename__ = 'notification_outbox'
    id = ident()
    event_key = Column(String(255), nullable=False, unique=True)
    term_id = ref('terms')
    recipient_user_id = ref('users')
    type = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    status = Column(String(16), nullable=False, server_default='PENDING')
    attempt_count = Column(Integer, nullable=False, server_default='0')
    next_attempt_at = stamp()
    lease_until = Column(DateTime(timezone=True))
    claim_token = Column(UUID(as_uuid=False))
    sent_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    __table_args__ = (CheckConstraint("status IN ('PENDING','PROCESSING','SENT','FAILED')", name='ck_outbox_status'),
                      CheckConstraint('attempt_count BETWEEN 0 AND 3', name='ck_outbox_attempts'),
                      Index('ix_outbox_poll', 'status', 'next_attempt_at'))
