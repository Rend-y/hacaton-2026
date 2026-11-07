"""Выполнение одного запроса по уже прочитанным документам и сохранение результата.

Этот слой не читает датасеты, не разбирает CLI и не выбирает папки по имени.
Каждая группа документов передаётся только связанным с ней вопросам.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from src.generation.answer_engine import log_batch_plan, process_questions, question_batches
from src.generation.answers import abstain
from src.generation.client import Client
from src.progress import RunLog, emit
from src.generation.prompts import BASELINE, VERIFIED
from src.files.run_storage import save_answers, write_json
from src.settings import RunOptions


def run_groups(options: RunOptions, questions, package_data, source_metadata=None, *, batch_limit: int) -> int:
    """Сохраняет снимок, выполняет запрос с проверками и формирует локальный отчёт."""
    grouped = {}
    for question in questions:
        grouped.setdefault(question['package'], []).append(question)
    if not questions or set(grouped) != set(package_data) or len({q['id'] for q in questions}) != len(questions):
        raise ValueError('Некорректная связь вопросов и комплектов документов.')
    # INPUT и packages сохраняются только как формат прежних артефактов. Это не
    # имена требуемых директорий: документы здесь уже прочитаны из любых путей.
    input_data = {'questions': questions, 'packages': package_data}
    fingerprint = hashlib.sha256(json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    input_data['source_metadata'] = source_metadata or {}
    output = Path(options.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory is not empty; choose a new output to preserve prior runs')
    if options.mode not in ('baseline', 'verified'):
        raise ValueError('mode must be baseline or verified')
    if not 0 <= options.repair_attempts <= 3:
        raise ValueError('repair-attempts must be 0–3')
    client = Client(os.environ.get('API_KEY', ''), os.environ.get('BASE_URL', ''),
                    os.environ.get('MODEL', ''), options.timeout, options.max_tokens,
                    context_token_limit=options.context_token_limit, max_workers=options.max_workers)
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
                'max_tokens': options.max_tokens, 'context_token_limit': options.context_token_limit,
                'input_fingerprint': fingerprint, 'timeout_seconds': options.timeout,
                'question_ids': [q['id'] for q in questions], 'created_at': datetime.now(timezone.utc).isoformat(),
                'repair_attempts': options.repair_attempts if options.mode == 'verified' else 0,
                'max_questions_per_request': batch_limit,
                'workers': options.max_workers, 'status': 'running', 'stats': dict(client.stats)}
    write_json(output / 'manifest.json', manifest)
    progress('Старт: режим %s; модель %s; вопросов %s; документов %s; максимум потоков %s.' %
             (options.mode, client.model, len(questions), sum(len(pair[0]) for pair in package_data.values()),
              options.max_workers))
    progress('Входные данные и промпты сохранены. Эталоны не загружались; журнал этапов: ' + str(output / 'progress.log'))
    answers = {}

    def process_batch(name, rows, batch='root'):
        documents, read_errors = package_data[name]
        try:
            return process_questions(client, options.mode, name, rows, documents,
                                     read_errors, options.repair_attempts, trace, progress,
                                     batch_limit=batch_limit, _batch=batch)
        except Exception as exc:
            reason = 'Internal input failure: ' + type(exc).__name__
            trace({'package': name, 'batch': batch, 'event': 'internal_error', 'error': reason})
            progress('[%s/%s] Внутренняя ошибка: %s.' % (name, batch, reason))
            return {q['id']: {**abstain(q, reason), 'error_type': 'internal'} for q in rows}

    def save_progress(result):
        answers.update(result)
        save_answers(output, questions, answers)
        manifest.update(completed_questions=len(answers), stats=dict(client.stats),
                        elapsed_seconds=round(time.monotonic() - started, 3))
        write_json(output / 'manifest.json', manifest)
        progress('Общий результат сохранён: %s/%s вопросов.' % (len(answers), len(questions)))

    if options.max_workers == 1:
        for name, rows in grouped.items():
            save_progress(process_batch(name, rows))
    else:
        executor = ThreadPoolExecutor(max_workers=options.max_workers, thread_name_prefix='model-request')
        try:
            futures = []
            for name, rows in grouped.items():
                groups = question_batches(rows, batch_limit) if package_data[name][0] else [rows]
                if len(groups) > 1:
                    log_batch_plan(name, groups, batch_limit, trace, progress)
                for index, batch in enumerate(groups, 1):
                    batch_id = f'root.{index}' if len(groups) > 1 else 'root'
                    futures.append(executor.submit(process_batch, name, batch, batch_id))
            for future in as_completed(futures):
                save_progress(future.result())
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    progress('Сохранены answers.csv и answers.json; готово %s/%s вопросов.' % (len(answers), len(questions)))
    failures = [answer for answer in answers.values() if answer.get('error_type')]
    manifest.update(status='completed_with_errors' if failures else 'completed',
                    completed_questions=len(answers), elapsed_seconds=round(time.monotonic() - started, 3),
                    stats=dict(client.stats))
    write_json(output / 'manifest.json', manifest)
    # Для отчёта о работе эталоны не нужны. Официальная оценка — следующий,
    # отдельно запрошенный шаг сценария query, после завершения генерации.
    from src.reporting.report import write_run_report
    report_path = write_run_report(output)
    progress('HTML-отчёт: ' + str(report_path))
    progress('Готово. CSV: ' + str(output / 'answers.csv'))
    progress('Запросов к API: %s; время: %.1f с; технически не обработано вопросов: %s. manifest.json и trace.jsonl сохранены.' %
             (client.stats['requests'], manifest['elapsed_seconds'], len(failures)))
    return 2 if failures else 0
