import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import Depends, Request
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .errors import DomainError

hasher = PasswordHasher()
COOKIE_NAME = 'course_session'


def hash_password(password):
    return hasher.hash(password)


def user_dict(user, student=None, instructor=None):
    return {'id': user.id, 'login': user.login, 'role': user.role,
            'display_name': user.display_name, 'student_id': student.id if student else None,
            'instructor_id': instructor.id if instructor else None}


def linked_identity(db, user):
    """Each role reads only its own profile: a student row or an instructor row."""
    from .models import Student, Instructor
    student = db.scalar(select(Student).where(Student.user_id == user.id)) if user.role == 'STUDENT' else None
    instructor = db.scalar(select(Instructor).where(Instructor.user_id == user.id)) if user.role == 'INSTRUCTOR' else None
    return user_dict(user, student, instructor)


def login(db, login_name, password):
    from .models import User, AuthSession
    user = db.scalar(select(User).where(User.login == login_name.strip().lower(), User.is_active.is_(True)))
    try:
        valid = user is not None and hasher.verify(user.password_hash, password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid:
        raise DomainError(401, 'INVALID_CREDENTIALS', '帳號或密碼不正確。')
    token = secrets.token_urlsafe(48)
    db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                       expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)))
    return token, linked_identity(db, user)


def current_user(request: Request):
    from .models import User, AuthSession
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise DomainError(401, 'AUTH_REQUIRED', '請先登入。')
    with SessionLocal() as db:
        session = db.get(AuthSession, hashlib.sha256(token.encode()).hexdigest())
        if not session or session.revoked_at or session.expires_at <= datetime.now(timezone.utc):
            raise DomainError(401, 'SESSION_EXPIRED', '登入已過期，請重新登入。')
        user = db.get(User, session.user_id)
        if not user or not user.is_active:
            raise DomainError(401, 'AUTH_REQUIRED', '帳號已停用。')
        return linked_identity(db, user)


def student_user(user=Depends(current_user)):
    if user['role'] != 'STUDENT' or not user['student_id']:
        raise DomainError(403, 'STUDENT_REQUIRED', '此操作限學生使用。')
    return user


def instructor_user(user=Depends(current_user)):
    if user['role'] != 'INSTRUCTOR' or not user['instructor_id']:
        raise DomainError(403, 'INSTRUCTOR_REQUIRED', '此操作限授課教師使用。')
    return user


def admin_user(user=Depends(current_user)):
    if user['role'] != 'ADMIN':
        raise DomainError(403, 'ADMIN_REQUIRED', '此操作限教務管理員使用。')
    return user
