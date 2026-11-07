"""Вопросы к произвольным локальным документам через существующий конвейер CLI.

Здесь нет собственной модели или оценщика: модуль подготавливает входы, организует
ввод вопросов и передаёт их обычному run с теми же проверками и HTML-отчётом.
"""
from argparse import Namespace
from datetime import datetime
import json
from pathlib import Path
import re

import cli
from input_loader import MAX_QUESTIONS, MAX_QUESTION_CHARS, load_documents, load_questions
from prompts import messages


def _show_inputs(loaded):
    """До обращения к модели показывает пользователю фактический состав контекста."""
    documents = loaded['documents']
    cli.emit('Загружено документов: %s; символов: %s.' % (len(documents), sum(map(len, documents.values()))))
    for name, text in documents.items():
        cli.emit('  + %s (%s символов)' % (name, len(text)))
    for item in loaded['skipped']:
        cli.emit('  Пропущен: %s — %s' % (item['path'], item['reason']))
    for error in loaded['read_errors']:
        cli.emit('  Проблема чтения: ' + error)
    cli.emit('Вопросы относятся ко всем загруженным документам вместе.')


def _questions(values):
    """Проверяет ввод до API, сохраняя порядок и формируя устойчивые ID внутри пакета."""
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
    return cli.ROOT / 'runs' / name


def _run_batch(args, loaded, values):
    rows = _questions(values)
    if getattr(args, 'evaluate', False):
        from query_evaluation import plan_query_evaluation
        # До API проверяются только формулировки вопросов и исходные документы.
        # Правильные ответы читаются отдельным этапом после завершения генерации.
        plan = plan_query_evaluation(rows, loaded['documents'], loaded['read_errors'], cli.ROOT / 'data' / 'public')
        cli.emit('Для оценки сопоставлено вопросов: %s; пакет: %s. Эталоны ответов ещё не загружались.' %
                 (len(plan['matches']), plan['package']))
    # Используем ту же консервативную оценку контекста, что и API-клиент. Большой
    # набор отклоняется до запроса, а не урезается скрытно до удобного размера.
    payload = messages(args.mode, loaded['documents'], rows, loaded['read_errors'])
    estimate = sum(len(item['content'].encode('utf-8')) + 32 for item in payload) + args.max_tokens
    if estimate > 128000:
        raise ValueError('Набор слишком большой для текущего полного контекста 128k. Выберите меньше документов или вопросов; файлы автоматически не обрезаются.')
    output = _output_path()
    options = Namespace(**{**vars(args), 'output': str(output), 'evaluate': False})
    prepared = {'questions': rows, 'package_data': {'INPUT': (loaded['documents'], loaded['read_errors'])},
                'source_metadata': {'sources': loaded['sources'], 'skipped': loaded['skipped']}}
    code = cli.run(options, prepared=prepared)
    if getattr(args, 'evaluate', False):
        from query_evaluation import evaluate_query_run
        from report import write_run_report
        cli.emit('Генерация завершена. Оцениваем только заданные вопросы по публичным эталонам.')
        evaluate_query_run(output, cli.ROOT / 'data' / 'public')
        write_run_report(output)
    answers = json.loads((output / 'answers.json').read_text(encoding='utf-8'))
    questions = {row['id']: row['question'] for row in rows}
    cli.emit('\nОтветы по документам:')
    for answer in answers:
        cli.emit('\n%s. %s' % (answer['id'], questions[answer['id']]))
        cli.emit('Статус: ' + answer.get('status', 'не указан'))
        if answer.get('answer'):
            cli.emit('Ответ: ' + answer['answer'])
        if answer.get('reason'):
            cli.emit('Основание: ' + answer['reason'])
        for evidence in answer.get('evidence') or []:
            if isinstance(evidence, dict):
                cli.emit('Источник: %s\n  %s' % (evidence.get('file', ''), evidence.get('quote', '')))
    cli.emit('\nОтчёт: ' + str(output / 'report.html'))
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


def run_query(args):
    """Два входа: документы и вопросы. Режим выбирает способ проверки ответов."""
    values, question_files = _question_inputs(args.promt)
    # Файлы с вопросами не входят в доказательства ответа. Старые результаты
    # распознаёт загрузчик по служебным файлам, без требования к именам папок.
    loaded = load_documents(args.atacment, exclude_paths=question_files)
    _show_inputs(loaded)
    if not loaded['documents']:
        raise ValueError('Не найдено ни одного читаемого документа. Проверьте пути и сообщения выше.')
    return _run_batch(args, loaded, values)
