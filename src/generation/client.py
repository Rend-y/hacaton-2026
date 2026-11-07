"""Связь с API модели: ключ, ограничения нагрузки, повторы и статистика.

Используется только стандартная библиотека. Внутренние рассуждения модели
не возвращаются вызывающему коду и не попадают в журнал.
"""
import collections
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from src.progress import heartbeat
from src.settings import CONTEXT_TOKEN_LIMIT, DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_SECONDS


def estimate_request_tokens(messages, max_tokens):
    """Консервативная оценка по байтам UTF-8 без зависимости от токенизатора."""
    return sum(len(message['content'].encode('utf-8')) + 32 for message in messages) + max_tokens


def load_env(path):
    """Читает локальный .env, не заменяя уже заданные переменные окружения."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if sep and key.strip().startswith('ITFEST_'):
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


class ApiError(RuntimeError):
    """Понятная ошибка API без заголовков запроса и секретного ключа."""
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не передаём секретный ключ другому адресу при перенаправлении HTTP.
        return None


class ResponseTruncated(ApiError):
    """Модель исчерпала бюджет ответа; движок может разделить группу вопросов."""


class RateLimiter:
    """Ограничивает запросы и оценку токенов в скользящем окне одного процесса."""
    def __init__(self, rpm=40, tpm=600000):
        self.rpm = rpm
        self.tpm = tpm
        self.events = collections.deque()
        self.lock = threading.Lock()

    def acquire(self, tokens, progress=None):
        """Резервирует бюджет до запроса; при нехватке ждёт освобождения окна."""
        if tokens > self.tpm:
            raise ApiError('Request exceeds configured token-per-minute budget')
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] >= 61:
                    self.events.popleft()
                used = sum(event[1] for event in self.events)
                if len(self.events) < self.rpm and used + tokens <= self.tpm:
                    self.events.append((now, tokens))
                    return
                delay = max(0.05, 61 - (now - self.events[0][0]))
            if progress:
                progress({'event': 'rate_limit_wait', 'wait_seconds': delay})
            time.sleep(delay)


class Client:
    """Выполняет запросы к /chat/completions и повторяет только временные сбои."""
    def __init__(self, key, base_url, model, timeout=DEFAULT_TIMEOUT_SECONDS, max_tokens=DEFAULT_MAX_TOKENS):
        if not key:
            raise ValueError('Set ITFEST_API_KEY in .env or the environment')
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('ITFEST_BASE_URL must be an HTTPS URL without credentials or query parameters')
        self.key = key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.limiter = RateLimiter()
        self.semaphore = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.stats = {'requests': 0, 'successful_requests': 0, 'prompt_tokens': 0,
                      'completion_tokens': 0, 'http_retries': 0}
        self.opener = urllib.request.build_opener(NoRedirect())

    def complete(self, messages, progress=None):
        """Возвращает текст ответа и расход токенов; рассуждения провайдера исключены."""
        def report(event, **fields):
            if progress:
                progress({'event': event, **fields})

        estimate = estimate_request_tokens(messages, self.max_tokens)
        if estimate > CONTEXT_TOKEN_LIMIT:
            raise ApiError('Package is too large for the conservative 128k context guard; split documents before running')
        payload = {'model': self.model, 'messages': messages, 'temperature': 0,
                   'max_tokens': self.max_tokens}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        for attempt in range(3):
            retry_after = 0
            self.limiter.acquire(estimate, progress=progress)
            with self.semaphore:
                with self.lock:
                    self.stats['requests'] += 1
                request = urllib.request.Request(self.base_url + '/chat/completions', data=body,
                    headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
                report('api_request', http_attempt=attempt + 1, timeout_seconds=self.timeout)
                started = time.monotonic()
                try:
                    # Пока сервер не вернул ответ, мы знаем лишь время ожидания.
                    on_wait = (lambda elapsed: report('api_wait', elapsed_seconds=elapsed)) if progress else None
                    with heartbeat(on_wait):
                        with self.opener.open(request, timeout=self.timeout) as response:
                            data = json.load(response)
                    choice = data['choices'][0]
                    content = choice['message'].get('content')
                    usage = data.get('usage') or {}
                    with self.lock:
                        self.stats['successful_requests'] += 1
                        for field in ('prompt_tokens', 'completion_tokens'):
                            self.stats[field] += int(usage.get(field) or 0)
                    if choice.get('finish_reason') == 'length':
                        raise ResponseTruncated('Model response truncated: output token limit reached')
                    if not isinstance(content, str) or not content.strip():
                        raise ApiError('Model returned no answer text')
                    report('api_response', elapsed_seconds=time.monotonic() - started,
                           prompt_tokens=int(usage.get('prompt_tokens') or 0),
                           completion_tokens=int(usage.get('completion_tokens') or 0))
                    return content, {'usage': usage, 'finish_reason': choice.get('finish_reason'),
                                     'returned_model': data.get('model')}
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    value = exc.headers.get('Retry-After', '')
                    retry_after = min(120, float(value)) if value.isdigit() else 0
                    if status == 429:
                        # Короткий повтор не поможет, если на сервере ещё заняты слоты.
                        retry_after = max(retry_after, 30 * (attempt + 1))
                    exc.close()
                    report('api_error', http_status=status, http_attempt=attempt + 1)
                    if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                        raise ApiError('Model API HTTP %s; check credentials, service availability and limits' % status) from None
                except (urllib.error.URLError, TimeoutError, OSError):
                    report('api_error', error_type='network', http_attempt=attempt + 1)
                    if attempt == 2:
                        raise ApiError('Model API network failure or timeout after 3 attempts') from None
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    raise ApiError('Model API returned an invalid completion envelope') from None
            with self.lock:
                self.stats['http_retries'] += 1
            delay = max(retry_after, 2 ** (attempt + 1))
            report('api_retry', wait_seconds=delay, next_http_attempt=attempt + 2)
            time.sleep(delay)
        raise ApiError('Request failed')
