"""Сопоставление сохранённых запусков по тексту вопросов для дашборда."""
from collections import Counter, defaultdict
import re

from src.evaluation.query_evaluation import _document_signature
from src.evaluation.runs import comparison_data
from src.generation.validation import norm
from src.reporting.report import _json, build_run_model


def _load_run(root, identifier, mode):
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
        raise ValueError('Выберите запуск из истории.')
    directory = root / identifier
    if not directory.is_dir() or directory.resolve() != directory:
        raise ValueError('Запуск не найден в истории.')
    artifacts = ('manifest.json', 'inputs.json', 'answers.csv', 'answers.json',
                 'evaluation.csv', 'evaluation.json', 'trace.jsonl')
    if any((directory / name).resolve() != directory / name for name in artifacts):
        raise ValueError('Артефакты сравнения должны находиться в папке запуска.')
    manifest = _json(directory / 'manifest.json')
    if manifest.get('status') not in ('completed', 'completed_with_errors') or manifest.get('mode') != mode:
        raise ValueError(f'Выберите завершённый запуск в режиме {mode}.')
    snapshot = _json(directory / 'inputs.json')
    run = build_run_model(directory)['runs'][0]
    selected = set(manifest.get('question_ids', [row['id'] for row in run['answers']]))
    saved_questions = snapshot.get('questions')
    if not isinstance(saved_questions, list):
        raise ValueError('В запуске нет сохранённых текстов вопросов.')
    questions = defaultdict(list)
    for question in saved_questions:
        if not isinstance(question, dict) or not isinstance(question.get('id'), str) or question['id'] not in selected:
            continue
        text = question.get('question')
        # Сохраняем смысловую пунктуацию внутри вопроса, включая знак процента и минус.
        if isinstance(text, str) and (key := norm(text).rstrip('.?!… ')):
            questions[key].append(question)
    if not questions:
        raise ValueError('В запуске нет сохранённых текстов вопросов.')
    return directory, run, questions, snapshot.get('packages', {})


def _corpus(packages):
    """Переименование файла сохраняет содержимое комплекта."""
    signature = Counter()
    if not isinstance(packages, dict):
        return None
    for pair in packages.values():
        if not isinstance(pair, list) or len(pair) != 2 or pair[1]:
            return None
        try:
            signature.update(_document_signature(pair[0]))
        except ValueError:
            return None
    return signature or None


def _answer_view(answer):
    status = answer.get('status')
    if answer.get('error_type'):
        status = 'техническая ошибка'
    elif status not in ('ответ', 'расхождение', 'нет в документах', 'не знаю'):
        status = 'нет строки' if not status or status == 'нет строки' else 'другой статус'
    return {'status': status, 'answer': answer.get('answer', ''), 'verdict': answer.get('verdict')}


def compare_history_runs(root, baseline, verified):
    baseline_dir, before, left, left_docs = _load_run(root, baseline, 'baseline')
    verified_dir, after, right, right_docs = _load_run(root, verified, 'verified')
    left_answers, right_answers = ({row['id']: row for row in run['answers']} for run in (before, after))
    ambiguous = {key for key in left.keys() & right.keys() if len(left[key]) != 1 or len(right[key]) != 1}
    rows, transitions = [], Counter()
    for key in left:
        if key not in right or key in ambiguous:
            continue
        question = left[key][0]
        first = _answer_view(left_answers.get(question['id'], {}))
        second = _answer_view(right_answers.get(right[key][0]['id'], {}))
        rows.append({'question': question['question'], 'baseline': first, 'verified': second,
                     'changed': (first['status'], first['answer']) != (second['status'], second['answer'])})
        transitions[first['status'], second['status']] += 1

    left_signature, right_signature = _corpus(left_docs), _corpus(right_docs)
    documents_match = None if left_signature is None or right_signature is None else left_signature == right_signature
    fields = {'model': 'модель', 'temperature': 'температура', 'max_tokens': 'лимит токенов ответа',
              'context_token_limit': 'лимит контекста', 'max_questions_per_request': 'вопросов на запрос',
              'max_requests': 'лимит запросов', 'workers': 'число воркеров', 'timeout_seconds': 'таймаут'}
    left_config, right_config = before['config'], after['config']
    settings_changed = [label for key, label in fields.items()
                        if key in left_config and key in right_config and left_config[key] != right_config[key]]
    quality = None
    quality_note = 'Для сравнения качества нужны оценки обоих запусков по одному эталону.'
    if settings_changed:
        quality_note = 'Для сравнения качества нужны одинаковые настройки генерации.'
    elif before['source_audit_conflicts'] or after['source_audit_conflicts']:
        quality_note = 'Сохранённая оценка включает верные ответы с неподтверждёнными источниками. Нужна повторная оценка.'
    elif before['quality'] is not None and after['quality'] is not None:
        if documents_match is not True:
            quality_note = 'Для сравнения качества нужен один комплект документов.'
        elif not all(_json(path / 'evaluation.json').get('evaluation_fingerprint')
                     for path in (baseline_dir, verified_dir)):
            quality_note = 'В сохранённых оценках нет данных для сверки эталона.'
        else:
            try:
                strict = comparison_data(baseline_dir, verified_dir)
                quality = {key: strict[key] for key in ('baseline', 'verified')}
                quality_note = ''
            except ValueError as exc:
                quality_note = ('Для сравнения качества нужны одинаковые входные данные.'
                                if 'различается input_fingerprint' in str(exc) else str(exc))
    return {
        'baseline': {key: before[key] for key in ('model', 'operations', 'n_questions', 'quality', 'source_audit_conflicts')},
        'verified': {key: after[key] for key in ('model', 'operations', 'n_questions', 'quality', 'source_audit_conflicts')},
        'documents_match': documents_match,
        'settings_changed': settings_changed,
        'coverage': {'paired': len(rows), 'baseline_only': sum(len(left[key]) for key in left.keys() - right.keys()),
                     'verified_only': sum(len(right[key]) for key in right.keys() - left.keys()),
                     'ambiguous': len(ambiguous)},
        'transitions': [{'baseline': a, 'verified': b, 'count': count} for (a, b), count in transitions.items()],
        'questions': rows, 'quality': quality, 'quality_note': quality_note,
    }
