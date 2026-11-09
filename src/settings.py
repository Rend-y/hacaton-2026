"""Типизированные входы сценариев и единые значения настроек генерации."""
from dataclasses import dataclass
import math
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPERATURE = 0
# До 15 минут на одну HTTP-попытку; сетевые повторы учитываются отдельно.
DEFAULT_TIMEOUT_SECONDS = 900
# Бюджет ответа включает токены, которые провайдер тратит на рассуждение модели.
DEFAULT_MAX_TOKENS = 32768
DEFAULT_CONTEXT_TOKEN_LIMIT = 128_000
# Делим вопросы заранее, чтобы уменьшить риск обрезания ответа модели.
DEFAULT_MAX_QUESTIONS_PER_REQUEST = 5
DEFAULT_MAX_WORKERS = 1
GENERATION_DEFAULTS = {
    'TOKENIZER_PATH': '',
    'BASE_URL': '', 'MODEL': '', 'API_KEY': '', 'MAX_REQUESTS': '',
    'TEMPERATURE': str(DEFAULT_TEMPERATURE),
    'TIMEOUT_SECONDS': str(DEFAULT_TIMEOUT_SECONDS), 'MAX_TOKENS': str(DEFAULT_MAX_TOKENS),
    'CONTEXT_TOKEN_LIMIT': str(DEFAULT_CONTEXT_TOKEN_LIMIT),
    'MAX_QUESTIONS_PER_REQUEST': str(DEFAULT_MAX_QUESTIONS_PER_REQUEST),
    'MAX_WORKERS': str(DEFAULT_MAX_WORKERS),
}


def read_env(path: Path, *, keys=GENERATION_DEFAULTS) -> dict[str, str]:
    """Читает настройки из .env без изменения окружения процесса."""
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        key, sep, value = line.partition('=')
        key = key.strip()
        if sep and key in keys:
            values.setdefault(key, value.strip().strip('\"\''))
    return values


def _positive_env_int(environ, name: str, default: int) -> int:
    try:
        value = int(environ.get(name, default))
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f'{name} должен быть целым числом больше нуля.')
    return value


def question_batch_limit(environ=None) -> int:
    return _positive_env_int(os.environ if environ is None else environ,
                             'MAX_QUESTIONS_PER_REQUEST', DEFAULT_MAX_QUESTIONS_PER_REQUEST)


@dataclass(frozen=True)
class QueryOptions:
    """Два пользовательских входа и режим; лимиты остаются настройками приложения."""

    attachment: list[str]
    prompt: list[str]
    mode: str = 'verified'
    repair_attempts: int = 2
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RunOptions:
    """Настройки выполнения уже подготовленного запроса, без аргументов CLI."""

    mode: str
    output: Path
    repair_attempts: int = 2
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    context_token_limit: int = DEFAULT_CONTEXT_TOKEN_LIMIT
    max_workers: int = DEFAULT_MAX_WORKERS
    max_requests: int | None = None
    temperature: float = DEFAULT_TEMPERATURE
    tokenizer_path: str = ''

    def __post_init__(self):
        if not 0 <= self.temperature <= 2:
            raise ValueError('TEMPERATURE должна быть числом от 0 до 2.')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('TIMEOUT_SECONDS должен быть конечным числом больше нуля.')
        if not 0 < self.max_tokens < self.context_token_limit:
            raise ValueError('MAX_TOKENS должен быть больше нуля и меньше CONTEXT_TOKEN_LIMIT.')
        if self.max_workers < 1:
            raise ValueError('MAX_WORKERS должен быть целым числом больше нуля.')
        if self.max_requests is not None and self.max_requests < 1:
            raise ValueError('MAX_REQUESTS должен быть целым числом больше нуля.')

    @classmethod
    def from_env(cls, mode: str, output: Path, *, repair_attempts: int = 2,
                 max_tokens: int = DEFAULT_MAX_TOKENS, timeout: float = DEFAULT_TIMEOUT_SECONDS, environ=None):
        environ = os.environ if environ is None else environ
        try:
            timeout = float(environ.get('TIMEOUT_SECONDS', timeout))
        except ValueError:
            raise ValueError('TIMEOUT_SECONDS должен быть конечным числом больше нуля.') from None
        try:
            temperature = float(environ.get('TEMPERATURE', DEFAULT_TEMPERATURE))
        except ValueError:
            raise ValueError('TEMPERATURE должна быть числом от 0 до 2.') from None
        max_requests = _positive_env_int(environ, 'MAX_REQUESTS', 0) if environ.get('MAX_REQUESTS') else None
        return cls(mode, output, repair_attempts=repair_attempts,
                   max_tokens=_positive_env_int(environ, 'MAX_TOKENS', max_tokens), timeout=timeout,
                   context_token_limit=_positive_env_int(environ, 'CONTEXT_TOKEN_LIMIT', DEFAULT_CONTEXT_TOKEN_LIMIT),
                   max_workers=_positive_env_int(environ, 'MAX_WORKERS', DEFAULT_MAX_WORKERS),
                   max_requests=max_requests, temperature=temperature,
                   tokenizer_path=environ.get('TOKENIZER_PATH', ''))
