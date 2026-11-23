"""Match vetted curriculum paths to public sections and the student's own progress."""
from collections import defaultdict
import re
from unicodedata import normalize

from sqlalchemy import select

from . import registration as reg
from .catalog_data import COURSE_GROUPS, DEPARTMENTS, SCHOOLS, normalize_name
from .chat_sources import source
from .models import Course, CourseCompletion, Department, School, Section, Term
from .roadmap_data import REFERENCES, ROADMAP_VERSION, TRACKS


PLAN_REQUEST = re.compile(r'試排|排課表|代選|自動(?:選課|排課)|(?:幫我|替我)(?:自動)?(?:挑|選|安排)')
QUICK_PLAN_REQUEST = re.compile(
    r'(?:請|再|麻煩|幫我|替我)*(?:試排|排課表|自動選課|自動排課|選課)'
    r'(?:這個方向的)?(?:本期(?:的?課程|符合條件的班別)?)?'
)
DIRECTION_REQUEST = re.compile(r'進階課|基礎課|修課路徑|研究路徑|研究專題|先修什麼|修課建議')
BACKGROUND = re.compile(r'(?:我)?(?:已經)?(?:修過|學過|通過|熟悉)[^,，。？?]+[,，。]')
LATIN = re.compile(r'[a-z0-9 _-]+')


def alias_pattern(alias):
    alias = normalize('NFKC', alias).casefold()
    if LATIN.fullmatch(alias):
        return re.compile(r'(?<![a-z0-9])' + r'\s*'.join(map(re.escape, alias.split())) + r'(?![a-z0-9])')
    return re.compile(re.escape(normalize_name(alias)))


TRACK_ALIASES = {
    code: [(alias_pattern(alias), bool(LATIN.fullmatch(normalize('NFKC', alias).casefold())))
           for alias in (track.title, code, *track.aliases)]
    for code, track in TRACKS.items()
}
CATALOG_REFERENCES = {
    f'{group}-{number:03d}': (title, group, url)
    for group, rows in COURSE_GROUPS.items() for number, (title, _, url) in enumerate(rows, 1)
}


def wants_plan(message):
    negated = re.search(r'(?:不用|不要|不必|無需|不需要)\s*(?:幫我|替我)?\s*'
                        r'(?:試排|排課表|代選|(?:自動)?選課|自動排課)', message)
    return bool(PLAN_REQUEST.search(message) and not negated)


def is_quick_plan_request(message):
    return QUICK_PLAN_REQUEST.fullmatch(normalize_name(message).rstrip('。.!?')) is not None


def identify_tracks(message):
    message = BACKGROUND.sub('', normalize('NFKC', message).casefold())
    compact = normalize_name(message)
    exact = [code for code, track in TRACKS.items() if compact in {
        normalize_name(track.title), normalize_name(code), *(normalize_name(alias) for alias in track.aliases),
    }]
    if exact:
        return exact
    matches = [code for code, aliases in TRACK_ALIASES.items()
               if any(pattern.search(message if latin else compact) for pattern, latin in aliases)]
    if 'ic設計' in compact and not {'digital_ic', 'analog_ic', 'photonics'}.intersection(matches):
        matches.extend(('digital_ic', 'analog_ic'))
    if re.search(r'(?<![a-z0-9])nvm(?![a-z0-9])', message) and not {'memory_systems', 'semiconductor_process'}.intersection(matches):
        matches.extend(('memory_systems', 'semiconductor_process'))
    return matches


def mentioned_school(message):
    compact = normalize_name(message)
    mentioned = {code for code, names in SCHOOLS.items() for name in names if normalize_name(name) in compact}
    mentioned.update(code for code in SCHOOLS if re.search(
        rf'(?<![a-z]){code.lower()}(?![a-z-])', normalize('NFKC', message).casefold()))
    return next(iter(mentioned)) if len(mentioned) == 1 else None


def passed_courses(db, student_id, term=None):
    query = select(CourseCompletion.course_id).join(Term, Term.id == CourseCompletion.completed_term_id).where(
        CourseCompletion.student_id == student_id)
    if term:
        query = query.where(Term.end_date < term.start_date)
    return set(db.scalars(query))


def build_roadmap(db, student_id, code, *, term=None, baseline=None, phase=None, school=None, availability=()):
    track = TRACKS[code]
    course_codes = [key for key in (*track.foundation, *track.specialization) if key not in REFERENCES]
    rows = db.execute(
        select(Course, Department, School).join(Department, Course.department_id == Department.id)
        .join(School, Department.school_id == School.id).where(Course.code.in_(course_codes))
    )
    courses = {course.code: (course, department, school_row) for course, department, school_row in rows}
    passed = baseline.passed_courses if baseline else passed_courses(db, student_id, term)
    sections_by_course = defaultdict(list)
    if term and courses:
        ids = list(db.scalars(select(Section.id).join(Course, Course.id == Section.course_id).where(
            Section.term_id == term.id, Course.code.in_(course_codes),
        ).order_by(Course.code, Section.section_no, Section.id)))
        for section in reg.section_rows(db, ids):
            reason, _ = reg.evaluate_admission(
                term.id, section, baseline, 'INITIAL' if phase == 'INITIAL' else 'ADD_DROP',
            ) if baseline else ('TERM_INELIGIBLE', None)
            if reason is None and not reg.within_availability(section['meetings'], availability):
                reason = 'TIME_PREFERENCE_MISMATCH'
            if reason is None and phase not in ('INITIAL', 'ADD_DROP'):
                reason = 'REGISTRATION_CLOSED'
            sections_by_course[section['code']].append({
                'section_id': section['id'], 'section_no': section['section_no'], 'school_code': section['school_code'],
                'remaining': section['remaining'], 'meetings': section['meetings'],
                'is_selectable': section['is_selectable'], 'eligibility': {'eligible': reason is None, 'reason_code': reason},
                'source': source(term.id, section['id'], f'{section["title"]} {section["section_no"]} 班'),
            })

    def entries(keys):
        result = []
        for key in keys:
            if key not in courses and (key in REFERENCES or key in CATALOG_REFERENCES):
                if key in REFERENCES:
                    item = REFERENCES[key]
                    title, group, url = item.title, item.department, item.source_url
                else:
                    title, group, url = CATALOG_REFERENCES[key]
                campus = group.split('-')[0]
                result.append({
                    'title': title, 'school_code': campus, 'school_name': SCHOOLS[campus][0],
                    'department_name': DEPARTMENTS[group][1], 'source_url': url,
                    'completed': None, 'availability': 'CURRICULUM_REFERENCE', 'sections': [],
                })
            elif key in courses:
                course, department, school_row = courses[key]
                sections = sections_by_course[key]
                result.append({
                    'title': course.title, 'course_code': course.code, 'department_name': department.name,
                    'school_code': school_row.code, 'school_name': school_row.name, 'source_url': course.source_url,
                    'completed': course.id in passed, 'availability': (
                        'OFFERED' if sections else 'NO_PUBLIC_SECTION' if term else 'TERM_NOT_SELECTED'),
                    'sections': sections,
                })
        return [row for row in result if school is None or school == row['school_code']]

    return {
        'version': ROADMAP_VERSION, 'track_code': code, 'title': track.title,
        'stages': [
            {'stage': 'FOUNDATION', 'goals': track.foundation_goals, 'courses': entries(track.foundation), 'milestone': None},
            {'stage': 'SPECIALIZATION', 'goals': ('選擇與研究問題直接相關的進階課，做出可運作的原型。',),
             'courses': entries(track.specialization), 'milestone': None},
            {'stage': 'RESEARCH', 'goals': ('固定測試條件，重現基準並比較結果。',), 'courses': [],
             'milestone': {'project': track.project, 'deliverable': track.deliverable, 'metrics': track.metrics}},
        ],
    }
