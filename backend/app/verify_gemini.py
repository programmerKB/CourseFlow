"""Verify real Gemini course search and research recommendations against PostgreSQL."""
import argparse
import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select, text
from .config import settings
from .db import SessionLocal
from .models import User, Student, Term
from .auth import user_dict
from .chat import failure_reason, handle_chat
from .gemini_diagnostics import safe_provider_diagnostic
from .gemini_rest import generate_content
from .seed import uid


COURSE_QUESTION = '找這學期星期二還有空位的電子系課'
RESEARCH_QUESTION = '我的研究方向是大語言模型，請推薦這學期相關的課，幫我選三門。'
REFERENCE_COURSES = ('DEMO-501', 'DEMO-502', 'DEMO-503', 'DEMO-504', 'DEMO-505')


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


async def check_connection():
    # Synthetic public input, no database or structured-output configuration.
    await generate_content('Reply with OK.')


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


def check_course_result(result, expected):
    actual = {item['section_id'] for item in result.get('sources', []) if item.get('section_id')}
    if result.get('mode') != 'sql_query' or actual != expected:
        raise VerificationError('VERIFY_COURSE_QUERY_MISMATCH')


def check_recommendation_result(result, expected):
    recommendation = result.get('recommendation') or {}
    candidates = recommendation.get('candidates', [])
    actual = {item['section_id']: item['course_code'] for item in candidates}
    linked = {item['section_id'] for item in result.get('sources', []) if item.get('section_id')}
    if (result.get('mode') != 'recommendation' or recommendation.get('topic') != '大語言模型'
            or not set(REFERENCE_COURSES).issubset(actual.values()) or linked != set(actual)
            or any(expected.get(section) != code for section, code in actual.items())
            or not all(entry.get('match_reason') for entry in candidates)):
        raise VerificationError('VERIFY_RECOMMENDATION_MISMATCH')
    # The report may be archived. Keep only the public evidence and omit the
    # authenticated student's timetable and the private confirmation draft.
    return {'mode': result['mode'], 'as_of': result['as_of'], 'topic': recommendation['topic'],
            'candidates': [{key: item[key] for key in ('section_id', 'course_code', 'course_title', 'score', 'match_reason')}
                           for item in candidates], 'sources': result['sources']}


def main(connection_only=False):
    if settings.chat_stub or not settings.gemini_api_key.strip():
        raise SystemExit('Real verification requires GEMINI_API_KEY and CHAT_STUB=false. No API call was made.')
    if connection_only:
        asyncio.run(check_connection())
        print('Gemini connection succeeded. Structured output and the course query have not been verified.')
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
        check_course_result(result, course_ids)
    with SessionLocal() as db:
        research = asyncio.run(handle_chat(db, actor, RESEARCH_QUESTION, term_id, str(uuid4())))
        evidence = check_recommendation_result(research, research_courses)
    record = {'status': 'executed', 'model': settings.gemini_model, 'timestamp': datetime.now(timezone.utc).isoformat(),
              'query': COURSE_QUESTION, 'response': result,
              'research_query': RESEARCH_QUESTION, 'recommendation': evidence}
    # The image runs as a non-root user; /app contains root-owned source files.
    output=Path(tempfile.gettempdir()) / 'gemini-verification.json'
    output.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(f'Real Gemini course search and recommendation verified. Record: {output.resolve()}')


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection-only', action='store_true',
                        help='Send only a minimal public Gemini request, without database access or a query schema.')
    args = parser.parse_args(argv)
    try:
        main(connection_only=args.connection_only)
    except Exception as error:
        model = settings.gemini_model
        diagnostic = {'status': 'failed',
                      'model': model if re.fullmatch(r'(?:models/)?gemini-[A-Za-z0-9._-]{1,80}', model) else '[invalid model name]',
                      'stage': 'connection' if args.connection_only else 'course_query',
                      'reason_code': error.code if isinstance(error, VerificationError) else failure_reason(error),
                      **safe_provider_diagnostic(error)}
        if isinstance(error, VerificationError):
            diagnostic['hint'] = ('請先建立示範學期與課程，並確認電子系示範班仍有名額。' if error.code == 'VERIFY_DEMO_DATA_REQUIRED'
                                  else 'Gemini 結果未通過資料庫比對，請檢查實際回應與意圖解析。')
        if args.connection_only and not diagnostic.get('provider_reasons') and not diagnostic.get('request_fields'):
            diagnostic['hint'] = '最小請求已失敗。請使用 scripts/diagnose-gemini.py 查看遮蔽金鑰的 Google 訊息。'
        print(json.dumps(diagnostic, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__': cli()
