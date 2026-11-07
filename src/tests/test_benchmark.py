"""Общая выгрузка всех вопросов: модель подменена, официальный оценщик настоящий."""
import contextlib
import csv
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src import cli
from src.workflows import pipeline
from src import settings
from src.generation.client import ApiError, ResponseTruncated
from src.reporting.report import build_run_model


class BenchmarkTests(unittest.TestCase):
    def exercise(self, failure=False, truncated=False):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            shutil.copytree(settings.ROOT / 'data' / 'public', root / 'data' / 'public')
            calls = []
            expected_calls = 18 if truncated else 16
            owner = self

            class OfflineClient:
                key = 'offline-secret'
                model = 'offline-model'
                base_url = 'https://example.test/v1'

                def __init__(self):
                    self.stats = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}

                def complete(self, messages, progress=None):
                    payload = json.loads(messages[1]['content'])
                    owner.assertLessEqual(len(payload['questions']), 5)
                    packages = {q['package'] for q in payload['questions']}
                    owner.assertEqual(len(packages), 1)
                    name = next(iter(packages))
                    expected = {p.read_text(encoding='utf-8-sig') for p in (root/'data/public/packages'/name).glob('*.txt')}
                    owner.assertEqual({d['text'] for d in payload['documents']}, expected)
                    calls.append(payload)
                    self.stats['requests'] += 1
                    if truncated and len(calls) == 1:
                        raise ResponseTruncated('output limit')
                    if failure and len(calls) == 1:
                        raise ApiError('Model API HTTP 429')
                    answers = [{'id': q['id'], 'status': 'не знаю', 'answer': '', 'evidence': []}
                               for q in payload['questions']]
                    return json.dumps({'answers': answers}), {'finish_reason': 'stop'}

            original_open = Path.open

            def guard(path, *args, **kwargs):
                if path.name == 'answers_public.json':
                    owner.assertEqual(len(calls), expected_calls, 'Эталон открыт до окончания генерации')
                return original_open(path, *args, **kwargs)

            with patch.object(settings, 'ROOT', root), patch.object(pipeline, 'Client', return_value=OfflineClient()), \
                 patch.object(pipeline, 'load_env'), patch.object(Path, 'open', guard), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(['check-all'])
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
            self.assertEqual(model['config']['max_tokens'], 32768)
            self.assertEqual(model['config']['max_questions_per_request'], 5)
            self.assertEqual(model['config']['timeout_seconds'], 900)
            self.assertEqual(code, 2 if failure else 0)
            self.assertEqual(model['status'], 'completed_with_errors' if failure else 'completed')
            self.assertEqual(model['operations']['technical_errors'], 5 if failure else 0)
            self.assertTrue((output/'report.html').is_file())

    def test_all_questions_have_one_result_and_global_score(self):
        self.exercise()

    def test_failure_does_not_drop_other_groups_or_hide_errors(self):
        self.exercise(failure=True)

    def test_truncation_recovers_into_one_report_with_all_questions(self):
        self.exercise(truncated=True)
