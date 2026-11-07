"""Сценарий вопросов к файлу, нескольким файлам или произвольной директории.

Здесь нет собственной модели или оценщика: модуль подготавливает входы, организует
ввод вопросов и передаёт готовые тексты конвейеру проверки и сохранения результатов.
"""
from datetime import datetime
import json
from pathlib import Path
import re

from src.workflows import pipeline
from src import settings
from src.progress import emit
from src.settings import QueryOptions, RunOptions
from src.files.input_loader import MAX_QUESTIONS, MAX_QUESTION_CHARS, load_documents, load_questions
from src.generation.answer_engine import question_batches
from src.generation.client import estimate_request_tokens, load_env
from src.generation.prompts import messages


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


def _run_batch(args: QueryOptions, loaded, values):
    rows = _questions(values)
    if args.evaluate:
        from src.evaluation.query_evaluation import plan_query_evaluation
        # До API проверяются только формулировки вопросов и исходные документы.
        # Правильные ответы читаются отдельным этапом после завершения генерации.
        plan = plan_query_evaluation(rows, loaded['documents'], loaded['read_errors'], settings.ROOT / 'data' / 'public')
        emit('Для оценки сопоставлено вопросов: %s; пакет: %s. Эталоны ответов ещё не загружались.' %
                 (len(plan['matches']), plan['package']))
    # Используем ту же консервативную оценку контекста, что и API-клиент. Большой
    # набор отклоняется до запроса, а не урезается скрытно до удобного размера.
    load_env(settings.ROOT / '.env')
    batch_limit = settings.question_batch_limit(args.mode)
    options = RunOptions.from_env(args.mode, _output_path(), repair_attempts=args.repair_attempts,
                                  max_tokens=args.max_tokens, timeout=args.timeout)
    for batch in question_batches(rows, batch_limit):
        payload = messages(args.mode, loaded['documents'], batch, loaded['read_errors'])
        estimate = estimate_request_tokens(payload, options.max_tokens)
        if estimate > options.context_token_limit:
            raise ValueError(f'Оценка размера группы ({estimate}) превышает '
                             f'CONTEXT_TOKEN_LIMIT={options.context_token_limit}. '
                             'Сократите вопросы или объём документов.')
    output = options.output
    package_data = {'INPUT': (loaded['documents'], loaded['read_errors'])}
    source_metadata = {'sources': loaded['sources'], 'skipped': loaded['skipped']}
    code = pipeline.run_groups(options, rows, package_data, source_metadata=source_metadata, batch_limit=batch_limit)
    if args.evaluate:
        from src.evaluation.query_evaluation import evaluate_query_run
        from src.reporting.report import write_run_report
        emit('Генерация завершена. Оцениваем только заданные вопросы по публичным эталонам.')
        evaluate_query_run(output, settings.ROOT / 'data' / 'public')
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
                emit('Источник: %s\n  %s' % (evidence.get('file', ''), evidence.get('quote', '')))
    emit('\nОтчёт: ' + str(output / 'report.html'))
    return code


def _question_inputs(values):
    """Один параметр принимает текст вопросов или файлы; порядок ввода сохраняется."""
    questions, files = [], []
    for value in values:
        if not value.strip():
            raise ValueError('Вопрос не должен быть пустым.')
        path = Path(value).expanduser()
        # Опечатка в пути не должна становиться вопросом модели. Обычный текст
        # вроде «Какая гарантия?» при этом не требует специального префикса.
        looks_like_path = (path.is_absolute() or value.startswith(('./', '../', '.\\', '..\\', '~/', '~\\'))
                           or bool(re.match(r'^[A-Za-z]:[\\/]', value))
                           or path.suffix.lower() in {'.txt', '.md', '.rst', '.csv', '.json'})
        try:
            exists = path.exists()
        except (OSError, ValueError):
            exists = False
        if exists:
            if not path.is_file():
                raise ValueError('--promt ожидает текст вопроса или файл, а не папку: ' + value)
            questions.extend(load_questions(path))
            files.append(path)
        elif looks_like_path:
            raise ValueError('Файл вопросов не найден: ' + value)
        else:
            questions.append(value)
    # Проверяем вопросы до чтения контента и до создания API-клиента.
    _questions(questions)
    return questions, files


def run_query(args: QueryOptions) -> int:
    """Два входа: документы и вопросы. Режим выбирает способ проверки ответов."""
    values, question_files = _question_inputs(args.promt)
    # Файлы с вопросами не входят в доказательства ответа. Старые результаты
    # распознаёт загрузчик по служебным файлам, без требования к именам папок.
    loaded = load_documents(args.atacment, exclude_paths=question_files)
    _show_inputs(loaded)
    if not loaded['documents']:
        raise ValueError('Не найдено ни одного читаемого документа. Проверьте пути и сообщения выше.')
    return _run_batch(args, loaded, values)
