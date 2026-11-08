"""Чтение и безопасное обновление настроек локального дашборда."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from src.generation.client import validate_base_url
from src.settings import GENERATION_DEFAULTS, RunOptions, question_batch_limit, read_env


INPUT_RETENTION_KEY = 'RUN_INPUT_RETENTION_HOURS'
DEFAULT_INPUT_RETENTION_HOURS = 24


class DashboardConfig:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()

    def _overrides(self):
        return {key: os.environ[key] for key in GENERATION_DEFAULTS if key in os.environ}

    def snapshot(self):
        with self.lock:
            return {**GENERATION_DEFAULTS, **read_env(self.path), **self._overrides()}

    def input_retention_hours(self):
        with self.lock:
            raw = self._retention_raw()
        return self._retention_hours(raw)

    def _retention_raw(self):
        values = read_env(self.path, keys=(INPUT_RETENTION_KEY,))
        return os.environ.get(INPUT_RETENTION_KEY, values.get(INPUT_RETENTION_KEY, DEFAULT_INPUT_RETENTION_HOURS))

    @staticmethod
    def _retention_hours(raw):
        try:
            hours = int(raw)
        except (TypeError, ValueError):
            hours = -1
        if hours < 0:
            raise ValueError(f'{INPUT_RETENTION_KEY} должен быть целым числом от 0.')
        return hours

    def _locked(self):
        locked = set(self._overrides())
        if INPUT_RETENTION_KEY in os.environ:
            locked.add(INPUT_RETENTION_KEY)
        return locked

    def public(self):
        with self.lock:
            overrides = self._overrides()
            values = {**GENERATION_DEFAULTS, **read_env(self.path), **overrides}
            values[INPUT_RETENTION_KEY] = str(self._retention_raw())
            locked = self._locked()
        key_set = bool(values.pop('API_KEY'))
        shown = (*GENERATION_DEFAULTS, INPUT_RETENTION_KEY)
        return {'values': values, 'api_key_set': key_set, 'overridden': [key for key in shown if key in locked]}

    def save(self, changes):
        allowed = set(GENERATION_DEFAULTS) | {INPUT_RETENTION_KEY}
        if not isinstance(changes, dict) or not changes.keys() <= allowed:
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
            if name == INPUT_RETENTION_KEY:
                value = str(self._retention_hours(value))
            updates[name] = value
        with self.lock:
            overrides = self._overrides()
            if updates.keys() & self._locked():
                raise ValueError('Параметры из окружения процесса нужно менять в окружении.')
            generation = {key: value for key, value in updates.items() if key in GENERATION_DEFAULTS}
            effective = {**GENERATION_DEFAULTS, **read_env(self.path), **generation, **overrides}
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
