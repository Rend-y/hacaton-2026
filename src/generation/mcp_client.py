"""Передача запросов конвейера модели MCP-клиента через инструменты сервера."""

from copy import deepcopy
import threading
from uuid import uuid4

from src.generation.client import ApiError, estimate_request_tokens
from src.settings import DEFAULT_CONTEXT_TOKEN_LIMIT, DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_SECONDS


class McpModelClient:
    """Один запуск, один ожидающий ответ. Генерацию выполняет подключённый агент."""

    key = ''
    base_url = ''
    model = 'mcp-client'
    temperature = None

    def __init__(self, *, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout
        self.condition = threading.Condition()
        self._request = None
        self._response = None
        self._stats = {'requests': 0, 'successful_requests': 0}

    @property
    def stats(self):
        # Расход токенов неизвестен: его учитывает сам MCP-клиент.
        with self.condition:
            return dict(self._stats)

    def pending_request(self):
        with self.condition:
            return deepcopy(self._request)

    def submit(self, request_id, response):
        if not isinstance(response, str) or not response.strip() or len(response) > 1_000_000:
            raise ValueError('Передайте непустой текст ответа модели длиной до 1 000 000 символов.')
        with self.condition:
            if self._request is None or self._request['request_id'] != request_id:
                raise ValueError('Запрос уже обработан или устарел. Получите состояние через get_document_run.')
            self._response = response
            self._request = None
            self.condition.notify_all()

    def complete(self, messages, progress=None):
        if estimate_request_tokens(messages, DEFAULT_MAX_TOKENS) > DEFAULT_CONTEXT_TOKEN_LIMIT:
            raise ApiError('Запрос превышает лимит контекста MCP. Выберите меньше документов или вопросов.')
        with self.condition:
            if self._request is not None or self._response is not None:
                raise ApiError('В этом запуске уже ожидается ответ модели MCP-клиента.')
            request_id = uuid4().hex
            self._stats['requests'] += 1
            self._request = {'request_id': request_id, 'messages': deepcopy(messages),
                             'max_tokens': DEFAULT_MAX_TOKENS}
            self.condition.notify_all()
        if progress:
            progress({'event': 'mcp_request', 'request_id': request_id})
        with self.condition:
            if not self.condition.wait_for(lambda: self._response is not None, timeout=self.timeout):
                self._request = None
                raise ApiError('Истекло время ожидания ответа MCP-клиента. Запустите вопрос повторно.')
            response = self._response
            self._response = None
            self._stats['successful_requests'] += 1
        if progress:
            progress({'event': 'mcp_response', 'request_id': request_id})
        return response, {'transport': 'mcp-tools', 'request_id': request_id}
