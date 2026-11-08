"""Проверки CLI на временных файлах и заранее заданных ответах модели.

Проверяем CSV, безопасное чтение пакетов, расчёты и исправление только ошибочных
ответов. Исходный режим должен сохранять ответ модели без дополнительных проверок.
"""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from src import cli
from src.generation import answer_engine
from src.generation import answers as answer_format
from src.evaluation import reference_data
from src.files import run_storage
from src import settings
from src.generation.client import ApiError, ResponseTruncated


class FakeClient:
    """Имитировать ответы и сбои API, не выполняя настоящие сетевые запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, progress=None):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError('Unexpected extra model request')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {'usage': {'prompt_tokens': 12, 'completion_tokens': 5}, 'finish_reason': 'stop'}


class CliTests(unittest.TestCase):
    """Проверять чтение, обработку и экспорт как независимые этапы CLI."""

    def setUp(self):
        # Файлы внутри проекта доступны и на Windows с ограничениями песочницы.
        self.temp = tempfile.TemporaryDirectory(prefix='cli-test-', dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.quote = 'Срок выполнения работ составляет 60 календарных дней.'
        self.documents = {'notice.txt': self.quote}
        self.questions = [
            {'id': 'P01-01', 'package': 'P01', 'question': 'Какой срок выполнения работ?'},
            {'id': 'P01-02', 'package': 'P01', 'question': 'За сколько дней выполняются работы?'},
        ]
        self.events = []
        self.emit_patch = patch.object(answer_engine, 'emit')
        self.emit_patch.start()
        self.addCleanup(self.emit_patch.stop)

    def answer(self, question_id='P01-01', value='60 календарных дней'):
        return {'id': question_id, 'status': 'ответ', 'answer': value,
                'evidence': [{'file': 'notice.txt', 'quote': self.quote}]}

    def response(self, *answers):
        return json.dumps({'answers': list(answers)}, ensure_ascii=False)

    def process(self, client, *, mode='verified', questions=None, attempts=2, documents=None, read_errors=None):
        return answer_engine.process_questions(client, mode, 'P01', questions or self.questions,
                                   self.documents if documents is None else documents,
                                   read_errors or [], attempts, self.events.append)

    def write_questions(self, text, encoding='utf-8'):
        (self.root / 'questions.csv').write_text(text, encoding=encoding)

    def test_parser_commands_and_defaults(self):
        _, arguments, _ = cli.app.parse_args(
            ['query', '--attachment', 'contract.txt', '--prompt', 'Срок?'],
            exit_on_error=False, print_error=False)
        arguments.apply_defaults()
        self.assertEqual(arguments.arguments['mode'], 'verified')
        defaults = settings.QueryOptions(attachment=['contract.txt'], prompt=['Срок?'])
        self.assertEqual((defaults.repair_attempts, defaults.max_tokens, defaults.timeout), (2, 32768, 900))
    def test_parser_requires_query_arguments_and_valid_mode(self):
        for argv in (['query'], ['query', '--attachment', 'contract.txt'],
                     ['query', '--attachment', 'contract.txt', '--prompt', 'Срок?', '--mode', 'unknown']):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(argv), 2)

    def test_questions_semicolon_bom_and_quoted_delimiter(self):
        self.write_questions('id;package;question\nP01-01;P01;"Какая сумма; в рублях?"\n', encoding='utf-8-sig')
        self.assertEqual(reference_data.read_questions(self.root), [
            {'id': 'P01-01', 'package': 'P01', 'question': 'Какая сумма; в рублях?'}])

    def test_questions_comma_delimiter_cp1251(self):
        self.write_questions('id,package,question\nP01-01,P01,"Сумма, рубли?"\n', encoding='cp1251')
        self.assertEqual(reference_data.read_questions(self.root)[0]['question'], 'Сумма, рубли?')

    def test_questions_multiline_csv_preserves_word_boundary(self):
        self.write_questions('id;package;question\nP01-01;P01;"Какой срок\nв календарных днях?"\n')
        self.assertEqual(reference_data.read_questions(self.root)[0]['question'].replace('\r\n', '\n'), 'Какой срок\nв календарных днях?')

    def test_duplicate_question_ids_rejected(self):
        self.write_questions('id;package;question\nP01-01;P01;Первый?\nP01-01;P02;Другой?\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate question ID'):
            reference_data.read_questions(self.root)

    def test_unsafe_package_names_rejected_by_both_readers(self):
        for package in ('../P02', '..', 'P01/../../secret', 'P01\\..\\P02', 'C:\\data', '/etc', ''):
            with self.subTest(package=package):
                self.write_questions(f'id;package;question\nP01-01;{package};Вопрос?\n')
                with self.assertRaises(ValueError):
                    reference_data.read_questions(self.root)
                with self.assertRaises(ValueError):
                    reference_data.read_package(self.root, package)

    def test_missing_columns_and_no_questions_rejected(self):
        for content in ('', 'id;question\n1;Что?\n', 'id;package;question\n', 'id;package;question\n1;P01;\n'):
            with self.subTest(content=content):
                self.write_questions(content)
                with self.assertRaises(ValueError):
                    reference_data.read_questions(self.root)

    def test_read_package_reports_bad_files_and_keeps_good_ones(self):
        package = self.root / 'packages' / 'P01'
        package.mkdir(parents=True)
        (package / 'good.txt').write_text(self.quote, encoding='utf-8')
        (package / 'empty.txt').write_text('', encoding='utf-8')
        (package / 'binary.txt').write_bytes(b'\x00\x01\x02')
        documents, errors = reference_data.read_package(self.root, 'P01')
        self.assertEqual(documents, {'good.txt': self.quote})
        self.assertEqual(len(errors), 2)
        self.assertTrue(any('empty.txt' in error for error in errors))
        self.assertTrue(any('binary.txt' in error for error in errors))
        self.assertEqual(reference_data.read_package(self.root, 'MISSING'), ({}, ['Package directory is missing']))

    def test_parse_answers_accepts_json_fence_and_reasoning_wrapper(self):
        response = '<think>Unneeded internal text</think>\n```json\n' + self.response(self.answer()) + '\n```'
        self.assertEqual(answer_format.parse_answers(response), {'P01-01': self.answer()})
        self.assertEqual(answer_format.parse_answers(json.dumps([self.answer()])), {'P01-01': self.answer()})

    def test_parse_answers_rejects_duplicate_ids_and_bad_shape(self):
        for response in (self.response(self.answer(), self.answer()), '{"answers":{}}', '{"answers":[{}]}', 'not json'):
            with self.subTest(response=response), self.assertRaises(ValueError):
                answer_format.parse_answers(response)

    def test_csv_export_preserves_quotes_semicolons_and_input_order(self):
        self.quote = 'Срок: 60 дней; значение "уточнено".\nВторая строка.'
        answers = {'P01-02': self.answer('P01-02'), 'P01-01': self.answer('P01-01')}
        run_storage.save_answers(self.root, self.questions, answers)
        with (self.root / 'answers.csv').open(encoding='utf-8', newline='') as stream:
            reader = csv.DictReader(stream, delimiter=';')
            self.assertEqual(reader.fieldnames, answer_format.FIELDS)
            rows = list(reader)
        self.assertEqual([row['id'] for row in rows], ['P01-01', 'P01-02'])
        self.assertEqual(rows[0]['quote'], 'Срок: 60 дней; значение "уточнено". Вторая строка.')
        self.assertEqual(json.loads((self.root / 'answers.json').read_text(encoding='utf-8'))[0]['evidence'][0]['quote'], self.quote)
        self.assertFalse((self.root / 'answers.csv.tmp').exists())

    def test_csv_cautious_status_clears_unsupported_content(self):
        for status in ('не знаю', 'нет в документах'):
            answer = self.answer()
            answer['status'] = status
            row = answer_format.csv_row(answer)
            self.assertEqual((row['answer'], row['quote'], row['file']), ('', '', ''))

    def test_percent_is_recomputed_by_code_without_mutating_input(self):
        answer = self.answer(value='Неверная арифметика модели')
        answer['calculation'] = {'operation': 'percent', 'base': '1800000', 'rate': '5', 'base_evidence': 0, 'rate_evidence': 1}
        result = answer_format.calculate(answer)
        self.assertEqual(result['answer'], '90000.00 руб.')
        self.assertEqual(answer['answer'], 'Неверная арифметика модели')
        answer['calculation'].update(base='10.10', rate='5')
        self.assertEqual(answer_format.calculate(answer)['answer'], '0.51 руб.')

    def test_unsupported_calculation_not_executed(self):
        for base in ('NaN', 'Infinity', '-10', '1e100', '__import__("os")'):
            answer = self.answer()
            answer['calculation'] = {'operation': 'percent', 'base': base, 'rate': '5'}
            self.assertIs(answer_format.calculate(answer), answer)
        answer['calculation']['operation'] = 'eval'
        self.assertIs(answer_format.calculate(answer), answer)

    def test_verified_retries_only_invalid_answer_and_attaches_reason(self):
        client = FakeClient(self.response(self.answer(), self.answer('P01-02', '75 дней')),
                            self.response(self.answer('P01-02')))
        answers = self.process(client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(set(answers), {'P01-01', 'P01-02'})
        self.assertTrue(all(answer['status'] == 'ответ' for answer in answers.values()))
        second_request = json.loads(client.calls[1][1]['content'])
        self.assertEqual([question['id'] for question in second_request['questions']], ['P01-02'])
        errors = second_request['previous_answers_and_validation_errors'][0]['errors']
        self.assertTrue(any('75' in error for error in errors))
        self.assertTrue(any(event.get('event') == 'validation' and event['errors'] for event in self.events))

    def test_verified_correction_gets_exact_source_for_near_match(self):
        filename = 'notice.txt'
        quote = 'Участник должен иметь лицензию на деятельность по технической защите конфиденциальной информации.'
        answer = {'id': 'P01-01', 'status': 'ответ', 'answer': 'Лицензия на техническую защиту информации'}
        wrong = {**answer, 'evidence': [
            {'file': filename, 'quote': 'на деятельность по технической защиты конфиденциальной информации.'}]}
        corrected = {**answer, 'evidence': [{'file': filename, 'quote': quote}]}
        client = FakeClient(self.response(wrong), self.response(corrected))

        answers = self.process(client, questions=self.questions[:1], documents={filename: quote}, attempts=1)

        self.assertEqual(len(client.calls), 2)
        correction = json.loads(client.calls[1][1]['content'])['previous_answers_and_validation_errors'][0]
        self.assertIn('цитата не найдена', correction['errors'][0])
        self.assertIn(quote, correction['errors'][0])
        self.assertEqual(answers['P01-01'], {**corrected, 'answer': quote})

    def test_verified_restores_case_ending_from_file_without_retry(self):
        filename = 'notice.txt'
        source = '2.2. Участник должен иметь лицензию на деятельность по технической защите конфиденциальной информации.'
        quote = source.removeprefix('2.2. ').replace('защите', 'защиты')
        raw = {'id': 'P01-01', 'status': 'ответ', 'answer': 'Лицензия на техническую защиту информации',
               'evidence': [{'file': filename, 'quote': quote}]}
        client = FakeClient(self.response(raw))

        answers = self.process(client, questions=self.questions[:1], documents={filename: source})

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(answers['P01-01']['answer'], source)
        self.assertEqual(answers['P01-01']['evidence'], [{'file': filename, 'quote': source}])
        self.assertTrue(any(event['event'] == 'quote_restored' for event in self.events if 'event' in event))

    def test_missing_id_is_retried_then_fails_closed(self):
        client = FakeClient(self.response(self.answer()), self.response())
        answers = self.process(client, attempts=1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(answers['P01-01']['status'], 'ответ')
        self.assertEqual(answers['P01-02']['status'], 'не знаю')
        self.assertIn('Missing answer ID', answers['P01-02']['reason'])
        self.assertEqual(answers['P01-02']['error_type'], 'model_format')

    def test_unrequested_id_rejects_envelope(self):
        client = FakeClient(self.response(self.answer(), self.answer('P99-01')))
        answers = self.process(client, attempts=0)
        self.assertTrue(all(answer['status'] == 'не знаю' for answer in answers.values()))
        self.assertTrue(any(event.get('parse_error') for event in self.events))

    def test_api_error_preserves_already_accepted_answers_and_refuses_pending(self):
        client = FakeClient(self.response(self.answer()), ApiError('Model API HTTP 429'))
        answers = self.process(client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(answers['P01-01']['status'], 'ответ')
        self.assertNotIn('error_type', answers['P01-01'])
        self.assertEqual(answers['P01-02']['status'], 'не знаю')
        self.assertEqual(answers['P01-02']['reason'], 'Model API HTTP 429')
        self.assertEqual(answers['P01-02']['error_type'], 'api')

    def test_truncated_api_reply_is_marked_as_technical_failure(self):
        # Тип технического сбоя не должен зависеть от формулировки сообщения.
        client = FakeClient(ApiError('Model response truncated: increase --max-tokens'))
        answers = self.process(client)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(answer['status'] == 'не знаю' for answer in answers.values()))
        self.assertTrue(all(answer['error_type'] == 'api' for answer in answers.values()))
        self.assertTrue(any(event.get('event') == 'api_error' for event in self.events))

    def test_requests_are_capped_for_arbitrary_question_counts_in_both_modes(self):
        limit = 5
        for mode in ('baseline', 'verified'):
            for count in (1, 5, 6, 10, 13, 21):
                with self.subTest(mode=mode, count=count):
                    self.events.clear()
                    questions = [{'id': 'custom-%s' % i, 'question': 'Какой срок для этапа %s?' % i}
                                 for i in range(count)]
                    groups = [questions[i:i + limit] for i in range(0, count, limit)]
                    if mode == 'baseline':
                        client = FakeClient(*(json.dumps([
                            {'id': q['id'], 'status': 'ответ', 'answer': '30 дней', 'quote': '', 'file': ''}
                            for q in group]) for group in groups))
                    else:
                        client = FakeClient(*(self.response(*(self.answer(q['id']) for q in group))
                                              for group in groups))
                    answers = self.process(client, mode=mode, questions=questions)
                    payloads = [json.loads(call[1]['content']) for call in client.calls]
                    self.assertEqual([len(p['questions']) for p in payloads], [len(g) for g in groups])
                    self.assertEqual([q['id'] for p in payloads for q in p['questions']],
                                     [q['id'] for q in questions])
                    if mode == 'baseline':
                        self.assertTrue(all(p.keys() == {'documents', 'questions'} for p in payloads))
                    self.assertEqual(list(answers), [q['id'] for q in questions])
                    self.assertTrue(all(a['status'] == 'ответ' for a in answers.values()))
                    for payload in payloads:
                        self.assertEqual(payload['documents'], [{'file': name, 'text': text}
                                                               for name, text in self.documents.items()])
                    plans = [e for e in self.events if e.get('event') == 'batch_plan']
                    self.assertEqual(len(plans), int(count > limit))
                    if plans:
                        self.assertEqual(plans[0]['sizes'], [len(g) for g in groups])

    def test_truncation_splits_verified_questions_and_keeps_documents(self):
        client = FakeClient(ResponseTruncated('limit'), self.response(self.answer()),
                            self.response(self.answer('P01-02')))
        answers = self.process(client)
        self.assertEqual(set(answers), {'P01-01', 'P01-02'})
        self.assertTrue(all(a['status'] == 'ответ' for a in answers.values()))
        payloads = [json.loads(call[1]['content']) for call in client.calls]
        self.assertEqual([[q['id'] for q in p['questions']] for p in payloads],
                         [['P01-01', 'P01-02'], ['P01-01'], ['P01-02']])
        for payload in payloads[1:]:
            self.assertEqual(payload['documents'], payloads[0]['documents'])
        self.assertEqual(len([e for e in self.events if e.get('event') == 'batch_split']), 1)
        self.assertEqual({e['batch'] for e in self.events}, {'root', 'root.1', 'root.2'})

    def test_split_during_repair_preserves_answers_and_remaining_budget(self):
        # Деление на последнем исправлении не разрешает дополнительные исправления.
        questions = self.questions + [{'id': 'P01-03', 'package': 'P01', 'question': 'Срок?'}]
        client = FakeClient(self.response(self.answer()), ResponseTruncated('limit'),
                            self.response(self.answer('P01-02', '75 дней')),
                            self.response(self.answer('P01-03')))
        answers = self.process(client, questions=questions, attempts=1)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(answers['P01-01']['status'], 'ответ')
        self.assertEqual(answers['P01-02']['status'], 'не знаю')
        self.assertEqual(answers['P01-03']['status'], 'ответ')
        for call, question_id in zip(client.calls[2:], ['P01-02', 'P01-03']):
            payload = json.loads(call[1]['content'])
            self.assertEqual([q['id'] for q in payload['questions']], [question_id])
            self.assertEqual([q['id'] for q in payload['previous_answers_and_validation_errors']], [question_id])
        self.assertTrue(all(e['attempt'] <= 1 for e in self.events if 'attempt' in e))

    def test_recursive_split_stops_at_singleton_and_preserves_other_results(self):
        questions = self.questions + [{'id': 'P01-03', 'package': 'P01', 'question': 'Срок?'}]
        client = FakeClient(ResponseTruncated('limit'), self.response(self.answer()),
                            ResponseTruncated('limit'), ResponseTruncated('single limit'),
                            self.response(self.answer('P01-03')))
        answers = self.process(client, questions=questions)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(set(answers), {q['id'] for q in questions})
        self.assertEqual(answers['P01-01']['status'], 'ответ')
        self.assertEqual(answers['P01-02']['error_type'], 'api')
        self.assertEqual(answers['P01-02']['reason'], 'single limit')
        self.assertEqual(answers['P01-03']['status'], 'ответ')
        self.assertEqual(len([e for e in self.events if e.get('event') == 'batch_split']), 2)

    def test_evidence_failure_is_cautious_result_not_technical_failure(self):
        client = FakeClient(self.response(self.answer(value='75 дней')))
        answers = self.process(client, questions=self.questions[:1], attempts=0)
        self.assertEqual(answers['P01-01']['status'], 'не знаю')
        self.assertNotIn('error_type', answers['P01-01'])

    def test_verified_copies_numeric_context_from_document_quotes(self):
        cases = (
            ('Каков размер обеспечения исполнения контракта?', '5',
             '2.4. Обеспечение исполнения контракта устанавливается в размере 5 % от цены контракта.'),
            ('За сколько максимум календарных дней может быть получена выписка из ЕГРЮЛ?', '30',
             '1.4. Выписка из ЕГРЮЛ (дата получения — не более 30 календарных дней до дня подачи заявки).'),
            ('Насколько свежей должна быть выписка из ЕГРЮЛ?', '30',
             'Выписка из ЕГРЮЛ, полученная не ранее чем за 30 дней до даты подачи заявки.'),
        )
        for question, value, quote in cases:
            with self.subTest(question=question):
                raw = self.answer(value=value)
                raw['evidence'][0]['quote'] = quote
                client = FakeClient(self.response(raw))
                result = self.process(client, questions=[{**self.questions[0], 'question': question}],
                                      documents={'notice.txt': quote})['P01-01']
                self.assertEqual(result['answer'], quote)
                self.assertEqual(result['evidence'], raw['evidence'])
                self.assertEqual(len(client.calls), 1)

    def test_verified_preserves_source_punctuation_and_whitespace(self):
        source = 'Отчёт — «Принято».\nОтправка —  через портал.'
        raw = self.answer(value='Отчёт отправили через портал')
        raw['evidence'][0]['quote'] = 'отчет - "принято". отправка - через портал'
        result = self.process(FakeClient(self.response(raw)), questions=self.questions[:1],
                              documents={'notice.txt': source})['P01-01']
        self.assertEqual(result['answer'], source)
        self.assertEqual(result['evidence'][0]['quote'], source)
        self.assertEqual(answer_format.csv_row(result)['answer'], source)

    def test_verified_preserves_conflict_summary_and_explicit_binary_answer(self):
        conflict = {**self.answer(value='60 дней по извещению, 75 дней по проекту.'), 'status': 'расхождение'}
        other_quote = 'Договорный срок составляет 75 календарных дней.'
        conflict['evidence'].append({'file': 'contract.txt', 'quote': other_quote})
        binary = self.answer('P01-02', value='Да.')
        questions = [self.questions[0], {**self.questions[1], 'question': 'Указан ли срок? Ответьте да или нет.'}]
        results = self.process(FakeClient(self.response(conflict, binary)), questions=questions,
                               documents={**self.documents, 'contract.txt': other_quote})
        self.assertEqual(results['P01-01'], conflict)
        self.assertEqual(results['P01-02'], binary)

    def test_invalid_json_then_valid_retry(self):
        client = FakeClient('not JSON', self.response(self.answer(), self.answer('P01-02')))
        answers = self.process(client)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(answer['status'] == 'ответ' for answer in answers.values()))

    def test_no_readable_documents_never_calls_model(self):
        client = FakeClient()
        answers = self.process(client, documents={}, read_errors=['No readable TXT documents'])
        self.assertEqual(client.calls, [])
        self.assertEqual(len(answers), 2)
        self.assertTrue(all(answer['status'] == 'не знаю' for answer in answers.values()))
        self.assertTrue(all(answer['error_type'] == 'input_read' for answer in answers.values()))

    def test_baseline_does_not_validate_repair_or_recompute(self):
        wrong = {'id': 'P01-01', 'status': 'ответ', 'answer': '999999 руб.',
                 'file': 'fake.txt', 'quote': 'Вымышленная цитата'}
        malformed = {**wrong, 'id': 'P01-02', 'answer': 999999}
        client = FakeClient(json.dumps([malformed, wrong]))
        with patch.object(answer_engine, 'validate_answer', side_effect=AssertionError('Baseline must not validate')), \
             patch.object(answer_engine, 'calculate', side_effect=AssertionError('Baseline must not calculate')):
            answers = self.process(client, mode='baseline', attempts=3)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(answers['P01-01']['answer'], '999999 руб.')
        self.assertEqual(answers['P01-01']['evidence'][0]['file'], 'fake.txt')
        self.assertEqual(answers['P01-02']['status'], 'не знаю')
        self.assertEqual(answers['P01-02']['error_type'], 'model_format')


if __name__ == '__main__':
    unittest.main()
