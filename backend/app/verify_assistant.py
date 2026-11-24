"""Verify the Chinese assistant's course search and research recommendations with the real local
Llama model, comparing each answer with PostgreSQL.

Usage (inside the API container):
  python -m app.verify_assistant                    # course query + recommendation, saves a record
  python -m app.verify_assistant --connection-only  # model server and installed model only

The record in /tmp/assistant-verification.json holds public course data and the model server's
token counts and timings. It omits the student's timetable and the confirmation draft.
"""
import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text

from . import llama
from .auth import user_dict
from .chat import failure_reason, handle_chat
from .config import settings
from .db import SessionLocal
from .errors import DomainError
from .models import Student, Term, User
from .seed import uid

COURSE_QUESTION = '電子系本學期有哪些在星期二上課、而且還有空位的班別？'
RESEARCH_QUESTION = '我的研究方向是大語言模型，請推薦這學期相關的課，幫我選三門。'
REFERENCE_COURSES = ('DEMO-501', 'DEMO-502', 'DEMO-503', 'DEMO-504', 'DEMO-505')
HINTS = {
    'VERIFY_DEMO_DATA_REQUIRED': '請先建立示範學期與課程，並確認電子系示範班仍有名額。',
    'VERIFY_COURSE_QUERY_MISMATCH': '模型的查詢計畫未通過資料庫比對，請查看 API 日誌的 intent 與 llm 欄位。',
    'VERIFY_RECOMMENDATION_MISMATCH': '推薦結果未通過資料庫比對，請查看 API 日誌的 intent 與 llm 欄位。',
    'VERIFY_MODEL_NOT_CALLED': '回應不是由本機模型產生（可能是 CHAT_STUB 或本機規則），請確認設定。',
}


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def expected_results(db, term_id):
    course_ids = set(db.scalars(text('''
        SELECT c.section_id
        FROM v_public_section_catalog c
        WHERE c.term_id = :term_id AND c.department_name LIKE '%電子工程%' AND c.is_selectable AND c.remaining > 0
        AND EXISTS (SELECT 1 FROM v_public_section_meetings m WHERE m.section_id = c.section_id AND m.weekday = 2)
        ORDER BY c.code, c.section_no, c.section_id LIMIT 20
    '''), {'term_id': term_id}))
    research_courses = {str(row.section_id): row.code for row in db.execute(text('''
        SELECT section_id, code FROM v_public_section_catalog WHERE term_id = :term_id
    '''), {'term_id': term_id})}
    if not course_ids or not set(REFERENCE_COURSES).issubset(research_courses.values()):
        raise VerificationError('VERIFY_DEMO_DATA_REQUIRED')
    return {str(section_id) for section_id in course_ids}, research_courses


def model_evidence(result):
    """The model server's own report for this answer; local rules or the stub do not count."""
    agent = result.get('agent') or {}
    llm = agent.get('llm') or {}
    if 'llama.plan_query' not in agent.get('tools', []) or not llm.get('server_model'):
        raise VerificationError('VERIFY_MODEL_NOT_CALLED')
    return {'intent': agent['intent'], 'tools': agent['tools'], 'llm': llm}


def check_course_result(result, expected):
    actual = {item['section_id'] for item in result.get('sources', []) if item.get('section_id')}
    if result.get('mode') != 'sql_query' or actual != expected:
        raise VerificationError('VERIFY_COURSE_QUERY_MISMATCH')
    return model_evidence(result)


def check_recommendation_result(result, expected):
    recommendation = result.get('recommendation') or {}
    candidates = recommendation.get('candidates', [])
    actual = {item['section_id']: item['course_code'] for item in candidates}
    linked = {item['section_id'] for item in result.get('sources', []) if item.get('section_id')}
    roadmap = recommendation.get('roadmap') or {}
    if (result.get('mode') != 'recommendation' or roadmap.get('track_code') != 'llm'
            or not set(REFERENCE_COURSES).intersection(actual.values()) or linked != set(actual)
            or any(expected.get(section) != code for section, code in actual.items())
            or not all(entry.get('match_reason') for entry in candidates)):
        raise VerificationError('VERIFY_RECOMMENDATION_MISMATCH')
    # The report may be archived. Keep only the public evidence and omit the
    # authenticated student's timetable and the private confirmation draft.
    return {'mode': result['mode'], 'as_of': result['as_of'], 'track_code': roadmap['track_code'],
            'target_count': recommendation.get('target_count'),
            'candidates': [{key: item[key] for key in ('section_id', 'course_code', 'course_title', 'score', 'match_reason')}
                           for item in candidates], 'sources': result['sources'], 'agent': model_evidence(result)}


def main(connection_only=False):
    if settings.chat_stub or not settings.llama_base_url:
        raise SystemExit('Real verification requires LLAMA_BASE_URL and CHAT_STUB=false. No model call was made.')
    if connection_only:
        status = asyncio.run(llama.server_status())
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if not status['ok']:
            raise SystemExit(1)
        print('The local model server answers and has the model. Query plans have not been verified.')
        return
    term_id = settings.public_initial_term_id or uid('term/main')
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.login == 'student01'))
        student = db.scalar(select(Student).where(Student.user_id == user.id)) if user else None
        if student is None or db.get(Term, term_id) is None:
            raise VerificationError('VERIFY_DEMO_DATA_REQUIRED')
        actor = user_dict(user, student)
        course_ids, research_courses = expected_results(db, term_id)
    with SessionLocal() as db:
        result = asyncio.run(handle_chat(db, actor, COURSE_QUESTION, term_id, str(uuid4())))
        course_agent = check_course_result(result, course_ids)
    with SessionLocal() as db:
        research = asyncio.run(handle_chat(db, actor, RESEARCH_QUESTION, term_id, str(uuid4())))
        evidence = check_recommendation_result(research, research_courses)
    record = {'status': 'executed', 'model': settings.llama_model, 'endpoint_style': settings.llama_api_style,
              'timestamp': datetime.now(timezone.utc).isoformat(),
              'query': COURSE_QUESTION, 'response': {key: result[key] for key in ('mode', 'answer', 'sources', 'as_of')},
              'query_agent': course_agent, 'research_query': RESEARCH_QUESTION, 'recommendation': evidence}
    # The image runs as a non-root user; /app contains root-owned source files.
    output = Path(tempfile.gettempdir()) / 'assistant-verification.json'
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + '\n')
    print(f'Local-model course search and recommendation verified. Record: {output.resolve()}')


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--connection-only', action='store_true',
                        help='Check the model server and installed model only, without database access.')
    args = parser.parse_args(argv)
    try:
        main(connection_only=args.connection_only)
    except VerificationError as error:
        print(json.dumps({'status': 'failed', 'model': settings.llama_model, 'reason_code': error.code,
                          'hint': HINTS[error.code]}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    except (DomainError, llama.LlamaError, TimeoutError) as error:
        reason = ((error.details or {}).get('reason_code', error.code) if isinstance(error, DomainError)
                  else failure_reason(error))
        print(json.dumps({'status': 'failed', 'model': settings.llama_model, 'reason_code': reason,
                          'hint': '請先執行 python -m app.verify_llama --server-only 檢查模型服務。'},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    cli()
