"""Общая выгрузка всех вопросов: модель подменена, официальный оценщик настоящий."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from src import cli
from src.workflows import pipeline
from src import settings
from src.generation.client import ApiError, ResponseTruncated
from src.reporting.report import build_run_model


class BenchmarkTests(unittest.TestCase):
    def exercise(self, failure=False, truncated=False, *, configured_limit=None, initial_calls=16, mode='verified'):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            shutil.copytree(settings.ROOT / 'data' / 'public', root / 'data' / 'public')
            timeout, max_tokens, context_limit = 900, 32768, 128000
            workers = 1
            if configured_limit is not None:
                timeout, max_tokens, context_limit = 75, 40000, 96000
                workers = 2
                (root / '.env').write_text(f'MAX_QUESTIONS_PER_REQUEST={configured_limit}\n'
                                          f'TIMEOUT_SECONDS={timeout}\nMAX_TOKENS={max_tokens}\n'
                                          f'CONTEXT_TOKEN_LIMIT={context_limit}\nMAX_WORKERS={workers}\n',
                                          encoding='utf-8')
            batch_limit = configured_limit or 5
            calls = []
            lock = threading.Lock()
            expected_calls = initial_calls + (2 if truncated else 0)
            owner = self

            class OfflineClient:
                key = 'offline-secret'
                model = 'offline-model'
                base_url = 'https://example.test/v1'

                def __init__(self):
                    self.stats = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}

                def complete(self, messages, progress=None):
                    payload = json.loads(messages[1]['content'])
                    owner.assertLessEqual(len(payload['questions']), batch_limit)
                    packages = {q['id'].split('-')[0] for q in payload['questions']}
                    owner.assertEqual(len(packages), 1)
                    name = next(iter(packages))
                    expected = {p.read_text(encoding='utf-8-sig') for p in (root/'data/public/packages'/name).glob('*.txt')}
                    owner.assertEqual({d['text'] for d in payload['documents']}, expected)
                    with lock:
                        calls.append(payload)
                        self.stats['requests'] += 1
                        first_request = len(calls) == 1
                    if truncated and first_request:
                        raise ResponseTruncated('output limit')
                    if failure and first_request:
                        raise ApiError('Model API HTTP 429')
                    if mode == 'baseline':
                        answers = [{'id': q['id'], 'status': 'не знаю', 'answer': '', 'quote': '', 'file': ''}
                                   for q in payload['questions']]
                        return json.dumps(answers), {'finish_reason': 'stop'}
                    answers = [{'id': q['id'], 'status': 'не знаю', 'answer': '', 'evidence': []}
                               for q in payload['questions']]
                    return json.dumps({'answers': answers}), {'finish_reason': 'stop'}

            original_open = Path.open

            def guard(path, *args, **kwargs):
                if path.name == 'answers_public.json':
                    owner.assertEqual(len(calls), expected_calls, 'Эталон открыт до окончания генерации')
                return original_open(path, *args, **kwargs)

            with patch.object(settings, 'ROOT', root), \
                 patch.object(pipeline, 'Client', return_value=OfflineClient()) as client_factory, \
                 patch.object(Path, 'open', guard), patch.dict(os.environ), \
                 contextlib.redirect_stdout(io.StringIO()):
                for name in ('MAX_QUESTIONS_PER_REQUEST', 'TIMEOUT_SECONDS', 'MAX_TOKENS',
                             'CONTEXT_TOKEN_LIMIT', 'MAX_WORKERS'):
                    os.environ.pop(name, None)
                code = cli.main(['check-all', '--mode', mode])
            self.assertEqual(client_factory.call_args.args[3:], (timeout, max_tokens))
            self.assertEqual(client_factory.call_args.kwargs['context_token_limit'], context_limit)
            self.assertEqual(client_factory.call_args.kwargs['max_workers'], workers)
            runs = list((root/'runs').iterdir())
            self.assertEqual(len(runs), 1)
            output = runs[0]
            with (output/'answers.csv').open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream, delimiter=';'))
            self.assertEqual(len(rows), 71)
            self.assertEqual(len({row['id'] for row in rows}), 71)
            model = build_run_model(output)['runs'][0]
            self.assertEqual(model['quality']['n'], 71)
            self.assertEqual(len(model['answers']), 71)
            self.assertEqual(model['operations']['requests'], expected_calls)
            self.assertEqual(model['config']['max_tokens'], max_tokens)
            self.assertEqual(model['config']['context_token_limit'], context_limit)
            self.assertEqual(model['config']['max_questions_per_request'], batch_limit)
            self.assertEqual(model['config']['timeout_seconds'], timeout)
            self.assertEqual(model['config']['workers'], workers)
            self.assertEqual(code, 2 if failure else 0)
            self.assertEqual(model['status'], 'completed_with_errors' if failure else 'completed')
            self.assertEqual(model['operations']['technical_errors'], batch_limit if failure else 0)
            self.assertTrue((output/'report.html').is_file())

    def test_all_questions_have_one_result_and_global_score(self):
        self.exercise()

    def test_check_all_reads_generation_limits_from_dotenv(self):
        self.exercise(configured_limit=3, initial_calls=27)

    def test_failure_does_not_drop_other_groups_or_hide_errors(self):
        self.exercise(failure=True)

    def test_truncation_recovers_into_one_report_with_all_questions(self):
        self.exercise(truncated=True)

    def test_baseline_batches_questions_and_splits_a_truncated_response(self):
        self.exercise(truncated=True, mode='baseline')
