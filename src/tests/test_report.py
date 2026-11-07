"""Проверки HTML-отчётов на искусственных данных без запросов к модели.

Числа фикстур проверяют арифметику и состояние интерфейса; это не результаты
качества решения на публичном или контрольном наборе хакатона.
"""

import contextlib
import csv
import hashlib
import io
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.evaluation import runs as evaluation
from src.reporting import report
from src import cli
from src.workflows import pipeline
from src.workflows import query
from src.evaluation import query_evaluation
from src import settings


class HtmlElements(HTMLParser):
    """Извлекает семантические элементы без привязки тестов к оформлению страницы."""

    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ReportTests(unittest.TestCase):
    """Проверяет происхождение метрик, отсутствие выдуманных оценок и безопасный HTML."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='report-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.create_run('verified', 'verified')

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def create_run(self, name, mode):
        directory = self.root / name
        directory.mkdir()
        questions = [
            {'id': 'P01-01', 'package': 'P01', 'question': 'Какой гарантийный срок?'},
            {'id': 'P01-02', 'package': 'P01', 'question': 'Сколько специалистов требуется?'},
        ]
        quote = 'Гарантийный срок составляет 12 месяцев.'
        answers = [
            {'id': 'P01-01', 'status': 'ответ', 'answer': '12 месяцев',
             'evidence': [{'file': 'contract.txt', 'quote': quote}], 'reason': 'Указано в контракте.'},
            {'id': 'P01-02', 'status': 'не знаю', 'answer': '', 'evidence': [],
             'reason': 'Недостаточно оснований.'},
        ]
        self.write_json(directory / 'manifest.json', {
            'model': 'synthetic-model', 'base_url': 'https://example.test/v1', 'mode': mode,
            'status': 'completed', 'question_ids': [item['id'] for item in questions],
            'input_fingerprint': 'synthetic-inputs', 'temperature': 0, 'max_tokens': 4096,
            'workers': 1, 'repair_attempts': 2 if mode == 'verified' else 0,
            'created_at': '2026-11-07T12:00:00+00:00', 'elapsed_seconds': 2.5,
            'stats': {'requests': 2, 'successful_requests': 2, 'prompt_tokens': 200,
                      'completion_tokens': 50, 'http_retries': 0},
        })
        self.write_json(directory / 'inputs.json', {'questions': questions,
            'packages': {'P01': [{'contract.txt': quote}, []]}})
        self.write_answers(directory, answers)
        (directory / 'trace.jsonl').write_text('', encoding='utf-8')
        return directory

    def write_answers(self, directory, answers):
        """Сохраняет согласованные внутренние ответы и их внешний CSV."""
        self.write_json(directory / 'answers.json', answers)
        with (directory / 'answers.csv').open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=['id', 'status', 'answer', 'quote', 'file'], delimiter=';')
            writer.writeheader()
            for item in answers:
                evidence = item.get('evidence') or []
                first = evidence[0] if evidence else {}
                cautious = item['status'] in ('не знаю', 'нет в документах')
                writer.writerow({'id': item['id'], 'status': item['status'],
                    'answer': '' if cautious else item.get('answer', ''),
                    'quote': '' if cautious else first.get('quote', ''),
                    'file': '' if cautious else first.get('file', '')})

    def write_evaluation(self, directory, verdicts=('верно', 'осторожно'), digest=True):
        rows = [{'id': 'P01-01', 'type': 'факт', 'verdict': verdicts[0],
                 'why': 'неверное значение' if verdicts[0] == 'обман' else ''},
                {'id': 'P01-02', 'type': 'нет сведений', 'verdict': verdicts[1],
                 'why': 'ответ есть в документах' if verdicts[1] == 'осторожно' else ''}]
        counts = evaluation._metrics_for_rows(rows)
        result = {**counts, 'scope': 'all_questions_in_gold',
                  'evaluation_fingerprint': 'synthetic-evaluation',
                  'by_type': {row['type']: evaluation._metrics_for_rows([row]) for row in rows}}
        if digest:
            result['answers_sha256'] = hashlib.sha256((directory / 'answers.csv').read_bytes()).hexdigest()
        self.write_json(directory / 'evaluation.json', result)
        with (directory / 'evaluation.csv').open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=['id', 'type', 'verdict', 'why'], delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
        return result

    def create_comparison(self):
        before = self.create_run('baseline', 'baseline')
        self.write_evaluation(before, ('обман', 'осторожно'))
        self.write_evaluation(self.run, ('верно', 'осторожно'))
        output = self.root / 'comparison'
        with contextlib.redirect_stdout(io.StringIO()):
            evaluation.compare_runs(before, self.run, output)
        return before, output

    def test_repair_counts_separate_batches_but_not_http_retries(self):
        events = [
            {'package': 'P01', 'attempt': 1, 'event': 'api_request'},
            {'package': 'P01', 'attempt': 1, 'event': 'api_error'},
            {'package': 'P01', 'batch': 'root.1', 'attempt': 1, 'event': 'api_request'},
            {'package': 'P01', 'batch': 'root.1', 'attempt': 1, 'event': 'api_request'},
            {'package': 'P01', 'batch': 'root.2', 'attempt': 1, 'response': '{}'},
            {'package': 'P01', 'batch': 'root.2', 'attempt': 0, 'event': 'api_request'},
        ]
        (self.run / 'trace.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
        operations = report.build_run_model(self.run)['runs'][0]['operations']
        self.assertEqual(operations['repair_requests'], 3)

    def test_no_evaluation_does_not_invent_quality(self):
        model = report.build_run_model(self.run)
        run = model['runs'][0]
        self.assertIsNone(run['quality'])
        self.assertEqual(run['n_questions'], 2)
        self.assertEqual(run['operations']['requests'], 2)
        self.assertEqual(run['operations']['elapsed_seconds'], 2.5)
        self.assertTrue(all(answer.get('verdict') is None for answer in run['answers']))

    def test_detailed_answers_must_match_exported_csv(self):
        answers = self.read_json(self.run / 'answers.json')
        answers[0]['answer'] = '24 месяца'
        self.write_json(self.run / 'answers.json', answers)
        with self.assertRaises(ValueError):
            report.build_run_model(self.run)

    def test_csv_only_run_preserves_answer_and_evidence(self):
        (self.run / 'answers.json').unlink()
        run = report.build_run_model(self.run)['runs'][0]
        self.assertEqual(run['answers'][0]['answer'], '12 месяцев')
        self.assertEqual(run['answers'][0]['evidence'], [{
            'file': 'contract.txt', 'quote': 'Гарантийный срок составляет 12 месяцев.'}])
        self.assertIsNone(run['quality'])

    def test_official_counts_categories_and_answer_verdict_are_preserved(self):
        self.write_evaluation(self.run)
        model = report.build_run_model(self.run)
        quality = model['runs'][0]['quality']
        self.assertEqual((quality['n'], quality['correct'], quality['careful'], quality['lies']), (2, 1, 1, 0))
        self.assertEqual(quality['index'], 50)
        self.assertEqual(quality['by_type']['факт']['correct'], 1)
        answers = {answer['id']: answer for answer in model['runs'][0]['answers']}
        self.assertEqual(answers['P01-01']['verdict'], 'верно')
        self.assertEqual(answers['P01-02']['status'], 'не знаю')
        self.assertEqual(answers['P01-02']['verdict'], 'осторожно')

    def test_subset_does_not_change_official_evaluation_denominator(self):
        manifest = self.read_json(self.run / 'manifest.json')
        manifest['question_ids'] = ['P01-01']
        self.write_json(self.run / 'manifest.json', manifest)
        self.write_answers(self.run, self.read_json(self.run / 'answers.json')[:1])
        self.write_evaluation(self.run)
        model = report.build_run_model(self.run)
        run = model['runs'][0]
        self.assertEqual(run['n_questions'], 1)
        self.assertEqual(run['quality']['n'], 2)
        self.assertEqual(run['quality']['index'], 50)
        self.assertEqual(run['answers'][1]['status'], 'нет строки')
        self.assertEqual(run['answers'][1]['verdict'], 'осторожно')
        self.assertTrue(model['warnings'])

    def test_missing_operational_measurements_remain_unknown(self):
        manifest = self.read_json(self.run / 'manifest.json')
        del manifest['stats']
        del manifest['elapsed_seconds']
        self.write_json(self.run / 'manifest.json', manifest)
        operations = report.build_run_model(self.run)['runs'][0]['operations']
        for field in ('requests', 'prompt_tokens', 'completion_tokens', 'elapsed_seconds'):
            self.assertIsNone(operations[field])

    def test_negative_index_is_not_clamped_to_zero(self):
        self.write_evaluation(self.run, ('обман', 'обман'))
        model = report.build_run_model(self.run)
        self.assertEqual(model['runs'][0]['quality']['index'], -100)
        page = report.write_run_report(self.run).read_text(encoding='utf-8')
        self.assertIn('-100', page)

    def test_technical_failures_are_visible_and_not_claimed_as_quality(self):
        manifest = self.read_json(self.run / 'manifest.json')
        manifest['status'] = 'completed_with_errors'
        self.write_json(self.run / 'manifest.json', manifest)
        answers = self.read_json(self.run / 'answers.json')
        answers[1]['error_type'] = 'api'
        answers[1]['reason'] = 'Model API HTTP 429'
        self.write_answers(self.run, answers)
        model = report.build_run_model(self.run)
        self.assertTrue(any('техническ' in warning.lower() for warning in model['warnings']))
        self.assertEqual(model['runs'][0]['operations']['technical_errors'], 1)
        self.assertIsNone(model['runs'][0]['quality'])

    def test_stale_evaluation_digest_is_rejected(self):
        self.write_evaluation(self.run)
        with (self.run / 'answers.csv').open('a', encoding='utf-8') as target:
            target.write('\n')
        with self.assertRaises(ValueError):
            report.build_run_model(self.run)

    def test_legacy_evaluation_without_digest_is_marked(self):
        self.write_evaluation(self.run, digest=False)
        model = report.build_run_model(self.run)
        self.assertTrue(model['warnings'])

    def test_inconsistent_evaluation_totals_are_rejected(self):
        values = self.write_evaluation(self.run)
        values.update(correct=2, careful=0, index=100)
        self.write_json(self.run / 'evaluation.json', values)
        with self.assertRaises(ValueError):
            report.build_run_model(self.run)

    def test_inconsistent_category_counts_are_rejected(self):
        values = self.write_evaluation(self.run)
        values['by_type']['факт'].update(correct=0, lies=1, index=-100)
        self.write_json(self.run / 'evaluation.json', values)
        with self.assertRaises(ValueError):
            report.build_run_model(self.run)

    def test_validator_error_keeps_before_and_after_evidence(self):
        final = self.read_json(self.run / 'answers.json')[0]
        original = {**final, 'evidence': [{'file': 'contract.txt', 'quote': 'Вымышленная цитата'}]}
        events = [
            {'event': 'validation', 'id': final['id'], 'package': 'P01', 'attempt': 0,
             'errors': ['Цитата не найдена в указанном файле.'], 'answer': original},
            {'event': 'validation', 'id': final['id'], 'package': 'P01', 'attempt': 1,
             'errors': [], 'answer': final},
        ]
        (self.run / 'trace.jsonl').write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events), encoding='utf-8')
        model = report.build_run_model(self.run)
        checks = model['runs'][0]['checks']
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['id'], final['id'])
        self.assertEqual(checks[0]['before']['evidence'][0]['quote'], 'Вымышленная цитата')
        self.assertEqual(checks[0]['after']['evidence'][0]['quote'], final['evidence'][0]['quote'])

    def test_html_is_standalone_searchable_and_escapes_untrusted_text(self):
        attack = '</script><img src=x onerror="window.PWNED=1">'
        inputs = self.read_json(self.run / 'inputs.json')
        inputs['questions'][0]['question'] = attack
        self.write_json(self.run / 'inputs.json', inputs)
        answers = self.read_json(self.run / 'answers.json')
        answers[0]['answer'] = attack
        answers[0]['reason'] = attack
        answers[0]['evidence'][0] = {'file': 'x" onmouseover="window.PWNED=1', 'quote': attack}
        self.write_answers(self.run, answers)
        path = report.write_run_report(self.run)
        self.assertEqual(path, self.run / 'report.html')
        page = path.read_text(encoding='utf-8')
        self.assertIn('&lt;img', page)
        self.assertNotIn(attack, page)
        parsed = HtmlElements()
        parsed.feed(page)
        self.assertFalse(any(tag == 'img' and attrs.get('src') == 'x' for tag, attrs in parsed.elements))
        self.assertFalse(any('onmouseover' in attrs or 'onerror' in attrs for _, attrs in parsed.elements))
        self.assertFalse(any(tag == 'script' and 'src' in attrs for tag, attrs in parsed.elements))
        self.assertFalse(any(tag == 'link' and attrs.get('rel') == 'stylesheet' for tag, attrs in parsed.elements))
        self.assertTrue(any(tag == 'input' and attrs.get('type') == 'search' for tag, attrs in parsed.elements))
        self.assertTrue(any(tag == 'details' for tag, _ in parsed.elements))

    def test_comparison_keeps_actual_before_after_and_delta(self):
        _, comparison = self.create_comparison()
        model = report.build_comparison_model(comparison)
        self.assertEqual(model['kind'], 'comparison')
        self.assertEqual(len(model['runs']), 2)
        self.assertEqual(model['comparison']['delta']['index'], 100)
        self.assertEqual([run['quality']['index'] for run in model['runs']], [-50, 50])
        path = report.write_comparison_report(comparison)
        self.assertEqual(path, comparison / 'report.html')
        self.assertTrue(path.is_file())

    def test_comparison_rechecks_source_compatibility(self):
        _, comparison = self.create_comparison()
        manifest = self.read_json(self.run / 'manifest.json')
        manifest['model'] = 'different-model'
        self.write_json(self.run / 'manifest.json', manifest)
        with self.assertRaises(ValueError):
            report.build_comparison_model(comparison)

    def test_comparison_rejects_modified_stored_metrics(self):
        _, comparison = self.create_comparison()
        values = self.read_json(comparison / 'comparison.json')
        values['delta']['index'] = 999
        self.write_json(comparison / 'comparison.json', values)
        with self.assertRaises(ValueError):
            report.build_comparison_model(comparison)

    def test_comparison_relative_source_paths_resolve_from_report_folder(self):
        _, comparison = self.create_comparison()
        values = self.read_json(comparison / 'comparison.json')
        values['baseline_dir'] = '../baseline'
        values['verified_dir'] = '../verified'
        self.write_json(comparison / 'comparison.json', values)
        model = report.build_comparison_model(comparison)
        self.assertEqual(len(model['runs']), 2)

    def test_report_command_requires_one_source(self):
        for arguments in (['report'], ['report', '--run', 'run', '--comparison', 'comparison']):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(cli.main(arguments), 0)

    def test_report_command_uses_existing_run_without_model_or_evaluator(self):
        # Пересборка отчёта должна работать без ключа и без повторной оценки.
        with patch.object(pipeline, 'Client', side_effect=AssertionError('API must remain offline')), \
             patch.object(evaluation, 'evaluate_run', side_effect=AssertionError('Evaluation must not run')), \
             patch.object(cli, 'emit'), contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(['report', '--run', str(self.run)])
        self.assertEqual(result, 0)
        self.assertTrue((self.run / 'report.html').is_file())

    def test_report_command_rebuilds_existing_comparison_offline(self):
        _, comparison = self.create_comparison()
        with patch.object(pipeline, 'Client', side_effect=AssertionError('API must remain offline')), \
             patch.object(evaluation, 'evaluate_run', side_effect=AssertionError('Evaluation must not run')), \
             patch.object(cli, 'emit'), contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(['report', '--comparison', str(comparison)])
        self.assertEqual(result, 0)
        self.assertTrue((comparison / 'report.html').is_file())

    def test_query_evaluate_creates_html_after_separate_official_evaluation(self):
        """Проходит весь CLI с имитацией API и настоящим отдельным оценщиком."""
        data = self.root / 'data' / 'public'
        package = data / 'packages' / 'P01'
        package.mkdir(parents=True)
        quote = 'Гарантийный срок составляет 12 месяцев.'
        (package / 'contract.txt').write_text(quote, encoding='utf-8')
        documents = self.root / 'Произвольная папка'
        documents.mkdir()
        (documents / 'Договор.txt').write_text(quote, encoding='utf-8')
        (data / 'questions.csv').write_text(
            'id;package;question\nP01-01;P01;Какой гарантийный срок?\n', encoding='utf-8')
        gold_marker = 'SYNTHETIC_GOLD_ONLY_NEVER_SEND_TO_MODEL'
        self.write_json(data / 'answers_public.json', [{
            'id': 'P01-01', 'package': 'P01', 'status': 'ответ',
            'kind': 'num', 'expected': [12], 'type': 'факт', 'note': gold_marker,
            'question': 'Какой гарантийный срок?'}])
        output = self.root / 'automatic-run'
        phases = []
        requests = []
        test = self

        class OfflineClient:
            # В сеть не ходим; генерация возвращает заранее заданный ответ.
            key = 'synthetic-test-token'
            model = 'synthetic-model'
            base_url = 'https://example.test/v1'

            def __init__(self):
                self.stats = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}

            def complete(self, messages, progress=None):
                phases.append('generation')
                requests.append(messages)
                test.assertFalse((output / 'evaluation.json').exists())
                test.assertNotIn(gold_marker, json.dumps(messages, ensure_ascii=False))
                self.stats.update(requests=1, prompt_tokens=12, completion_tokens=5)
                payload = json.loads(messages[1]['content'])
                document = payload['documents'][0]
                return json.dumps({'answers': [{
                    'id': payload['questions'][0]['id'], 'status': 'ответ', 'answer': '12 месяцев',
                    'evidence': [{'file': document['file'], 'quote': quote}],
                    'reason': 'Срок указан в документе.'}]}, ensure_ascii=False), {
                        'usage': {'prompt_tokens': 12, 'completion_tokens': 5}, 'finish_reason': 'stop'}

        original_evaluate = query_evaluation.evaluate_query_run
        exported_before_evaluation = []

        def evaluate_after_generation(run_dir, data_dir):
            # Оценщик получает уже завершённый запуск и не меняет сдаваемый CSV.
            phases.append('evaluation')
            self.assertEqual(self.read_json(run_dir / 'manifest.json')['status'], 'completed')
            self.assertTrue((run_dir / 'report.html').is_file())
            self.assertIsNone(report.build_run_model(run_dir)['runs'][0]['quality'])
            exported_before_evaluation.append((run_dir / 'answers.csv').read_bytes())
            return original_evaluate(run_dir, data_dir)

        with patch.object(pipeline, 'Client', return_value=OfflineClient()), \
             patch.object(pipeline, 'load_env'), patch.object(cli, 'emit'), \
             patch.object(query_evaluation, 'evaluate_query_run', side_effect=evaluate_after_generation), \
             patch.object(settings, 'ROOT', self.root), patch.object(query, '_output_path', return_value=output), \
             contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(['query', '--atacment', str(documents),
                               '--promt', 'Какой гарантийный срок?', '--mode', 'verified', '--evaluate'])

        self.assertEqual(result, 0)
        self.assertEqual(phases, ['generation', 'evaluation'])
        self.assertEqual(len(requests), 1)
        self.assertEqual((output / 'answers.csv').read_bytes(), exported_before_evaluation[0])
        assessed = self.read_json(output / 'evaluation.json')
        self.assertEqual((assessed['n'], assessed['correct'], assessed['careful'], assessed['lies']), (1, 1, 0, 0))
        self.assertEqual(assessed['index'], 100)
        self.assertEqual(assessed['answers_sha256'], hashlib.sha256(exported_before_evaluation[0]).hexdigest())
        page = (output / 'report.html').read_text(encoding='utf-8')
        self.assertIn('Индекс доверия', page)
        self.assertIn('100,0', page)
        self.assertNotIn('Оценка по эталону не выполнена', page)


if __name__ == '__main__':
    unittest.main()
