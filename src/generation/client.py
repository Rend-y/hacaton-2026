"""Связь с API модели: ключ, ограничения нагрузки, повторы и статистика.

Внутренние рассуждения модели
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
from src.generation.token_budget import get_token_counter
from src.settings import (
    DEFAULT_CONTEXT_TOKEN_LIMIT, DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, DEFAULT_TIMEOUT_SECONDS, read_env,
)


def estimate_request_tokens(messages, max_tokens, token_counter=None):
    """Токенизатор + запас; для неизвестной модели — безопасная оценка по байтам."""
    if token_counter is not None:
        return token_counter.estimate(messages, max_tokens)
    return sum(len(message['content'].encode('utf-8')) + 32 for message in messages) + max_tokens


def load_env(path):
    """Загружает настройки генерации из .env с приоритетом окружения процесса."""
    for key, value in read_env(Path(path)).items():
        os.environ.setdefault(key, value)


def validate_base_url(base_url):
    try:
        parsed = urlparse(base_url)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None
                 and '?' not in base_url and '#' not in base_url and not any(char.isspace() for char in base_url)
                 and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('BASE_URL must be an HTTPS URL without credentials or query parameters')


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
    def __init__(self, key, base_url, model, timeout=DEFAULT_TIMEOUT_SECONDS, max_tokens=DEFAULT_MAX_TOKENS,
                 *, context_token_limit=DEFAULT_CONTEXT_TOKEN_LIMIT, max_workers=2, max_requests=None,
                 temperature=DEFAULT_TEMPERATURE, tokenizer_path=''):
        if not key:
            raise ValueError('Set API_KEY in .env or the environment')
        if not base_url:
            raise ValueError('Set BASE_URL in .env or the environment')
        validate_base_url(base_url)
        if not model:
            raise ValueError('Set MODEL in .env or the environment')
        self.key = key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.context_token_limit = context_token_limit
        self.token_counter = get_token_counter(model, tokenizer_path)
        self.max_requests = max_requests
        self.limiter = RateLimiter()
        self.semaphore = threading.BoundedSemaphore(max_workers)
        self.lock = threading.Lock()
        self._stats = {'requests': 0, 'successful_requests': 0, 'prompt_tokens': 0,
                       'completion_tokens': 0, 'http_retries': 0}
        self._transport = threading.local()

    @property
    def opener(self):
        """Создаёт HTTP-транспорт для текущего потока при первом обращении."""
        if not hasattr(self._transport, 'opener'):
            self._transport.opener = urllib.request.build_opener(NoRedirect())
        return self._transport.opener

    @opener.setter
    def opener(self, value):
        self._transport.opener = value

    @property
    def stats(self):
        with self.lock:
            return self._stats.copy()

    def _check_request_budget(self):
        # Вызывается под self.lock, в том числе перед увеличением счётчика.
        if self.max_requests is not None and self._stats['requests'] >= self.max_requests:
            raise ApiError(f'Достигнут лимит HTTP-запросов на обработку (MAX_REQUESTS={self.max_requests}).')

    def complete(self, messages, progress=None):
        """Возвращает текст ответа и расход токенов; рассуждения провайдера исключены."""
        def report(event, **fields):
            if progress:
                progress({'event': event, **fields})

        estimate = estimate_request_tokens(messages, self.max_tokens, self.token_counter)
        if estimate > self.context_token_limit:
            raise ApiError(f'Estimated request size {estimate} exceeds CONTEXT_TOKEN_LIMIT={self.context_token_limit}')
        payload = {'model': self.model, 'messages': messages, 'temperature': self.temperature,
                   'max_tokens': self.max_tokens}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        for attempt in range(3):
            retry_after = 0
            with self.lock:
                self._check_request_budget()
            self.limiter.acquire(estimate, progress=progress)
            with self.semaphore:
                with self.lock:
                    self._check_request_budget()
                    self._stats['requests'] += 1
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
                        self._stats['successful_requests'] += 1
                        for field in ('prompt_tokens', 'completion_tokens'):
                            self._stats[field] += int(usage.get(field) or 0)
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
                self._check_request_budget()
                self._stats['http_retries'] += 1
            delay = max(retry_after, 2 ** (attempt + 1))
            report('api_retry', wait_seconds=delay, next_http_attempt=attempt + 2)
            time.sleep(delay)
        raise ApiError('Request failed')
