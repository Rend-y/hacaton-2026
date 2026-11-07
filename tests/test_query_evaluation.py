"""Оценка выборки query на искусственных эталонах без API модели."""
import contextlib
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from query_evaluation import plan_query_evaluation, evaluate_query_run


class QueryEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.run = self.root / 'run'
        self.run.mkdir()
        package = self.data / 'packages' / 'P01'
        package.mkdir(parents=True)
        self.document = 'Цена контракта: 1800000 руб. Срок выполнения работ: 60 календарных дней. Гарантийный срок: 12 месяцев.'
        self.security = 'Размер обеспечения исполнения контракта: 5 процентов. Размер обеспечения заявки: 18000 рублей.'
        (package / 'contract.txt').write_text(self.document, encoding='utf-8')
        (package / 'security.txt').write_text(self.security, encoding='utf-8')
        second = self.data / 'packages' / 'P02'
        second.mkdir()
        (second / 'contract.txt').write_text('Другая закупка, гарантия 24 месяца.', encoding='utf-8')
        formulations = [
            ('P01-01', 'Какая начальная (максимальная) цена контракта, в рублях?', 1800000, self.document),
            ('P01-07', 'Какой срок выполнения работ, в календарных днях?', 60, self.document),
            ('P01-08', 'Какой гарантийный срок на результаты работ, в месяцах?', 12, self.document),
            ('P01-04', 'Какой размер обеспечения исполнения контракта, в процентах?', 5, self.security),
            ('P01-03', 'Какой размер обеспечения заявки, в рублях?', 18000, self.security),
        ]
        self.reference = [{'id': identifier, 'package': 'P01', 'question': question}
                          for identifier, question, _, _ in formulations]
        self.reference.append({'id': 'P02-01', 'package': 'P02', 'question': formulations[2][1]})
        self.write_reference()
        self.gold = [{'id': identifier, 'package': 'P01', 'question': question, 'kind': 'num',
                      'expected': [value], 'type': 'факт', 'status': 'ответ'}
                     for identifier, question, value, _ in formulations]
        self.gold.append({'id': 'P02-01', 'package': 'P02', 'question': formulations[2][1],
                          'kind': 'num', 'expected': [24], 'type': 'факт', 'status': 'ответ'})
        self.write_json(self.data / 'answers_public.json', self.gold)
        selected = [formulations[0][1].replace('(максимальная)', 'максимальная'),
                    'Какой срок выполнения работ?', 'Какой гарантийный срок на результаты работ?',
                    formulations[3][1], formulations[4][1]]
        self.questions = [{'id': 'INPUT-%03d' % (index + 1), 'package': 'INPUT', 'question': value}
                          for index, value in enumerate(selected)]
        self.documents = {'копия/contract.txt': self.document, 'переименован.txt': self.security}
        self.snapshot = {'questions': self.questions, 'packages': {'INPUT': [self.documents, []]}}
        self.write_snapshot()
        with (self.run / 'answers.csv').open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=['id', 'status', 'answer', 'quote', 'file'], delimiter=';')
            writer.writeheader()
            for index, (_, _, value, quote) in enumerate(formulations):
                writer.writerow({'id': self.questions[index]['id'], 'status': 'ответ', 'answer': str(value),
                                 'quote': quote, 'file': next(name for name, text in self.documents.items() if text == quote)})

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def write_reference(self):
        with (self.data / 'questions.csv').open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=['id', 'package', 'question'], delimiter=';')
            writer.writeheader()
            writer.writerows(self.reference)

    def write_snapshot(self):
        self.write_json(self.run / 'inputs.json', self.snapshot)
        fingerprint = hashlib.sha256(json.dumps(self.snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.write_json(self.run / 'manifest.json', {'status': 'completed', 'input_fingerprint': fingerprint,
                        'question_ids': [row['id'] for row in self.questions]})

    def plan(self, questions=None, documents=None, errors=None):
        return plan_query_evaluation(self.questions if questions is None else questions,
            self.documents if documents is None else documents, errors or [], self.data)

    def evaluate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return evaluate_query_run(self.run, self.data)

    def test_plan_matches_five_questions_without_reading_gold(self):
        # При удалённых правильных ответах план остаётся доступен генератору.
        (self.data / 'answers_public.json').unlink()
        result = self.plan()
        self.assertEqual(result['package'], 'P01')
        self.assertEqual(result['source_question_count'], 6)
        self.assertEqual([row['reference_id'] for row in result['matches']],
                         ['P01-01', 'P01-07', 'P01-08', 'P01-04', 'P01-03'])
        self.assertEqual([row['match'] for row in result['matches']],
                         ['normalized', 'explicit_alias', 'explicit_alias', 'normalized', 'normalized'])

    def test_changed_units_negation_or_unknown_question_is_rejected(self):
        for question in ('Какой срок выполнения работ, в рабочих днях?',
                         'Какой срок выполнения работ не установлен?', 'Какой цвет стен?'):
            with self.subTest(question=question):
                rows = copy.deepcopy(self.questions)
                rows[1]['question'] = question
                with self.assertRaisesRegex(ValueError, 'не найден единственный'):
                    self.plan(questions=rows)

    def test_duplicates_cannot_increase_the_denominator(self):
        rows = copy.deepcopy(self.questions)
        rows.append({**rows[1], 'id': 'INPUT-006'})
        with self.assertRaisesRegex(ValueError, 'одному эталону'):
            self.plan(questions=rows)

    def test_only_complete_matching_document_set_can_be_scored(self):
        for documents in ({'contract.txt': self.document},
                          {**self.documents, 'extra.txt': 'Лишние сведения.'},
                          {**self.documents, 'переименован.txt': self.security + ' Изменено.'}):
            with self.subTest(documents=list(documents)), self.assertRaisesRegex(ValueError, 'весь комплект'):
                self.plan(documents=documents)
        with self.assertRaisesRegex(ValueError, 'не полностью'):
            self.plan(errors=['Повреждённый файл'])

    def test_ambiguous_package_or_question_is_rejected(self):
        package = self.data / 'packages' / 'P02'
        (package / 'contract.txt').write_text(self.document, encoding='utf-8')
        (package / 'security.txt').write_text(self.security, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'однозначно'):
            self.plan()
        (package / 'contract.txt').write_text('Другая закупка.', encoding='utf-8')
        self.reference.append({**self.reference[0], 'id': 'P01-extra'})
        self.write_reference()
        with self.assertRaisesRegex(ValueError, 'не найден единственный'):
            self.plan()

    def test_real_official_checker_uses_only_selected_five(self):
        preserved = {name: (self.run / name).read_bytes() for name in ('answers.csv', 'inputs.json', 'manifest.json')}
        result = self.evaluate()
        self.assertEqual((result['n'], result['correct'], result['index']), (5, 5, 100.0))
        self.assertEqual(result['scope'], 'matched_query_questions')
        self.assertEqual(result['source_question_count'], 6)
        self.assertEqual(result['source_package'], 'P01')
        self.assertEqual(len(result['question_mapping']), 5)
        with (self.run / 'evaluation.csv').open(encoding='utf-8', newline='') as source:
            rows = list(csv.DictReader(source, delimiter=';'))
        self.assertEqual([row['id'] for row in rows], [row['id'] for row in self.questions])
        self.assertEqual(result['answers_sha256'], hashlib.sha256(preserved['answers.csv']).hexdigest())
        for name, data in preserved.items():
            self.assertEqual((self.run / name).read_bytes(), data)
        self.assertFalse(list(self.run.glob('query-evaluation-*')))

    def test_missing_answer_still_counts_in_selected_denominator(self):
        lines = (self.run / 'answers.csv').read_text(encoding='utf-8').splitlines()
        (self.run / 'answers.csv').write_text('\n'.join(lines[:-1]) + '\n', encoding='utf-8')
        result = self.evaluate()
        self.assertEqual((result['n'], result['correct'], result['careful'], result['index']), (5, 4, 1, 80.0))

    def test_unfinished_or_changed_snapshot_is_not_scored(self):
        manifest = json.loads((self.run / 'manifest.json').read_text(encoding='utf-8'))
        self.write_json(self.run / 'manifest.json', {**manifest, 'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'дождитесь'):
            self.evaluate()
        self.write_json(self.run / 'manifest.json', manifest)
        self.snapshot['questions'][0]['question'] = 'Изменённый вопрос?'
        self.write_json(self.run / 'inputs.json', self.snapshot)
        with self.assertRaisesRegex(ValueError, 'изменился'):
            self.evaluate()
        self.assertFalse((self.run / 'evaluation.json').exists())

    def test_changed_reference_document_is_not_scored(self):
        (self.data / 'packages' / 'P01' / 'contract.txt').write_text(self.document + ' Исправлено.', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'весь комплект'):
            self.evaluate()
        self.assertFalse((self.run / 'evaluation.json').exists())

    def test_gold_must_match_reference_question_and_package(self):
        for change in ({'package': 'P02'}, {'question': 'Другой вопрос?'}):
            with self.subTest(change=change):
                changed = copy.deepcopy(self.gold)
                changed[0].update(change)
                self.write_json(self.data / 'answers_public.json', changed)
                with self.assertRaisesRegex(ValueError, 'расходятся'):
                    self.evaluate()

    def test_extra_answer_id_is_not_silently_ignored(self):
        with (self.run / 'answers.csv').open('a', encoding='utf-8') as target:
            target.write('OTHER;не знаю;;;\n')
        with self.assertRaisesRegex(ValueError, 'лишние'):
            self.evaluate()


if __name__ == '__main__':
    unittest.main()
