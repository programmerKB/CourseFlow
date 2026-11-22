"""Opt-in end-to-end SMTP failure/retry/recovery against a local Mailpit."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import httpx
from sqlalchemy import select
from app import worker
from app.config import settings
from app.db import SessionLocal
from app.models import NotificationOutbox
from app.seed import uid

tid=uid('term/main');sid=uid('section/main/CS101/A')
worker.run_once()  # Drain older demonstration events using the configured real SMTP.
with httpx.Client(base_url=os.environ.get('API_URL','http://127.0.0.1:8000')) as c:
    c.post('/api/v1/auth/login',json={'login':'student02','password':'Demo123!'}).raise_for_status()
    r=c.post(f'/api/v1/me/terms/{tid}/enrollments',json={'section_id':sid},headers={'Idempotency-Key':str(uuid4())})
    r.raise_for_status()
    good_port=settings.smtp_port
    settings.smtp_port=1  # Real refused TCP connection, without interrupting the API transaction.
    assert worker.process_one()
    settings.smtp_port=good_port
    with SessionLocal.begin() as db:
        job=db.scalar(select(NotificationOutbox).where(NotificationOutbox.recipient_user_id==uid('user/student02'),NotificationOutbox.status=='PENDING'))
        assert job and job.attempt_count==1 and job.last_error=='ConnectionRefusedError'
        job_id=job.id
        job.next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    assert worker.process_one()
    with SessionLocal() as db:
        job=db.get(NotificationOutbox,job_id)
        assert job.status=='SENT' and job.attempt_count==2
    messages=httpx.get(os.environ.get('MAILPIT_URL','http://127.0.0.1:8025')+'/api/v1/messages').json()
    assert messages['total']>=1
    c.delete(f'/api/v1/me/terms/{tid}/enrollments/{sid}',headers={'Idempotency-Key':str(uuid4())}).raise_for_status()
worker.run_once()
report={'timestamp':datetime.now(timezone.utc).isoformat(),'enrollment_status':201,'smtp_failure':'ConnectionRefusedError',
        'retry_attempt':2,'final_status':'SENT','mailpit_message_count':messages['total'],
        'message_id':f'<outbox-{job_id}@course-registration.example.test>'}
out=Path(__file__).resolve().parents[2]/'docs'/'mailpit-verification.json'
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
