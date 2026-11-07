"""Типизированные входы сценариев и единые значения настроек генерации."""
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
# До 15 минут на одну HTTP-попытку; сетевые повторы учитываются отдельно.
DEFAULT_TIMEOUT_SECONDS = 900
# Бюджет ответа включает токены, которые провайдер тратит на рассуждение модели.
DEFAULT_MAX_TOKENS = 32768


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
