from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app import registration as reg
from app.config import settings
from app.db import SessionLocal
from app.models import (AllocationRun, Application, AuditEvent, Course, CourseCompletion, Enrollment,
                        RegistrationPeriod, Section, StudentTerm, Term)
from app.seed import seed, uid


MAIN = uid('term/main')
MARKER = uid('run/main')


def empty_database():
    # conftest permits only a disposable *_test database.
    with SessionLocal.begin() as db:
        db.execute(text('TRUNCATE users, departments, terms, courses RESTART IDENTITY CASCADE'))


def windows(db):
    return {p.kind: (p.starts_at, p.ends_at) for p in db.scalars(select(RegistrationPeriod).where(
        RegistrationPeriod.term_id == MAIN))}


def test_fresh_scoped_seed_has_one_public_term_and_main_recommendation_catalog(monkeypatch):
    empty_database()
    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    seed('initial', recommendation_demo=True)
    with SessionLocal() as db:
        terms = {term.id: term for term in db.scalars(select(Term))}
        history_id = uid('term/history')
        assert set(terms) == {MAIN, history_id}
        assert terms[MAIN].code.startswith('115-1') and terms[MAIN].catalog_published_at
        assert terms[history_id].catalog_published_at is None
        assert db.get(CourseCompletion, (uid('student/student01'), uid('course/CS201'), history_id))
        assert db.scalar(select(AllocationRun).where(AllocationRun.term_id == MAIN)) is None
        start, end = windows(db)['INITIAL']
        assert start <= datetime.now(timezone.utc) < end
        courses = list(db.scalars(select(Course.code).join(Section, Section.course_id == Course.id)
                                  .where(Section.term_id == MAIN)))
        assert {code for code in courses if code.startswith('DEMO-')} == {f'DEMO-{n}' for n in range(501, 506)}
        assert courses.count('CS101') == 2
        # Adding recommendation sections cannot overwrite a student's main-term limit or enroll them.
        assert db.get(StudentTerm, (uid('student/student01'), MAIN)).credit_limit == 25
        assert db.scalar(select(Enrollment).where(Enrollment.student_id == uid('student/student01'))) is None


def test_scoped_seed_reopens_old_demo_once_and_preserves_records(monkeypatch):
    empty_database()
    seed('add-drop', recommendation_demo=True)
    student = uid('student/student01')
    enrolled_id = uid('test/real-enrollment')
    application_id = uid('test/pending-application')
    with SessionLocal.begin() as db:
        before = windows(db)
        assert db.get(AllocationRun, MARKER) is not None
        term_ids = set(db.scalars(select(Term.id)))
        db.add(Enrollment(id=enrolled_id, student_id=student, term_id=MAIN, course_id=uid('course/CS101'),
                          section_id=uid('section/main/CS101/A'), status='ENROLLED', source='ADD_DROP'))
        db.add(Application(id=application_id, student_id=student, term_id=MAIN, course_id=uid('course/CS303'),
                           section_id=uid('section/main/CS303/A'), status='PENDING'))

    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    assert seed('initial', recommendation_demo=True)['main_initial_reopened'] is True
    with SessionLocal() as db:
        after = windows(db)
        start, end = after['INITIAL']
        assert start <= datetime.now(timezone.utc) < end
        assert end - start == timedelta(days=7)
        assert end <= after['ADD_DROP'][0]
        assert after != before
        assert db.get(AllocationRun, MARKER) is None
        assert db.get(Enrollment, enrolled_id).status == 'ENROLLED'
        assert db.get(Application, application_id).status == 'PENDING'
        assert set(db.scalars(select(Term.id))) == term_ids
        assert db.get(Section, uid('section/main/DEMO-501/A')) is not None
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'DEMO_INITIAL_REOPENED'))
        assert event.target_id == MARKER and event.actor_user_id is None
        assert event.payload['previous_periods'][0]['kind'] == 'INITIAL'
        assert datetime.fromisoformat(event.payload['previous_periods'][0]['ends_at']) == before['INITIAL'][1]

    assert seed('initial', recommendation_demo=True)['main_initial_reopened'] is False
    with SessionLocal() as db:
        assert windows(db) == after
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'DEMO_INITIAL_REOPENED')))) == 1

    monkeypatch.setattr(reg, 'now', lambda db: end + timedelta(seconds=1))
    with SessionLocal.begin() as db:
        term = db.get(Term, MAIN)
        assert reg.phase(db, term) == 'AWAITING_ALLOCATION'
        result = reg.allocate(db, MAIN, uid('user/admin'))
        assert result['run_id'] != MARKER
        assert result['accepted_count'] == 1
        assert db.get(Application, application_id).status == 'ACCEPTED'
        assert db.get(Enrollment, enrolled_id).status == 'ENROLLED'
        assert reg.phase(db, term) == 'RESULTS'


@pytest.mark.parametrize('evidence', ['result', 'publication', 'changed_schedule'])
def test_scoped_seed_does_not_reset_real_or_modified_allocation(monkeypatch, evidence):
    empty_database()
    seed('add-drop')
    with SessionLocal.begin() as db:
        if evidence == 'result':
            db.add(Application(student_id=uid('student/student01'), term_id=MAIN, course_id=uid('course/CS101'),
                               section_id=uid('section/main/CS101/A'), status='ACCEPTED', allocation_run_id=MARKER))
        elif evidence == 'publication':
            db.add(AuditEvent(actor_user_id=uid('user/admin'), term_id=MAIN, action='ALLOCATION_PUBLISHED',
                              target_type='allocation_run', target_id=MARKER))
        else:
            period = db.get(RegistrationPeriod, (MAIN, 'INITIAL'))
            period.ends_at += timedelta(hours=1)
        db.flush()
        before = windows(db)

    monkeypatch.setattr(settings, 'public_initial_term_id', MAIN)
    assert seed('initial')['main_initial_reopened'] is False
    with SessionLocal() as db:
        assert db.get(AllocationRun, MARKER) is not None
        assert windows(db) == before
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'DEMO_INITIAL_REOPENED')) is None
