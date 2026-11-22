"""Run a real public-course Gemini query through exactly the production graph."""
import argparse
import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from .config import settings
from .db import SessionLocal
from .models import User, Student, Term
from .auth import user_dict
from .chat import failure_reason, handle_chat
from .gemini_diagnostics import safe_provider_diagnostic
from .gemini_rest import generate_content


async def check_connection():
    # Synthetic public input, no database or structured-output configuration.
    await generate_content('Reply with OK.')


def main(connection_only=False):
    if settings.chat_stub or not settings.gemini_api_key.strip():
        raise SystemExit('Real verification requires GEMINI_API_KEY and CHAT_STUB=false. No API call was made.')
    if connection_only:
        asyncio.run(check_connection())
        print('Gemini connection succeeded. Structured output and the course query have not been verified.')
        return
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.login=='student01'))
        student = db.scalar(select(Student).where(Student.user_id==user.id))
        term = db.scalar(select(Term).where(Term.catalog_published_at.is_not(None)).order_by(Term.start_date))
        query='找這學期星期二還有空位的電子系課'
        result=asyncio.run(handle_chat(db,user_dict(user,student),query,term.id,str(uuid4())))
        record={'status':'executed','model':settings.gemini_model,'timestamp':datetime.now(timezone.utc).isoformat(),
                'query':query,'response':result}
    # The image runs as a non-root user; /app contains root-owned source files.
    output=Path(tempfile.gettempdir()) / 'gemini-verification.json'
    output.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(f'Real Gemini query completed. Record: {output.resolve()}')


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
                      'reason_code': failure_reason(error),
                      **safe_provider_diagnostic(error)}
        if args.connection_only and not diagnostic.get('provider_reasons') and not diagnostic.get('request_fields'):
            diagnostic['hint'] = '最小請求已失敗。請使用 scripts/diagnose-gemini.py 查看遮蔽金鑰的 Google 訊息。'
        print(json.dumps(diagnostic, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__': cli()
