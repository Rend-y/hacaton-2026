"""Проверяет query --evaluate через CLI и настоящий официальный оценщик.

Все документы, эталоны и ответы здесь искусственные. Имитируется только модель;
проверки цитат, запись CSV, сопоставление вопросов и HTML выполняются обычным кодом.
"""
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
import query
import report


class QueryEvaluationCliTests(unittest.TestCase):
    """Индекс выборки относится только к выбранным вопросам, а не ко всему набору."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='query-eval-cli-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data' / 'public'
        self.package = self.data / 'packages' / 'P01'
        self.package.mkdir(parents=True)
        self.quote = 'Синтетический показатель в документе составляет 12 единиц.'
        (self.package / 'contract.txt').write_text(self.quote, encoding='utf-8')
        other_package = self.data / 'packages' / 'P02'
        other_package.mkdir()
        (other_package / 'other.txt').write_text('Другой набор документов, показатель 99 единиц.', encoding='utf-8')
        # Проверяем также переименование исходного файла: важен весь текст корпуса.
        self.attachments = self.root / 'Папка документов'
        self.attachments.mkdir()
        (self.attachments / 'Другое имя.txt').write_text(self.quote, encoding='utf-8')
        self.output = self.root / 'result'
        self.official_questions = [
            'Какая начальная (максимальная) цена контракта, в рублях?',
            'Какой срок выполнения работ, в календарных днях?',
            'Какой гарантийный срок на результаты работ, в месяцах?',
            'Какой размер обеспечения исполнения контракта, в процентах?',
            'Какой размер обеспечения заявки, в рублях?',
        ]
        self.questions = [
            'Какая начальная максимальная цена контракта, в рублях?',
            'Какой срок выполнения работ?',
            'Какой гарантийный срок на результаты работ?',
            'Какой размер обеспечения исполнения контракта, в процентах?',
            'Какой размер обеспечения заявки, в рублях?',
        ]
        rows = [{'id': 'P01-%02d' % (index + 1), 'package': 'P01', 'question': question}
                for index, question in enumerate(self.official_questions)]
        rows.extend({'id': 'P02-%02d' % (index + 1), 'package': 'P02',
                     'question': 'Синтетический дополнительный вопрос %s?' % index}
                    for index in range(66))
        with (self.data / 'questions.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['id', 'package', 'question'], delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
        self.gold_marker = 'SYNTHETIC_GOLD_ONLY_NOT_MODEL_CONTEXT'
        self.gold_path = self.data / 'answers_public.json'
        gold = [{**row, 'source': row['package'], 'key': 'synthetic', 'kind': 'num',
                 'expected': [12 if row['package'] == 'P01' else 99], 'type': 'факт',
                 'status': 'ответ', 'file': 'contract.txt', 'quote': self.quote,
                 'audit_only': self.gold_marker} for row in rows]
        self.gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding='utf-8')
        self.phases, self.requests = [], []
        self.client = self.fake_client()

    def fake_client(self):
        owner = self

        class OfflineClient:
            """Берёт цитату из запроса и не открывает эталонный файл."""
            key = 'synthetic-query-eval-token'
            model = 'synthetic-model'
            base_url = 'https://example.test/v1'

            def __init__(self):
                self.stats = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}

            def complete(self, messages, progress=None):
                owner.phases.append('generation')
                owner.requests.append(messages)
                owner.assertFalse((owner.output / 'evaluation.json').exists())
                owner.assertNotIn(owner.gold_marker, json.dumps(messages, ensure_ascii=False))
                payload = json.loads(messages[1]['content'])
                document = payload['documents'][0]
                self.stats.update(requests=1, prompt_tokens=30, completion_tokens=10)
                answers = [{'id': item['id'], 'status': 'ответ', 'answer': '12 единиц',
                            'evidence': [{'file': document['file'], 'quote': document['text']}],
                            'reason': 'Синтетический показатель указан в документе.'}
                           for item in payload['questions']]
                return json.dumps({'answers': answers}, ensure_ascii=False), {
                    'usage': {'prompt_tokens': 30, 'completion_tokens': 10}, 'finish_reason': 'stop'}

        return OfflineClient()

    def invoke(self, arguments, forbid_api=False):
        api_patch = (patch.object(cli, 'Client', side_effect=AssertionError('No API expected'))
                     if forbid_api else patch.object(cli, 'Client', return_value=self.client))
        with api_patch, patch.object(cli, 'ROOT', self.root), patch.object(cli, 'load_env'), \
             patch.object(cli, 'emit'), patch.object(query, '_output_path', return_value=self.output), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(arguments)

    def arguments(self, questions=None):
        return ['query', '--atacment', str(self.attachments), '--promt', *(questions or self.questions)]

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_flag_is_supported_but_optional(self):
        args = cli.parser().parse_args(self.arguments() + ['--evaluate'])
        self.assertTrue(args.evaluate)
        args = cli.parser().parse_args(self.arguments())
        self.assertFalse(args.evaluate)

    def test_five_query_answers_are_evaluated_after_generation_not_as_seventy_one(self):
        original_open = Path.open
        before_evaluation = []

        def guard_gold(path, *args, **kwargs):
            if path.resolve() == self.gold_path.resolve():
                # Сам эталон разрешён только после записи готовых ответов.
                self.assertEqual(self.phases[0], 'generation')
                self.assertEqual(self.read_json(self.output / 'manifest.json')['status'], 'completed')
                before_evaluation.append((self.output / 'answers.csv').read_bytes())
                self.phases.append('gold')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', guard_gold):
            result = self.invoke(self.arguments() + ['--mode', 'verified', '--evaluate'])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(before_evaluation)
        assessed = self.read_json(self.output / 'evaluation.json')
        self.assertEqual((assessed['n'], assessed['correct'], assessed['careful'], assessed['lies']), (5, 5, 0, 0))
        self.assertEqual(assessed['index'], 100)
        self.assertEqual(assessed['scope'], 'matched_query_questions')
        self.assertEqual(assessed['source_question_count'], 71)
        self.assertEqual((self.output / 'answers.csv').read_bytes(), before_evaluation[0])
        self.assertEqual(assessed['answers_sha256'], hashlib.sha256(before_evaluation[0]).hexdigest())
        self.assertEqual(self.read_json(self.output / 'manifest.json')['question_ids'],
                         ['INPUT-%03d' % index for index in range(1, 6)])
        self.assertEqual(report.build_run_model(self.output)['runs'][0]['quality']['n'], 5)
        page = (self.output / 'report.html').read_text(encoding='utf-8')
        self.assertIn('Индекс доверия', page)
        self.assertIn('100,0', page)
        self.assertNotIn('Оценка по эталону не выполнена', page)

    def test_saved_legacy_query_can_be_evaluated_without_api_or_answer_changes(self):
        self.assertEqual(self.invoke(self.arguments()), 0)
        self.assertFalse((self.output / 'evaluation.json').exists())
        before = {name: (self.output / name).read_bytes()
                  for name in ('answers.csv', 'answers.json', 'manifest.json')}
        result = self.invoke(['evaluate', '--run', str(self.output), '--data', str(self.data)], forbid_api=True)
        self.assertEqual(result, 0)
        self.assertEqual(len(self.requests), 1)
        for name, content in before.items():
            self.assertEqual((self.output / name).read_bytes(), content)
        self.assertEqual(self.read_json(self.output / 'evaluation.json')['n'], 5)
        self.assertIn('Индекс доверия', (self.output / 'report.html').read_text(encoding='utf-8'))

    def test_baseline_evaluation_counts_error_without_enabling_local_repairs(self):
        original_complete = self.client.complete

        def wrong_first_answer(messages, progress=None):
            content, meta = original_complete(messages, progress)
            payload = json.loads(content)
            payload['answers'][0]['answer'] = '99 единиц'
            return json.dumps(payload, ensure_ascii=False), meta

        self.client.complete = wrong_first_answer
        result = self.invoke(self.arguments() + ['--mode', 'baseline', '--evaluate'])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.read_json(self.output / 'manifest.json')['mode'], 'baseline')
        self.assertEqual(self.read_json(self.output / 'answers.json')[0]['answer'], '99 единиц')
        assessed = self.read_json(self.output / 'evaluation.json')
        self.assertEqual((assessed['n'], assessed['correct'], assessed['careful'], assessed['lies']), (5, 4, 0, 1))
        self.assertEqual(assessed['index'], 60)

    def test_unknown_question_fails_before_api_instead_of_grading_a_subset(self):
        result = self.invoke(self.arguments([self.questions[0], 'Кто победил в закупке?']) + ['--evaluate'], forbid_api=True)
        self.assertEqual(result, 1)
        self.assertFalse(self.output.exists())

    def test_changed_corpus_fails_before_api(self):
        (self.attachments / 'Новое условие.txt').write_text('Дополнительные условия изменяют договор.', encoding='utf-8')
        result = self.invoke(self.arguments() + ['--evaluate'], forbid_api=True)
        self.assertEqual(result, 1)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
