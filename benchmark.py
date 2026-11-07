"""Проверка всего поставляемого набора с единой выгрузкой и общей оценкой.

Это отдельный сценарий для готового тестового набора. Обычный query по-прежнему
принимает произвольные файлы и директории без структуры датасета.
"""
from datetime import datetime

import settings
import pipeline
from reference_data import read_questions, read_package
from evaluation import evaluate_run
from report import write_run_report
from progress import emit


def run_public(mode='verified'):
    """Генерирует все ответы, затем один раз запускает check.py по полному эталону."""
    data = settings.ROOT / 'data' / 'public'
    questions = read_questions(data)
    documents = {name: read_package(data, name) for name in dict.fromkeys(q['package'] for q in questions)}
    # Правильные ответы здесь не читаются и в генерацию не передаются.
    output = settings.ROOT / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-all-' + mode)
    code = pipeline.run_groups(settings.RunOptions(mode=mode, output=output), questions, documents)
    emit('Все группы обработаны. Проверяем единую выгрузку официальным check.py.')
    evaluate_run(output, data)
    emit('Общий HTML-отчёт: ' + str(write_run_report(output)))
    return code
