"""Real planning, chunk coverage, recursive reduction and workflow; offline model only."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.generation.answer_engine import process_questions
from src.generation.client import ApiError, ResponseTruncated, estimate_request_tokens
from src.generation.context_loop import DocumentLoop, validate_context
from src.generation.prompts import BASELINE, messages
from src.dashboard.agent import stages_from_trace
from src.settings import QueryOptions
from src.workflows.query import run_query


QUESTION = {'id': 'Q1', 'package': 'INPUT', 'question': 'Какой срок гарантии?'}
QUOTE = 'Гарантийный срок составляет 12 месяцев.'


def summary(answer='', evidence=None, uncertain=False):
    return {'answer': answer, 'evidence': evidence or [], 'uncertain': uncertain}


class Model:
    key, model, base_url = 'fake-key', 'offline', 'https://example.test/v1'
    max_tokens, context_token_limit = 512, 12000

    def __init__(self, handler=None):
        self.calls = []
        self.stats = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
        self.handler = handler or self.respond

    def complete(self, request, progress=None):
        size = estimate_request_tokens(request, self.max_tokens)
        if size > self.context_token_limit:
            raise AssertionError(f'Oversized request: {size}')
        payload = json.loads(request[1]['content'])
        self.calls.append(payload)
        self.stats['requests'] += 1
        self.stats['prompt_tokens'] += size - self.max_tokens
        self.stats['completion_tokens'] += 30
        result = self.handler(payload)
        if isinstance(result, Exception):
            raise result
        if request[0]['content'] == BASELINE:
            result = [{key: row[key] for key in ('id', 'status', 'answer')} |
                      {'file': row['evidence'][0]['file'] if row['evidence'] else '',
                       'quote': row['evidence'][0]['quote'] if row['evidence'] else ''}
                      for row in result['answers']]
        return (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)), {'usage': {}}

    def respond(self, payload):
        if payload.get('stage') == 'map':
            evidence = [{'file': doc['file'], 'quote': QUOTE}
                        for doc in payload['documents'] if QUOTE in doc['text']]
            return summary(QUOTE if evidence else '', evidence)
        if payload.get('stage') == 'reduce':
            evidence = [ev for row in payload['partial_answers'] for ev in row['evidence']]
            return summary(QUOTE if evidence else '', evidence[:1],
                           any(row['uncertain'] for row in payload['partial_answers']))
        evidence = [ev for row in payload.get('partial_answers', []) for ev in row['evidence']]
        if not evidence:
            evidence = [{'file': doc['file'], 'quote': QUOTE}
                        for doc in payload['documents'] if QUOTE in doc['text']]
        return {'answers': [{'id': q['id'], 'status': 'ответ' if evidence else 'нет в документах',
                             'answer': QUOTE if evidence else '', 'evidence': evidence[:1]}
                            for q in payload['questions']]}


class ContextLoopTests(unittest.TestCase):
    def setUp(self):
        self.events, self.logs = [], []

    def run_engine(self, model, documents, *, questions=None, mode='verified', attempts=1, **kwargs):
        return process_questions(model, mode, 'INPUT', questions or [QUESTION], documents, [], attempts,
                                 self.events.append, self.logs.append, **kwargs)

    def plan(self, documents, **kwargs):
        return DocumentLoop('verified', documents, QUESTION, kwargs.pop('read_errors', []),
                            kwargs.pop('correction', None), max_tokens=512, context_token_limit=12000, **kwargs)

    def test_small_documents_keep_single_request_and_existing_prompt(self):
        model = Model()
        answer = self.run_engine(model, {'a.txt': QUOTE})['Q1']
        self.assertEqual(answer['answer'], QUOTE)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0], json.loads(messages('verified', {'a.txt': QUOTE}, [QUESTION], [])[1]['content']))

    def test_large_unicode_file_has_complete_coverage_and_answer_from_tail(self):
        text = ('Строка с "кавычками", слешем \\ и эмодзи 😀.\n' * 2000) + QUOTE
        model = Model()
        result = self.run_engine(model, {'Договор.txt': text})['Q1']
        self.assertEqual(result['answer'], QUOTE)
        mapped = [part for call in model.calls if call.get('stage') == 'map' for part in call['documents']]
        self.assertGreater(len(mapped), 2)
        end = 0
        for part in mapped:
            self.assertLessEqual(part['start'], end)
            self.assertGreater(part['end'], end)
            self.assertEqual(part['text'], text[part['start']:part['end']])
            self.assertEqual(part['file'], 'Договор.txt')
            end = part['end']
        self.assertEqual(end, len(text))
        self.assertEqual(model.calls[-1]['coverage'], {'complete': True, 'chunks_processed': len(mapped)})
        self.assertEqual(model.calls[-1]['documents'], [])
        self.assertTrue(any(event['event'] == 'context_plan' for event in self.events))

    def test_many_small_files_are_packed_with_original_names(self):
        documents = {f'{i}.txt': 'Текст документа.\n' * 30 for i in range(40)}
        chunks = list(self.plan(documents).chunks())
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(len(chunk) > 1 for chunk in chunks))
        for name, text in documents.items():
            parts = [part for chunk in chunks for part in chunk if part['file'] == name]
            self.assertEqual(parts[0]['start'], 0)
            self.assertEqual(parts[-1]['end'], len(text))
            self.assertTrue(all(part['text'] == text[part['start']:part['end']] for part in parts))

    def test_long_unbroken_text_and_empty_files_terminate(self):
        text = '😀' * 9000
        chunks = list(self.plan({'empty.txt': '', 'long.txt': text}).chunks())
        self.assertLess(len(chunks), 20)
        self.assertTrue(any(part['file'] == 'empty.txt' for chunk in chunks for part in chunk))
        parts = [part for chunk in chunks for part in chunk if part['file'] == 'long.txt']
        self.assertEqual(parts[-1]['end'], len(text))
        self.assertTrue(all(part['end'] > part['start'] for part in parts))

    def test_multi_level_reduction_is_bounded_and_preserves_uncertainty(self):
        chunks = [[{'file': f'{i}.txt', 'start': 0, 'end': 180,
                    'text': f'Условие {i}: ' + 'значение ' * 17}] for i in range(180)]

        def respond(payload):
            if payload.get('stage') == 'map':
                doc = payload['documents'][0]
                return summary('Условие', [{'file': doc['file'], 'quote': doc['text']}], True)
            if payload.get('stage') == 'reduce':
                evidence = [ev for row in payload['partial_answers'] for ev in row['evidence']]
                # A deliberately forgetful model cannot erase the uncertainty flag.
                return summary('Условие', evidence[:2], False)
            return {'answers': [{'id': 'Q1', 'status': 'не знаю', 'answer': '', 'evidence': []}]}

        plan, model = self.plan({'x': 'unused'}), Model(respond)
        with patch.object(plan, 'chunks', return_value=iter(chunks)):
            plan.complete(model, progress=None, trace=self.events.append, say=self.logs.append)
        final_event = next(event for event in self.events if event.get('stage') == 'final')
        self.assertGreaterEqual(final_event['levels'], 2)
        self.assertEqual(final_event['chunks'], 180)
        self.assertTrue(all(row['uncertain'] for row in model.calls[-1]['partial_answers']))
        self.assertLess(model.stats['requests'], 220)

    def test_malformed_summary_is_retried_and_fake_citation_is_rejected(self):
        model = Model()
        def respond(payload):
            if payload.get('stage') == 'map' and 'retry' not in payload:
                return summary('Выдуманный факт', [{'file': 'missing.txt', 'quote': 'Выдуманный факт'}])
            return model.respond(payload)
        model.handler = respond
        result = self.run_engine(model, {'a.txt': 'Текст. ' * 2500 + QUOTE})['Q1']
        self.assertEqual(result['answer'], QUOTE)
        self.assertTrue(any(call.get('retry') for call in model.calls))
        self.assertTrue(any(e['event'] == 'context_summary_error' for e in self.events))

    def test_oversized_or_invalid_summary_stops_after_bounded_retries(self):
        for output in ('not json', summary('x' * 20000, [{'file': 'a.txt', 'quote': QUOTE}]),
                       {'overflow': True}, summary('unsupported answer'), ResponseTruncated('output limit')):
            with self.subTest(output=str(output)[:70]):
                model = Model(lambda payload: output)
                result = self.run_engine(model, {'a.txt': QUOTE + 'Текст. ' * 3000}, attempts=1)['Q1']
                self.assertEqual(result['status'], 'не знаю')
                self.assertEqual(result['error_type'], 'api')
                self.assertEqual(len(model.calls), 2)
                self.assertTrue(all(call.get('stage') == 'map' for call in model.calls))

    def test_failed_chunk_never_produces_absence_or_final_answer(self):
        model = Model()
        def respond(payload):
            if len(model.calls) == 2:
                return ApiError('MAX_REQUESTS reached')
            return model.respond(payload)
        model.handler = respond
        result = self.run_engine(model, {'a.txt': 'Текст. ' * 7000})['Q1']
        self.assertEqual(result['status'], 'не знаю')
        self.assertEqual(result['error_type'], 'api')
        self.assertEqual(len(model.calls), 2)
        self.assertFalse(any('coverage' in call for call in model.calls))

    def test_questions_are_processed_independently_and_failure_does_not_erase_success(self):
        questions = [QUESTION, {**QUESTION, 'id': 'Q2'}]
        model = Model()
        def respond(payload):
            if payload.get('stage') == 'map' and payload['question']['id'] == 'Q1':
                return ApiError('first question failed')
            return model.respond(payload)
        model.handler = respond
        results = self.run_engine(model, {'a.txt': 'Текст. ' * 3000 + QUOTE}, questions=questions)
        self.assertEqual(results['Q1']['error_type'], 'api')
        self.assertEqual(results['Q2']['answer'], QUOTE)
        self.assertTrue(all(len(call.get('questions', [])) <= 1 for call in model.calls))

    def test_baseline_also_batches_large_sources(self):
        model = Model()
        result = self.run_engine(model, {'a.txt': 'Текст. ' * 3000 + QUOTE}, mode='baseline')['Q1']
        self.assertEqual(result['answer'], QUOTE)
        self.assertTrue(any(call.get('stage') == 'map' for call in model.calls))

    def test_repair_feedback_and_read_errors_survive_final_request(self):
        correction = [{'id': 'Q1', 'answer': {'answer': 'wrong'}, 'errors': ['Исправь срок']}]
        plan = self.plan({'a.txt': 'Текст. ' * 3000 + QUOTE}, correction=correction, read_errors=['bad.pdf unreadable'])
        model = Model()
        plan.complete(model, progress=None, trace=self.events.append, say=self.logs.append)
        final = model.calls[-1]
        self.assertEqual(final['previous_answers_and_validation_errors'], correction)
        self.assertEqual(final['read_errors'], ['bad.pdf unreadable'])

    def test_reduction_cannot_invent_citations_or_discard_all_evidence(self):
        plan = self.plan({'a.txt': QUOTE})
        items = [summary(QUOTE, [{'file': 'a.txt', 'quote': QUOTE}])]
        for result in (summary(), summary('wrong', [{'file': 'b.txt', 'quote': QUOTE}])):
            with self.assertRaises(ValueError):
                plan._parse_summary(json.dumps(result), 'reduce', items)

    def test_conflicting_conditions_from_separate_files_reach_final_validation(self):
        other = 'Гарантийный срок составляет 24 месяца.'
        model = Model()
        def respond(payload):
            if payload.get('stage') == 'map':
                evidence = [{'file': doc['file'], 'quote': quote} for doc in payload['documents']
                            for quote in (QUOTE, other) if quote in doc['text']]
                return summary('Разные сроки' if evidence else '', evidence)
            evidence = [ev for row in payload['partial_answers'] for ev in row['evidence']]
            self.assertEqual({ev['file'] for ev in evidence}, {'a.txt', 'b.txt'})
            return {'answers': [{'id': 'Q1', 'status': 'расхождение',
                                 'answer': '12 месяцев и 24 месяца', 'evidence': evidence}]}
        model.handler = respond
        result = self.run_engine(model, {'a.txt': QUOTE + '\nтекст' * 3000,
                                         'b.txt': 'текст\n' * 3000 + other})['Q1']
        self.assertEqual(result['status'], 'расхождение')
        self.assertNotIn('error_type', result)

    def test_calculation_uses_operands_found_in_different_chunks(self):
        base, rate = 'НМЦК составляет 1800000 рублей.', 'Размер обеспечения составляет 5 процентов.'
        model = Model()
        def respond(payload):
            if payload.get('stage') == 'map':
                evidence = [{'file': doc['file'], 'quote': quote} for doc in payload['documents']
                            for quote in (base, rate) if quote in doc['text']]
                return summary('Основания расчёта' if evidence else '', evidence)
            evidence = [ev for row in payload['partial_answers'] for ev in row['evidence']]
            self.assertEqual([ev['quote'] for ev in evidence], [base, rate])
            return {'answers': [{'id': 'Q1', 'status': 'ответ', 'answer': 'incorrect calculation', 'evidence': evidence,
                                 'calculation': {'operation': 'percent', 'base': '1800000', 'rate': '5',
                                                 'base_evidence': 0, 'rate_evidence': 1}}]}
        model.handler = respond
        result = self.run_engine(model, {'a.txt': base + '\nтекст' * 3000,
                                         'b.txt': 'текст\n' * 3000 + rate},
                                 questions=[{**QUESTION, 'question': 'Каков размер обеспечения в рублях?'}])['Q1']
        self.assertEqual(result['answer'], '90000.00 руб.')
        self.assertEqual(result['status'], 'ответ')

    def test_final_false_absence_is_not_accepted_when_map_found_evidence(self):
        model = Model()
        def respond(payload):
            if payload.get('stage'):
                return model.respond(payload)
            return {'answers': [{'id': 'Q1', 'status': 'нет в документах', 'answer': '', 'evidence': []}]}
        model.handler = respond
        result = self.run_engine(model, {'a.txt': 'текст\n' * 3000 + QUOTE}, attempts=0)['Q1']
        self.assertEqual(result['status'], 'не знаю')
        self.assertIn('релевантные сведения', result['reason'])

    def test_final_answer_repair_uses_bounded_context_and_original_citations(self):
        model = Model()
        def respond(payload):
            result = model.respond(payload)
            if 'coverage' in payload and 'previous_answers_and_validation_errors' not in payload:
                result['answers'][0]['answer'] = '999 месяцев'
            return result
        model.handler = respond
        result = self.run_engine(model, {'a.txt': 'текст\n' * 3000 + QUOTE})['Q1']
        self.assertEqual(result['answer'], QUOTE)
        finals = [call for call in model.calls if 'coverage' in call]
        self.assertEqual(len(finals), 2)
        self.assertIn('previous_answers_and_validation_errors', finals[-1])

    def test_agent_shows_every_chunk_and_reduction_stage(self):
        events = [
            {'event': 'context_request', 'stage': 'map', 'chunk': 1, 'attempt': 0, 'id': 'Q1'},
            {'event': 'context_summary', 'stage': 'map', 'chunk': 1, 'attempt': 0, 'id': 'Q1', 'summary': summary()},
            {'event': 'context_request', 'stage': 'map', 'chunk': 2, 'attempt': 0, 'id': 'Q1'},
            {'event': 'context_request', 'stage': 'map', 'chunk': 2, 'attempt': 0, 'id': 'Q1', 'retry': 1},
            {'event': 'context_request', 'stage': 'reduce', 'level': 1, 'group': 1, 'attempt': 0, 'id': 'Q1'},
            {'event': 'context_request', 'stage': 'final', 'attempt': 0, 'id': 'Q1'},
        ]
        stages = stages_from_trace('\n'.join(json.dumps(event) for event in events))
        self.assertEqual(len(stages), 4)
        self.assertIn('части 1', stages[0]['title'])
        self.assertIn('части 2', stages[1]['title'])
        self.assertIn('уровень 1', stages[2]['title'])
        self.assertIn('итоговый', stages[3]['title'])
        self.assertIn('evidence', stages[0]['detail'])

    def test_preflight_rejects_only_impossible_overhead(self):
        validate_context('verified', {'a.txt': 'Текст. ' * 10000}, [QUESTION], [],
                         max_tokens=512, context_token_limit=12000)
        for documents, question in (({'a.txt': 'text'}, {**QUESTION, 'question': 'я' * 10000}),
                                    ({'я' * 10000: 'text'}, QUESTION)):
            with self.assertRaises(ValueError):
                validate_context('verified', documents, [question], [], max_tokens=512, context_token_limit=12000)

    def test_query_persists_report_trace_originals_and_all_request_stats(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source, output = root / 'Договор.txt', root / 'run'
            text = 'Текст. ' * 5000 + QUOTE
            source.write_text(text, encoding='utf-8')
            model = Model()
            with patch('src.workflows.pipeline.Client', return_value=model), contextlib.redirect_stdout(io.StringIO()):
                code = run_query(QueryOptions(attachment=[str(source)], prompt=[QUESTION['question']]),
                                 output=output, environ={'MAX_TOKENS': '512', 'CONTEXT_TOKEN_LIMIT': '12000'})
            self.assertEqual(code, 0)
            manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'completed')
            self.assertEqual(manifest['stats']['requests'], len(model.calls))
            self.assertGreater(len(model.calls), 2)
            self.assertTrue((output / 'report.html').is_file())
            self.assertIn('context_chunk', (output / 'trace.jsonl').read_text(encoding='utf-8'))
            self.assertIn(text, (json.loads((output / 'inputs.json').read_text(encoding='utf-8'))['packages']['INPUT'][0]).values())
            self.assertEqual(source.read_text(encoding='utf-8'), text)


if __name__ == '__main__':
    unittest.main()
