from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'postgresql+psycopg://course_app:course_app@localhost:5432/courses'
    public_database_url: str = 'postgresql+psycopg://course_public:course_public@localhost:5432/courses'
    app_origin: str = 'http://localhost:8080'
    cookie_secure: bool = False
    session_hours: int = 24
    gemini_model: str = 'gemini-3.5-flash-lite'
    gemini_api_key: str = ''
    chat_stub: bool = False
    smtp_host: str = 'mailpit'
    smtp_port: int = 1025
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_from: str = '選課通知 <courses@example.test>'
    smtp_starttls: bool = False


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
