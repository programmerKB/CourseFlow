from functools import lru_cache
import logging
from typing import Literal
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
    chat_stub: bool = False
    # Both assistants use the local Llama 3.2 3B behind an Ollama or OpenAI-compatible endpoint:
    # English add/drop (FR-21/22) and the Chinese assistant's query plans. No cloud model is used.
    llama_base_url: str = ''
    llama_api_style: Literal['ollama', 'openai'] = 'ollama'
    llama_model: str = 'llama3.2:3b'
    llama_api_key: SecretStr = SecretStr('')
    llama_timeout_seconds: float = 45.0
    # Voice input (FR-15/16): speech-to-text runs in the API process with faster-whisper.
    whisper_model: str = 'base'
    whisper_model_dir: str = ''
    whisper_device: Literal['cpu', 'cuda', 'auto'] = 'cpu'
    whisper_compute_type: str = 'int8'
    whisper_cpu_threads: int = 2
    whisper_preload: bool = False
    # Email transport. smtp (default): Gmail SMTP with an app password (smtp.gmail.com:587,
    # STARTTLS); with the default SMTP_HOST=mailpit and no account, mail lands in the local Mailpit.
    # api: the Gmail API (OAuth refresh token, users.messages.send), as the teacher spec names it.
    # The older value dev-smtp is accepted as smtp.
    gmail_transport: Literal['smtp', 'api'] = 'smtp'
    smtp_host: str = 'mailpit'
    smtp_port: int = 1025
    smtp_user: str = ''
    smtp_password: SecretStr = SecretStr('')
    smtp_from: str = ''
    smtp_starttls: bool = False
    gmail_client_id: str = ''
    gmail_client_secret: SecretStr = SecretStr('')
    gmail_refresh_token: SecretStr = SecretStr('')
    gmail_sender: str = ''

    @field_validator('llama_base_url')
    @classmethod
    def validate_llama_base_url(cls, value: str) -> str:
        value = value.strip().rstrip('/')
        if value:
            url = AnyHttpUrl(value)
            if url.username is not None or url.password is not None or url.query is not None:
                raise ValueError('LLAMA_BASE_URL must be a plain HTTP(S) base URL without credentials or parameters.')
        return value

    @field_validator('smtp_host', 'smtp_user', 'smtp_from', 'gmail_sender', 'gmail_client_id', mode='before')
    @classmethod
    def strip_text(cls, value):
        # A stray space after a value in .env must not turn into a DNS or login failure.
        return value.strip() if isinstance(value, str) else value

    @field_validator('gmail_transport', mode='before')
    @classmethod
    def legacy_transport(cls, value):
        value = value.strip().lower() if isinstance(value, str) else value
        return 'smtp' if value in ('dev-smtp', '', None) else value

    @field_validator('whisper_cpu_threads')
    @classmethod
    def validate_whisper_threads(cls, value: int) -> int:
        if not 0 <= value <= 64:
            raise ValueError('WHISPER_CPU_THREADS must be between 0 and 64 (0 lets CTranslate2 choose).')
        return value

    @field_validator('llama_timeout_seconds')
    @classmethod
    def validate_llama_timeout(cls, value: float) -> float:
        if not 1 <= value <= 300:
            raise ValueError('LLAMA_TIMEOUT_SECONDS must be between 1 and 300.')
        return value

    @property
    def gmail_configured(self) -> bool:
        return bool(self.gmail_client_id.strip() and self.gmail_client_secret.get_secret_value().strip()
                    and self.gmail_refresh_token.get_secret_value().strip() and self.gmail_sender.strip())

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
