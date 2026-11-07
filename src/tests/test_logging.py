"""Проверки живого прогресса без настоящих запросов к модели.

Проверяем, что ожидание заканчивается вместе с запросом, секреты не попадают
в вывод, а сообщения различают решение модели и локальные проверки кода.
"""

import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from src.generation import answer_engine
from src.generation.client import ApiError, Client
from src.progress import RunLog, heartbeat


class FakeClient:
    """Отдаёт готовые ответы, чтобы проверить сообщения о проверках и исправлении."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages, progress=None):
        self.calls.append(messages)
        answer = self.answers.pop(0)
        return json.dumps(answer if 'question' in json.loads(messages[1]['content']) else {'answers': [answer]},
                          ensure_ascii=False), {'usage': {}}


class FakeOpener:
    """Имитирует HTTP без сети; тело ошибки специально содержит тестовый секрет."""

    def __init__(self, *results):
        self.results = list(results)

    def open(self, request, timeout):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode('utf-8'))


class LoggingTests(unittest.TestCase):
    def test_console_and_file_receive_same_sanitized_lines(self):
        with tempfile.TemporaryDirectory(prefix='logging-test-', dir=Path(__file__).parent) as directory:
            target = Path(directory) / 'progress.log'
            console = []
            log = RunLog(target, console.append, secrets=('synthetic-secret-123',))
            log('Начало: synthetic-secret-123\nПодмена строки\r\x1b[31m красный')
            log('Окончание проверки')
            lines = target.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines, console)
        self.assertEqual(len(lines), 2)
        text = '\n'.join(lines)
        self.assertNotIn('synthetic-secret-123', text)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\r', text)
        self.assertIn('Начало', text)
        self.assertIn('Окончание проверки', text)

    def test_heartbeat_reports_wait_and_stops_after_success(self):
        received = threading.Event()
        elapsed = []

        def report(seconds):
            elapsed.append(seconds)
            received.set()

        with heartbeat(report, interval=0.01):
            self.assertTrue(received.wait(1), 'Ожидание запроса должно быть видно пользователю')
        count = len(elapsed)
        time.sleep(0.04)
        self.assertGreater(count, 0)
        self.assertEqual(len(elapsed), count)
        self.assertTrue(all(value >= 0 for value in elapsed))

    def test_heartbeat_stops_when_request_raises(self):
        received = threading.Event()
        calls = []

        def report(seconds):
            calls.append(seconds)
            received.set()

        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            with heartbeat(report, interval=0.01):
                self.assertTrue(received.wait(1))
                raise RuntimeError('synthetic failure')
        count = len(calls)
        time.sleep(0.04)
        self.assertEqual(len(calls), count)

    def test_api_retry_events_are_ordered_and_do_not_expose_sensitive_content(self):
        key = 'synthetic-credential-for-logging-test'
        error = urllib.error.HTTPError('https://example.test/v1/chat/completions', 429,
                                      key, {'Retry-After': '7'}, io.BytesIO(key.encode()))
        envelope = {'model': 'test-model', 'choices': [{'message': {
            'content': '{"answers":[]}', 'reasoning_content': 'PRIVATE INTERNAL REASONING'},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}
        client = Client(key, 'https://example.test/v1', 'test-model', max_tokens=256)
        client.opener = FakeOpener(error, envelope)
        client.limiter = Mock()
        events = []
        with patch('src.generation.client.time.sleep'):
            client.complete([{'role': 'user', 'content': 'PRIVATE DOCUMENT BODY'}], progress=events.append)
        important = [event['event'] for event in events if event['event'] in
                     {'api_request', 'api_error', 'api_retry', 'api_response'}]
        self.assertEqual(important, ['api_request', 'api_error', 'api_retry', 'api_request', 'api_response'])
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(key, serialized)
        self.assertNotIn('PRIVATE INTERNAL REASONING', serialized)
        self.assertNotIn('PRIVATE DOCUMENT BODY', serialized)


class WorkflowLogTests(unittest.TestCase):
    """Проверяем существенный смысл сообщений, не фиксируя всю формулировку лога."""

    def setUp(self):
        self.questions = [{'id': 'P02-custom', 'package': 'P02', 'question': 'Какой гарантийный срок?'}]
        self.quote = 'Гарантийный срок на результаты работ составляет 12 месяцев.'
        self.documents = {'contract.txt': self.quote}
        self.logs = []
        self.traces = []

    def process(self, client, mode='verified', attempts=1):
        return answer_engine.process_questions(client, mode, 'P02', self.questions, self.documents, [],
                                   attempts, self.traces.append, progress=self.logs.append)

    def answer(self, quote=None):
        return {'id': 'P02-custom', 'status': 'ответ', 'answer': '12 месяцев',
                'evidence': [{'file': 'contract.txt', 'quote': quote or self.quote}]}

    def test_absence_is_attributed_to_model_not_proven_by_local_code(self):
        answer = {'id': 'P02-custom', 'status': 'нет в документах', 'answer': '', 'evidence': []}
        result = self.process(FakeClient(answer))
        text = '\n'.join(self.logs).lower()
        self.assertEqual(result['P02-custom']['status'], 'нет в документах')
        self.assertIn('нет в документах', text)
        self.assertIn('модел', text)
        self.assertRegex(text, r'не доказ|не подтвержда|не проверя|не устанавлива')

    def test_failed_quote_is_logged_before_repair_and_success(self):
        client = FakeClient(self.answer('В документе якобы указан гарантийный срок 12 месяцев.'), self.answer())
        result = self.process(client)
        text = '\n'.join(self.logs).lower()
        self.assertEqual(result['P02-custom']['status'], 'ответ')
        self.assertEqual(len(client.calls), 2)
        self.assertIn('цитат', text)
        self.assertIn('не найден', text)
        self.assertIn('исправ', text)
        self.assertIn('p02-custom', text)

    def test_baseline_explicitly_reports_disabled_validation(self):
        quote = 'Несуществующая цитата со сроком 12 месяцев.'
        answer = {'status': 'ответ', 'answer': '12 месяцев', 'file': 'source.txt', 'quote': quote}
        result = self.process(FakeClient(answer), mode='baseline')
        text = '\n'.join(self.logs).lower()
        self.assertEqual(result['P02-custom']['evidence'][0]['quote'], quote)
        self.assertIn('baseline', text)
        self.assertRegex(text, r'без[^\n]*провер|проверк\w* отключ|не выполня')


if __name__ == '__main__':
    unittest.main()
