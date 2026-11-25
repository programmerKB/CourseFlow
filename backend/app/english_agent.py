"""LangGraph English registration agent (FR-21, FR-22; teacher items T-01, T-09, T-10, T-11).

Graph: understand -> decide -> registration_tool | read_tool | reply -> END

* ``understand`` asks Llama 3.2 3B for a structured intent (JSON schema). The model never
  writes SQL, never chooses the student, and never sees database rows.
* ``decide`` applies deterministic guards. A write needs an add/drop intent from the model,
  an explicit first-person request, and no negation, condition, quotation, third party or
  second course. Anything else becomes a read-only answer or a clarification.
* ``registration_tool`` resolves exactly one section in the backend and calls the same
  transaction service as the web (`registration.add_enrollment` / `drop_enrollment`).
* ``read_tool`` uses designated read tools: eligibility, the student's own timetable,
  registration status and a parameterised public-catalog query.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal, TypedDict
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from . import llama
from . import registration as reg
from .catalog_data import normalize_name
from .chat_sources import source
from .config import settings
from .db import SessionLocal
from .errors import DomainError
from .models import Course, CourseAlias, Enrollment, Section

log = logging.getLogger('courses.agent.english')
GRAPH_NAME = 'english_registration'
TAIPEI = ZoneInfo('Asia/Taipei')
WRITE_INTENTS = ('add_course', 'drop_course')
READ_INTENTS = ('check_eligibility', 'my_timetable', 'search_courses', 'registration_status')
INTENTS = (*WRITE_INTENTS, *READ_INTENTS, 'rules', 'greeting', 'unsupported')
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

ENGLISH_REASONS = {
    'SECTION_UNAVAILABLE': 'This section is not open for registration.',
    'ALREADY_ENROLLED': 'You are already enrolled in this course (a different section of the same course counts).',
    'PREREQUISITE_MISSING': 'You have not passed every prerequisite course in an earlier term.',
    'TIME_CONFLICT': 'This section overlaps with a class that is already in your timetable.',
    'CREDIT_LIMIT_EXCEEDED': 'Adding it would exceed your credit limit for this term (at most 18 credits).',
    'SECTION_FULL': 'This section has no seats left.',
    'REGISTRATION_CLOSED': 'Add/drop is not open for this term right now.',
    'ENROLLMENT_NOT_FOUND': 'That section is not in your current timetable.',
}
UNAVAILABLE = {
    'LLAMA_NOT_CONFIGURED': 'The English assistant is not set up yet (no Llama 3.2 3B endpoint is configured).',
    'LLAMA_TIMEOUT': 'The Llama 3.2 3B model did not answer in time.',
    'LLAMA_UNREACHABLE': 'The Llama 3.2 3B model server cannot be reached.',
    'LLAMA_UNAVAILABLE': 'The Llama 3.2 3B model service is unavailable.',
    'LLAMA_MODEL_UNAVAILABLE': 'The configured Llama model is not available on the model server.',
    'LLAMA_AUTH_FAILED': 'The model server rejected the assistant credentials.',
    'LLAMA_RATE_LIMITED': 'The model server is busy. Please retry shortly.',
    'LLAMA_INVALID_OUTPUT': 'The language model returned an answer the assistant could not verify.',
}
RULES_TEXT = ('Registration rules: each term allows at most 18 credits (a lower personal limit also applies). '
              'Sections whose meeting times overlap cannot both be taken, but back-to-back classes are fine. '
              'Every prerequisite must be passed in an earlier term, you can hold one section per course, '
              'and a section must still have seats. Adding and dropping happen only during the add/drop period; '
              'the web page and this assistant apply the same checks.')
HELP_TEXT = ('I can add or drop one course at a time during add/drop, check whether you can take a section, '
             'show your timetable, list sections and tell you the registration dates. '
             'For example: "Please enroll me in CS101, section A." or "Drop CS101 A."')

# ---- deterministic language signals -------------------------------------------------------
CJK = re.compile(r'[　-〿㐀-䶿一-鿿豈-﫿＀-￯]')
COURSE_CODE = re.compile(r'\b([A-Za-z]{2,6}(?:-[A-Za-z]{2,6})*-?\d{3}[A-Za-z]?)\b')
SECTION_WORD = re.compile(r'\b(?:section|sec\.?|class|group)\s*(?:no\.?\s*)?([A-Za-z]|\d{1,3})\b', re.I)
CODE_SECTION = re.compile(r'\b[A-Za-z]{2,6}(?:-[A-Za-z]{2,6})*-?\d{3}(?:\s*[,-]\s*|\s+)([A-Za-z]|\d{2})\b(?![\'’])')
EXPLICIT = re.compile(
    r"^(?:(?:hi|hello|hey|ok(?:ay)?)[\s,!.]+)?(?:please[\s,]+)?(?:"
    r"(?:enroll|register|sign)\s+me\b|(?:add|drop|remove|withdraw\s+me\s+from|unenroll\s+me\s+from|unregister\s+me\s+from|"
    r"take\s+me\s+out\s+of|put\s+me\s+in(?:to)?|get\s+me\s+(?:into|out\s+of))\b|"
    r"i(?:'d|\s+would)\s+like\s+to\s+(?:enroll|register|add|take|join|drop|remove|withdraw|unenroll|sign\s+up)|"
    r"i\s+(?:want|need|wish)\s+to\s+(?:enroll|register|add|take|join|drop|remove|withdraw|unenroll|sign\s+up)|"
    r"(?:can|could|would|will)\s+(?:you|u)\s+(?:please\s+|pls\s+)?(?:enroll|register|add|sign|put|get|drop|remove|withdraw|unenroll|"
    r"take\s+me\s+out))",
    re.I)
NEGATION = re.compile(r"\b(?:don'?t|do\s+not|doesn'?t|never|no\s+need|not|cancel\s+that|never\s*mind|stop)\b", re.I)
QUESTION = re.compile(r'^\s*(?:(?:can|could|may|might|should|would)\s+i|am\s+i|is\s+it\s+possible|is\s+there|'
                      r'do\s+i|will\s+i|are\s+there)\b', re.I)
# Polite idioms such as "if possible" do not make a request conditional.
CONDITIONAL = re.compile(r"\b(?:if(?!\s+(?:possible|you\s+can|you\s+could|that'?s\s+(?:ok|okay|alright)))|unless|"
                         r"in\s+case|whenever|provided\s+that|as\s+long\s+as|only\s+when)\b", re.I)
QUOTED = re.compile(r'["“”«»]|\b(?:said|says|told\s+me|asked\s+me)\b', re.I)
THIRD_PARTY = re.compile(
    r"\bmy\s+(?:friend|classmate|roommate|brother|sister|partner|girlfriend|boyfriend|son|daughter|teammate)s?\b|"
    r"\bon\s+behalf\s+of\b|\bfor\s+(?:him|her|them|someone(?:\s+else)?|another\s+student|other\s+students?)\b|"
    r"\b(?:enroll|register|sign|drop|remove|withdraw|put|add)\s+(?:him|her|them|someone)\b|"
    r"\b(?:his|her|their|(?!(?:today|tomorrow|tonight|week|term|semester|year|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)'s)\w+'s)\s+(?:schedule|timetable|courses?|classes|registration|grades?)\b|"
    r"\bstudent\s*\d{1,3}\b|\b[bB]\d{7,}\b|\bother\s+(?:student|people)'?s?\b", re.I)
SECOND_ACTION = re.compile(r'\b(?:and|&|plus|then|also)\s+(?:also\s+)?(?:add|drop|enroll|remove|register|take)\b', re.I)
VAGUE = {'', 'it', 'this', 'that', 'course', 'thecourse', 'thiscourse', 'thatcourse', 'class', 'theclass',
         'thisclass', 'thatclass', 'one', 'thatone', 'thisone', 'something', 'acourse', 'mycourse', 'courses',
         'allcourses', 'everything', 'all'}


REGISTRATION_WORDS = re.compile(
    r'\b(?:enroll|register|add|drop|remove|withdraw|unenroll|sign\s+me|put\s+me|take|join|timetable|schedule|'
    r'credits?|seats?|sections?|courses?|classes|deadline|prerequisites?|eligib\w*|rules?|hi|hello|hey|thanks?)\b', re.I)


def is_english(message):
    return not CJK.search(message) and len(re.findall(r'[A-Za-z]{2,}', message)) >= 1


# Verbs that change a timetable, and words that ask for study advice.
WRITE_VERBS = re.compile(
    r'\b(?:enroll|register|add|drop|remove|withdraw|unenroll|sign\s+me|put\s+me|get\s+me\s+(?:into|out))\b', re.I)
ADVICE_WORDS = re.compile(
    r'\b(?:research|topic|direction|path|roadmap|study|studying|interested|interest|recommend\w*|suggest\w*|advice|'
    r'plan|focus|field|career|learn\w*)\b', re.I)


def handles(message, identify_tracks):
    """English registration requests go to this agent. A message that names a research track
    such as "RF" or "Edge AI", alone or with study-advice words and without asking to add or
    drop, keeps using the roadmap flow (FR-20), e.g. "My research topic is Edge AI, 2 courses"."""
    if not is_english(message):
        return False
    if not identify_tracks(message) or WRITE_VERBS.search(message):
        return True
    return not (ADVICE_WORDS.search(message) or not REGISTRATION_WORDS.search(message))


class Signals(BaseModel):
    explicit: bool
    negated: bool
    question: bool
    question_mark: bool
    conditional: bool
    quoted: bool
    third_party: bool
    multiple: bool
    codes: list[str]
    sections: list[str]


def analyze(message):
    text_value = ' '.join(message.split())
    codes = list(dict.fromkeys(code.upper() for code in COURSE_CODE.findall(text_value)))
    sections = list(dict.fromkeys(value.upper() for value in SECTION_WORD.findall(text_value)
                                  + CODE_SECTION.findall(text_value)))
    explicit = bool(EXPLICIT.search(text_value))
    return Signals(
        explicit=explicit,
        negated=bool(NEGATION.search(text_value)),
        question=bool(QUESTION.search(text_value)),
        question_mark=text_value.endswith('?'),
        conditional=bool(CONDITIONAL.search(text_value)),
        quoted=bool(QUOTED.search(text_value)),
        third_party=bool(THIRD_PARTY.search(text_value)),
        multiple=len(codes) > 1 or bool(SECOND_ACTION.search(text_value)),
        codes=codes, sections=sections,
    )


# ---- model contract ---------------------------------------------------------------------------
class Intent(BaseModel):
    model_config = ConfigDict(extra='ignore')
    intent: Literal[INTENTS]
    course: str | None = Field(default=None, max_length=80)
    section: str | None = Field(default=None, max_length=16)
    weekday: int | None = Field(default=None, ge=1, le=7)
    has_seats: bool = False
    negated: bool = False
    other_person: bool = False
    explicit_request: bool = False

    @field_validator('course', 'section', mode='before')
    @classmethod
    def blank_is_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator('has_seats', 'negated', 'other_person', 'explicit_request', mode='before')
    @classmethod
    def null_is_false(cls, value):
        return False if value is None else value


INTENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'intent': {'type': 'string', 'enum': list(INTENTS)},
        'course': {'type': ['string', 'null']},
        'section': {'type': ['string', 'null']},
        'weekday': {'type': ['integer', 'null']},
        'has_seats': {'type': 'boolean'},
        'negated': {'type': 'boolean'},
        'other_person': {'type': 'boolean'},
        'explicit_request': {'type': 'boolean'},
    },
    'required': ['intent', 'course', 'section', 'weekday', 'has_seats', 'negated', 'other_person',
                 'explicit_request'],
}
SYSTEM_PROMPT = """You read ONE message from a university student and extract their course-registration request.
Return only a JSON object with these keys: intent, course, section, weekday, has_seats, negated, other_person,
explicit_request.
Never answer the student, never invent course codes, never output SQL.
intent values:
- add_course: the student asks you to enroll/add/register them in a course now.
- drop_course: the student asks you to drop/remove/withdraw them from a course now.
- check_eligibility: the student asks whether they can or may take/add a course.
- my_timetable: the student asks about their own schedule, enrolled courses or credits.
- search_courses: the student wants to find or list courses, sections or open seats.
- registration_status: the student asks about registration dates, deadlines or the current phase.
- rules: the student asks about registration rules (credit limit, time conflicts, prerequisites).
- greeting: hello, thanks or small talk.
- unsupported: anything else.
course: the course code (like CS101) or course name exactly as written by the student, else null.
section: the section label (like A, B, 01) only when the student states it, else null.
weekday: 1=Monday ... 7=Sunday when the student names a day, else null.
has_seats: true when the student asks for open seats.
negated: true when the student says NOT to do something (e.g. "don't drop").
other_person: true when the request is for someone other than the student.
explicit_request: true only when the student directly asks you to make an add or drop change now, in any wording
(polite, casual or imperative). false for questions, past events, plans, hypotheticals and anything else.
Examples:
"Please enroll me in CS101, section A." -> {"intent":"add_course","course":"CS101","section":"A","weekday":null,"has_seats":false,"negated":false,"other_person":false,"explicit_request":true}
"I'd like to add CS101 A." -> {"intent":"add_course","course":"CS101","section":"A","weekday":null,"has_seats":false,"negated":false,"other_person":false,"explicit_request":true}
"Remove CS101 A from my schedule." -> {"intent":"drop_course","course":"CS101","section":"A","weekday":null,"has_seats":false,"negated":false,"other_person":false,"explicit_request":true}
"Can I take Data Structures?" -> {"intent":"check_eligibility","course":"Data Structures","section":null,"weekday":null,"has_seats":false,"negated":false,"other_person":false,"explicit_request":false}
"Don't drop CS101." -> {"intent":"drop_course","course":"CS101","section":null,"weekday":null,"has_seats":false,"negated":true,"other_person":false,"explicit_request":false}
"Which courses on Friday still have seats?" -> {"intent":"search_courses","course":null,"section":null,"weekday":5,"has_seats":true,"negated":false,"other_person":false,"explicit_request":false}
"Add CS201 for my friend." -> {"intent":"add_course","course":"CS201","section":null,"weekday":null,"has_seats":false,"negated":false,"other_person":true,"explicit_request":true}
"I took CS101 last year." -> {"intent":"unsupported","course":"CS101","section":null,"weekday":null,"has_seats":false,"negated":false,"other_person":false,"explicit_request":false}"""


def stub_intent(message):
    """Deterministic test double for the model (CHAT_STUB=true). It never decides on its own
    whether a write is allowed; the same guards run after it as after the real model."""
    lower = message.lower().replace('add/drop', 'add-drop period')
    signals = analyze(message)
    course = signals.codes[0] if signals.codes else None
    if course is None:
        match = re.search(r"\b(?:in|into|from|add|drop|take|remove)\s+(?!me\b)([a-z][a-z &]{2,60}?)"
                          r"(?=\s*(?:,|\.|\?|!|$|\bsection\b|\bfrom\b|\bplease\b|\bnow\b))", lower)
        course = match[1].strip() if match else None
    day = next((index + 1 for index, name in enumerate(
        ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')) if name in lower), None)
    if re.search(r'(?<![-/])\b(drop|remove|withdraw|unenroll|unregister)\b', lower):
        intent = 'drop_course'
    elif re.search(r'\b(enroll|register|add|sign me up|take|join)\b(?!-drop)', lower):
        intent = 'add_course'
    elif re.search(r'\b(timetable|schedule|my courses|my credits|enrolled in)\b', lower):
        intent = 'my_timetable'
    elif re.search(r'\b(find|list|search|seats|sections|which courses|show courses)\b', lower):
        intent = 'search_courses'
    elif re.search(r'\b(when|deadline|period|phase|open)\b', lower):
        intent = 'registration_status'
    elif re.search(r'\b(rule|limit|conflict|prerequisite)', lower):
        intent = 'rules'
    elif re.search(r'\b(hi|hello|hey|thanks|thank you)\b', lower):
        intent = 'greeting'
    else:
        intent = 'unsupported'
    return {'intent': intent, 'course': course, 'section': signals.sections[0] if signals.sections else None,
            'weekday': day, 'has_seats': 'seat' in lower, 'negated': signals.negated,
            'other_person': signals.third_party, 'explicit_request': signals.explicit}


async def parse_intent(message):
    """Return the validated intent and evidence of where it came from.

    With CHAT_STUB=true no request leaves the application and the evidence says so. Otherwise
    the evidence holds what the model server reported about this inference.
    """
    if settings.chat_stub:
        raw, evidence = stub_intent(message), {'stub': True}
    else:
        evidence = {}
        raw = await llama.chat_json(SYSTEM_PROMPT, message, INTENT_SCHEMA, meta=evidence)
    try:
        intent = Intent.model_validate(raw)
    except ValidationError:
        raise llama.LlamaError('LLAMA_INVALID_OUTPUT') from None
    return intent, {**evidence, 'output': intent.model_dump()}


def model_label():
    return 'stub (CHAT_STUB)' if settings.chat_stub else settings.llama_model


# ---- grounding and section resolution ---------------------------------------------------------
def compact(value):
    return re.sub(r'[\s\-_.,]', '', normalize_name(value or ''))


def grounded_target(intent, signals, message):
    """Use the model's course only when it appears in the message; prefer explicit codes."""
    course = intent.course
    if course and (compact(course) not in compact(message) or compact(course) in VAGUE):
        course = None
    if signals.codes and (course is None or compact(course) not in {compact(code) for code in signals.codes}):
        course = signals.codes[0] if len(signals.codes) == 1 else course
    section = intent.section.upper() if intent.section else None
    if section not in signals.sections:
        section = signals.sections[0] if len(signals.sections) == 1 else None
    if course and section is None:
        # "CS101A" style: a trailing letter right after the digits is the section.
        match = re.fullmatch(r'(.*\d)([a-z])', compact(course))
        if match:
            course, section = match[1], match[2].upper()
    return course, section


def same_section_no(left, right):
    left, right = normalize_name(left), normalize_name(right)
    return left == right or (left.isdecimal() and right.isdecimal() and int(left) == int(right))


def match_sections(db, term_id, hint, section_no, *, enrolled_by=None):
    """Return (exact, partial) section IDs, optionally restricted to a student's ENROLLED sections."""
    stmt = (select(Section.id, Section.section_no, Course.id, Course.code, Course.title)
            .join(Course, Course.id == Section.course_id).where(Section.term_id == term_id)
            .order_by(Course.code, Section.section_no, Section.id))
    if enrolled_by is not None:
        stmt = stmt.join(Enrollment, Enrollment.section_id == Section.id).where(
            Enrollment.student_id == enrolled_by, Enrollment.term_id == term_id, Enrollment.status == 'ENROLLED')
    rows = db.execute(stmt).all()
    if hint is None:
        return ([], [row[0] for row in rows if section_no is None or same_section_no(row[1], section_no)])
    key, loose = normalize_name(hint), compact(hint)
    exact_alias = set(db.scalars(select(CourseAlias.course_id).where(CourseAlias.normalized_alias.in_({key, loose}))))
    partial_alias = set(db.scalars(select(CourseAlias.course_id).where(
        CourseAlias.normalized_alias.contains(key, autoescape=True)))) if len(key) >= 3 else set()
    exact, partial = [], []
    for section_id, number, course_id, code, title in rows:
        if section_no is not None and not same_section_no(number, section_no):
            continue
        code_key, title_key = compact(code), normalize_name(title)
        if loose == code_key or key == title_key or course_id in exact_alias:
            exact.append(section_id)
        elif len(loose) >= 3 and (loose in code_key or key in title_key or course_id in partial_alias):
            partial.append(section_id)
    return exact, partial


def section_label(section):
    return f'{section["code"]} {section["title"]}, section {section["section_no"]}'


def meeting_text(meetings):
    return ', '.join(f'{DAYS[m["weekday"] - 1]} {m["start_time"]:%H:%M}-{m["end_time"]:%H:%M}' for m in meetings) \
        or 'time to be announced'


def credits_text(value):
    return f'{Decimal(value):.1f}'


def taipei(value):
    return value.astimezone(TAIPEI).strftime('%Y-%m-%d %H:%M')


def timetable_links(term_id):
    return source(term_id, title='My timetable', url='/timetable?' + urlencode({'term': term_id}))


def web_sources(term_id):
    return [source(term_id, title='Course search (web add/drop)'),
            source(term_id, title='My timetable', url='/timetable' + (f'?{urlencode({"term": term_id})}' if term_id else ''))]


def choices_payload(db, term, section_ids, action):
    rows = reg.section_rows(db, section_ids[:5])
    return {
        'choices': [{**{key: row[key] for key in (
            'id', 'code', 'title', 'school_code', 'school_name', 'department_name', 'section_no',
            'meetings', 'remaining', 'is_selectable', 'credits', 'instructor_name')}, 'section_id': row['id']}
            for row in rows],
        'choice_total': len(section_ids), 'choice_action': action,
        'registration': reg.registration_status(db, term),
    }


# ---- graph ------------------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    user: dict
    message: str
    term_id: str | None
    request_id: str
    request_key: str | None
    digest: str | None
    selected_section: str | None
    intent: Intent
    signals: Signals
    course: str | None
    section: str | None
    route: str
    reply: str
    tools: list[str]
    llm: dict
    result: dict


async def understand(state):
    intent, evidence = await parse_intent(state['message'])
    signals = analyze(state['message'])
    course, section = grounded_target(intent, signals, state['message'])
    return {'intent': intent, 'llm': evidence, 'signals': signals, 'course': course, 'section': section,
            'tools': [*state.get('tools', []), 'stub.parse_intent' if evidence.get('stub') else 'llama.parse_intent']}


def decide(state):
    intent, signals = state['intent'], state['signals']
    kind = intent.intent
    if signals.third_party or intent.other_person:
        return {'route': 'reply', 'reply': 'privacy'}
    if kind in WRITE_INTENTS:
        # Llama judges whether the student asked for the change now; the regex accepts common
        # imperative forms. The checks below can still veto the write.
        explicit = signals.explicit or intent.explicit_request
        question = signals.question or (signals.question_mark and not explicit)
        if state.get('selected_section') and not (signals.negated or intent.negated):
            # The student picked one listed section for their original explicit request.
            return {'route': 'write'} if explicit else {'route': 'reply', 'reply': 'confirm_explicit'}
        if signals.negated or intent.negated:
            return {'route': 'read', 'reply': 'no_change'}
        if question:
            return {'route': 'read', 'reply': 'eligibility' if kind == 'add_course' else 'drop_check'}
        if signals.conditional or signals.quoted:
            return {'route': 'reply', 'reply': 'confirm_explicit'}
        if signals.multiple:
            return {'route': 'reply', 'reply': 'one_at_a_time'}
        if not explicit:
            return {'route': 'reply', 'reply': 'confirm_explicit'}
        if state.get('course') is None and kind == 'add_course':
            return {'route': 'reply', 'reply': 'which_course'}
        return {'route': 'write'}
    if kind == 'check_eligibility':
        return {'route': 'read', 'reply': 'eligibility'}
    if kind in READ_INTENTS:
        return {'route': 'read', 'reply': kind}
    return {'route': 'reply', 'reply': kind}


def base_result(state, mode, answer, sources, **extra):
    return {'mode': mode, 'answer': answer, 'sources': sources, 'request_id': state['request_id'],
            'as_of': extra.pop('as_of', None) or datetime.now(timezone.utc),
            'agent': agent_trace(state, extra.pop('tools', state.get('tools', []))), **extra}


def english_error(code, term_id, section_id=None, details=None, db=None):
    details = dict(details or {})
    message = ENGLISH_REASONS.get(code, 'The request could not be completed.')
    if db is not None and code == 'TIME_CONFLICT' and details.get('conflicting_section_ids'):
        rows = reg.section_rows(db, details['conflicting_section_ids'])
        details['conflicting_sections'] = [
            {'section_id': row['id'], 'code': row['code'], 'title': row['title'], 'section_no': row['section_no']}
            for row in rows]
        message += ' Conflicts with: ' + '; '.join(section_label(row) for row in rows) + '.'
    if code == 'CREDIT_LIMIT_EXCEEDED' and 'total_credits' in details:
        message += (f' It would bring you to {credits_text(details["total_credits"])} credits; '
                    f'your limit is {credits_text(details["credit_limit"])}.')
    sources = [source(term_id, section_id, 'View this section on the web')] if section_id else []
    return {**details, 'section_id': section_id, 'sources': sources + web_sources(term_id), 'message': message}


def agent_trace(state, tools):
    return {'graph': GRAPH_NAME, 'model': model_label(), 'intent': state['intent'].intent, 'tools': tools,
            'llm': state.get('llm')}


def raise_english(error, term_id, section_id, db, agent=None):
    payload = english_error(error.code, term_id, section_id, error.details, db)
    if agent is not None:
        payload['agent'] = agent
    message = payload.pop('message')
    if error.code not in ENGLISH_REASONS:
        message = error.message
    raise DomainError(error.status, error.code, message, payload) from None


def registration_tool(state):
    """Run one add or drop inside a single locked transaction, then record the replay."""
    user, term_id, intent = state['user'], state['term_id'], state['intent'].intent
    tools = [*state['tools'], 'resolve_section']
    if term_id is None:
        return {'result': base_result(state, 'clarify', 'Please choose a term first.', web_sources(None), tools=tools)}
    if not state.get('request_key'):
        raise DomainError(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Adding or dropping through the assistant needs an Idempotency-Key.')
    try:
        with SessionLocal.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.execute(text("SET LOCAL statement_timeout = '3s'"))
            term = reg.get_term(db, term_id, 'share')
            st = reg.student_term(db, user['student_id'], term_id, True)
            replay = reg.replay_response(db, st.student_id, term_id, state['request_key'], state['digest'])
            if replay is not None:
                return {'result': replay[1]}
            registration = reg.registration_status(db, term)
            phase = registration['phase']
            exact, partial = match_sections(db, term_id, state.get('course'), state.get('section'),
                                            enrolled_by=st.student_id if intent == 'drop_course' else None)
            candidates = exact or partial
            selected = state.get('selected_section')
            if selected is not None:
                if selected not in candidates[:5]:
                    raise DomainError(409, 'CHAT_CHOICE_INVALID', 'That choice no longer matches your request. Please ask again.')
                section_id = selected
            elif len(exact) == 1 or (intent == 'drop_course' and not exact and len(partial) == 1 and state.get('course')):
                section_id = (exact or partial)[0]
            else:
                section_id = None

            if intent == 'add_course' and phase == 'INITIAL' and candidates:
                return {'result': base_result(
                    state, 'clarify', 'Initial registration is open, so courses cannot be added directly yet. '
                    'Pick a section below and confirm an initial registration; seats are assigned at allocation.',
                    [source(term_id)], tools=tools, **choices_payload(db, term, [section_id] if section_id else candidates, 'apply'))}
            if phase != 'ADD_DROP':
                raise DomainError(409, 'REGISTRATION_CLOSED', ENGLISH_REASONS['REGISTRATION_CLOSED'],
                                  {'registration': registration, 'sources': web_sources(term_id),
                                   'agent': agent_trace(state, tools)})
            if section_id is None:
                if intent == 'drop_course' and not candidates:
                    answer = ('That course is not in your current timetable.' if state.get('course')
                              else 'Your timetable has no enrolled courses to drop.')
                    return {'result': base_result(state, 'clarify', answer, [timetable_links(term_id)], tools=tools,
                                                  as_of=reg.now(db))}
                if not candidates:
                    return {'result': base_result(state, 'clarify', 'I could not find a section matching that course '
                                                  'in this term. Please give the course code, e.g. "CS101 section A".',
                                                  [source(term_id)], tools=tools, as_of=reg.now(db))}
                verb = 'drop' if intent == 'drop_course' else 'add'
                answer = (f'Several sections match. Choose the one to {verb}.' if len(candidates) > 1 else
                          f'Please confirm the section to {verb}.')
                return {'result': base_result(state, 'clarify', answer, [source(term_id)], tools=tools,
                                              as_of=reg.now(db), **choices_payload(db, term, candidates, verb))}

            extra = {'agent': GRAPH_NAME, 'model': model_label(), 'language': 'en'}
            try:
                if intent == 'add_course':
                    tools.append('add_course')
                    outcome = reg.add_enrollment(db, term, st, section_id, user['id'], channel='CHAT_TEXT', extra=extra)
                else:
                    tools.append('drop_course')
                    outcome = reg.drop_enrollment(db, term, st, section_id, user['id'], channel='CHAT_TEXT', extra=extra)
            except DomainError as error:
                raise_english(error, term_id, section_id, db, agent_trace(state, tools))
            section, submitted_at = outcome.section, reg.now(db)
            limit = reg.effective_credit_limit(st.credit_limit)
            if intent == 'add_course':
                answer = (f'Done. You are now enrolled in {section_label(section)} ({credits_text(section["credits"])} '
                          f'credits, {meeting_text(section["meetings"])}). Your registered total is '
                          f'{credits_text(outcome.total_credits)} of {credits_text(limit)} credits. '
                          'A confirmation email has been queued for delivery through Gmail.')
            else:
                answer = (f'Done. You have dropped {section_label(section)}. Your registered total is now '
                          f'{credits_text(outcome.total_credits)} of {credits_text(limit)} credits, '
                          'and the seat has been released.')
            response = jsonable_encoder(base_result(
                state, intent, answer, [source(term_id, section_id, f'{section["code"]} {section["title"]}'),
                                        timetable_links(term_id)],
                tools=tools, as_of=submitted_at, action={
                    'status': outcome.enrollment.status, 'term_id': term_id, 'enrollment_id': outcome.enrollment.id,
                    'section_id': section_id, 'course_code': section['code'], 'school_code': section['school_code'],
                    'school_name': section['school_name'], 'department_name': section['department_name'],
                    'course_title': section['title'], 'section_no': section['section_no'],
                    'total_credits': str(outcome.total_credits), 'remaining': section['remaining'],
                    'submitted_at': submitted_at, 'notification_id': outcome.notification_id,
                }))
            reg.record_response(db, st.student_id, term_id, state['request_key'], state['digest'], 200, response)
            return {'result': response, 'tools': tools}
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) not in ('55P03', '57014'):
            raise
        raise DomainError(503, 'ENROLLMENT_BUSY', 'Registration is busy with another request. Please resend the same request.') from None


@lru_cache
def public_engine():
    if settings.public_database_url == settings.database_url:
        raise RuntimeError('Separate public database account required')
    return create_engine(settings.public_database_url, pool_pre_ping=True, pool_size=2, max_overflow=2, pool_timeout=2,
                         connect_args={'connect_timeout': 2,
                                       'options': '-c default_transaction_read_only=on -c statement_timeout=2000'})


def query_public_courses(term_id, course_ids, weekday, has_seats, limit=10):
    """Designated read tool: parameterised SELECT on the public views with the read-only account."""
    sql = text('''
        SELECT c.section_id, c.code, c.title, c.section_no, c.school_name, c.department_name, c.credits,
               c.remaining, c.capacity, c.is_selectable
        FROM v_public_section_catalog c
        WHERE c.term_id = CAST(:term_id AS uuid)
          AND (CAST(:course_ids AS uuid[]) IS NULL OR c.course_id = ANY(CAST(:course_ids AS uuid[])))
          AND (CAST(:weekday AS integer) IS NULL OR EXISTS (
                SELECT 1 FROM v_public_section_meetings m WHERE m.section_id = c.section_id AND m.weekday = :weekday))
          AND (NOT :has_seats OR (c.is_selectable AND c.remaining > 0))
        ORDER BY c.code, c.section_no, c.section_id LIMIT :limit''')
    with public_engine().connect() as connection:
        rows = [dict(row) for row in connection.execute(sql, {
            'term_id': term_id, 'course_ids': course_ids, 'weekday': weekday, 'has_seats': has_seats,
            'limit': limit}).mappings()]
        meetings = {}
        if rows:
            for row in connection.execute(text('''
                    SELECT section_id, weekday, start_time, end_time FROM v_public_section_meetings
                    WHERE section_id = ANY(CAST(:ids AS uuid[])) ORDER BY weekday, start_time'''),
                    {'ids': [str(row['section_id']) for row in rows]}).mappings():
                meetings.setdefault(row['section_id'], []).append(dict(row))
    for row in rows:
        row['meetings'] = meetings.get(row['section_id'], [])
    return rows


def read_tool(state):
    user, term_id, kind = state['user'], state['term_id'], state['reply']
    tools = list(state['tools'])
    if term_id is None:
        return {'result': base_result(state, 'clarify', 'Please choose a term first.', web_sources(None), tools=tools)}
    with SessionLocal() as db:
        term = reg.get_term(db, term_id)
        if kind == 'registration_status':
            tools.append('get_registration_status')
            status = reg.registration_status(db, term)
            names = {'INITIAL': 'Initial registration', 'ADD_DROP': 'Add/drop'}
            lines = [f'{term.code}: current phase is {status["phase"]}.'] + [
                f'{names[p["kind"]]}: {taipei(p["starts_at"])} to {taipei(p["ends_at"])} (Taipei time; closes at the end time).'
                for p in status['periods']]
            return {'result': base_result(state, 'simple_chat', '\n'.join(lines), [source(term_id)], tools=tools,
                                          as_of=status['as_of'], registration=status)}
        if kind in ('my_timetable', 'no_change', 'drop_check'):
            tools.append('get_my_timetable')
            data = reg.get_timetable(db, user['student_id'], term_id)
            lines = [f'• {section_label(row)} ({credits_text(row["credits"])} credits): {meeting_text(row["meetings"])}'
                     for row in data['items']]
            pending = [f'• {section_label(row)} (pending allocation)' for row in data['pending_items']]
            summary = (f'Registered total: {credits_text(data["total_credits"])} of '
                       f'{credits_text(data["credit_limit"])} credits.')
            body = '\n'.join(['Your enrolled courses:', *(lines or ['• none']), *pending, summary])
            if kind == 'no_change':
                body = 'Okay. I did not change your registration.\n' + body
            if kind == 'drop_check':
                exact, partial = match_sections(db, term_id, state.get('course'), state.get('section'),
                                                enrolled_by=user['student_id'])
                found = reg.section_rows(db, (exact or partial)[:1])
                phase = reg.phase(db, term)
                if not found:
                    body = 'That course is not in your current timetable.\n' + body
                elif phase != 'ADD_DROP':
                    body = f'{section_label(found[0])} is in your timetable, but add/drop is not open now.\n' + body
                else:
                    body = (f'Yes. {section_label(found[0])} is in your timetable and add/drop is open. '
                            f'Say "Please drop {found[0]["code"]} section {found[0]["section_no"]}." to drop it.\n' + body)
            return {'result': base_result(state, 'student_query', body, [timetable_links(term_id)], tools=tools,
                                          as_of=data['as_of'])}
        if kind == 'eligibility':
            tools.append('check_eligibility')
            exact, partial = match_sections(db, term_id, state.get('course'), state.get('section'))
            candidates = exact or partial
            if state.get('course') is None or not candidates:
                return {'result': base_result(state, 'clarify', 'Which course do you mean? Please give its code, '
                                              'for example "Can I take CS101 section A?"', [source(term_id)], tools=tools)}
            if len(candidates) > 1:
                return {'result': base_result(state, 'clarify', 'Several sections match. Open one to check it, or '
                                              'name the section, for example "Can I take CS101 section A?"',
                                              [source(term_id)], tools=tools,
                                              **choices_payload(db, term, candidates, None))}
            check = reg.eligibility(db, user['student_id'], term_id, candidates[0])
            [section] = reg.section_rows(db, candidates)
            if check['eligible']:
                answer = (f'Yes. You can add {section_label(section)} right now. Submitting re-checks every rule; '
                          f'say "Please enroll me in {section["code"]} section {section["section_no"]}." to add it.')
            elif check['reason_code'] == 'REGISTRATION_CLOSED':
                answer = f'Not right now: add/drop is not open (current phase: {check["phase"]}).'
            else:
                answer = f'Not right now: {english_error(check["reason_code"], term_id, None, check["details"], db)["message"]}'
            return {'result': base_result(state, 'student_query', answer, [source(term_id, section['id'], section['code'])],
                                          tools=tools, as_of=check['as_of'])}
        # search_courses
        tools.append('query_public_courses')
        course_ids = None
        if state.get('course'):
            exact, partial = match_sections(db, term_id, state['course'], state.get('section'))
            ids = exact or partial
            if not ids:
                return {'result': base_result(state, 'sql_query', 'No public sections match that course in this term.',
                                              [source(term_id)], tools=tools)}
            course_ids = sorted({row['course_id'] for row in reg.section_rows(db, ids)})
        intent = state['intent']
        rows = query_public_courses(term_id, course_ids, intent.weekday, intent.has_seats)
        if not rows:
            answer = 'No public sections match those conditions. Try fewer filters.'
        else:
            answer = f'Found {len(rows)} section(s) (showing up to 10):\n' + '\n'.join(
                f'• {row["code"]} {row["title"]} {row["section_no"]} | {row["school_name"]} {row["department_name"]} | '
                f'{meeting_text(row["meetings"])} | {row["remaining"]}/{row["capacity"]} seats left'
                + ('' if row['is_selectable'] else ' | not open') for row in rows)
        return {'result': base_result(state, 'sql_query', answer,
                                      [source(term_id, str(row['section_id']), f'{row["code"]} {row["section_no"]}')
                                       for row in rows] or [source(term_id)], tools=tools)}


def reply(state):
    kind = state['reply']
    term_id = state['term_id']
    answers = {
        'privacy': ('student_query', 'I can only act on your own registration. I cannot view or change another '
                    "student's courses or grades."),
        'confirm_explicit': ('clarify', 'I did not change anything. To add or drop a course, ask directly for one '
                             'course, for example "Please enroll me in CS101, section A." or "Please drop CS101 A."'),
        'one_at_a_time': ('clarify', 'I can add or drop one course per request. Please send each course separately.'),
        'which_course': ('clarify', 'Which course would you like to add? Please give the course code and section, '
                         'for example "Please enroll me in CS101, section A."'),
        'rules': ('simple_chat', RULES_TEXT),
        'greeting': ('simple_chat', 'Hello! ' + HELP_TEXT),
    }
    mode, answer = answers.get(kind, ('simple_chat', HELP_TEXT))
    sources = [source(title='Registration rules', url='/rules')] if kind == 'rules' else web_sources(term_id)
    return {'result': base_result(state, mode, answer, sources)}


def route_after_decide(state):
    return {'write': 'registration_tool', 'read': 'read_tool'}.get(state['route'], 'reply')


builder = StateGraph(AgentState)
builder.add_node('understand', understand)
builder.add_node('decide', decide)
builder.add_node('registration_tool', registration_tool)
builder.add_node('read_tool', read_tool)
builder.add_node('reply', reply)
builder.add_edge(START, 'understand')
builder.add_edge('understand', 'decide')
builder.add_conditional_edges('decide', route_after_decide, ['registration_tool', 'read_tool', 'reply'])
for node in ('registration_tool', 'read_tool', 'reply'):
    builder.add_edge(node, END)
graph = builder.compile()


def unavailable(term_id, reason_code):
    return DomainError(503, 'CHAT_UNAVAILABLE', UNAVAILABLE.get(reason_code, 'The English assistant is unavailable.')
                       + ' Your registration was not changed. You can still add or drop courses on the web page.',
                       {'mode': 'unavailable', 'reason_code': reason_code, 'sources': web_sources(term_id)})


async def handle(user, message, term_id, request_id, *, request_key=None, digest=None, selected_section=None):
    begin = time.monotonic()
    state: AgentState = {'user': user, 'message': message, 'term_id': term_id, 'request_id': request_id,
                         'request_key': request_key, 'digest': digest, 'selected_section': selected_section,
                         'tools': []}
    record: dict[str, Any] = {'request_id': request_id, 'graph': GRAPH_NAME, 'model': model_label(),
                              'api_style': settings.llama_api_style}
    try:
        result = await asyncio.wait_for(graph.ainvoke(state), timeout=settings.llama_timeout_seconds + 10)
    except llama.LlamaError as error:
        log.warning(json.dumps({**record, 'mode': 'unavailable', 'reason_code': error.reason_code,
                                'elapsed_ms': round((time.monotonic() - begin) * 1000)}))
        raise unavailable(term_id, error.reason_code) from None
    except (TimeoutError, asyncio.TimeoutError):
        log.warning(json.dumps({**record, 'mode': 'unavailable', 'reason_code': 'LLAMA_TIMEOUT'}))
        raise unavailable(term_id, 'LLAMA_TIMEOUT') from None
    except DomainError as error:
        agent = (error.details or {}).get('agent') or {}
        log.info(json.dumps({**record, 'mode': 'rejected', 'code': error.code, 'tools': agent.get('tools'),
                             'llm': {key: value for key, value in (agent.get('llm') or {}).items() if key != 'output'},
                             'elapsed_ms': round((time.monotonic() - begin) * 1000)}))
        raise
    output = result['result']
    agent = output.get('agent', {})
    evidence = {key: value for key, value in (agent.get('llm') or {}).items() if key != 'output'}
    log.info(json.dumps({**record, 'mode': output['mode'], 'intent': agent.get('intent'),
                         'route': result.get('route'), 'tools': agent.get('tools', []), 'llm': evidence,
                         'elapsed_ms': round((time.monotonic() - begin) * 1000)}))
    return output

