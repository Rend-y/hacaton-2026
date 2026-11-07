"""Выполнение одного запроса по уже прочитанным документам и сохранение результата.

Этот слой не читает датасеты, не разбирает CLI и не выбирает папки по имени.
Все переданные документы составляют один контекст для всех переданных вопросов.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from answer_engine import process_questions
from answers import abstain
from client import Client, load_env
from progress import RunLog, emit
from prompts import BASELINE, VERIFIED
from run_storage import save_answers, write_json
from settings import RunOptions
import settings


def run(options: RunOptions, prepared: dict) -> int:
    """Сохраняет снимок, выполняет запрос с проверками и формирует локальный отчёт."""
    load_env(settings.ROOT / '.env')
    questions = prepared['questions']
    documents, read_errors = prepared['documents'], prepared['read_errors']
    # INPUT и packages сохраняются только как формат прежних артефактов. Это не
    # имена требуемых директорий: документы здесь уже прочитаны из любых путей.
    input_data = {'questions': questions, 'packages': {'INPUT': (documents, read_errors)}}
    fingerprint = hashlib.sha256(json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    input_data['source_metadata'] = prepared.get('source_metadata', {})
    output = Path(options.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory is not empty; choose a new output to preserve prior runs')
    if options.mode not in ('baseline', 'verified'):
        raise ValueError('mode must be baseline or verified')
    if not 0 <= options.repair_attempts <= 3:
        raise ValueError('repair-attempts must be 0–3')
    if not 256 <= options.max_tokens <= 32768 or options.timeout <= 0:
        raise ValueError('max-tokens must be 256–32768 and timeout must be positive')
    client = Client(os.environ.get('ITFEST_API_KEY', ''),
                    os.environ.get('ITFEST_BASE_URL', 'https://ai.on-soft.ru/v1'),
                    os.environ.get('ITFEST_MODEL', 'qwen3-6-practice'), options.timeout, options.max_tokens)
    output.mkdir(parents=True, exist_ok=True)
    progress = RunLog(output / 'progress.log', emit, secrets=(client.key,))
    write_json(output / 'inputs.json', input_data)
    write_json(output / 'prompts.json', {'baseline': BASELINE, 'verified': VERIFIED})
    lock = threading.Lock()

    def trace(event):
        # Дополнительная очистка не допускает попадания настроенного ключа в журнал.
        line = json.dumps(event, ensure_ascii=False).replace(client.key, '[redacted]')
        with lock:
            with (output / 'trace.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')

    started = time.monotonic()
    manifest = {'model': client.model, 'base_url': client.base_url, 'mode': options.mode, 'temperature': 0,
                'max_tokens': options.max_tokens, 'input_fingerprint': fingerprint, 'timeout_seconds': options.timeout,
                'question_ids': [q['id'] for q in questions], 'created_at': datetime.now(timezone.utc).isoformat(),
                'repair_attempts': options.repair_attempts if options.mode == 'verified' else 0,
                'workers': 1, 'status': 'running', 'stats': dict(client.stats)}
    write_json(output / 'manifest.json', manifest)
    progress('Старт: режим %s; модель %s; вопросов %s; документов %s.' %
             (options.mode, client.model, len(questions), len(documents)))
    progress('Входные данные и промпты сохранены. Эталоны не загружались; журнал этапов: ' + str(output / 'progress.log'))
    try:
        answers = process_questions(client, options.mode, 'INPUT', questions, documents,
                                    read_errors, options.repair_attempts, trace, progress)
    except Exception as exc:
        # Технический сбой явно отмечается в артефактах и не маскируется верным ответом.
        reason = 'Internal input failure: ' + type(exc).__name__
        trace({'package': 'INPUT', 'event': 'internal_error', 'error': reason})
        progress('[INPUT] Внутренняя ошибка: %s; сохраняем осторожные отказы.' % reason)
        answers = {q['id']: {**abstain(q, reason), 'error_type': 'internal'} for q in questions}
    save_answers(output, questions, answers)
    progress('Сохранены answers.csv и answers.json; готово %s/%s вопросов.' % (len(answers), len(questions)))
    failures = [answer for answer in answers.values() if answer.get('error_type')]
    manifest.update(status='completed_with_errors' if failures else 'completed',
                    completed_questions=len(answers), elapsed_seconds=round(time.monotonic() - started, 3),
                    stats=dict(client.stats))
    write_json(output / 'manifest.json', manifest)
    # Для отчёта о работе эталоны не нужны. Официальная оценка — следующий,
    # отдельно запрошенный шаг сценария query, после завершения генерации.
    from report import write_run_report
    report_path = write_run_report(output)
    progress('HTML-отчёт: ' + str(report_path))
    progress('Готово. CSV: ' + str(output / 'answers.csv'))
    progress('Запросов к API: %s; время: %.1f с; технически не обработано вопросов: %s. manifest.json и trace.jsonl сохранены.' %
             (client.stats['requests'], manifest['elapsed_seconds'], len(failures)))
    return 2 if failures else 0
