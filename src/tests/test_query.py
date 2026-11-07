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
import unittest
from unittest.mock import patch

from src import cli
from src.workflows import pipeline
from src import settings
from src.evaluation import reference_data
from src.generation.client import ApiError
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
        answers = [{'id': item['id'], 'status': 'ответ', 'answer': '12 месяцев',
                    'evidence': [{'file': document['file'], 'quote': document['text']}],
                    'reason': 'Срок указан в переданном документе.'}
                   for item in payload['questions']]
        return json.dumps({'answers': answers}, ensure_ascii=False), {
            'usage': {'prompt_tokens': 30, 'completion_tokens': 10}, 'finish_reason': 'stop'}


class QueryTests(unittest.TestCase):
    """Проверяет CLI с --atacment, --promt и необязательным --mode."""

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
             patch.object(pipeline, 'load_env'), patch.object(pipeline, 'emit'), \
             patch.object(evaluation, 'evaluate_run', side_effect=AssertionError('Query must not load gold')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(arguments)

    def arguments(self, *questions, content=None):
        return ['query', '--atacment', str(content or self.documents), '--promt', *questions]

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_arbitrary_nested_folder_produces_answers_report_and_exact_citation(self):
        nested = self.documents / 'Приложения' / 'Новая редакция'
        nested.mkdir(parents=True)
        (nested / 'Требования.md').write_text('Заявки рассматриваются комиссией.', encoding='utf-8')
        before = {path.relative_to(self.documents): path.read_bytes()
                  for path in self.documents.rglob('*') if path.is_file()}
        result = self.invoke(self.arguments('Какой гарантийный срок?'))
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

    def test_several_files_and_questions_remain_one_ordered_batch(self):
        other = self.root / 'Условия.txt'
        other.write_text('Приёмка осуществляется заказчиком.', encoding='utf-8')
        questions = ['Какой гарантийный срок?', 'Сколько месяцев составляет гарантия?']
        result = self.invoke(['query', '--atacment', str(self.document), str(other), '--promt', *questions])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.client.calls), 1)
        payload = self.client.calls[0]
        self.assertEqual(len(payload['documents']), 2)
        self.assertEqual([item['question'] for item in payload['questions']], questions)
        with (self.output / 'answers.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream, delimiter=';'))
        self.assertEqual([item['id'] for item in rows], ['INPUT-001', 'INPUT-002'])

    def test_questions_file_inside_content_folder_is_not_sent_as_document(self):
        question_file = self.documents / 'Мои вопросы.csv'
        question_file.write_text('question\nКакой гарантийный срок?\n', encoding='utf-8-sig')
        result = self.invoke(self.arguments(str(question_file)))
        self.assertEqual(result, 0)
        payload = self.client.calls[0]
        self.assertEqual(len(payload['documents']), 1)
        self.assertEqual(payload['questions'][0]['question'], 'Какой гарантийный срок?')
        self.assertNotIn('question\n', payload['documents'][0]['text'])

    def test_inline_and_file_questions_keep_argument_order(self):
        question_file = self.root / 'questions.txt'
        question_file.write_text('Второй вопрос?\n\nТретий вопрос?\n', encoding='utf-8')
        result = self.invoke(self.arguments('Первый вопрос?', str(question_file), 'Четвёртый вопрос?'))
        self.assertEqual(result, 0)
        self.assertEqual([item['question'] for item in self.client.calls[0]['questions']],
                         ['Первый вопрос?', 'Второй вопрос?', 'Третий вопрос?', 'Четвёртый вопрос?'])

    def test_literal_question_with_date_or_filename_is_not_mistaken_for_path(self):
        questions = ['Какая гарантия действует на 01/11/2026?', 'Что указано в Договор.txt?']
        result = self.invoke(self.arguments(*questions))
        self.assertEqual(result, 0)
        self.assertEqual([item['question'] for item in self.client.calls[0]['questions']], questions)

    def test_gold_and_environment_file_are_not_sent_to_model(self):
        marker = 'SYNTHETIC_GOLD_MUST_NOT_ENTER_PROMPT'
        (self.documents / 'answers_public.json').write_text(json.dumps({'secret_answer': marker}), encoding='utf-8')
        (self.documents / '.env').write_text('ITFEST_API_KEY=DO_NOT_READ_THIS_SENTINEL', encoding='utf-8')
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

    def test_missing_question_file_is_not_silently_sent_as_text(self):
        for value in (str(self.root / 'missing.txt'), './missing.json', 'missing.csv'):
            with self.subTest(value=value):
                self.assertEqual(self.invoke(self.arguments(value), forbid_api=True), 1)
        self.assertFalse(self.output.exists())

    def test_question_directory_and_invalid_file_fail_before_api(self):
        invalid = self.root / 'bad.csv'
        invalid.write_text('unknown\nvalue\n', encoding='utf-8')
        for value in (str(self.documents), str(invalid)):
            with self.subTest(value=value):
                self.assertEqual(self.invoke(self.arguments(value), forbid_api=True), 1)

    def test_existing_result_is_preserved(self):
        self.output.mkdir()
        previous = self.output / 'answers.csv'
        previous.write_text('previous-result', encoding='utf-8')
        result = self.invoke(self.arguments('Вопрос?'), forbid_api=True)
        self.assertEqual(result, 1)
        self.assertEqual(previous.read_text(encoding='utf-8'), 'previous-result')

    def test_mode_defaults_to_verified_and_baseline_selects_original_prompt(self):
        from src.generation.prompts import BASELINE, VERIFIED
        result = self.invoke(self.arguments('Вопрос?'))
        self.assertEqual(result, 0)
        self.assertEqual(self.read_json(self.output / 'manifest.json')['mode'], 'verified')
        self.assertEqual(self.client.system_prompts, [VERIFIED])
        self.output = self.root / 'baseline-result'
        baseline = OfflineClient()
        result = self.invoke(self.arguments('Вопрос?') + ['--mode', 'baseline'], client=baseline)
        self.assertEqual(result, 0)
        self.assertEqual(self.read_json(self.output / 'manifest.json')['mode'], 'baseline')
        self.assertEqual(baseline.system_prompts, [BASELINE])

    def test_query_requires_both_inputs_and_shows_mode_and_evaluation(self):
        # Справка — пользовательский контракт: лишние режимы ввода больше не нужны.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main(['query', '--help'])
        self.assertEqual(result, 0)
        help_text = output.getvalue()
        for option in ('--atacment', '--promt', '--mode', '--evaluate'):
            self.assertIn(option, help_text)
        for option in ('--output', '--inspect', '--questions', '--timeout', '--workers', '--max-tokens'):
            self.assertNotIn(option, help_text)
        invalid = [[], ['--atacment', str(self.document)], ['--promt', 'Вопрос?'],
                   ['--atacment', str(self.document), '--promt', 'Вопрос?', '--inspect']]
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
        (self.documents / 'Приложение.doc').write_bytes(b'unsupported-document')
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
        result = self.invoke(self.arguments('Какой срок?'), client=client)
        self.assertEqual(result, 0)
        self.assertTrue(client.calls[0]['read_errors'])
        self.assertEqual(self.read_json(self.output / 'answers.json')[0]['status'], 'не знаю')


if __name__ == '__main__':
    unittest.main()
