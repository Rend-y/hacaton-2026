"""Проверки клиента модели, лимитов и обращения с ключом без доступа к сети.

Ответы HTTP и ожидание заменены имитациями: тесты не расходуют запросы и проверяют
повторы, ошибки формата и отсутствие чувствительных данных в сообщениях об ошибках.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.generation.client import ApiError, Client, NoRedirect, RateLimiter, ResponseTruncated, load_env


class FakeOpener:
    """Отдать подготовленный ответ HTTP или ошибку и сохранить параметры запроса."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError('Unexpected extra HTTP attempt')
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        body = result if isinstance(result, bytes) else json.dumps(result).encode('utf-8')
        return io.BytesIO(body)


class ClientTests(unittest.TestCase):
    """Проверять протокол, конфигурацию и обработку сбоев на искусственном ключе."""

    KEY = 'synthetic-credential-for-offline-tests'
    MESSAGES = [{'role': 'user', 'content': 'Ответь по документам'}]

    def setUp(self):
        self.sleep = patch('src.generation.client.time.sleep').start()
        self.addCleanup(patch.stopall)

    def completion(self, content='{"answers":[]}', finish='stop', usage=None):
        return {'model': 'returned-model', 'choices': [{'message': {
            'content': content, 'reasoning_content': 'PRIVATE REASONING MUST NOT BE RETURNED'},
            'finish_reason': finish}], 'usage': usage or {'prompt_tokens': 100, 'completion_tokens': 25}}

    def make_client(self, *results):
        client = Client(self.KEY, 'https://example.test/v1/', 'test-model', timeout=13, max_tokens=256)
        client.opener = FakeOpener(*results)
        client.limiter = Mock()
        return client

    def http_error(self, code, retry_after=None):
        headers = {} if retry_after is None else {'Retry-After': retry_after}
        return urllib.error.HTTPError('https://example.test/v1/chat/completions', code,
                                      'Sensitive upstream message: ' + self.KEY, headers,
                                      io.BytesIO(('Sensitive body: ' + self.KEY).encode()))

    def test_env_existing_process_values_win_and_unrelated_keys_are_ignored(self):
        with tempfile.TemporaryDirectory(prefix='client-test-', dir=Path(__file__).resolve().parent) as directory:
            env = Path(directory) / '.env'
            env.write_text('# comment\nITFEST_API_KEY="file-key"\nITFEST_MODEL=from-file\nOTHER_SECRET=ignored\nINVALID_LINE\n', encoding='utf-8-sig')
            with patch.dict(os.environ, {'ITFEST_API_KEY': 'process-key'}, clear=True):
                load_env(env)
                self.assertEqual(os.environ['ITFEST_API_KEY'], 'process-key')
                self.assertEqual(os.environ['ITFEST_MODEL'], 'from-file')
                self.assertNotIn('OTHER_SECRET', os.environ)

    def test_missing_env_file_is_optional(self):
        with patch.dict(os.environ, {}, clear=True):
            load_env(Path(__file__).parent / 'missing-test-env-file')
            self.assertNotIn('ITFEST_API_KEY', os.environ)

    def test_client_rejects_missing_key_and_insecure_or_credential_urls(self):
        with self.assertRaises(ValueError):
            Client('', 'https://example.test/v1', 'model')
        urls = ['http://example.test/v1', 'file:///secret', 'example.test', 'https://',
                'https://user:pass@example.test/v1', 'https://user@example.test/v1',
                'https://example.test/v1?key=secret', 'https://example.test/v1#fragment']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Client(self.KEY, url, 'model')

    def test_success_payload_authorization_usage_and_reasoning_exclusion(self):
        client = self.make_client(self.completion())
        content, metadata = client.complete(self.MESSAGES)
        request, timeout = client.opener.calls[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://example.test/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.KEY)
        self.assertEqual(timeout, 13)
        self.assertEqual(payload, {'model': 'test-model', 'messages': self.MESSAGES, 'temperature': 0, 'max_tokens': 256})
        self.assertNotIn(self.KEY, request.data.decode())
        self.assertEqual(content, '{"answers":[]}')
        self.assertNotIn('PRIVATE REASONING', str((content, metadata)))
        self.assertEqual(metadata['returned_model'], 'returned-model')
        self.assertEqual(client.stats, {'requests': 1, 'successful_requests': 1, 'prompt_tokens': 100,
                                        'completion_tokens': 25, 'http_retries': 0})
        client.limiter.acquire.assert_called_once()
        self.sleep.assert_not_called()

    def test_usage_accumulates_across_successful_calls(self):
        client = self.make_client(self.completion(), self.completion(usage={'prompt_tokens': 40, 'completion_tokens': 10}))
        client.complete(self.MESSAGES)
        client.complete(self.MESSAGES)
        self.assertEqual(client.stats['requests'], 2)
        self.assertEqual(client.stats['successful_requests'], 2)
        self.assertEqual(client.stats['prompt_tokens'], 140)
        self.assertEqual(client.stats['completion_tokens'], 35)

    def test_truncated_response_is_rejected_without_http_retry(self):
        client = self.make_client(self.completion('partial', finish='length'))
        with self.assertRaisesRegex(ResponseTruncated, 'truncated'):
            client.complete(self.MESSAGES)
        self.assertEqual(len(client.opener.calls), 1)
        self.assertEqual(client.stats['http_retries'], 0)
        self.sleep.assert_not_called()

    def test_empty_or_reasoning_only_content_is_rejected(self):
        for content in (None, '', '   ', []):
            with self.subTest(content=content):
                client = self.make_client(self.completion(content))
                with self.assertRaisesRegex(ApiError, 'no answer text'):
                    client.complete(self.MESSAGES)
                self.assertEqual(len(client.opener.calls), 1)

    def test_malformed_completion_envelopes_raise_api_error(self):
        invalid = [b'not json', {}, {'choices': []}, {'choices': [{'message': None}]},
                   {'choices': [{'message': []}]}, {'choices': [{'message': {'content': '{}'}}], 'usage': [1]}]
        for payload in invalid:
            with self.subTest(payload=payload):
                client = self.make_client(payload)
                with self.assertRaisesRegex(ApiError, 'invalid completion envelope'):
                    client.complete(self.MESSAGES)
                self.assertEqual(len(client.opener.calls), 1)

    def test_401_is_not_retried_and_error_does_not_echo_credentials(self):
        failure = self.http_error(401)
        client = self.make_client(failure)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(ApiError) as context:
            client.complete(self.MESSAGES)
        self.assertIn('401', str(context.exception))
        self.assertNotIn(self.KEY, str(context.exception) + stdout.getvalue() + stderr.getvalue())
        self.assertNotIn('Sensitive', str(context.exception))
        self.assertEqual(len(client.opener.calls), 1)
        self.assertEqual(client.stats['requests'], 1)
        self.assertEqual(client.stats['http_retries'], 0)
        self.sleep.assert_not_called()
        self.assertTrue(failure.fp.closed)

    def test_429_uses_minimum_thirty_second_backoff_then_succeeds(self):
        client = self.make_client(self.http_error(429, '7'), self.completion())
        content, _ = client.complete(self.MESSAGES)
        self.assertEqual(content, '{"answers":[]}')
        self.sleep.assert_called_once_with(30)
        self.assertEqual(client.stats['requests'], 2)
        self.assertEqual(client.stats['http_retries'], 1)
        self.assertEqual(client.stats['successful_requests'], 1)
        self.assertEqual(client.limiter.acquire.call_count, 2)

    def test_429_honors_larger_retry_after(self):
        client = self.make_client(self.http_error(429, '75'), self.completion())
        client.complete(self.MESSAGES)
        self.sleep.assert_called_once_with(75.0)
        self.assertEqual(client.stats['http_retries'], 1)

    def test_repeated_429_stops_after_three_attempts(self):
        client = self.make_client(self.http_error(429), self.http_error(429), self.http_error(429))
        with self.assertRaisesRegex(ApiError, '429'):
            client.complete(self.MESSAGES)
        self.assertEqual(len(client.opener.calls), 3)
        self.assertEqual(client.stats['http_retries'], 2)
        self.assertEqual(client.stats['successful_requests'], 0)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [30, 60])

    def test_transient_503_and_network_failure_are_retried(self):
        for error in (self.http_error(503), urllib.error.URLError('synthetic network failure'), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.sleep.reset_mock()
                client = self.make_client(error, self.completion())
                client.complete(self.MESSAGES)
                self.assertEqual(client.stats['requests'], 2)
                self.assertEqual(client.stats['http_retries'], 1)
                self.sleep.assert_called_once_with(2)

    def test_network_failure_final_error_is_sanitized(self):
        client = self.make_client(*(urllib.error.URLError(self.KEY) for _ in range(3)))
        with self.assertRaisesRegex(ApiError, 'after 3 attempts') as context:
            client.complete(self.MESSAGES)
        self.assertNotIn(self.KEY, str(context.exception))
        self.assertEqual(client.stats['requests'], 3)

    def test_context_guard_fails_before_any_http_request(self):
        client = self.make_client()
        with self.assertRaisesRegex(ApiError, '128k context guard'):
            client.complete([{'role': 'user', 'content': 'x' * 128001}])
        self.assertEqual(client.opener.calls, [])
        self.assertEqual(client.stats['requests'], 0)
        client.limiter.acquire.assert_not_called()

    def test_redirect_handler_never_forwards_authorization(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example/'))

    def test_concurrency_semaphore_has_two_slots(self):
        client = self.make_client()
        self.assertTrue(client.semaphore.acquire(blocking=False))
        self.assertTrue(client.semaphore.acquire(blocking=False))
        self.assertFalse(client.semaphore.acquire(blocking=False))
        client.semaphore.release()
        client.semaphore.release()


class RateLimiterTests(unittest.TestCase):
    """Проверять окна лимитов с управляемым временем, без реального ожидания."""

    def test_too_large_request_is_rejected(self):
        with self.assertRaises(ApiError):
            RateLimiter(tpm=100).acquire(101)

    def test_request_limit_waits_for_window_expiry(self):
        limiter = RateLimiter(rpm=1, tpm=100)
        with patch('src.generation.client.time.monotonic', side_effect=[0, 0, 61]), patch('src.generation.client.time.sleep') as sleep:
            limiter.acquire(20)
            limiter.acquire(30)
        sleep.assert_called_once_with(61)
        self.assertEqual(list(limiter.events), [(61, 30)])

    def test_token_limit_waits_even_when_request_count_is_low(self):
        limiter = RateLimiter(rpm=40, tpm=100)
        with patch('src.generation.client.time.monotonic', side_effect=[0, 0, 61]), patch('src.generation.client.time.sleep') as sleep:
            limiter.acquire(80)
            limiter.acquire(30)
        sleep.assert_called_once_with(61)
        self.assertEqual(list(limiter.events), [(61, 30)])


if __name__ == '__main__':
    unittest.main()
