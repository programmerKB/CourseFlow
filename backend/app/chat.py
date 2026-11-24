"""LangGraph assistant for student queries and selection previews.

Free-form questions are planned by the local Llama 3.2 3B model (see ``llama``). The model only
proposes a structured plan; every field it fills must be grounded in the student's message, and
the read-only tools below run the plan against PostgreSQL.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, time as local_time, timezone
from functools import lru_cache
from typing import Literal, TypedDict, Any
from urllib.parse import urlencode
from uuid import UUID

from httpx import TimeoutException
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select, text
from starlette.concurrency import run_in_threadpool
from .catalog_data import SCHOOLS, DEPARTMENTS, department_names, normalize_name, school_code, school_prefix
from .chat_enrollment import existing_response, fingerprint, handle_command, parse_command
from .chat_sources import source
from .config import settings
from .db import SessionLocal
from .errors import DomainError
from .models import Department, Student, User
from .roadmap_data import TRACKS
from . import english_agent, llama, recommendation, roadmap
from . import registration as reg

log = logging.getLogger('courses.agent')
GRAPH_NAME = 'course_assistant'
RULES_VERSION = 'SRS-4.0'
RULES = ('初選登記是待分發，並不代表立即選上。初選截止後，管理員依固定種子的 SHA-256 順序分發並公布。'
         '錄取依序檢查班別開放、同課重複、先修通過、衝堂、學分上限（每學期最多 18 學分）與名額。'
         '相鄰時段不算衝堂。即時加退選只在開放期間受理。助理可依明確中文指令加選唯一班別；'
         '英文助理可依明確指令加選或退選，例如 “Please drop CS101 A.”，也可到我的課表退選。')
UNAVAILABLE_MESSAGES = {
    'LLAMA_NOT_CONFIGURED': '選課助理尚未啟用：管理員尚未設定本機語言模型（LLAMA_BASE_URL）。',
    'LLAMA_UNREACHABLE': '選課助理連不到本機語言模型服務，請聯絡管理員檢查 Ollama 與網路設定。',
    'LLAMA_TIMEOUT': '選課助理連線逾時，請稍後重試。',
    'LLAMA_MODEL_UNAVAILABLE': '本機語言模型服務上沒有設定的模型，請聯絡管理員。',
    'LLAMA_AUTH_FAILED': '本機語言模型服務拒絕存取，請聯絡管理員檢查設定。',
    'LLAMA_RATE_LIMITED': '本機語言模型服務忙碌中，請稍後再試。',
    'LLAMA_UNAVAILABLE': '本機語言模型服務暫時無法完成請求，請稍後再試。',
    'LLAMA_INVALID_OUTPUT': '本機語言模型回傳的查詢計畫無法使用，請換個說法再試。',
    'CHAT_FAILED': '選課助理暫時無法使用。',
}


class Plan(BaseModel):
    mode: Literal['simple_chat', 'sql_query', 'student_query', 'recommendation', 'clarify'] = 'simple_chat'
    tool: Literal['rules', 'status', 'courses', 'timetable', 'applications', 'my_selections', 'eligibility', 'recommend',
                  'action', 'privacy', 'greeting', 'clarify'] = 'rules'
    keyword: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    department_id: str | None = None
    school_code: str | None = Field(default=None, max_length=32)
    topic_code: str | None = Field(default=None, max_length=64)
    weekday: int | None = Field(default=None, ge=1, le=7)
    has_seats: bool = False
    sort: Literal['code', 'remaining', 'title'] = 'code'
    limit: int = Field(default=20, ge=1, le=20)
    term_id: str | None = None
    topic: str | None = Field(default=None, max_length=100)
    track_code: str | None = Field(default=None, max_length=64)
    additional_keywords: list[str] = Field(default_factory=list, max_length=8)
    target_count: int = Field(default=3, ge=1, le=5)
    available_times: list[recommendation.AvailableTime] = Field(default_factory=list, max_length=14)


class State(TypedDict, total=False):
    db: Any
    user: dict
    message: str
    term_id: str | None
    request_id: str
    plan: Plan
    llm: dict
    result: dict
    tools: list
    count: int
    track_code: str | None


def unavailable(term_id, reason_code='CHAT_FAILED'):
    return DomainError(503, 'CHAT_UNAVAILABLE', UNAVAILABLE_MESSAGES[reason_code] + '請先使用課程查詢或我的課表。',
                       {'mode': 'unavailable', 'reason_code': reason_code,
                        'sources': [source(term_id), source(title='我的課表', url='/timetable')]})


def resolve_term_id(term_id):
    public_term_id = settings.public_initial_term_id
    if public_term_id and term_id and term_id != public_term_id:
        raise DomainError(404, 'TERM_NOT_FOUND', '找不到公開學期。')
    return term_id or public_term_id or None


def failure_reason(error):
    # Follow wrapped errors without logging their contents.
    for _ in range(8):
        if isinstance(error, (TimeoutError, TimeoutException)):
            return 'LLAMA_TIMEOUT'
        if isinstance(error, llama.LlamaError):
            return error.reason_code if error.reason_code in UNAVAILABLE_MESSAGES else 'CHAT_FAILED'
        error = error.__cause__ or error.__context__
        if error is None:
            break
    return 'CHAT_FAILED'


def redact(db, user, message):
    identifiers = [user.get('login'), user.get('display_name')]
    account = db.get(User, user['id'])
    student = db.get(Student, user['student_id'])
    identifiers += [account.email if account else None, student.student_no if student else None, student.name if student else None]
    for value in identifiers:
        if value:
            message = re.sub(re.escape(value), '[個資]', message, flags=re.I)
    patterns = [r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', r'(?i)bearer\s+\S+',
                r'(?i)(?:password|密碼|token|api[_ -]?key|session)\s*[:=：]?\s*\S+',
                r'\b[A-Za-z]\d{9}\b', r'\b\d{7,}\b', r'\b[A-Za-z0-9_-]{24,}\b',
                r'\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b', r'\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b']
    for pattern in patterns:
        message = re.sub(pattern, '[個資]', message)
    return message


def mentioned_weekday(message):
    match = re.search(r'(?:星期|[週周])([一二三四五六日天])', message)
    return '一二三四五六日天'.index(match[1]) % 7 + 1 if match and match[1] != '天' else (7 if match else None)


DEPARTMENT_ADVICE = re.compile(
    r'(?:我是\s*)?(?P<department>[\u4e00-\u9fffA-Za-z]{1,28}(?:系|所))\s*(?:的?學生)?'
    r'[\s,，:：。]*?(?:請\s*)?(?:(?:給我|提供給我|想要)\s*)?(?:(?:修課|選課)建議|推薦課程)'
    r'[\s。?!？！]*'
)
MY_SELECTIONS = re.compile(
    r'(?:請問\s*)?(?:我(?:剛剛|剛才|目前|已經)(?:選了|加選了|登記了)(?:什麼(?:課(?:程)?)?|哪(?:些|幾門)課(?:程)?)'
    r'|(?:查詢|查一下)?\s*我的選課紀錄)[\s。?!？！]*'
)
ELIGIBILITY_QUESTION = re.compile(
    r'(?:請問\s*)?(?:我(?:現在)?(?:能|可(?:以)?)加選\s*(?P<target>.{1,100}?)'
    r'|(?P<before>.{1,100}?)\s*(?:可(?:以)?|能)加選)(?:嗎[?。]?|\?)'
)
WEEKDAY_COURSE_SEARCH = re.compile(
    r'(?:請\s*)?(?:幫我\s*)?(?:找|查詢|查|列出)\s*(?:這學期\s*)?'
    r'(?P<weekday>(?:星期|[週周])[一二三四五六日天])\s*'
    r'(?:還|仍|尚)?有\s*(?:空位|名額)\s*的\s*'
    r'(?P<department>.{1,100}?)\s*(?:的\s*)?課(?:程)?[\s。?!？！]*'
)
DEPARTMENT_NAMES = frozenset(
    alias
    for group, (code, title, _) in DEPARTMENTS.items()
    for alias in department_names(code, title, group.split('-', 1)[0])
)
KNOWN_DEPARTMENTS = frozenset(normalize_name(name).replace('台', '臺') for name in DEPARTMENT_NAMES)


def local_plan(message):
    # Match only complete, explicit questions so extra constraints reach the model.
    message = recommendation.normalize_keyword(message)
    match = WEEKDAY_COURSE_SEARCH.fullmatch(message)
    if match:
        department, school = school_prefix(normalize_name(match['department']))
        if department in DEPARTMENT_NAMES:
            return Plan(mode='sql_query', tool='courses', department=department, school_code=school,
                        weekday=mentioned_weekday(match['weekday']), has_seats=True)
    if MY_SELECTIONS.fullmatch(message):
        return Plan(mode='student_query', tool='my_selections')
    match = DEPARTMENT_ADVICE.fullmatch(message)
    if match:
        return Plan(mode='recommendation', tool='recommend', department=match['department'])
    if re.fullmatch(r'(?:我)?(?:不想|不要|不用|不必|不會|別)(?:\s*幫我)?\s*加選.*', message):
        return Plan(mode='student_query', tool='action')
    match = ELIGIBILITY_QUESTION.fullmatch(message)
    if match:
        target = (match['target'] or match['before']).strip()
        if target in ('這門課', '這門', '它', '那門課'):
            return Plan(mode='student_query', tool='action')
        return Plan(mode='student_query', tool='eligibility', keyword=target)
    return None


def requested_count(message):
    count = re.search(r'([0-9]+|[一二三四五六七八九十])\s*門', message)
    if not count:
        return 3
    return int(count[1]) if count[1].isdigit() else '一二三四五六七八九十'.index(count[1]) + 1


def local_roadmap_plan(message, code):
    message = recommendation.normalize_keyword(message)
    count = requested_count(message)
    if not 1 <= count <= 5:
        return Plan(mode='clarify', tool='clarify')
    windows = []
    pattern = r'(?:星期|[週周])([一二三四五六日天])\s*(\d{1,2}:\d{2})\s*[-~～–—到至]\s*(\d{1,2}:\d{2})'
    for week, start, end in re.findall(pattern, message):
        try:
            windows.append(recommendation.AvailableTime(
                weekday='一二三四五六日'.index(week.replace('天', '日')) + 1,
                start_time=local_time.fromisoformat(start.zfill(5)), end_time=local_time.fromisoformat(end.zfill(5)),
            ))
        except ValueError:
            return Plan(mode='clarify', tool='clarify')
    if len(windows) > 14:
        return Plan(mode='clarify', tool='clarify')
    if not windows and mentioned_weekday(message):
        windows.append(recommendation.AvailableTime(weekday=mentioned_weekday(message), start_time=local_time.min,
                                                    end_time=local_time(23, 59, 59)))
    return Plan(mode='recommendation', tool='recommend', track_code=code, target_count=count,
                available_times=windows, has_seats=bool(re.search('空位|餘額', message)))


def stub_plan(message):
    # Test-only deterministic replacement for the model; execution uses the same graph/tools.
    if re.search('他人|別人|其他同學|student\\d', message, re.I):
        return Plan(mode='student_query', tool='privacy')
    if re.search(r'推薦|修課建議|研究方向|試排|幫我選', message) and not re.search('退選|取消登記', message):
        target = requested_count(message)
        if not 1 <= target <= 5:
            return Plan(mode='clarify', tool='clarify')
        topic = next((m[0] for m in re.finditer(r'大型?語言模型|LLM|large language models?', message, re.I)), '')
        if not topic:
            subject = re.search(r'研究方向(?:是|為|[:：])\s*([^，。,、\n]+?)(?=\s*(?:[，。,、\n]|請|幫我|推薦|$))', message)
            if not subject:
                subject = re.search(r'推薦\s*([^，。,、\n]+?)(?:相關|領域|方向)', message)
            topic = subject[1].strip() if subject else ''
        return Plan(mode='recommendation', tool='recommend', topic=topic[:100] or None, target_count=target)
    if re.search('幫我.*(退選|加選|登記)|替我.*(退選|加選)', message):
        return Plan(mode='student_query', tool='action')
    if re.search(r'課表|(?:星期|[週周]).有課', message):
        return Plan(mode='student_query', tool='timetable', weekday=mentioned_weekday(message))
    if re.search('結果|我的登記', message):
        return Plan(mode='student_query', tool='applications')
    if re.search('時間|日期|何時|截止|階段', message):
        return Plan(tool='status')
    if re.search('規則|立刻|馬上|初選.*(嗎|流程)', message):
        return Plan(tool='rules')
    keyword = next((x for x in ['資料庫設計', '程式設計', '資料結構', '電子學', '電路學', '計算機網路'] if x in message), None)
    day = mentioned_weekday(message)
    return Plan(mode='student_query' if re.search('能.*加選|可.*加選|資格', message) else 'sql_query',
                tool='eligibility' if re.search('能.*加選|可.*加選|資格', message) else 'courses', keyword=keyword,
                weekday=day, department='電子' if '電子系' in message else None, has_seats=bool(re.search('空位|名額|餘額', message)))


# ---- local model query planner -----------------------------------------------------------------
PLAN_TOOLS = ('rules', 'status', 'courses', 'eligibility', 'timetable', 'applications', 'my_selections',
              'recommend', 'action', 'privacy', 'greeting', 'clarify')
MODE_BY_TOOL = {'rules': 'simple_chat', 'greeting': 'simple_chat', 'status': 'simple_chat', 'courses': 'sql_query',
                'eligibility': 'student_query', 'timetable': 'student_query', 'applications': 'student_query',
                'my_selections': 'student_query', 'action': 'student_query', 'privacy': 'student_query',
                'recommend': 'recommendation', 'clarify': 'clarify'}
CLOCK = {'type': 'string', 'pattern': '^([01][0-9]|2[0-3]):[0-5][0-9]$'}
PLAN_SCHEMA = {
    'type': 'object',
    'properties': {
        'tool': {'type': 'string', 'enum': list(PLAN_TOOLS)},
        'keyword': {'type': ['string', 'null']},
        'department': {'type': ['string', 'null']},
        'school_code': {'type': ['string', 'null'], 'enum': ['NTU', 'NTNU', 'NTUST', None]},
        'weekday': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 7},
        'has_seats': {'type': 'boolean'},
        'sort': {'type': 'string', 'enum': ['code', 'remaining', 'title']},
        'topic': {'type': ['string', 'null']},
        'track_code': {'type': ['string', 'null'], 'enum': [*TRACKS, None]},
        'target_count': {'type': 'integer', 'minimum': 1, 'maximum': 5},
        'available_times': {'type': 'array', 'maxItems': 14, 'items': {
            'type': 'object', 'properties': {'weekday': {'type': 'integer', 'minimum': 1, 'maximum': 7},
                                             'start_time': CLOCK, 'end_time': CLOCK},
            'required': ['weekday', 'start_time', 'end_time']}},
    },
    'required': ['tool', 'keyword', 'department', 'school_code', 'weekday', 'has_seats', 'sort', 'topic',
                 'track_code', 'target_count', 'available_times'],
}
PLANNER_PROMPT = """You turn ONE message from a university student (usually Traditional Chinese) into a query plan for a course-registration assistant in Taiwan.
Return only a JSON object. Never answer the student, never write SQL, never invent course names. Copy words exactly as the student wrote them; leave a field null, false or empty when the student did not say it.

Choose tool by what the student wants:
- recommend: which courses to take, what to study, course suggestions, a study path, or a trial schedule (試排) for a research topic, field, direction or department. "該修什麼課", "要修哪些課", "推薦", "修課建議" are recommend.
- eligibility: whether the student can/may take or add ONE named course ("我可以修X嗎", "我能加選X嗎").
- courses: list or look up course sections by course name/code, department, school, weekday, open seats or remaining seats.
- timetable: the student's own timetable or whether they have class on a day.
- my_selections: which courses the student has selected or registered so far.
- applications: the student's initial-registration (初選) results or allocation result.
- status: dates, deadlines or the current phase of this term.
- rules: how registration works (allocation, conflicts, credit limit, add/drop rules) and anything not about courses (weather, chit-chat).
- action: the student tells you to drop, cancel or add a course for them.
- privacy: the student asks about another student's data.
- greeting: only a greeting such as 你好.
- clarify: wants advice but names no topic, field, department or course ("我該修什麼？", "有什麼建議").

Fields:
- keyword: a course name or course code the student named (e.g. 資料結構, CS101). Not a department, field or topic.
- department: department as written (電子系, 電機所), only if named.
- school_code: NTU (臺大/台大), NTNU (師大), NTUST (臺科大/台科大), only if named.
- weekday: 1-7 (Monday/週一/星期一 = 1 ... Sunday/週日 = 7), only if a day is named.
- has_seats: true only if the student asks for open seats (空位/名額/餘額).
- sort: "remaining" if they want the most remaining seats, "title" for by-name order, otherwise "code".
- topic: for recommend, the research topic or field words from the message (機器學習, 晶片設計, 影像處理).
- track_code: a code from Context.tracks only when the topic plainly names that track.
- target_count: how many courses they want, 1-5; 3 if not stated.
- available_times: for recommend, each day/time window the student named; whole day 00:00-23:59, morning 08:00-12:00, afternoon 13:00-18:00, evening 18:00-22:00.
- When Context.selected_track_code is set, the student is continuing that research track: use recommend and read only the new count, days, times and school.

Examples (keys not shown are null, false, "code", 3 or []):
"初選登記完會立刻選上嗎？" -> {"tool":"rules"}
"程式設計還有空位嗎" -> {"tool":"courses","keyword":"程式設計","has_seats":true}
"哪個班剩餘名額最多" -> {"tool":"courses","sort":"remaining"}
"我可以修計算機網路嗎" -> {"tool":"eligibility","keyword":"計算機網路"}
"我想做機器人，要修哪些課？" -> {"tool":"recommend","topic":"機器人","track_code":"control_robotics"}
"推薦一門跟訊號處理有關、星期五上午的課" -> {"tool":"recommend","topic":"訊號處理","track_code":"signal_sensing","target_count":1,"available_times":[{"weekday":5,"start_time":"08:00","end_time":"12:00"}]}
"有什麼建議嗎" -> {"tool":"clarify"}
"幫我退選電子學" -> {"tool":"action","keyword":"電子學"}
"""
CHINESE_DAY = re.compile(r'(?:星期|禮拜|[週周])([一二三四五六日天])')
ENGLISH_DAY = re.compile(r'\b(mon|tue|wed|thu|fri|sat|sun)(?:day|s|nesday|sday|rsday|urday)?\b', re.I)
ENGLISH_DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
COUNT = re.compile(r'(\d+|[一兩二三四五])\s*(?:門|堂|個|courses?|classes?)|\b(one|two|three|four|five)\s+(?:courses?|classes?)',
                   re.I)
NUMBERS = {'一': 1, '兩': 2, '二': 2, '三': 3, '四': 4, '五': 5, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}
PART_OF_DAY = ((re.compile(r'上午|早上|morning', re.I), '08:00', '12:00'),
               (re.compile(r'中午|noon', re.I), '12:00', '13:30'),
               (re.compile(r'下午|afternoon', re.I), '13:00', '18:00'),
               (re.compile(r'晚上|夜間|evening|night', re.I), '18:00', '22:00'))
SEATS = re.compile(r'空位|名額|餘額|seats?|vacanc', re.I)
GENERIC_KEYWORDS = frozenset({'課', '課程', '課表', '名額', '空位', '剩餘名額', '截止時間', '初選結果', '學分上限',
                              'course', 'courses', 'class', 'classes'})


def as_int(value):
    """An integer from the model, or None. Booleans, lists and objects are not integers."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_text(value):
    return value if isinstance(value, str) else None


def squash(value):
    return normalize_name(value or '').replace('台', '臺')


def grounded(value, message):
    """Return the model's text only when the student actually wrote it."""
    value = (value or '').strip() if isinstance(value, str) else ''
    return value if value and squash(value) in squash(message) else None


def named_weekdays(message):
    days = {'一二三四五六日天'.index(day) % 7 + 1 if day != '天' else 7 for day in CHINESE_DAY.findall(message)}
    return days | {ENGLISH_DAYS.index(day.lower()) + 1 for day in ENGLISH_DAY.findall(message)}


def named_counts(message):
    counts = set()
    for digit, word in COUNT.findall(message):
        token = (digit or word).lower()
        counts.add(int(token) if token.isdigit() else NUMBERS[token])
    return counts


def clock(value):
    try:
        return local_time.fromisoformat(value) if isinstance(value, str) and re.fullmatch(r'\d{2}:\d{2}', value) else None
    except ValueError:
        return None


def grounded_windows(windows, message):
    """Keep time windows for days the student named; times come from the student's own words."""
    days = named_weekdays(message)
    part = next(((start, end) for pattern, start, end in PART_OF_DAY if pattern.search(message)), None)
    hours = {int(hour) for hour in re.findall(r'(\d{1,2})\s*(?::\d{2}|[點时時])', message)}
    result = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or as_int(window.get('weekday')) not in days:
            continue
        start, end = clock(window.get('start_time')), clock(window.get('end_time'))
        if hours:
            if start is None or end is None or start.hour not in hours or end.hour not in hours:
                continue
        elif part:
            start, end = map(local_time.fromisoformat, part)
        else:
            start, end = local_time.min, local_time(23, 59)
        if start < end:
            entry = recommendation.AvailableTime(weekday=window['weekday'], start_time=start, end_time=end)
            if entry not in result:
                result.append(entry)
    return result[:14]


def known_department(value):
    """A department alias from the catalog (電子系, 電機所, ...), without a school prefix."""
    name = school_prefix(normalize_name(value or ''))[0]
    return name if name and not name.isascii() and squash(name) in KNOWN_DEPARTMENTS else None


def mentioned_department(message):
    compact = squash(message)
    found = [name for name in DEPARTMENT_NAMES if not name.isascii() and squash(name) in compact]
    return max(found, key=len) if found else None


def ground_plan(raw, message):
    """Turn the model's proposal into a Plan. Fields the message does not support are dropped."""
    # OpenAI-compatible servers return free JSON, so every field is type-checked before use.
    if not isinstance(raw, dict) or as_text(raw.get('tool')) not in PLAN_TOOLS:
        raise llama.LlamaError('LLAMA_INVALID_OUTPUT')
    tool = raw['tool']
    department = grounded(raw.get('department'), message)
    if department:
        department = school_prefix(normalize_name(department))[0] or None
    school = roadmap.mentioned_school(message)
    keyword = grounded(raw.get('keyword'), message)
    # A small model sometimes puts the department the student named into the keyword slot.
    if keyword and known_department(keyword):
        department, keyword = department or known_department(keyword), None
    if not department and tool in ('courses', 'eligibility', 'recommend'):
        department = mentioned_department(message)
    if keyword and (squash(keyword) in GENERIC_KEYWORDS or CHINESE_DAY.search(keyword) or ENGLISH_DAY.search(keyword)
                    or (department and squash(department) in squash(keyword))
                    or any(squash(name) in squash(keyword) for names in SCHOOLS.values() for name in names)):
        keyword = None
    days = named_weekdays(message)
    weekday = as_int(raw.get('weekday'))
    weekday = weekday if weekday in days else (next(iter(days)) if len(days) == 1 else None)
    topic = grounded(raw.get('topic'), message) if tool == 'recommend' else None
    track = as_text(raw.get('track_code')) if as_text(raw.get('track_code')) in TRACKS else None
    if track and not (topic or track in roadmap.identify_tracks(message)):
        track = None
    count = as_int(raw.get('target_count'))
    sort = as_text(raw.get('sort')) if as_text(raw.get('sort')) in ('remaining', 'title') else 'code'
    if sort == 'remaining' and not re.search(r'剩餘|最多|most|remaining', message, re.I):
        sort = 'code'
    elif sort == 'title' and not re.search(r'課名|名稱|title|name', message, re.I):
        sort = 'code'
    return Plan(mode=MODE_BY_TOOL[tool], tool=tool, keyword=keyword, department=department, school_code=school,
                weekday=weekday, has_seats=bool(raw.get('has_seats') is True and SEATS.search(message)), sort=sort,
                topic=topic, track_code=track,
                target_count=count if count in named_counts(message) and 1 <= count <= 5 else 3,
                available_times=grounded_windows(raw.get('available_times'), message) if tool == 'recommend' else [])


def planner_prompt(state):
    context = {'selected_track_code': state.get('track_code'),
               'tracks': {code: track.title for code, track in TRACKS.items()}}
    return PLANNER_PROMPT + '\nContext: ' + json.dumps(context, ensure_ascii=False)


async def request_plan(message, *, system_instruction, meta):
    """Ask the local model for a plan proposal (a JSON object). Tests replace this seam."""
    return await llama.chat_json(system_instruction, message, PLAN_SCHEMA, meta=meta, max_tokens=320)


async def classify(state):
    if 'plan' in state:
        return {'plan': state['plan'], 'llm': {'local_rules': True}}
    if settings.chat_stub:
        return {'plan': stub_plan(state['message']), 'llm': {'stub': True}}
    # End all lookup transactions before waiting for the model. Only the masked question and
    # public track names leave the application, and only to the configured local model server.
    with SessionLocal() as lookup:
        clean = redact(lookup, state['user'], state['message'])
    evidence = {}
    raw = await request_plan(clean, system_instruction=planner_prompt(state), meta=evidence)
    plan = ground_plan(raw, state['message'])
    return {'plan': plan, 'llm': {**evidence, 'output': plan.model_dump(mode='json', exclude_defaults=True)}}


def agent_trace(state, tools, plan):
    """What planned the answer, the tool that finally ran, and the model server's own evidence."""
    llm = state.get('llm') or {'local_rules': True}
    model = 'local rules' if llm.get('local_rules') else english_agent.model_label()
    step = 'rules.plan_query' if llm.get('local_rules') else 'stub.plan_query' if llm.get('stub') else 'llama.plan_query'
    return {'graph': GRAPH_NAME, 'model': model, 'intent': plan.tool, 'tools': [step, *tools], 'llm': llm}


@lru_cache
def public_engine():
    if settings.public_database_url == settings.database_url:
        raise RuntimeError('Separate public database account required')
    return create_engine(settings.public_database_url, pool_pre_ping=True, pool_size=4, max_overflow=2, pool_timeout=2,
                         connect_args={'connect_timeout': 2, 'options': '-c default_transaction_read_only=on -c statement_timeout=2000'})


def query_public_courses(db, term_id, plan):
    school = school_code(plan.school_code)
    department_ids = None
    if plan.department or plan.department_id:
        departments = recommendation.find_departments(db, plan.department, plan.department_id, school=school, term_id=term_id)
        department_ids = [department.id for department in departments]
        if not department_ids:
            return []
    sort = {'code': 'c.code, c.section_no, c.section_id', 'title': 'c.title, c.section_id',
            'remaining': 'c.remaining DESC, c.code, c.section_id'}[plan.sort]
    sql = '''SELECT c.* FROM v_public_section_catalog c
             WHERE c.term_id = CAST(:term_id AS uuid)
             AND (CAST(:keyword AS text) IS NULL OR strpos(lower(c.code), :keyword)>0 OR strpos(lower(c.title), :keyword)>0)
             AND (CAST(:school AS text) IS NULL OR c.school_code=:school)
             AND (CAST(:departments AS uuid[]) IS NULL OR c.department_id=ANY(CAST(:departments AS uuid[])))
             AND (CAST(:topic AS text) IS NULL OR EXISTS (SELECT 1 FROM v_public_section_topics t
                         WHERE t.section_id=c.section_id AND t.topic_code=:topic))
             AND (CAST(:weekday AS integer) IS NULL OR EXISTS (SELECT 1 FROM v_public_section_meetings m
                        WHERE m.section_id=c.section_id AND m.weekday=:weekday))
             AND (NOT :has_seats OR (c.is_selectable AND c.remaining>0))
             ORDER BY ''' + sort + ' LIMIT :limit'
    with public_engine().connect() as connection:
        return [dict(row) for row in connection.execute(text(sql), {'term_id': term_id,
                'keyword': recommendation.normalize_keyword(plan.keyword) or None if plan.keyword else None,
                'departments': department_ids, 'school': school, 'topic': plan.topic_code,
                'weekday': plan.weekday, 'has_seats': plan.has_seats, 'limit': plan.limit}).mappings()]


def execute(state):
    db, user, plan = state['db'], state['user'], state['plan']
    tid = state['term_id'] or plan.term_id
    if tid:
        try:
            tid = str(UUID(tid))
        except ValueError:
            tid = None
    tools = []
    mode = plan.mode
    sources = []
    answer = ''
    count = 0
    preview = None
    # Local privacy/action guards cannot be overridden by a model's query plan.
    if re.search('他人|別人|其他同學|student\\d', state['message'], re.I):
        plan = Plan(mode='student_query', tool='privacy')
    elif (re.search('幫我.*(退選|取消登記)|替我.*(退選|取消登記)', state['message'])
          or (plan.tool != 'recommend' and re.search('幫我.*(加選|登記)|替我.*加選', state['message']))):
        plan = Plan(mode='student_query', tool='action')
    track_code = state.get('track_code')
    if track_code and plan.tool not in ('recommend', 'clarify', 'action', 'privacy'):
        plan = plan.model_copy(update={'mode': 'recommendation', 'tool': 'recommend'})
    if plan.tool != state['plan'].tool:
        # Record when a local guard or the selected research track replaced the model's choice.
        tools.append(('track.' if plan.tool == 'recommend' else 'guard.') + plan.tool)
    if not track_code and plan.tool == 'recommend':
        track_code = plan.track_code if plan.track_code in TRACKS else recommendation.topic_code(plan.topic or '')
    has_research_subject = bool(track_code or (plan.topic or '').strip() or any(word.strip() for word in plan.additional_keywords))
    if plan.tool == 'privacy':
        answer = '我只能查詢目前登入者本人的資料，無法提供其他學生的課表或選課結果。'
        sources = [source(title='我的課表', url='/timetable')]
        mode = 'student_query'
    elif plan.tool == 'action':
        answer = '可在課程查詢登記或查看班別，退選請前往我的課表。若要由助理加選，請明確輸入「我要加選」與單一課名或課號。'
        sources = [source(tid), source(tid, title='我的課表', url='/timetable' + ('?' + urlencode({'term': tid}) if tid else ''))]
        mode = 'student_query'
    elif plan.tool == 'clarify' or plan.mode == 'clarify' or (
            plan.tool == 'recommend' and not (has_research_subject or plan.department or plan.department_id)):
        answer, mode = '請告訴我想找的系所、研究主題或課程關鍵字。你也可以指定 1–5 門和可上課時間。', 'clarify'
        sources = [source(tid, title='課程查詢', url='/courses' if not tid else None)]
    elif plan.tool in ('rules', 'greeting'):
        answer = ('你好！我可以幫你查課程、課表、選課條件與選課規則。\n' if plan.tool == 'greeting' else '') + RULES
        sources = [source(title=f'選課規則 {RULES_VERSION}', url='/rules')]
        mode = 'simple_chat'
    elif not tid and not (plan.tool == 'recommend' and track_code):
        answer, mode = '請先在畫面選擇要查詢的學期。', 'clarify'
        sources = [source(title='選擇學期並查課', url='/courses')]
    elif plan.tool == 'recommend':
        school = roadmap.mentioned_school(state['message']) or school_code(plan.school_code)
        departments = [] if has_research_subject else recommendation.find_departments(
            db, plan.department, plan.department_id, school=school, term_id=tid,
        )
        if len(departments) > 1 and not school and state['message'].strip().startswith('我是'):
            campus = db.scalar(select(Department.school_id).join(Student, Student.department_id == Department.id)
                               .where(Student.id == user['student_id']))
            departments = [entry for entry in departments if entry.school_id == campus] or departments
        department = departments[0] if len(departments) == 1 else None
        if department is None and not has_research_subject:
            answer = ('找到多個同名系所。請一併指定學校。' if departments else
                      '找不到這個系所。請提供完整系所名稱、研究主題或課程關鍵字。')
            mode = 'clarify'
            sources = [source(tid)]
        else:
            if track_code:
                tid = state['term_id']
            create_draft = not track_code or roadmap.wants_plan(state['message'])
            preview = recommendation.build_selection_plan(
                public_engine(), user['student_id'], tid, plan.topic or '', plan.additional_keywords,
                plan.target_count, plan.available_times, department=department, has_seats=plan.has_seats,
                school=school, track_code=track_code, create_draft=create_draft,
            )
            mode, count = 'recommendation', len(preview['candidates'])
            tools.extend(('search_department_sections' if department is not None else 'search_related_sections',
                          'build_selection_plan'))
            sources = [source(tid, c['section_id'], f'{c["course_code"]} {c["course_title"]} {c["section_no"]}')
                       for c in preview['candidates']] or [source(tid)]
            if track_code and not create_draft:
                next_sections = set(preview['roadmap'].get('next_section_ids', ()))
                next_courses = '、'.join(c['course_title'] for c in preview['candidates'] if c['section_id'] in next_sections)
                answer = f'{TRACKS[track_code].title}的基礎、進階及研究專題如下。'
                if next_courses:
                    answer += f'這學期可優先考慮 {next_courses}。'
                elif not tid:
                    answer += '選擇學期後可檢查實際班別與資格。'
                else:
                    answer += '目前沒有符合選課條件的下一步班別，可先依路徑準備。'
            else:
                answer = recommendation.describe_plan(preview) if tid else '請先選擇學期，再確認有哪些班可排入課表。'
    else:
        term = reg.get_term(db, tid)
        if plan.tool == 'status':
            from zoneinfo import ZoneInfo
            status = reg.registration_status(db, term)
            tools.append('get_registration_status')
            answer = f'{term.code} 目前階段：{status["phase"]}。\n' + '\n'.join(
                f'{"初選" if p["kind"] == "INITIAL" else "加退選"}：{p["starts_at"].astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d %H:%M")} 至 {p["ends_at"].astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d %H:%M")}（臺北時間，截止時刻不再受理）'
                for p in status['periods'])
            sources = [source(tid)]
            mode = 'simple_chat'
        elif plan.tool in ('courses', 'eligibility'):
            rows = query_public_courses(db, tid, plan)
            tools.append('query_public_courses')
            count = len(rows)
            sources = [source(tid, r['section_id'], f'{r["code"]} {r["title"]} {r["section_no"]}') for r in rows]
            if not rows:
                answer = '查無符合條件的公開班別，請調整搜尋條件。'
                sources = [source(tid)]
            elif plan.tool == 'eligibility':
                if len(rows) != 1:
                    mode = 'clarify'
                    answer = '找到多個班別，請從下方連結選擇一個班別，再於詳情檢查選課條件。'
                else:
                    result = reg.eligibility(db, user['student_id'], tid, str(rows[0]['section_id']))
                    tools.append('check_my_eligibility')
                    answer = f'{rows[0]["title"]}：{result["message"]} 查詢階段：{result["phase"]}。'
                    mode = 'student_query'
            else:
                answer = f'找到 {len(rows)} 個班別（最多顯示 {plan.limit} 筆）：\n' + '\n'.join(
                    f'• {r["code"]} {r["title"]} {r["section_no"]}｜{r["school_name"]} {r["department_name"]}｜{r["instructor_name"]}｜剩餘 {r["remaining"]}/{r["capacity"]} 名｜{"開放選課" if r["is_selectable"] else "未開放"}' for r in rows)
                mode = 'sql_query'
        elif plan.tool == 'timetable':
            data = reg.get_timetable(db, user['student_id'], tid)
            tools.append('get_my_timetable')
            day = plan.weekday or mentioned_weekday(state['message'])
            parts, matches = [], []
            for key, heading in (('items', '正式課表'), ('pending_items', '初選待分發')):
                lines = []
                for row in data[key]:
                    meetings = [meeting for meeting in row['meetings'] if not day or meeting['weekday'] == day]
                    if day and not meetings:
                        continue
                    times = '、'.join(
                        f'星期{"一二三四五六日"[m["weekday"] - 1]} {m["start_time"]:%H:%M}-{m["end_time"]:%H:%M}'
                        for m in meetings)
                    lines.append(f'• {row["code"]} {row["title"]} ' + (times or '上課時間尚未安排'))
                    matches.append(row)
                if lines:
                    parts.append(heading + '\n' + '\n'.join(lines))
                elif key == 'items':
                    parts.append('目前沒有符合條件的正式選課。')
            parts.append(f'本學期正式選課合計 {data["total_credits"]} 學分，上限 {data["credit_limit"]} 學分。')
            answer = '\n'.join(parts)
            count = len(matches)
            sources = [source(tid, row['id'], row['title']) for row in matches] + [source(title='我的課表', url='/timetable')]
            mode = 'student_query'
        elif plan.tool == 'applications':
            data = reg.applications(db, user['student_id'], tid)
            tools.append('get_my_application_results')
            count = len(data['items'])
            answer = '你的初選紀錄：\n' + '\n'.join(f'• {r["title"]}：{r["status"]} {r["reason"] or ""}' for r in data['items']) if data['items'] else '本學期尚無初選登記紀錄。'
            sources = [source(tid, r['id'], r['title']) for r in data['items']] + [source(title='我的選課', url='/applications')]
            mode = 'student_query'
        elif plan.tool == 'my_selections':
            timetable = reg.get_timetable(db, user['student_id'], tid)
            pending, enrolled = timetable['pending_items'], timetable['items']
            tools.append('get_my_timetable')
            count = len(pending) + len(enrolled)
            answer = '\n\n'.join(f'{heading}：\n' + ('\n'.join(
                f'• {r["code"]} {r["title"]} {r["section_no"]}' for r in rows) or '目前沒有紀錄。')
                for heading, rows in (('初選待分發', pending), ('正式選課', enrolled)))
            sources = [source(tid, title='我的選課', url='/applications?' + urlencode({'term': tid})),
                       source(tid, title='我的課表', url='/timetable?' + urlencode({'term': tid}))]
            mode = 'student_query'
    as_of = preview['registration']['as_of'] if preview and preview['registration'] else datetime.now(timezone.utc)
    result = {'mode': mode, 'answer': answer, 'sources': sources, 'as_of': as_of.isoformat(),
              'request_id': state['request_id'], 'agent': agent_trace(state, tools, plan)}
    if preview is not None:
        result['recommendation'] = preview
    return {'result': result, 'tools': tools, 'count': count}


builder = StateGraph(State)
builder.add_node('intent', classify)
builder.add_node('read_tools', execute)
builder.add_edge(START, 'intent')
builder.add_edge('intent', 'read_tools')
builder.add_edge('read_tools', END)
graph = builder.compile()


async def handle_chat(db, user, message, term_id, request_id, *, request_key=None, digest=None,
                      channel='CHAT_TEXT', selected_section=None, track_context=None, deadline=None):
    message = message.strip()
    if not message:
        raise DomainError(422, 'EMPTY_MESSAGE', '請輸入選課問題。')
    term_id = resolve_term_id(term_id)
    if digest is None and request_key:
        digest = fingerprint(term_id, channel, message.encode('utf-8'), selected_section)
        replay = await run_in_threadpool(existing_response, user, term_id, request_key, digest)
        if replay is not None:
            return replay
    if channel == 'CHAT_TEXT' and english_agent.handles(message, roadmap.identify_tracks):
        # Teacher specification: English add/drop through LangGraph and Llama 3.2 3B.
        return await english_agent.handle(user, message, term_id, request_id, request_key=request_key,
                                          digest=digest, selected_section=selected_section)
    command = parse_command(message)
    if selected_section and (command is None or not command.target):
        raise DomainError(422, 'INVALID_CHAT_CHOICE', '選擇班別時須提供原本的加選指令。')
    if command is not None:
        return await run_in_threadpool(handle_command, user, command, term_id, request_key, digest, request_id,
                                       channel, selected_section, deadline)

    plan = local_plan(message)
    tracks = []
    if plan is None and not re.search(r'^(?:請)?(?:查詢|查|列出)|剩餘名額|能加選|可加選', message):
        tracks = roadmap.identify_tracks(message)
    if not tracks and track_context in TRACKS and roadmap.wants_plan(message):
        tracks = [track_context]
    if len(tracks) > 1:
        return {'mode': 'clarify', 'answer': '請選擇一個研究方向，我會依方向提供修課路徑。',
                'track_choices': [{'track_code': code, 'title': TRACKS[code].title} for code in tracks],
                'sources': [source(term_id)], 'request_id': request_id, 'as_of': datetime.now(timezone.utc)}
    track = tracks[0] if tracks else None
    if track and plan is None and (roadmap.is_quick_plan_request(message)
                                   or settings.chat_stub or not settings.llama_base_url):
        plan = local_roadmap_plan(message, track)
    if plan is None and not track and (roadmap.DIRECTION_REQUEST.search(message) or roadmap.wants_plan(message)) and not re.search(
        r'研究方向(?:是|為|[:：])|推薦.+(?:相關|領域|方向)', message,
    ):
        plan = Plan(mode='clarify', tool='clarify')
    if plan is None and not settings.chat_stub and not settings.llama_base_url:
        log.warning(json.dumps({'request_id': request_id, 'mode': 'unavailable', 'reason_code': 'LLAMA_NOT_CONFIGURED'}))
        raise unavailable(term_id, 'LLAMA_NOT_CONFIGURED')
    begin = time.monotonic()
    state: State = {'db': db, 'user': user, 'message': message, 'term_id': term_id, 'request_id': request_id,
                    'track_code': track}
    if plan is not None:
        state['plan'] = plan
    try:
        # The local model may need to load into memory on its first request.
        result = await asyncio.wait_for(graph.ainvoke(state), timeout=settings.llama_timeout_seconds + 5)
        agent = result['result']['agent']
        # Log the plan and the model server's own evidence, never the student's message.
        log.info(json.dumps({'request_id': request_id, 'graph': GRAPH_NAME, 'model': agent['model'],
                             'mode': result['result']['mode'], 'intent': agent['intent'], 'tools': agent['tools'],
                             'llm': agent['llm'], 'elapsed_ms': round((time.monotonic()-begin)*1000),
                             'result_count': result.get('count', 0)}, ensure_ascii=False, default=str))
        return result['result']
    except DomainError:
        raise
    except Exception as error:
        reason_code = failure_reason(error)
        log.warning(json.dumps({'request_id': request_id, 'graph': GRAPH_NAME, 'mode': 'unavailable',
                                'reason_code': reason_code, 'error_class': type(error).__name__}))
        raise unavailable(term_id, reason_code) from None
