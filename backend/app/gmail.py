"""Gmail API delivery (GMAIL_TRANSPORT=api; teacher item T-03 names "Email Service: Gmail API").

The default transport is Gmail SMTP with an app password (see worker.deliver_smtp). The message
builders below are shared by both transports.

The worker exchanges the configured OAuth refresh token for an access token with the
``https://www.googleapis.com/auth/gmail.send`` scope, then calls ``users.messages.send``
with a base64url-encoded RFC 2822 message. Google's message ID is stored as delivery
evidence. Only fixed error codes are stored; provider responses can contain private data.
"""
import base64
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import urlencode

import httpx

from .config import settings

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'
DAYS = '一二三四五六日'
TAIPEI = ZoneInfo('Asia/Taipei')
# Used only when no sending account is configured (the Mailpit preview).
PREVIEW_SENDER = 'CourseFlow 選課通知 <courseflow@example.test>'
GMAIL_HINTS = {
    'GMAIL_NOT_CONFIGURED': '尚未設定 GMAIL_CLIENT_ID、GMAIL_CLIENT_SECRET、GMAIL_REFRESH_TOKEN、GMAIL_SENDER。'
                            '設定後重建 api 與 worker，再執行 python -m app.manage retry-failed 重寄。',
    'GMAIL_AUTH_FAILED': 'OAuth 用戶端或 refresh token 無效或已撤銷，請以 scripts/gmail-oauth.py 重新取得。',
    'GMAIL_FORBIDDEN': '帳號未授權 gmail.send，或 GMAIL_SENDER 不是授權帳號本身或其已驗證的別名。',
    'GMAIL_REJECTED': 'Gmail API 拒絕這封信（例如收件地址無效），請檢查收件人信箱。',
    'GMAIL_RATE_LIMITED': '超過 Gmail API 速率或每日寄信上限，稍後會自動重試。',
    'GMAIL_TIMEOUT': '連線 Gmail API 逾時，稍後會自動重試。',
    'GMAIL_UNAVAILABLE': 'Gmail API 暫時無法使用，稍後會自動重試。',
    'GMAIL_INVALID_RESPONSE': 'Gmail API 回應格式不正確，稍後會自動重試。',
}


class GmailError(Exception):
    """``permanent`` errors (configuration, authorization, rejected message) are not retried."""
    def __init__(self, code, *, permanent):
        self.code = code
        self.permanent = permanent
        super().__init__(code)


_token_lock = threading.Lock()
_token_cache: dict = {}


def setting_problems():
    """Names of missing or malformed GMAIL_* settings. Values are never returned or logged."""
    values = {'GMAIL_CLIENT_ID': settings.gmail_client_id.strip(),
              'GMAIL_CLIENT_SECRET': settings.gmail_client_secret.get_secret_value().strip(),
              'GMAIL_REFRESH_TOKEN': settings.gmail_refresh_token.get_secret_value().strip(),
              'GMAIL_SENDER': settings.gmail_sender.strip()}
    missing = [name for name, value in values.items() if not value]
    malformed = []
    if values['GMAIL_CLIENT_ID'] and not values['GMAIL_CLIENT_ID'].endswith('.apps.googleusercontent.com'):
        malformed.append('GMAIL_CLIENT_ID')
    if any(value.startswith(('<', '"', "'")) or ' ' in value
           for value in (values['GMAIL_CLIENT_SECRET'], values['GMAIL_REFRESH_TOKEN'])):
        malformed.extend(name for name in ('GMAIL_CLIENT_SECRET', 'GMAIL_REFRESH_TOKEN')
                         if values[name].startswith(('<', '"', "'")) or ' ' in values[name])
    if values['GMAIL_SENDER'] and '@' not in values['GMAIL_SENDER']:
        malformed.append('GMAIL_SENDER')
    return missing, malformed


def taipei_time(value):
    """The outbox stores ISO timestamps in UTC; students read Taipei time everywhere else."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TAIPEI).strftime('%Y/%m/%d %H:%M')


def sender():
    """Gmail only sends as the signed-in account (SMTP) or the authorized account/alias (API)."""
    if settings.gmail_transport == 'api':
        return settings.gmail_sender.strip() or PREVIEW_SENDER
    return settings.smtp_from.strip() or settings.smtp_user.strip() or PREVIEW_SENDER


def build_confirmation(job):
    """Render one confirmation. ``job`` carries the outbox payload and recipient address."""
    p = job['payload']
    message = EmailMessage()
    message['From'] = sender()
    message['To'] = job['email']
    message['Subject'] = f'{p["term_code"]} 選課確認 Course registration confirmed'
    message['Message-ID'] = make_msgid(idstring=f'outbox-{job["id"]}', domain='courseflow.example.test')
    label = {'ADD': '即時加選', 'PLAN': '試排批次加選', 'ALLOCATION': '初選分發錄取'}.get(p['operation'], '正式選課')
    lines = [f'{p["term_code"]} 選課確認（{label}）', '',
             '下列課程已完成正式選課（ENROLLED）：', 'The following sections are now in your timetable:']
    for item in p['items']:
        lines.append(f'• {item["code"]} {item["title"]} 第 {item["section_no"]} 班 · {item["credits"]} 學分'
                     f' · {item["school_name"]} {item["department_name"]}')
        for meeting in item.get('meetings', []):
            lines.append(f'  週{DAYS[meeting["weekday"] - 1]} {meeting["start_time"][:5]}–{meeting["end_time"][:5]}'
                         f' {meeting.get("room") or ""}'.rstrip())
    lines += ['', f'本學期正式選課合計 {p["total_credits"]} 學分（上限 {p["credit_limit"]} 學分）。',
              f'Registered total: {p["total_credits"]} of {p["credit_limit"]} credits.',
              f'提交時間 Submitted at: {taipei_time(p["submitted_at"])}（臺北時間 Taipei time）']
    path = p.get('url') or '/timetable?' + urlencode({'term': job['term_id']})
    lines += ['', '查看我的課表 View my timetable: ' + settings.app_origin.rstrip('/') + path,
              '', '此信由 CourseFlow 選課系統寄出。']
    message.set_content('\n'.join(lines))
    return message


def encode(message):
    return base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')


def _status_error(response, *, auth_step):
    status = response.status_code
    if status in (408, 429):
        return GmailError('GMAIL_RATE_LIMITED' if status == 429 else 'GMAIL_TIMEOUT', permanent=False)
    if status >= 500:
        return GmailError('GMAIL_UNAVAILABLE', permanent=False)
    if auth_step:
        # invalid_grant / invalid_client: the refresh token or client must be replaced.
        return GmailError('GMAIL_AUTH_FAILED', permanent=True)
    if status == 401:
        return GmailError('GMAIL_AUTH_FAILED', permanent=False)  # token may have expired; refresh and retry
    if status == 403:
        return GmailError('GMAIL_FORBIDDEN', permanent=True)
    return GmailError('GMAIL_REJECTED', permanent=True)


def access_token(client, *, force=False):
    if not settings.gmail_configured:
        raise GmailError('GMAIL_NOT_CONFIGURED', permanent=True)
    with _token_lock:
        cached = _token_cache.get('token')
        if cached and not force and cached[1] > time.monotonic() + 60:
            return cached[0], cached[2]
        try:
            response = client.post(TOKEN_URL, data={
                'client_id': settings.gmail_client_id.strip(),
                'client_secret': settings.gmail_client_secret.get_secret_value().strip(),
                'refresh_token': settings.gmail_refresh_token.get_secret_value().strip(),
                'grant_type': 'refresh_token',
            })
        except httpx.TimeoutException:
            raise GmailError('GMAIL_TIMEOUT', permanent=False) from None
        except httpx.HTTPError:
            raise GmailError('GMAIL_UNAVAILABLE', permanent=False) from None
        if response.status_code != 200:
            raise _status_error(response, auth_step=True)
        try:
            data = response.json()
            token, lifetime, scope = data['access_token'], int(data.get('expires_in', 3600)), data.get('scope', '')
        except (ValueError, KeyError, TypeError):
            raise GmailError('GMAIL_AUTH_FAILED', permanent=False) from None
        _token_cache['token'] = (token, time.monotonic() + lifetime, scope)
        return token, scope


def send_message(message, *, transport=None):
    """Send through ``users.messages.send``; return Gmail's message ID."""
    with httpx.Client(timeout=10, transport=transport) as client:
        token, _ = access_token(client)
        for attempt in (1, 2):
            try:
                response = client.post(SEND_URL, json={'raw': encode(message)},
                                       headers={'Authorization': f'Bearer {token}'})
            except httpx.TimeoutException:
                raise GmailError('GMAIL_TIMEOUT', permanent=False) from None
            except httpx.HTTPError:
                raise GmailError('GMAIL_UNAVAILABLE', permanent=False) from None
            if response.status_code == 401 and attempt == 1:
                token, _ = access_token(client, force=True)
                continue
            if response.status_code != 200:
                raise _status_error(response, auth_step=False)
            try:
                message_id = response.json()['id']
            except (ValueError, KeyError, TypeError):
                raise GmailError('GMAIL_INVALID_RESPONSE', permanent=False) from None
            if not isinstance(message_id, str) or not message_id or len(message_id) > 200:
                raise GmailError('GMAIL_INVALID_RESPONSE', permanent=False)
            return message_id
    raise GmailError('GMAIL_AUTH_FAILED', permanent=False)


def clear_token_cache():
    with _token_lock:
        _token_cache.clear()
