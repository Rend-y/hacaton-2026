"""Сценарий вопросов к одному или нескольким файлам.

Здесь нет собственной модели или оценщика: модуль подготавливает входы, организует
ввод вопросов и передаёт готовые тексты конвейеру проверки и сохранения результатов.
"""
from datetime import datetime
import json
import os
from pathlib import Path

from src.workflows import pipeline
from src import settings
from src.progress import emit
from src.settings import QueryOptions, RunOptions
from src.files.input_loader import MAX_QUESTIONS, MAX_QUESTION_CHARS, load_documents
from src.generation.client import load_env
from src.generation.context_loop import validate_context
from src.generation.token_budget import get_token_counter
from src.generation.validation import citation_error


def _show_inputs(loaded):
    """До обращения к модели показывает пользователю фактический состав контекста."""
    documents = loaded['documents']
    emit('Загружено документов: %s; символов: %s.' % (len(documents), sum(map(len, documents.values()))))
    for name, text in documents.items():
        emit('  + %s (%s символов)' % (name, len(text)))
    for item in loaded['skipped']:
        emit('  Пропущен: %s — %s' % (item['path'], item['reason']))
    for error in loaded['read_errors']:
        emit('  Проблема чтения: ' + error)
    emit('Вопросы относятся ко всем загруженным документам вместе.')


def _questions(values):
    """Проверяет ввод до API, сохраняя порядок и формируя устойчивые ID внутри запуска."""
    if not values or len(values) > MAX_QUESTIONS:
        raise ValueError('Укажите от 1 до %s вопросов за один запуск.' % MAX_QUESTIONS)
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Вопрос не должен быть пустым.')
        if len(value) > MAX_QUESTION_CHARS:
            raise ValueError('Вопрос длиннее %s символов. Сократите его или разделите.' % MAX_QUESTION_CHARS)
        result.append({'id': 'INPUT-%03d' % (len(result) + 1), 'package': 'INPUT', 'question': value.strip()})
    return result


def _output_path():
    """Результаты получают новую папку автоматически, без дополнительных параметров."""
    name = datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-query'
    return settings.ROOT / 'runs' / name


def _run_batch(args: QueryOptions, loaded, rows, output: Path | None = None, *, environ=None, evaluation_data=None):
    if evaluation_data is not None:
        from src.evaluation.query_evaluation import plan_query_evaluation
        # До API проверяются только формулировки вопросов и исходные документы.
        # Правильные ответы читаются отдельным этапом после завершения генерации.
        plan = plan_query_evaluation(rows, loaded['documents'], loaded['read_errors'], evaluation_data)
        emit('Для оценки сопоставлено вопросов: %s; пакет: %s. Эталоны ответов ещё не загружались.' %
                 (len(plan['matches']), plan['package']))
    # Большие документы обрабатываются частями; до API проверяем, что в контекст
    # помещаются обязательные инструкции, вопрос и минимальная сводка.
    if environ is None:
        load_env(settings.ROOT / '.env')
    batch_limit = settings.question_batch_limit(environ)
    options = RunOptions.from_env(args.mode, output or _output_path(), repair_attempts=args.repair_attempts,
                                  max_tokens=args.max_tokens, timeout=args.timeout, environ=environ)
    validate_context(args.mode, loaded['documents'], rows, loaded['read_errors'],
                     max_tokens=options.max_tokens, context_token_limit=options.context_token_limit,
                     token_counter=get_token_counter((os.environ if environ is None else environ).get('MODEL', ''),
                                                     options.tokenizer_path))
    output = options.output
    package_data = {'INPUT': (loaded['documents'], loaded['read_errors'])}
    source_metadata = {'sources': loaded['sources'], 'skipped': loaded['skipped']}
    code = pipeline.run_groups(options, rows, package_data, source_metadata=source_metadata,
                               batch_limit=batch_limit, environ=environ)
    if evaluation_data is not None:
        from src.evaluation.query_evaluation import evaluate_query_run
        from src.reporting.report import write_run_report
        emit('Генерация завершена. Оцениваем только заданные вопросы по эталону.')
        evaluate_query_run(output, evaluation_data)
        write_run_report(output)
    answers = json.loads((output / 'answers.json').read_text(encoding='utf-8'))
    questions = {row['id']: row['question'] for row in rows}
    emit('\nОтветы по документам:')
    for answer in answers:
        emit('\n%s. %s' % (answer['id'], questions[answer['id']]))
        emit('Статус: ' + answer.get('status', 'не указан'))
        if answer.get('answer'):
            emit('Ответ: ' + answer['answer'])
        if answer.get('reason'):
            emit('Основание: ' + answer['reason'])
        for evidence in answer.get('evidence') or []:
            if isinstance(evidence, dict):
                filename, quote = evidence.get('file'), evidence.get('quote')
                issue = citation_error(filename, quote, loaded['documents'])
                if issue:
                    emit('Источник не подтверждён (%s): %s' % (filename or 'файл не указан', issue))
                else:
                    emit('Источник: %s\n  %s' % (filename, quote))
    emit('\nОтчёт: ' + str(output / 'report.html'))
    return code


def run_query(args: QueryOptions, *, output: Path | None = None, environ=None, evaluation_data: Path | None = None) -> int:
    """Два входа: документы и вопросы. Режим выбирает способ проверки ответов."""
    rows = _questions(args.prompt)
    for value in args.attachment:
        if not Path(value).expanduser().is_file():
            raise ValueError('Укажите существующий файл документа: ' + value)
    loaded = load_documents(args.attachment)
    _show_inputs(loaded)
    if not loaded['documents']:
        raise ValueError('Не найдено ни одного читаемого документа. Проверьте пути и сообщения выше.')
    return _run_batch(args, loaded, rows, output, environ=environ, evaluation_data=evaluation_data)
