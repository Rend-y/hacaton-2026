"""Чтение и безопасное обновление настроек локального дашборда."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from src.generation.client import validate_base_url
from src.settings import GENERATION_DEFAULTS, RunOptions, question_batch_limit, read_env


class DashboardConfig:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()

    def _overrides(self):
        return {key: os.environ[key] for key in GENERATION_DEFAULTS if key in os.environ}

    def snapshot(self):
        with self.lock:
            return {**GENERATION_DEFAULTS, **read_env(self.path), **self._overrides()}

    def public(self):
        with self.lock:
            overrides = self._overrides()
            values = {**GENERATION_DEFAULTS, **read_env(self.path), **overrides}
        key_set = bool(values.pop('API_KEY'))
        return {'values': values, 'api_key_set': key_set, 'overridden': list(overrides)}

    def save(self, changes):
        if not isinstance(changes, dict) or not changes.keys() <= GENERATION_DEFAULTS.keys():
            raise ValueError('Форма содержит неизвестные настройки.')
        updates = {}
        for name, value in changes.items():
            if (not isinstance(value, str) or any(not char.isprintable() for char in value)
                    or value.strip().startswith(('"', "'")) or value.strip().endswith(('"', "'"))):
                raise ValueError(f'{name} должен быть однострочным значением без кавычек по краям.')
            value = value.strip()
            if name == 'API_KEY' and not value:
                continue
            if name == 'API_KEY' and (not value.isascii() or any(char.isspace() for char in value)):
                raise ValueError('API_KEY должен состоять из ASCII-символов без пробелов.')
            updates[name] = value
        with self.lock:
            overrides = self._overrides()
            if updates.keys() & overrides.keys():
                raise ValueError('Параметры из окружения процесса нужно менять в окружении.')
            effective = {**GENERATION_DEFAULTS, **read_env(self.path), **updates, **overrides}
            if effective['BASE_URL']:
                try:
                    validate_base_url(effective['BASE_URL'])
                except ValueError:
                    raise ValueError('Укажите базовый HTTPS-адрес API без логина, пароля, параметров и фрагмента.') from None
            question_batch_limit(effective)
            RunOptions.from_env('verified', self.path.parent, environ=effective)
            if updates:
                self._write(updates)

    def _write(self, updates):
        try:
            lines = self.path.read_text(encoding='utf-8-sig').splitlines()
        except FileNotFoundError:
            lines = []
        pending = updates.copy()
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, prefix='.env.',
                                suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                for line in lines:
                    key, sep, _ = line.partition('=')
                    key = key.strip()
                    if sep and key in updates:
                        if key in pending:
                            stream.write(f'{key}={pending.pop(key)}\n')
                    else:
                        stream.write(line + '\n')
                for key, value in pending.items():
                    stream.write(f'{key}={value}\n')
                stream.close()
                temporary.replace(self.path)
            finally:
                stream.close()
                temporary.unlink(missing_ok=True)
