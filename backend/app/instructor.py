"""Instructor rosters and grades (FR-25, FR-26, FR-27).

Every query is scoped by the signed-in instructor's ID. A section taught by someone else,
or an enrollment outside the instructor's section, is reported as not found (404) so that
other classes are not disclosed. Grade writes lock in the shared order: term (share), then
the enrollment row, then the grade row.
"""
from decimal import Decimal

from sqlalchemy import select, func

from .errors import DomainError
from .models import Course, Enrollment, Grade, Section, SectionMeeting, Student, Department, School
from .registration import audit, get_term, now

SCORE_STEP = Decimal('0.01')


def owned_section(db, instructor_id, term_id, section_id, *, lock=False):
    stmt = select(Section).where(Section.id == section_id, Section.term_id == term_id,
                                 Section.instructor_id == instructor_id)
    if lock:
        stmt = stmt.with_for_update(read=True)
    section = db.scalar(stmt.execution_options(populate_existing=True))
    if section is None:
        raise DomainError(404, 'SECTION_NOT_FOUND', '找不到你在此學期授課的班別。')
    return section


def section_summaries(db, section_ids):
    if not section_ids:
        return []
    counts = (select(Enrollment.section_id, func.count().label('n'))
              .where(Enrollment.status == 'ENROLLED', Enrollment.section_id.in_(section_ids))
              .group_by(Enrollment.section_id).subquery())
    rows = db.execute(
        select(Section, Course, Department, School, func.coalesce(counts.c.n, 0))
        .join(Course, Course.id == Section.course_id)
        .join(Department, Department.id == Course.department_id)
        .join(School, School.id == Department.school_id)
        .outerjoin(counts, counts.c.section_id == Section.id)
        .where(Section.id.in_(section_ids))
        .order_by(Course.code, Section.section_no, Section.id)).all()
    meetings = {}
    for meeting in db.scalars(select(SectionMeeting).where(SectionMeeting.section_id.in_(section_ids))
                              .order_by(SectionMeeting.weekday, SectionMeeting.start_time)):
        meetings.setdefault(meeting.section_id, []).append({
            'weekday': meeting.weekday, 'start_time': meeting.start_time, 'end_time': meeting.end_time,
            'room': meeting.room})
    return [{'section_id': section.id, 'term_id': section.term_id, 'course_id': course.id, 'code': course.code,
             'title': course.title, 'section_no': section.section_no, 'credits': section.credits,
             'capacity': section.capacity, 'enrolled_count': enrolled, 'school_name': school.name,
             'department_name': department.name, 'meetings': meetings.get(section.id, [])}
            for section, course, department, school, enrolled in rows]


def teaching_sections(db, instructor_id, term_id):
    term = get_term(db, term_id)
    ids = list(db.scalars(select(Section.id).where(Section.term_id == term.id, Section.instructor_id == instructor_id)))
    return {'term_id': term.id, 'term_code': term.code, 'items': section_summaries(db, ids), 'as_of': now(db)}


def roster(db, instructor_id, term_id, section_id):
    term = get_term(db, term_id)
    section = owned_section(db, instructor_id, term.id, section_id)
    # One query reads active enrollments and their current score (NULL until graded).
    rows = db.execute(
        select(Enrollment.id, Student.student_no, Student.name, Grade.score, Grade.updated_at)
        .join(Student, Student.id == Enrollment.student_id)
        .outerjoin(Grade, Grade.enrollment_id == Enrollment.id)
        .where(Enrollment.section_id == section.id, Enrollment.term_id == term.id, Enrollment.status == 'ENROLLED')
        .order_by(Student.student_no)).all()
    return {'section': section_summaries(db, [section.id])[0],
            'items': [{'enrollment_id': enrollment_id, 'student_no': student_no, 'student_name': name,
                       'score': None if score is None else str(score), 'graded_at': graded_at}
                      for enrollment_id, student_no, name, score, graded_at in rows],
            'as_of': now(db)}


def set_grade(db, user, term_id, section_id, enrollment_id, score):
    """Assign or update one current score; an identical resend returns the stored grade."""
    score = Decimal(score).quantize(SCORE_STEP)
    term = get_term(db, term_id, 'share')
    section = owned_section(db, user['instructor_id'], term.id, section_id, lock=True)
    enrollment = db.scalar(select(Enrollment).where(
        Enrollment.id == enrollment_id, Enrollment.section_id == section.id, Enrollment.term_id == term.id,
    ).with_for_update().execution_options(populate_existing=True))
    if enrollment is None or enrollment.status != 'ENROLLED':
        raise DomainError(404, 'ENROLLMENT_NOT_FOUND', '找不到此班別的有效選課學生。')
    grade = db.scalar(select(Grade).where(Grade.enrollment_id == enrollment.id).with_for_update()
                      .execution_options(populate_existing=True))
    at = now(db)
    previous = grade.score if grade else None
    if grade is not None and grade.score == score:
        return grade_dict(grade, changed=False)
    if grade is None:
        grade = Grade(enrollment_id=enrollment.id, score=score, graded_by_user_id=user['id'], created_at=at, updated_at=at)
        db.add(grade)
    else:
        grade.score, grade.graded_by_user_id, grade.updated_at = score, user['id'], at
    db.flush()
    course = db.get(Course, section.course_id)
    audit(db, user['id'], term.id, 'GRADE_ASSIGNED' if previous is None else 'GRADE_UPDATED', 'enrollment',
          enrollment.id, {'instructor_id': user['instructor_id'], 'section_id': section.id,
                          'course_code': course.code, 'section_no': section.section_no,
                          'student_id': enrollment.student_id,
                          'previous_score': None if previous is None else str(previous), 'score': str(score)})
    return grade_dict(grade, changed=True)


def grade_dict(grade, *, changed):
    return {'enrollment_id': grade.enrollment_id, 'score': str(grade.score), 'graded_at': grade.updated_at,
            'changed': changed}
