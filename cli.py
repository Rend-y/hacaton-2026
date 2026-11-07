#!/usr/bin/env python3
"""Команды CLI и полный путь от документов закупки до итогового CSV.

Этот модуль читает входы, распределяет закупки между рабочими потоками,
вызывает модель и проверки, сохраняет результаты. Эталоны подключаются
только отдельной командой оценки. Требуется Python 3.10+, без внешних пакетов.
"""
import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

from client import ApiError, Client, load_env
from prompts import BASELINE, VERIFIED, messages
from progress import RunLog
from validation import norm, validate_answer

ROOT = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()
FIELDS = ['id', 'status', 'answer', 'quote', 'file']


def emit(text):
    """Печатает прогресс без перемешивания строк от разных рабочих потоков."""
    with PRINT_LOCK:
        print(text, flush=True)


def write_json(path, value):
    """Сохраняет читаемый JSON с русским текстом в UTF-8."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read_text(path):
    """Читает UTF-8 или Windows-1251; двоичные и UTF-16 файлы отклоняет явно."""
    raw = path.read_bytes()
    if b'\x00' in raw:
        raise ValueError('binary or UTF-16 file unsupported')
    for encoding in ('utf-8-sig', 'cp1251'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError('unsupported encoding')


def read_questions(data):
    """Проверяет таблицу вопросов, уникальность ID и безопасные имена закупок."""
    text = read_text(data / 'questions.csv')
    header = text.splitlines()[0] if text else ''
    delimiter = ';' if header.count(';') >= header.count(',') else ','
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not {'id', 'package', 'question'} <= set(reader.fieldnames or []):
        raise ValueError('questions.csv must have id, package, question columns')
    rows = []
    seen = set()
    for row in reader:
        question = {key: (row.get(key) or '').strip() for key in ('id', 'package', 'question')}
        if not all(question.values()) or not re.fullmatch(r'[A-Za-z0-9_-]+', question['package']):
            raise ValueError('Invalid question row or unsafe package name')
        if question['id'] in seen:
            raise ValueError('Duplicate question ID: ' + question['id'])
        seen.add(question['id'])
        rows.append(question)
    if not rows:
        raise ValueError('No questions found')
    return rows


def read_package(data, package):
    """Возвращает тексты и проблемы чтения, не подменяя ошибки отсутствием сведений."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', package):
        raise ValueError('Unsafe package name')
    directory = data / 'packages' / package
    documents, errors = {}, []
    if not directory.resolve().is_relative_to((data / 'packages').resolve()):
        return {}, ['Package directory escapes the dataset']
    if not directory.is_dir():
        return {}, ['Package directory is missing']
    for path in sorted(directory.glob('*.txt')):
        try:
            if path.is_symlink():
                raise ValueError('symbolic links are unsupported')
            text = read_text(path)
            if not text.strip():
                raise ValueError('empty document')
            documents[path.name] = text
        except (OSError, ValueError) as exc:
            errors.append(path.name + ': ' + str(exc))
    if not documents:
        errors.append('No readable TXT documents')
    return documents, errors


def parse_answers(content):
    """Разбирает JSON модели; дубли ID считаются ошибкой, а не скрыто заменяются."""
    text = re.sub(r'<think>.*?</think>', '', content, flags=re.S).strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    parsed = json.loads(text)
    rows = parsed.get('answers') if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError('Expected JSON object with answers list')
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise ValueError('Answer must be an object with string id')
        if row['id'] in found:
            raise ValueError('Duplicate answer ID')
        found[row['id']] = row
    return found


def calculate(answer):
    """Считает процент через Decimal; основания операндов затем проверяет валидатор."""
    calculation = answer.get('calculation')
    if not isinstance(calculation, dict) or calculation.get('operation') != 'percent' or answer.get('status') != 'ответ':
        return answer
    try:
        values = []
        for key in ('base', 'rate'):
            value = str(calculation.get(key, ''))
            if len(value) > 80 or not re.fullmatch(r'\d+(?:\.\d+)?', value):
                return answer
            values.append(Decimal(value))
        with localcontext() as context:
            context.prec = 180
            total = (values[0] * values[1] / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return {**answer, 'answer': format(total, 'f') + ' руб.'}
    except (InvalidOperation, ValueError):
        return answer


def abstain(question, reason):
    """Формирует осторожный отказ с причиной вместо неподтверждённого ответа."""
    return {'id': question['id'], 'status': 'не знаю', 'answer': '', 'evidence': [], 'reason': reason}


def csv_row(answer):
    """Преобразует внутренний ответ в пять колонок формата организаторов."""
    # В CSV помещается одна цитата; остальные основания остаются в JSON и журнале.
    evidence = answer.get('evidence')
    first = evidence[0] if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict) else {}
    status = answer.get('status', '')
    empty = status in ('не знаю', 'нет в документах')
    text = answer.get('answer', '')
    quote = first.get('quote', '')
    return {'id': answer['id'], 'status': status if isinstance(status, str) else '',
            'answer': re.sub(r'\s+', ' ', text).strip() if isinstance(text, str) and not empty else '',
            'quote': re.sub(r'\s+', ' ', quote).strip() if isinstance(quote, str) and not empty else '',
            'file': first.get('file', '') if isinstance(first.get('file', ''), str) and not empty else ''}


def save_answers(output, questions, answers):
    """Сохраняет результат в порядке вопросов; CSV заменяется только после записи."""
    ordered = [answers[q['id']] for q in questions if q['id'] in answers]
    temporary = output / 'answers.csv.tmp'
    with temporary.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=';')
        writer.writeheader()
        writer.writerows(csv_row(a) for a in ordered)
    temporary.replace(output / 'answers.csv')
    write_json(output / 'answers.json', ordered)


def process_package(client, mode, package, questions, documents, read_errors, attempts, trace, progress=None):
    """Обрабатывает одну закупку; повторяет только ответы с ошибками проверок.

    В baseline содержательные ошибки сохраняются для честного исходного замера.
    В verified неподтверждённый ответ исправляется либо заменяется на «не знаю».
    """
    accepted = {}
    pending = questions[:]
    correction = None
    log = progress if progress is not None else RunLog(None, emit)

    def say(message):
        log('[%s] %s' % (package, message))

    def api_progress(event):
        """Связывает безопасные события API с закупкой, терминалом и журналом JSON."""
        kind = event['event']
        if kind == 'rate_limit_wait':
            say('Локальный лимит запросов/токенов: ожидание %.0f с.' % event['wait_seconds'])
        elif kind == 'api_request':
            say('API: отправляем запрос, HTTP-попытка %s/3; таймаут %s с.' %
                (event['http_attempt'], event['timeout_seconds']))
        elif kind == 'api_wait':
            say('API: ожидаем ответ сервера уже %.0f с; локальная проверка ещё не началась.' % event['elapsed_seconds'])
        elif kind == 'api_response':
            say('API: ответ получен за %.1f с; токены входа: %s, выхода: %s.' %
                (event['elapsed_seconds'], event['prompt_tokens'], event['completion_tokens']))
        elif kind == 'api_error':
            say('API: HTTP %s.' % event['http_status'] if 'http_status' in event else 'API: сетевая ошибка или таймаут.')
        elif kind == 'api_retry':
            say('API: повтор того же запроса через %.0f с, HTTP-попытка %s/3. Это сетевой повтор, не исправление ответа.' %
                (event['wait_seconds'], event['next_http_attempt']))
        trace({'package': package, 'attempt': attempt, **event})

    say('Загружено документов: %s; символов: %s; вопросов: %s.' %
        (len(documents), sum(len(text) for text in documents.values()), len(questions)))
    if documents:
        say('Файлы: ' + ', '.join(documents))
    for error in read_errors:
        say('Проблема чтения: ' + error)
    for question in questions:
        say('%s — вопрос: %s' % (question['id'], question['question'][:200]))
    if not documents:
        say('Нет читаемых документов: API не вызываем, возвращаем «не знаю».')
        trace({'package': package, 'event': 'unreadable', 'read_errors': read_errors})
        return {q['id']: {**abstain(q, '; '.join(read_errors)), 'error_type': 'input_read'} for q in questions}
    for attempt in range(1 + (attempts if mode == 'verified' else 0)):
        requested_ids = {q['id'] for q in pending}
        request_messages = messages(mode, documents, pending, read_errors, correction)
        started = time.monotonic()
        stage = 'первичный запрос' if attempt == 0 else 'исправление %s/%s' % (attempt, attempts)
        say('Готовим %s: весь пакет документов и %s вопросов. Эталоны модели не передаются.' % (stage, len(pending)))
        try:
            content, meta = client.complete(request_messages, progress=api_progress)
            say('Локально: разбираем JSON и проверяем идентификаторы вопросов.')
            try:
                candidates = parse_answers(content)
                if set(candidates) - requested_ids:
                    raise ValueError('Response contains unrequested question IDs')
                parse_error = None
            except (ValueError, TypeError):
                candidates = {}
                parse_error = 'Response was not valid answers JSON or IDs were invalid'
                say('Локально: JSON или идентификаторы некорректны; ответы текущего запроса нужно исправить.')
            if parse_error is None:
                say('Локально: JSON разобран, записей ответа: %s.' % len(candidates))
            trace({'package': package, 'attempt': attempt, 'question_ids': sorted(requested_ids),
                   'elapsed_seconds': round(time.monotonic() - started, 3),
                   'response': re.sub(r'<think>.*?</think>', '', content, flags=re.S),
                   'api': meta, 'parse_error': parse_error})
        except ApiError as exc:
            reason = str(exc)
            trace({'package': package, 'attempt': attempt, 'event': 'api_error', 'error': reason})
            say('API не дал пригодного ответа: %s. Ожидающим вопросам назначаем «не знаю» с технической причиной.' % reason)
            for q in pending:
                accepted[q['id']] = {**abstain(q, reason), 'error_type': 'api'}
            return accepted
        next_pending, correction = [], []
        for question in pending:
            raw = candidates.get(question['id'])
            errors = []
            if raw is None:
                say('%s: ответа в JSON нет; фиксируем ошибку формата.' % question['id'])
                errors = [parse_error or 'Missing answer ID']
                answer = abstain(question, errors[0])
            elif mode == 'verified':
                status = norm(raw['status']) if isinstance(raw.get('status'), str) else ''
                say('%s: статус модели — «%s».' % (question['id'], status or 'не указан'))
                if status == 'нет в документах':
                    say('%s: модель сообщает об отсутствии сведений. Локально проверяем читаемость пакета; отсутствие ответа кодом не доказано.' % question['id'])
                elif status == 'не знаю':
                    say('%s: осторожный отказ модели; факт и цитаты не проверяем.' % question['id'])
                else:
                    say('%s: локально проверяем статус, цитаты в указанных файлах, числа, даты и время; для расхождения нужны два файла.' % question['id'])
                answer = calculate(raw)
                if answer is not raw:
                    say('%s: процент пересчитан кодом: %s. Теперь проверяем основания обоих операндов.' %
                        (question['id'], answer['answer']))
                errors = validate_answer(answer, documents, read_errors)
                if not errors:
                    answer = {**answer, 'status': norm(answer['status'])}
                    if status == 'нет в документах':
                        say('%s: статус допустим по локальным условиям; полнота поиска остаётся оценкой модели.' % question['id'])
                    elif status != 'не знаю':
                        say('%s: локальные проверки пройдены, подтверждённых цитат: %s. Это не независимая проверка смысла.' %
                            (question['id'], len(answer.get('evidence') or [])))
            else:
                answer = raw
                say('%s: baseline — сохраняем ответ модели без содержательных проверок и исправлений.' % question['id'])
            for error in errors:
                say('%s: проверка не пройдена — %s' % (question['id'], error))
            trace({'package': package, 'id': question['id'], 'attempt': attempt,
                   'event': 'validation' if mode == 'verified' else 'baseline_answer',
                   'errors': errors, 'answer': answer})
            if errors and mode == 'verified' and attempt < attempts:
                next_pending.append(question)
                correction.append({'id': question['id'], 'answer': raw, 'errors': errors})
                say('%s: отправим вопрос на исправление вместе с причинами ошибок.' % question['id'])
            elif errors:
                accepted[question['id']] = abstain(question, '; '.join(errors))
                if raw is None:
                    accepted[question['id']]['error_type'] = 'model_format'
                say('%s: попытки исправления закончились либо отключены; итог — «не знаю».' % question['id'])
            else:
                accepted[question['id']] = answer
                say('%s: итог — «%s»%s.' % (question['id'], str(answer.get('status', ''))[:60],
                    '; ' + str(answer['answer'])[:200] if answer.get('answer') else ''))
        say('Итерация %s завершена: зафиксировано %s/%s ответов, на исправление — %s.' %
            (attempt + 1, len(accepted), len(questions), len(next_pending)))
        pending = next_pending
        if not pending:
            break
    return accepted


def run(args):
    """Организует запуск, снимок входов, частичные результаты и итоговый статус."""
    load_env(ROOT / '.env')
    data = Path(args.data).resolve()
    if args.command == 'ask':
        questions = [{'id': args.package + '-custom', 'package': args.package, 'question': args.question}]
    else:
        questions = read_questions(data)
        if args.packages:
            selected = set(args.packages.split(','))
            unknown = selected - {q['package'] for q in questions}
            if unknown:
                raise ValueError('Unknown packages: ' + ', '.join(sorted(unknown)))
            questions = [q for q in questions if q['package'] in selected]
    grouped = {}
    for question in questions:
        grouped.setdefault(question['package'], []).append(question)
    package_data = {name: read_package(data, name) for name in grouped}
    input_data = {'questions': questions, 'packages': package_data}
    fingerprint = hashlib.sha256(json.dumps(input_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    output = Path(args.output or ROOT / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + args.mode)).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory is not empty; choose a new --output to preserve prior runs')
    if not 1 <= args.workers <= 2 or not 0 <= args.repair_attempts <= 3:
        raise ValueError('workers must be 1–2 and repair-attempts 0–3')
    if not 256 <= args.max_tokens <= 32768 or args.timeout <= 0:
        raise ValueError('max-tokens must be 256–32768 and timeout must be positive')
    client = Client(os.environ.get('ITFEST_API_KEY', ''),
                    os.environ.get('ITFEST_BASE_URL', 'https://ai.on-soft.ru/v1'),
                    os.environ.get('ITFEST_MODEL', 'qwen3-6-practice'), args.timeout, args.max_tokens)
    output.mkdir(parents=True, exist_ok=True)
    progress = RunLog(output / 'progress.log', emit, secrets=(client.key,))
    write_json(output / 'inputs.json', input_data)
    write_json(output / 'prompts.json', {'baseline': BASELINE, 'verified': VERIFIED})
    lock = threading.Lock()
    def trace(event):
        # Ключ не входит в промпт; дополнительная очистка защищает журнал от утечки.
        line = json.dumps(event, ensure_ascii=False).replace(client.key, '[redacted]')
        with lock:
            with (output / 'trace.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
    started = time.monotonic()
    manifest = {'model': client.model, 'base_url': client.base_url, 'mode': args.mode, 'temperature': 0,
                'max_tokens': args.max_tokens, 'input_fingerprint': fingerprint, 'timeout_seconds': args.timeout,
                'question_ids': [q['id'] for q in questions], 'created_at': datetime.now(timezone.utc).isoformat(),
                'repair_attempts': args.repair_attempts if args.mode == 'verified' else 0,
                'workers': args.workers, 'status': 'running', 'stats': dict(client.stats)}
    write_json(output / 'manifest.json', manifest)
    answers = {}
    progress('Старт: режим %s; модель %s; вопросов %s; закупок %s; рабочих потоков %s.' %
             (args.mode, client.model, len(questions), len(grouped), args.workers))
    progress('Входные данные и промпты сохранены. Эталоны не загружались; журнал этапов: ' + str(output / 'progress.log'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_package, client, args.mode, name, rows,
                               *package_data[name], args.repair_attempts, trace, progress): name for name, rows in grouped.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                answers.update(future.result())
            except Exception as exc:
                # Не печатаем детали запроса с секретами; сохраняем остальные закупки.
                reason = 'Internal package failure: ' + type(exc).__name__
                trace({'package': name, 'event': 'internal_error', 'error': reason})
                progress('[%s] Внутренняя ошибка: %s; сохраняем осторожные отказы.' % (name, reason))
                answers.update({q['id']: {**abstain(q, reason), 'error_type': 'internal'} for q in grouped[name]})
            save_answers(output, questions, answers)
            progress('[%s] Сохранены answers.csv и answers.json; готово %s/%s вопросов.' % (name, len(answers), len(questions)))
            manifest.update(stats=dict(client.stats), completed_questions=len(answers),
                            elapsed_seconds=round(time.monotonic() - started, 3))
            write_json(output / 'manifest.json', manifest)
    failures = [a for a in answers.values() if a.get('error_type')]
    manifest.update(status='completed_with_errors' if failures else 'completed',
                    elapsed_seconds=round(time.monotonic() - started, 3), stats=dict(client.stats))
    write_json(output / 'manifest.json', manifest)
    progress('Готово. CSV: ' + str(output / 'answers.csv'))
    progress('Запросов к API: %s; время: %.1f с; технически не обработано вопросов: %s. manifest.json и trace.jsonl сохранены.' %
             (client.stats['requests'], manifest['elapsed_seconds'], len(failures)))
    if args.command == 'ask':
        emit(json.dumps(answers[questions[0]['id']], ensure_ascii=False, indent=2))
    return 2 if failures else 0


def parser():
    """Описывает команды и допустимые параметры, показываемые через --help."""
    main = argparse.ArgumentParser(description='ИИ-помощник по закупкам: ответы, проверки и сравнение. Python 3.10+, без зависимостей.')
    commands = main.add_subparsers(dest='command', required=True)
    for command in ('run', 'ask'):
        sub = commands.add_parser(command, help='Обработать questions.csv' if command == 'run' else 'Задать свой вопрос')
        sub.add_argument('--data', default=str(ROOT / 'data' / 'public'), help='Папка с packages и questions.csv')
        sub.add_argument('--output', help='Новая папка результатов; существующие запуски не перезаписываются')
        sub.add_argument('--mode', choices=['baseline', 'verified'], default='verified')
        sub.add_argument('--workers', type=int, default=1)
        sub.add_argument('--repair-attempts', type=int, default=2)
        sub.add_argument('--max-tokens', type=int, default=16384)
        sub.add_argument('--timeout', type=float, default=300)
        if command == 'ask':
            sub.add_argument('--package', required=True)
            sub.add_argument('--question', required=True)
        else:
            sub.add_argument('--packages', help='Необязательный фильтр, например P01,P04')
    evaluate = commands.add_parser('evaluate', help='Запустить официальный оценщик отдельно от модели')
    evaluate.add_argument('--run', required=True)
    evaluate.add_argument('--data', default=str(ROOT / 'data' / 'public'))
    compare = commands.add_parser('compare', help='Сравнить оценённые запуски одной модели на одних входах')
    compare.add_argument('--baseline', required=True)
    compare.add_argument('--verified', required=True)
    compare.add_argument('--output', required=True)
    return main


def main(argv=None):
    """Точка входа: выбор команды, русский вывод и понятный код завершения."""
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        if args.command in ('run', 'ask'):
            return run(args)
        from evaluation import evaluate_run, compare_runs
        if args.command == 'evaluate':
            evaluate_run(Path(args.run).resolve(), Path(args.data).resolve())
        else:
            report = compare_runs(Path(args.baseline).resolve(), Path(args.verified).resolve(), Path(args.output).resolve())
            emit(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, ApiError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
