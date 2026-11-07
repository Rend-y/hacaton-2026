"""Проверяет оценку и честность сравнения на искусственных локальных результатах.

Тесты не вызывают API модели. Их числа проверяют поведение кода и не являются
результатами качества решения на публичном наборе хакатона.
"""

import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import evaluation


class EvaluationTests(unittest.TestCase):
    """Проверки экспорта метрик и отказа от сравнения несовместимых запусков."""

    def setUp(self):
        """Создаёт два сопоставимых запуска в изолированной временной папке."""
        # Папка внутри tests доступна и в среде с ограниченной записью в системный TEMP.
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temporary.name)
        self.before = self.root / 'baseline'
        self.after = self.root / 'verified'
        self.output = self.root / 'comparison'
        self.before.mkdir()
        self.after.mkdir()
        for directory, mode in ((self.before, 'baseline'), (self.after, 'verified')):
            self.write_json(directory / 'manifest.json', {
                'mode': mode, 'model': 'example', 'base_url': 'https://api.example/v1',
                'input_fingerprint': 'same-inputs', 'question_ids': ['P01-01', 'P01-02'],
                'temperature': 0, 'max_tokens': 16384, 'elapsed_seconds': 3.1,
                'stats': {'requests': 1, 'prompt_tokens': 100, 'completion_tokens': 50},
            })
        self.write_evaluation(self.before, 1, 0, 1)
        self.write_evaluation(self.after, 2, 0, 0)

    def tearDown(self):
        """Удаляет только временные файлы текущего теста."""
        self.temporary.cleanup()

    @staticmethod
    def write_json(path, value):
        """Сохраняет входные данные теста в том же формате JSON, что и CLI."""
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def write_evaluation(self, directory, correct, careful, lies):
        """Создаёт согласованные искусственные счётчики и индекс доверия."""
        n = correct + careful + lies
        counts = {'n': n, 'correct': correct, 'careful': careful, 'lies': lies,
                  'index': round((correct - lies) / n * 100, 1)}
        self.write_json(directory / 'evaluation.json', {**counts, 'by_type': {'факт': counts}})

    def change_manifest(self, **changes):
        """Меняет выбранные параметры проверяемого запуска для негативных сценариев."""
        path = self.after / 'manifest.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update(changes)
        self.write_json(path, data)

    def test_comparison_writes_metrics_and_category_rows(self):
        """Общий результат и категории сохраняются в JSON и CSV без потери значений."""
        with contextlib.redirect_stdout(io.StringIO()):
            result = evaluation.compare_runs(self.before, self.after, self.output)
        self.assertEqual(result['delta']['index'], 100)
        self.assertEqual(result['delta']['lies'], -1)
        self.assertEqual(result['by_type']['факт']['verified']['correct'], 2)
        self.assertEqual(json.loads((self.output / 'comparison.json').read_text(encoding='utf-8')), result)
        with (self.output / 'comparison.csv').open(encoding='utf-8', newline='') as source:
            rows = list(csv.DictReader(source, delimiter=';'))
        self.assertTrue(any(row['scope'] == 'type' and row['metric'] == 'index' for row in rows))

    def test_different_configuration_rejected(self):
        """Смена модели, входов или бюджета не должна выглядеть как улучшение проверок."""
        for field, value in [('model', 'other'), ('base_url', 'https://other.example/v1'),
                             ('input_fingerprint', 'changed'), ('question_ids', ['P02-01']),
                             ('temperature', 1), ('max_tokens', 32768), ('mode', 'baseline')]:
            with self.subTest(field=field):
                path = self.after / 'manifest.json'
                original = path.read_text(encoding='utf-8')
                self.change_manifest(**{field: value})
                with self.assertRaises(ValueError):
                    evaluation.compare_runs(self.before, self.after, self.output)
                path.write_text(original, encoding='utf-8')
        self.assertFalse(self.output.exists())

    def test_same_questions_in_different_order_are_comparable(self):
        """Порядок обработки не влияет на сопоставимость одинакового набора вопросов."""
        self.change_manifest(question_ids=['P01-02', 'P01-01'])
        with contextlib.redirect_stdout(io.StringIO()):
            result = evaluation.compare_runs(self.before, self.after, self.output)
        self.assertEqual(result['baseline']['n'], 2)

    def test_missing_metadata_is_rejected(self):
        """Без отпечатка входов нельзя доказать, что сравниваются одинаковые данные."""
        self.change_manifest(input_fingerprint='')
        with self.assertRaisesRegex(ValueError, 'input_fingerprint'):
            evaluation.compare_runs(self.before, self.after, self.output)

    def test_incomplete_or_failed_runs_cannot_be_compared(self):
        """Незавершённый запуск или технический сбой исключается из сравнения качества."""
        for directory, mode in ((self.before, 'baseline'), (self.after, 'verified')):
            for status in ('running', 'completed_with_errors', 'failed', None):
                with self.subTest(mode=mode, status=status):
                    path = directory / 'manifest.json'
                    original = path.read_text(encoding='utf-8')
                    manifest = json.loads(original)
                    manifest['status'] = status
                    self.write_json(path, manifest)
                    with self.assertRaisesRegex(ValueError, 'Повторите ' + mode):
                        evaluation.compare_runs(self.before, self.after, self.output)
                    path.write_text(original, encoding='utf-8')
        self.assertFalse(self.output.exists())

    def test_completed_status_allows_comparison(self):
        """Два полностью завершённых запуска проходят проверку статусов."""
        for directory in (self.before, self.after):
            path = directory / 'manifest.json'
            manifest = json.loads(path.read_text(encoding='utf-8'))
            manifest['status'] = 'completed'
            self.write_json(path, manifest)
        with contextlib.redirect_stdout(io.StringIO()):
            result = evaluation.compare_runs(self.before, self.after, self.output)
        self.assertEqual(result['delta']['index'], 100)

    def test_tampered_metrics_are_rejected(self):
        """Индекс, не соответствующий числу ответов, не попадает в итоговый отчёт."""
        path = self.after / 'evaluation.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['index'] = 50
        self.write_json(path, data)
        with self.assertRaisesRegex(ValueError, 'Индекс'):
            evaluation.compare_runs(self.before, self.after, self.output)

    def test_changed_evaluation_data_is_rejected(self):
        """Изменение эталонов или оценщика делает ранее полученные оценки несопоставимыми."""
        path = self.after / 'evaluation.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['evaluation_fingerprint'] = 'different-gold-or-checker'
        self.write_json(path, data)
        with self.assertRaisesRegex(ValueError, 'набор оценки'):
            evaluation.compare_runs(self.before, self.after, self.output)

    def test_evaluation_invokes_official_checker_and_preserves_all_gold_scope(self):
        """Команда оценщика получает эталоны отдельно и сохраняет полный знаменатель."""
        data = self.root / 'data'
        (data / 'packages').mkdir(parents=True)
        (data / 'answers_public.json').write_text('[]', encoding='utf-8')
        (self.after / 'answers.csv').write_text('id;status;answer;quote;file\n', encoding='utf-8')
        checker = self.root / 'check.py'
        checker.write_text('# test placeholder', encoding='utf-8')

        def invoke(command, **kwargs):
            """Подменяет запуск процесса согласованными RESULT и CSV для проверки обвязки."""
            self.assertEqual(command[1], str(checker))
            self.assertIn('--gold', command)
            self.assertEqual(kwargs['env']['PYTHONIOENCODING'], 'utf-8')
            with (self.after / 'evaluation.csv').open('w', encoding='utf-8', newline='') as target:
                writer = csv.DictWriter(target, fieldnames=['id', 'type', 'verdict', 'why'], delimiter=';')
                writer.writeheader()
                writer.writerow({'id': 'P01-01', 'type': 'факт', 'verdict': 'верно', 'why': ''})
                writer.writerow({'id': 'P01-02', 'type': 'нет данных', 'verdict': 'осторожно', 'why': 'нет строки'})
            metrics = {'n': 2, 'correct': 1, 'careful': 1, 'lies': 0, 'index': 50.0}
            return evaluation.subprocess.CompletedProcess(command, 0, 'Report\nRESULT ' + json.dumps(metrics) + '\n', '')

        with patch.object(evaluation, 'EVALUATOR_PATH', checker), patch.object(evaluation.subprocess, 'run', side_effect=invoke), contextlib.redirect_stdout(io.StringIO()):
            result = evaluation.evaluate_run(self.after, data)
        self.assertEqual(result['scope'], 'all_questions_in_gold')
        self.assertEqual(result['by_type']['нет данных']['careful'], 1)
        self.assertEqual(result['index'], 50)
        self.assertEqual(len(result['evaluation_fingerprint']), 64)
        self.assertTrue((self.after / 'evaluation.json').is_file())

    def test_missing_gold_produces_actionable_error(self):
        """При отсутствии эталонов ошибка называет недостающий файл."""
        (self.after / 'answers.csv').write_text('id;status;answer;quote;file\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'answers_public.json'):
            evaluation.evaluate_run(self.after, self.root / 'missing')


if __name__ == '__main__':
    unittest.main()
