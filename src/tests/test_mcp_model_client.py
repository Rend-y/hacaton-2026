"""Ожидание и защита ответов модели MCP-клиента без API и SDK MCP."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.generation.client import ApiError
from src.generation.mcp_client import McpModelClient
from src.mcp_runs import DocumentRuns


class McpModelClientTests(unittest.TestCase):
    def test_saved_runs_from_previous_storage_remain_readable(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve()
            run_id = '20261108-120000-abcdef123456-web'
            legacy = root / '.dashboard' / run_id
            legacy.mkdir(parents=True)
            job = {'status': 'completed', 'label': 'P01', 'chat_id': None}
            (legacy / 'job.json').write_text(json.dumps(job), encoding='utf-8')
            runs = DocumentRuns(root)
            self.assertEqual(runs.get(run_id), job)
            self.assertIsNone(runs.get('../' + run_id))

    def test_response_is_bound_to_one_request_and_not_reused(self):
        client = McpModelClient(timeout=1)
        messages = [{'role': 'user', 'content': 'Первый вопрос'}]
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(client.complete, messages)
            with client.condition:
                self.assertTrue(client.condition.wait_for(lambda: client.pending_request() is not None, timeout=1))
            request = client.pending_request()
            request['messages'][0]['content'] = 'Не меняет исходный запрос'
            self.assertEqual(client.pending_request()['messages'], messages)
            with self.assertRaises(ValueError):
                client.submit('wrong-request', '{}')
            client.submit(request['request_id'], '{"answers":[]}')
            self.assertEqual(future.result(timeout=1)[0], '{"answers":[]}')
            with self.assertRaises(ValueError):
                client.submit(request['request_id'], '{}')
        self.assertIsNone(client.pending_request())
        self.assertEqual(client.stats, {'requests': 1, 'successful_requests': 1})

    def test_timeout_clears_pending_request_and_rejects_late_response(self):
        client = McpModelClient(timeout=0.05)
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(client.complete, [{'role': 'user', 'content': 'Вопрос'}])
            with client.condition:
                self.assertTrue(client.condition.wait_for(lambda: client.pending_request() is not None, timeout=1))
            request = client.pending_request()
            with self.assertRaises(ApiError):
                future.result(timeout=1)
            self.assertIsNone(client.pending_request())
            with self.assertRaises(ValueError):
                client.submit(request['request_id'], '{}')
