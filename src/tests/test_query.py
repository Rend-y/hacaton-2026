"""Проверяет два входа query и режим работы без настоящего API.

Имитируется только модель: загрузка документов и вопросов, локальная проверка,
CSV и HTML проходят через настоящие модули приложения.
"""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

from src import cli
from src.workflows import pipeline
from src import settings
from src.evaluation import reference_data
from src.generation.client import ApiError
from src.generation.prompts import BASELINE, VERIFIED
from src.evaluation import runs as evaluation
from src.workflows import query


class OfflineClient:
    """Отвечает по точной цитате из контекста, не обращаясь в сеть."""
    key = 'synthetic-test-token'
    model = 'synthetic-model'
    base_url = 'https://example.test/v1'

    def __init__(self):
        self.stats = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
        self.calls = []
        self.system_prompts = []

    def complete(self, messages, progress=None):
        payload = json.loads(messages[1]['content'])
        self.calls.append(payload)
        self.system_prompts.append(messages[0]['content'])
        document = next(item for item in payload['documents'] if '12 месяцев' in item['text'])
        self.stats['requests'] += 1
        self.stats['prompt_tokens'] += 30
        self.stats['completion_tokens'] += 10
        if messages[0]['content'] == BASELINE:
            response = [{'id': item['id'], 'status': 'ответ', 'answer': '12 месяцев',
                         'file': document['file'], 'quote': document['text']} for item in payload['questions']]
        else:
            response = {'answers': [{'id': item['id'], 'status': 'ответ', 'answer': '12 месяцев',
                                     'evidence': [{'file': document['file'], 'quote': document['text']}],
                                     'reason': 'Срок указан в переданном документе.'}
                                    for item in payload['questions']]}
        return json.dumps(response, ensure_ascii=False), {
            'usage': {'prompt_tokens': 30, 'completion_tokens': 10}, 'finish_reason': 'stop'}


class QueryTests(unittest.TestCase):
    """Проверяет CLI с --attachment, --prompt и необязательным --mode."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='query-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.documents = self.root / 'Любая папка с пробелами'
        self.documents.mkdir()
        self.quote = 'Гарантийный срок составляет 12 месяцев.'
        self.document = self.documents / 'Договор.txt'
        self.document.write_text(self.quote, encoding='utf-8')
        self.output = self.root / 'result'
        self.client = OfflineClient()

    def invoke(self, arguments, *, client=None, forbid_api=False):
        # Фиксируем папку результата только внутри теста. Публичный CLI выбирает
        # её сам, а тест никогда не записывает файлы в пользовательские runs.
        client_patch = (patch.object(pipeline, 'Client', side_effect=AssertionError('Invalid input must not call API'))
                        if forbid_api else patch.object(pipeline, 'Client', return_value=client or self.client))
        with client_patch, patch.object(query, '_output_path', return_value=self.output), \
             patch.object(query, 'load_env'), patch.object(pipeline, 'emit'), \
             patch.object(evaluation, 'evaluate_run', side_effect=AssertionError('Query must not load gold')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(arguments)

    def arguments(self, *questions, content=None):
        return ['query', '--attachment', str(content or self.document), '--prompt', *questions]

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_files_in_separate_folders_produce_answers_report_and_exact_citation(self):
        nested = self.documents / 'Приложения' / 'Новая редакция'
        nested.mkdir(parents=True)
        requirements = nested / 'Требования.md'
        requirements.write_text('Заявки рассматриваются комиссией.', encoding='utf-8')
        before = {path.relative_to(self.documents): path.read_bytes()
                  for path in self.documents.rglob('*') if path.is_file()}
        result = self.invoke(['query', '--attachment', str(self.document), str(requirements),
                              '--prompt', 'Какой гарантийный срок?'])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.client.calls), 1)
        payload = self.client.calls[0]
        self.assertEqual(len(payload['documents']), 2)
        self.assertEqual(payload['questions'][0]['id'], 'INPUT-001')
        self.assertEqual(self.read_json(self.output / 'manifest.json')['status'], 'completed')
        answers = self.read_json(self.output / 'answers.json')
        self.assertEqual(answers[0]['answer'], '12 месяцев')
        citation = answers[0]['evidence'][0]
        self.assertIn(citation['file'], {item['file'] for item in payload['documents']})
        self.assertEqual(citation['quote'], self.quote)
        self.assertFalse((self.output / 'evaluation.json').exists())
        page = (self.output / 'report.html').read_text(encoding='utf-8')
        self.assertIn('Оценка по эталону не выполнена', page)
        self.assertIn('12 месяцев', page)
        after = {path.relative_to(self.documents): path.read_bytes()
                 for path in self.documents.rglob('*') if path.is_file()}
        self.assertEqual(after, before)

    def test_five_files_and_twenty_questions_share_one_ordered_run(self):
        files = [self.document]
        for index in range(1, 5):
            path = self.root / f'Условия {index}.txt'
            path.write_text('Приёмка осуществляется заказчиком.', encoding='utf-8')
            files.append(path)
        questions = [f'Вопрос {index} о гарантийном сроке?' for index in range(1, 21)]
        with patch.dict('os.environ', {'MAX_QUESTIONS_PER_REQUEST': '5', 'MAX_WORKERS': '1', 'MAX_REQUESTS': ''}):
            result = self.invoke(['query', '--attachment', *map(str, files), '--prompt', *questions])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.client.calls), 4)
        self.assertTrue(all(len(payload['documents']) == 5 for payload in self.client.calls))
        self.assertEqual([item['question'] for payload in self.client.calls for item in payload['questions']], questions)
        with (self.output / 'answers.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream, delimiter=';'))
        self.assertEqual([item['id'] for item in rows], [f'INPUT-{index:03d}' for index in range(1, 21)])

    def test_verified_uses_configured_generation_limits(self):
        questions = ['Первый вопрос?', 'Второй вопрос?', 'Третий вопрос?']
        with patch.dict('os.environ', {'MAX_QUESTIONS_PER_REQUEST': '2', 'TIMEOUT_SECONDS': '30.5', 'TEMPERATURE': '0.35',
                                       'MAX_TOKENS': '640', 'CONTEXT_TOKEN_LIMIT': '10000'}):
            result = self.invoke(self.arguments(*questions))

        self.assertEqual(result, 0)
        self.assertEqual([[item['question'] for item in call['questions']] for call in self.client.calls],
                         [questions[:2], questions[2:]])
        manifest = self.read_json(self.output / 'manifest.json')
        self.assertEqual(manifest['max_questions_per_request'], 2)
        self.assertEqual(manifest['timeout_seconds'], 30.5)
        self.assertEqual(manifest['temperature'], 0.35)
        self.assertEqual(manifest['max_tokens'], 640)
        self.assertEqual(manifest['context_token_limit'], 10000)

    def test_invalid_settings_and_insufficient_context_fail_before_model_request(self):
        for env in ({'MAX_QUESTIONS_PER_REQUEST': '0'}, {'TIMEOUT_SECONDS': 'nan'},
                    {'TEMPERATURE': 'nan'}, {'TEMPERATURE': 'inf'}, {'TEMPERATURE': 'invalid'},
                    {'TEMPERATURE': '-0.1'}, {'TEMPERATURE': '2.01'},
                    {'MAX_TOKENS': '0'}, {'CONTEXT_TOKEN_LIMIT': 'abc'}, {'MAX_WORKERS': '0'},
                    {'MAX_TOKENS': '256', 'CONTEXT_TOKEN_LIMIT': '256'},
                    {'MAX_TOKENS': '256', 'CONTEXT_TOKEN_LIMIT': '300'}):
            with self.subTest(env=env), patch.dict('os.environ', env):
                result = self.invoke(self.arguments('Вопрос?'), forbid_api=True)

                self.assertEqual(result, 1)
                self.assertFalse(self.output.exists())

    def test_baseline_workers_overlap_batches_of_five_and_keep_output_order(self):
        first_two = threading.Barrier(2)
        third_started = threading.Event()
        lock = threading.Lock()
        transports = []
        batches = []
        active = peak = 0
        test = self

        class OfflineTransport:
            def __init__(self, *_handlers):
                self.worker = threading.current_thread()
                with lock:
                    transports.append(self)

            def open(self, request, timeout):
                nonlocal active, peak
                test.assertIs(threading.current_thread(), self.worker)
                messages = json.loads(request.data)['messages']
                ids = [row['id'] for row in json.loads(messages[1]['content'])['questions']]
                first_id = ids[0]
                with lock:
                    batches.append(ids)
                    active += 1
                    peak = max(peak, active)
                try:
                    if first_id in ('INPUT-001', 'INPUT-006'):
                        first_two.wait(timeout=5)
                    if first_id == 'INPUT-001':
                        test.assertTrue(third_started.wait(timeout=5))
                    if first_id == 'INPUT-011':
                        third_started.set()
                        raise urllib.error.HTTPError(request.full_url, 400, 'offline failure', {}, io.BytesIO())
                    answers = [{'id': question_id, 'status': 'не знаю', 'answer': '', 'quote': '', 'file': ''}
                               for question_id in reversed(ids)]
                    return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                        'message': {'content': json.dumps(answers)}}]}).encode('utf-8'))
                finally:
                    with lock:
                        active -= 1

        env = {'MAX_WORKERS': '2', 'MAX_QUESTIONS_PER_REQUEST': '5', 'API_KEY': 'test-key',
               'BASE_URL': 'https://example.test/v1', 'MODEL': 'offline-model'}
        with patch.dict('os.environ', env), patch.object(query, 'load_env'), \
             patch.object(query, '_output_path', return_value=self.output), \
             patch('src.generation.client.urllib.request.build_opener', side_effect=OfflineTransport), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(self.arguments(*(f'Вопрос {i}?' for i in range(1, 12))) + ['--mode', 'baseline'])

        self.assertEqual(result, 2)
        self.assertEqual(peak, 2)
        self.assertEqual(len(transports), 2)
        ids = [f'INPUT-{i:03d}' for i in range(1, 12)]
        self.assertEqual(sorted(batches), [ids[:5], ids[5:10], ids[10:]])
        answers = self.read_json(self.output / 'answers.json')
        self.assertEqual([answer['id'] for answer in answers], ids)
        self.assertEqual([answer.get('error_type') for answer in answers], [None] * 10 + ['api'])
        manifest = self.read_json(self.output / 'manifest.json')
        self.assertEqual(manifest['workers'], 2)
        self.assertEqual(manifest['stats']['requests'], 3)
        self.assertEqual(manifest['stats']['successful_requests'], 2)

    def test_literal_question_with_date_or_filename_is_not_mistaken_for_path(self):
        questions = ['Какая гарантия действует на 01/11/2026?', 'Что указано в Договор.txt?']
        result = self.invoke(self.arguments(*questions))
        self.assertEqual(result, 0)
        self.assertEqual([item['question'] for item in self.client.calls[0]['questions']], questions)

    def test_gold_and_environment_file_are_not_sent_to_model(self):
        marker = 'SYNTHETIC_GOLD_MUST_NOT_ENTER_PROMPT'
        (self.documents / 'answers_public.json').write_text(json.dumps({'secret_answer': marker}), encoding='utf-8')
        (self.documents / '.env').write_text('API_KEY=DO_NOT_READ_THIS_SENTINEL', encoding='utf-8')
        result = self.invoke(self.arguments('Какой срок?'))
        self.assertEqual(result, 0)
        serialized = json.dumps(self.client.calls, ensure_ascii=False)
        self.assertNotIn(marker, serialized)
        self.assertNotIn('DO_NOT_READ_THIS_SENTINEL', serialized)
        self.assertEqual(len(self.client.calls[0]['documents']), 1)

    def test_missing_content_path_rejected_before_client_creation(self):
        result = self.invoke(self.arguments('Вопрос?', content=self.root / 'missing'), forbid_api=True)
        self.assertEqual(result, 1)
        self.assertFalse(self.output.exists())

    def test_empty_question_rejected_before_client_creation(self):
        self.assertEqual(self.invoke(self.arguments('   '), forbid_api=True), 1)
        self.assertFalse(self.output.exists())

    def test_directory_attachment_is_rejected_before_api(self):
        self.assertEqual(self.invoke(self.arguments('Вопрос?', content=self.documents), forbid_api=True), 1)
        self.assertFalse(self.output.exists())

    def test_prompt_is_literal_even_if_a_file_exists(self):
        question_file = self.root / 'Вопросы.txt'
        question_file.write_text('Чужой текст.', encoding='utf-8')
        self.assertEqual(self.invoke(self.arguments(str(question_file))), 0)
        self.assertEqual(self.client.calls[0]['questions'][0]['question'], str(question_file))

    def test_existing_result_is_preserved(self):
        self.output.mkdir()
        previous = self.output / 'answers.csv'
        previous.write_text('previous-result', encoding='utf-8')
        result = self.invoke(self.arguments('Вопрос?'), forbid_api=True)
        self.assertEqual(result, 1)
        self.assertEqual(previous.read_text(encoding='utf-8'), 'previous-result')

    def test_mode_defaults_to_verified_and_baseline_selects_flat_prompt(self):
        result = self.invoke(self.arguments('Вопрос?'))
        self.assertEqual(result, 0)
        self.assertEqual(self.read_json(self.output / 'manifest.json')['mode'], 'verified')
        self.assertEqual(self.client.system_prompts, [VERIFIED])
        self.output = self.root / 'baseline-result'
        baseline = OfflineClient()
        with patch.dict('os.environ', {'MAX_QUESTIONS_PER_REQUEST': '2'}):
            result = self.invoke(self.arguments('Первый?', 'Второй?', 'Третий?') + ['--mode', 'baseline'],
                                 client=baseline)
        self.assertEqual(result, 0)
        manifest = self.read_json(self.output / 'manifest.json')
        self.assertEqual(manifest['mode'], 'baseline')
        self.assertEqual(manifest['max_questions_per_request'], 2)
        self.assertEqual(baseline.system_prompts, [BASELINE, BASELINE])
        self.assertEqual([[row['question'] for row in call['questions']] for call in baseline.calls],
                         [['Первый?', 'Второй?'], ['Третий?']])
        self.assertEqual(self.read_json(self.output / 'answers.json')[0]['answer'], '12 месяцев')

    def test_query_requires_both_inputs_and_shows_mode(self):
        # Справка — пользовательский контракт: лишние режимы ввода больше не нужны.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main(['query', '--help'])
        self.assertEqual(result, 0)
        help_text = output.getvalue()
        for option in ('--attachment', '--prompt', '--mode'):
            self.assertIn(option, help_text)
        for option in ('--evaluate', '--atacment', '--promt', '--output', '--inspect', '--questions'):
            self.assertNotIn(option, help_text)
        invalid = [[], ['--attachment', str(self.document)], ['--prompt', 'Вопрос?'],
                   ['--attachment', str(self.document), '--prompt', 'Вопрос?', '--inspect']]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(['query', *arguments], forbid_api=True), 2)

    def test_generation_without_evaluate_never_reads_reference_dataset(self):
        # Произвольный документ должен работать даже без поставляемого датасета.
        with patch.object(settings, 'ROOT', self.root / 'no-reference-data'), \
             patch.object(reference_data, 'read_questions', side_effect=AssertionError('Reference questions are not inputs')), \
             patch.object(reference_data, 'read_package', side_effect=AssertionError('Reference packages are not inputs')):
            result = self.invoke(self.arguments('Какой гарантийный срок?', content=self.document))
        self.assertEqual(result, 0)
        self.assertEqual(len(self.client.calls), 1)

    def test_legacy_dataset_commands_and_options_are_rejected_before_api(self):
        invalid = [
            ['run', '--packages', 'P01'],
            ['ask', '--package', 'P01', '--question', 'Срок?'],
            self.arguments('Срок?') + ['--data', str(self.root)],
            self.arguments('Срок?') + ['--packages', 'P01'],
        ]
        for argv in invalid:
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(argv, forbid_api=True), 2)
        self.assertFalse(self.output.exists())

    def test_technical_failure_returns_nonzero_and_keeps_auditable_results(self):
        def failed_request(messages, progress=None):
            raise ApiError('Model API HTTP 429')

        self.client.complete = failed_request
        result = self.invoke(self.arguments('Какой гарантийный срок?'))
        self.assertEqual(result, 2)
        manifest = self.read_json(self.output / 'manifest.json')
        self.assertEqual(manifest['status'], 'completed_with_errors')
        self.assertEqual(manifest['completed_questions'], 1)
        answer = self.read_json(self.output / 'answers.json')[0]
        self.assertEqual((answer['status'], answer['error_type']), ('не знаю', 'api'))
        self.assertIn('429', answer['reason'])
        self.assertTrue((self.output / 'report.html').is_file())

    def test_unreadable_document_prevents_confident_absence(self):
        unsupported = self.documents / 'Приложение.doc'
        unsupported.write_bytes(b'unsupported-document')
        client = OfflineClient()

        def absent_response(messages, progress=None):
            payload = json.loads(messages[1]['content'])
            client.calls.append(payload)
            client.stats.update(requests=client.stats['requests'] + 1, prompt_tokens=30, completion_tokens=10)
            return json.dumps({'answers': [{'id': question['id'], 'status': 'нет в документах',
                'answer': '', 'evidence': [], 'reason': 'Сведения не найдены.'}
                for question in payload['questions']]}, ensure_ascii=False), {
                    'usage': {'prompt_tokens': 30, 'completion_tokens': 10}, 'finish_reason': 'stop'}

        client.complete = absent_response
        result = self.invoke(['query', '--attachment', str(self.document), str(unsupported),
                              '--prompt', 'Какой срок?'], client=client)
        self.assertEqual(result, 0)
        self.assertTrue(client.calls[0]['read_errors'])
        self.assertEqual(self.read_json(self.output / 'answers.json')[0]['status'], 'не знаю')


if __name__ == '__main__':
    unittest.main()
