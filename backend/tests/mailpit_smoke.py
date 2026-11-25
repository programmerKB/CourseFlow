"""Opt-in end-to-end SMTP failure/retry/recovery against a local Mailpit (no Gmail account needed)."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import httpx
from app import worker
from app.config import settings
from app.db import SessionLocal
from app.models import NotificationOutbox
from app.seed import uid

settings.gmail_transport='smtp'
tid=uid('term/main');sid=uid('section/main/CS101/A')
worker.run_once()  # Drain older demonstration events into the preview inbox.
with httpx.Client(base_url=os.environ.get('API_URL','http://127.0.0.1:8000')) as c:
    c.post('/api/v1/auth/login',json={'login':'student02','password':'Demo123!'}).raise_for_status()
    r=c.post(f'/api/v1/me/terms/{tid}/enrollments',json={'section_id':sid},headers={'Idempotency-Key':str(uuid4())})
    r.raise_for_status()
    r=c.post(f'/api/v1/me/terms/{tid}/timetable/email',headers={'Idempotency-Key':str(uuid4())})
    r.raise_for_status()
    job_id=r.json()['notification_id']
    good_port=settings.smtp_port
    try:
        settings.smtp_port=1  # Real refused TCP connection after the request has committed.
        assert worker.process_one()
    finally:
        settings.smtp_port=good_port
    with SessionLocal.begin() as db:
        job=db.get(NotificationOutbox,job_id)
        assert job.type=='TIMETABLE' and job.recipient_user_id==uid('user/student02')
        assert job.status=='PENDING' and job.attempt_count==1 and job.last_error=='SMTP_UNREACHABLE'
        job.next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    assert worker.process_one()
    with SessionLocal() as db:
        job=db.get(NotificationOutbox,job_id)
        assert job.status=='SENT' and job.attempt_count==2
        message_id=job.provider_message_id.removeprefix('smtp:')
    mailpit=os.environ.get('MAILPIT_URL','http://127.0.0.1:8025')
    messages=httpx.get(mailpit+'/api/v1/messages').raise_for_status().json()
    message=next(row for row in messages['messages'] if row['MessageID'].strip('<>')==message_id)
    content=httpx.get(mailpit+'/api/v1/message/'+message['ID']).raise_for_status().json()
    assert '每週課表' in content['Subject'] and '週一 09:00–10:30 TR-211' in content['Text']
    c.delete(f'/api/v1/me/terms/{tid}/enrollments/{sid}',headers={'Idempotency-Key':str(uuid4())}).raise_for_status()
worker.run_once()
report={'timestamp':datetime.now(timezone.utc).isoformat(),'timetable_email_status':201,'smtp_failure':'SMTP_UNREACHABLE',
        'retry_attempt':2,'final_status':'SENT','mailpit_message_count':messages['total'],
        'message_id':f'<{message_id}>'}
out=Path(__file__).resolve().parents[2]/'docs'/'mailpit-verification.json'
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
