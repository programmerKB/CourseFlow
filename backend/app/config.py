from functools import lru_cache
import logging
from uuid import UUID
from pydantic import AnyHttpUrl, AwareDatetime, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def normalize_origin(value: str) -> str:
    url = AnyHttpUrl(value.strip())
    if (url.path not in ('', '/') or url.username is not None or url.password is not None
            or url.query is not None or url.fragment is not None or '*' in (url.host or '')):
        raise ValueError('Use an exact HTTP or HTTPS origin without paths, parameters, or wildcards.')
    return str(url).rstrip('/')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', hide_input_in_errors=True)
    database_url: str = 'postgresql+psycopg://course_app:course_app@localhost:5432/courses'
    public_database_url: str = 'postgresql+psycopg://course_public:course_public@localhost:5432/courses'
    database_password: SecretStr | None = None
    public_database_password: SecretStr | None = None
    app_origin: str = 'http://localhost:8080'
    app_allowed_origins: str = ''
    cookie_secure: bool = False
    public_initial_term_id: str = ''
    initial_start_at: AwareDatetime | None = None
    initial_end_at: AwareDatetime | None = None
    session_hours: int = 24
    gemini_model: str = 'gemini-3.5-flash-lite'
    gemini_audio_model: str = 'gemini-3.5-flash-lite'
    gemini_api_key: str = ''
    chat_stub: bool = False
    smtp_host: str = 'mailpit'
    smtp_port: int = 1025
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_from: str = '選課通知 <courses@example.test>'
    smtp_starttls: bool = False

    @field_validator('app_origin')
    @classmethod
    def validate_app_origin(cls, value: str) -> str:
        return normalize_origin(value)

    @field_validator('app_allowed_origins')
    @classmethod
    def validate_app_allowed_origins(cls, value: str) -> str:
        return ','.join(normalize_origin(origin) for origin in value.split(',') if origin.strip())

    @property
    def browser_origins(self) -> frozenset[str]:
        return frozenset((self.app_origin, *filter(None, self.app_allowed_origins.split(','))))

    @field_validator('public_initial_term_id')
    @classmethod
    def validate_public_initial_term_id(cls, value: str) -> str:
        value = value.strip()
        return str(UUID(value)) if value else ''

    @field_validator('initial_start_at', 'initial_end_at', mode='before')
    @classmethod
    def optional_initial_date(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode='after')
    def validate_initial_window(self):
        if (self.initial_start_at is None) != (self.initial_end_at is None):
            raise ValueError('INITIAL_START_AT 與 INITIAL_END_AT 須同時設定。')
        if self.initial_start_at is not None and self.initial_start_at >= self.initial_end_at:
            raise ValueError('INITIAL_START_AT 須早於 INITIAL_END_AT。')
        return self

    @model_validator(mode='after')
    def apply_database_passwords(self):
        if self.database_password is not None:
            self.database_url = make_url(self.database_url).set(
                password=self.database_password.get_secret_value()).render_as_string(hide_password=False)
        if self.public_database_password is not None:
            self.public_database_url = make_url(self.public_database_url).set(
                password=self.public_database_password.get_secret_value()).render_as_string(hide_password=False)
        return self


@lru_cache
def get_settings():
    try:
        return Settings()
    except PermissionError as error:
        if error.filename != '.env':
            raise
        logging.getLogger('courses').warning('目前執行帳號無法讀取 .env，將使用程序環境變數和預設值。')
        return Settings(_env_file=None)


settings = get_settings()
