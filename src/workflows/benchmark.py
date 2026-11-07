"""Проверка всего поставляемого набора с единой выгрузкой и общей оценкой.

Это отдельный сценарий для готового тестового набора. Обычный query по-прежнему
принимает произвольные файлы и директории без структуры датасета.
"""
from datetime import datetime

from src import settings
from src.workflows import pipeline
from src.evaluation.reference_data import read_questions, read_package
from src.evaluation.runs import evaluate_run
from src.generation.client import load_env
from src.reporting.report import write_run_report
from src.progress import emit


def run_public(mode='verified'):
    """Генерирует все ответы, затем один раз запускает check.py по полному эталону."""
    load_env(settings.ROOT / '.env')
    batch_limit = settings.question_batch_limit()
    data = settings.ROOT / 'data' / 'public'
    questions = read_questions(data)
    documents = {name: read_package(data, name) for name in dict.fromkeys(q['package'] for q in questions)}
    # Правильные ответы здесь не читаются и в генерацию не передаются.
    output = settings.ROOT / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-all-' + mode)
    options = settings.RunOptions.from_env(mode, output)
    code = pipeline.run_groups(options, questions, documents, batch_limit=batch_limit)
    emit('Все группы обработаны. Проверяем единую выгрузку официальным check.py.')
    evaluate_run(output, data)
    emit('Общий HTML-отчёт: ' + str(write_run_report(output)))
    return code
