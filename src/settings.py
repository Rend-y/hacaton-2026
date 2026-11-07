"""Типизированные входы сценариев и единые значения настроек генерации."""
from dataclasses import dataclass
import math
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# До 15 минут на одну HTTP-попытку; сетевые повторы учитываются отдельно.
DEFAULT_TIMEOUT_SECONDS = 900
# Бюджет ответа включает токены, которые провайдер тратит на рассуждение модели.
DEFAULT_MAX_TOKENS = 32768
DEFAULT_CONTEXT_TOKEN_LIMIT = 128_000
# Делим вопросы заранее, чтобы уменьшить риск обрезания ответа модели.
DEFAULT_MAX_QUESTIONS_PER_REQUEST = 5
DEFAULT_MAX_WORKERS = 1


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f'{name} должен быть целым числом больше нуля.')
    return value


def question_batch_limit() -> int:
    return _positive_env_int('MAX_QUESTIONS_PER_REQUEST', DEFAULT_MAX_QUESTIONS_PER_REQUEST)


@dataclass(frozen=True)
class QueryOptions:
    """Два пользовательских входа и режим; лимиты остаются настройками приложения."""

    atacment: list[str]
    promt: list[str]
    mode: str = 'verified'
    evaluate: bool = False
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

    def __post_init__(self):
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('TIMEOUT_SECONDS должен быть конечным числом больше нуля.')
        if not 0 < self.max_tokens < self.context_token_limit:
            raise ValueError('MAX_TOKENS должен быть больше нуля и меньше CONTEXT_TOKEN_LIMIT.')
        if self.max_workers < 1:
            raise ValueError('MAX_WORKERS должен быть целым числом больше нуля.')

    @classmethod
    def from_env(cls, mode: str, output: Path, *, repair_attempts: int = 2,
                 max_tokens: int = DEFAULT_MAX_TOKENS, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        try:
            timeout = float(os.environ.get('TIMEOUT_SECONDS', timeout))
        except ValueError:
            raise ValueError('TIMEOUT_SECONDS должен быть конечным числом больше нуля.') from None
        return cls(mode, output, repair_attempts=repair_attempts,
                   max_tokens=_positive_env_int('MAX_TOKENS', max_tokens), timeout=timeout,
                   context_token_limit=_positive_env_int('CONTEXT_TOKEN_LIMIT', DEFAULT_CONTEXT_TOKEN_LIMIT),
                   max_workers=_positive_env_int('MAX_WORKERS', DEFAULT_MAX_WORKERS))
