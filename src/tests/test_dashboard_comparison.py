"""Проверяет сопоставление запусков из истории на сохранённых артефактах."""
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.dashboard.comparison import compare_history_runs
from src.evaluation.runs import _metrics_for_rows
from src.files.run_storage import save_answers, write_json


class DashboardComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def run_artifacts(self, mode, items, *, document='Срок — 30 дней.', verdict=None, **metadata):
        directory = self.root / mode
        directory.mkdir()
        questions = [{'id': identifier, 'question': text, 'package': 'INPUT'} for identifier, text, _, _ in items]
        answers = {identifier: {'id': identifier, 'status': status, 'answer': answer, 'evidence': []}
                   for identifier, _, status, answer in items}
        write_json(directory / 'inputs.json', {'questions': questions, 'packages': {'INPUT': [{'notice.txt': document}, []]}})
        write_json(directory / 'manifest.json', {'mode': mode, 'status': 'completed', 'model': 'model',
                  'base_url': 'https://example.test/v1', 'input_fingerprint': 'same-inputs',
                  'question_ids': list(answers), **metadata})
        save_answers(directory, questions, answers)
        if verdict:
            rows = [{'id': identifier, 'type': 'факт', 'verdict': verdict, 'why': ''} for identifier in answers]
            with (directory / 'evaluation.csv').open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys(), delimiter=';')
                writer.writeheader()
                writer.writerows(rows)
            metrics = _metrics_for_rows(rows)
            write_json(directory / 'evaluation.json', {**metrics, 'by_type': {'факт': metrics},
                       'evaluation_fingerprint': 'same-reference'})

    def test_compares_common_questions_by_text_and_preserves_multiline_answers(self):
        self.run_artifacts('baseline', [('B1', 'Какой срок?', 'ответ', '30'),
                                        ('B2', 'Цена?', 'ответ', '40')])
        self.run_artifacts('verified', [('V1', 'КАКОЙ срок!', 'не знаю', ''),
                                        ('V2', 'Где склад?', 'ответ', 'Город.\nУлица.')], document='Другой документ.')
        data = compare_history_runs(self.root, 'baseline', 'verified')
        self.assertEqual(data['coverage'], {'paired': 1, 'baseline_only': 1, 'verified_only': 1, 'ambiguous': 0})
        self.assertEqual(data['transitions'], [{'baseline': 'ответ', 'verified': 'не знаю', 'count': 1}])
        self.assertTrue(data['questions'][0]['changed'])
        self.assertFalse(data['documents_match'])
        self.assertIsNone(data['quality'])

    def test_uses_existing_quality_check_for_same_inputs_and_reference(self):
        self.run_artifacts('baseline', [('Q1', 'Какой срок?', 'ответ', '35 дней.')], verdict='обман')
        self.run_artifacts('verified', [('Q1', 'Какой срок?', 'ответ', 'Срок:\n30 дней.')], verdict='верно')
        data = compare_history_runs(self.root, 'baseline', 'verified')
        self.assertTrue(data['documents_match'])
        self.assertEqual(data['quality']['baseline']['correct'], 0)
        self.assertEqual(data['quality']['verified']['correct'], 1)
        self.assertEqual(data['questions'][0]['verified']['answer'], 'Срок:\n30 дней.')

    def test_keeps_each_saved_score_when_quality_delta_is_unavailable(self):
        self.run_artifacts('baseline', [('Q1', 'Какой срок?', 'ответ', '35 дней.')], verdict='обман')
        self.run_artifacts('verified', [('Q1', 'Какой срок?', 'ответ', '30 дней.')], verdict='верно', temperature=0.5,
                           document='Действующий срок — 30 календарных дней.')
        data = compare_history_runs(self.root, 'baseline', 'verified')
        self.assertIsNone(data['quality'])
        self.assertEqual(data['baseline']['quality']['index'], -100)
        self.assertEqual(data['verified']['quality']['by_type']['факт']['index'], 100)

    def test_excludes_ambiguous_pairing_and_rejects_unsafe_selection(self):
        self.run_artifacts('baseline', [('B1', 'Какой срок?', 'ответ', '30'), ('B2', 'Какой срок?', 'ответ', '40')])
        self.run_artifacts('verified', [('V1', 'Какой срок?', 'ответ', '30')])
        data = compare_history_runs(self.root, 'baseline', 'verified')
        self.assertEqual((data['coverage']['paired'], data['coverage']['ambiguous']), (0, 1))
        with self.assertRaises(ValueError):
            compare_history_runs(self.root, '../baseline', 'verified')
        with self.assertRaises(ValueError):
            compare_history_runs(self.root, 'verified', 'baseline')

    def test_preserves_meaningful_symbols_in_question_matching(self):
        self.run_artifacts('baseline', [('B1', 'Срок — 30 дней?', 'ответ', '30'),
                                        ('B2', 'Применяется ставка 5%?', 'ответ', 'Да')])
        self.run_artifacts('verified', [('V1', 'Срок - 30 дней!', 'ответ', '30'),
                                        ('V2', 'Применяется ставка 5?', 'ответ', 'Да')])
        data = compare_history_runs(self.root, 'baseline', 'verified')
        self.assertEqual(data['coverage'], {'paired': 1, 'baseline_only': 1, 'verified_only': 1, 'ambiguous': 0})
