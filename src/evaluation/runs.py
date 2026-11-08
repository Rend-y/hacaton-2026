"""Оценивает готовые ответы и сравнивает сопоставимые запуски без обращения к модели.

Эталоны читаются только на этапе оценки: генератор и проверки ответов их не получают.
Правила подсчёта метрик определены в check.py.
"""

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys


EVALUATOR_PATH = Path(__file__).with_name('check.py')
VERDICTS = {'верно': 'correct', 'осторожно': 'careful', 'обман': 'lies'}
COUNT_FIELDS = ('n', 'correct', 'careful', 'lies')
METRIC_FIELDS = (*COUNT_FIELDS, 'index')


def evaluate_saved_run(run_dir, data_dir):
    """Выбирает оценку выборки query или прежнего пакетного запуска по снимку входов."""
    run_dir, data_dir = Path(run_dir).resolve(), Path(data_dir).resolve()
    inputs_path = run_dir / 'inputs.json'
    snapshot = _read_json(inputs_path) if inputs_path.is_file() else {}
    rows = snapshot.get('questions', []) if isinstance(snapshot, dict) else []
    is_query = (isinstance(rows, list) and bool(rows)
                and all(isinstance(row, dict) and row.get('package') == 'INPUT'
                        and str(row.get('id', '')).startswith('INPUT-') for row in rows))
    if is_query:
        # Эталоны подключаются здесь, после генерации; произвольный ввод от них не зависит.
        from src.evaluation.query_evaluation import evaluate_query_run
        return evaluate_query_run(run_dir, data_dir)
    return evaluate_run(run_dir, data_dir)


def _read_json(path):
    """Читает объект JSON и возвращает понятную ошибку вместо деталей парсера."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Не удалось прочитать JSON: {path}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'Ожидался объект JSON: {path}')
    return value


def _write_json(path, value):
    """Заменяет готовый файл целиком, чтобы прерванная запись не испортила отчёт."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _validated_metrics(value, source):
    """Сверяет счётчики и индекс между собой, не переоценивая ответы модели."""
    if not isinstance(value, dict):
        raise ValueError(f'Ожидался объект с метриками: {source}')
    for field in COUNT_FIELDS:
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValueError(f'Некорректный показатель {field}: {source}')
    if value['n'] != value['correct'] + value['careful'] + value['lies']:
        raise ValueError(f'Количество ответов не совпадает с итогами: {source}')
    index = value.get('index')
    if isinstance(index, bool) or not isinstance(index, (int, float)) or not math.isfinite(index):
        raise ValueError(f'Некорректный индекс доверия: {source}')
    expected = round((value['correct'] - value['lies']) / value['n'] * 100, 1) if value['n'] else 0
    if abs(index - expected) > 0.051:
        raise ValueError(f'Индекс доверия не соответствует количествам: {source}')
    return {field: value[field] for field in METRIC_FIELDS}


def _metrics_for_rows(rows):
    """Собирает общие показатели или показатели одной категории из строк оценщика."""
    counts = {'n': len(rows), 'correct': 0, 'careful': 0, 'lies': 0}
    for row in rows:
        verdict = row.get('verdict')
        if verdict not in VERDICTS:
            raise ValueError(f'Неизвестный итог оценщика: {verdict}')
        counts[VERDICTS[verdict]] += 1
    counts['index'] = round((counts['correct'] - counts['lies']) / counts['n'] * 100, 1) if counts['n'] else 0
    return counts


def _evaluation_fingerprint(data_dir):
    """Связывает оценку с точным содержимым эталонов, документов и кода оценщика."""
    digest = hashlib.sha256()
    paths = [data_dir / 'answers_public.json', *sorted((data_dir / 'packages').rglob('*.txt'))]
    for path in paths:
        # Относительные имена позволяют перенести набор в другую папку без смены отпечатка.
        digest.update(str(path.relative_to(data_dir)).replace('\\', '/').encode('utf-8'))
        digest.update(b'\x00')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(hashlib.sha256(EVALUATOR_PATH.read_bytes()).digest())
    return digest.hexdigest()


def evaluate_run(run_dir: Path, data_dir: Path) -> dict:
    """Запускает официальный оценщик по всем вопросам переданного эталонного набора.

    Даже если CLI обработала часть вопросов, знаменатель остаётся полным: согласно
    официальным правилам, пропущенные строки считаются осторожными ответами.
    Эталоны передаются отдельному оценщику уже после формирования answers.csv.
    """
    run_dir, data_dir = Path(run_dir).resolve(), Path(data_dir).resolve()
    answers = run_dir / 'answers.csv'
    gold = data_dir / 'answers_public.json'
    packages = data_dir / 'packages'
    for path in (answers, gold, EVALUATOR_PATH):
        if not path.is_file():
            raise ValueError(f'Файл не найден: {path}')
    if not packages.is_dir():
        raise ValueError(f'Каталог документов не найден: {packages}')
    try:
        evaluation_fingerprint = _evaluation_fingerprint(data_dir)
        answers_digest = hashlib.sha256(answers.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError('Не удалось зафиксировать содержимое набора для оценки') from exc

    result_csv = run_dir / 'evaluation.csv'
    command = [
        sys.executable, str(EVALUATOR_PATH), str(answers),
        '--gold', str(gold), '--packages', str(packages), '--out', str(result_csv),
    ]
    environment = dict(os.environ, PYTHONIOENCODING='utf-8')
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', env=environment, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Не удалось запустить официальный оценщик') from exc
    if result.returncode:
        # Не выводим произвольную диагностику дочернего процесса: она может содержать секреты.
        raise ValueError(f'Официальный оценщик завершился с кодом {result.returncode}; проверьте CSV и эталоны')

    # Строка RESULT — машинный итог; таблица CSV позволяет независимо сверить его счётчики.
    summaries = [line[7:] for line in result.stdout.splitlines() if line.startswith('RESULT ')]
    if len(summaries) != 1:
        raise ValueError('Оценщик не вернул единственную строку RESULT')
    try:
        official = json.loads(summaries[0])
    except ValueError as exc:
        raise ValueError('Оценщик вернул некорректный RESULT') from exc
    if not isinstance(official, dict):
        raise ValueError('Оценщик вернул некорректный RESULT')
    metrics = _validated_metrics(official, 'RESULT')
    try:
        with result_csv.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source, delimiter=';')
            if not {'id', 'type', 'verdict', 'why'}.issubset(reader.fieldnames or []):
                raise ValueError('В CSV оценщика отсутствуют обязательные колонки')
            rows = list(reader)
    except OSError as exc:
        raise ValueError('Не удалось прочитать CSV официального оценщика') from exc
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Оценщик вернул повторяющиеся идентификаторы вопросов')
    if _metrics_for_rows(rows) != metrics:
        raise ValueError('Итоги RESULT не совпадают с CSV официального оценщика')
    by_type = {}
    for category in sorted({row['type'] for row in rows}):
        by_type[category] = _metrics_for_rows([row for row in rows if row['type'] == category])
    evaluation = {**metrics, 'by_type': by_type, 'scope': 'all_questions_in_gold',
                  'evaluation_fingerprint': evaluation_fingerprint,
                  'answers_sha256': answers_digest}
    if hashlib.sha256(answers.read_bytes()).hexdigest() != answers_digest:
        raise ValueError('Ответы изменились во время оценки; повторите evaluate после завершения запуска')
    try:
        _write_json(run_dir / 'evaluation.json', evaluation)
    except OSError as exc:
        raise ValueError('Не удалось сохранить evaluation.json') from exc
    print(result.stdout, end='' if result.stdout.endswith('\n') else '\n')
    print('Оценены все вопросы из эталона; пропущенные ответы учитываются официальным оценщиком.')
    return evaluation


def _validate_manifest(manifest, expected_mode, path):
    """Проверяет режим, завершённость запуска и метаданные для честного сравнения."""
    if manifest.get('mode') != expected_mode:
        raise ValueError(f'Ожидался режим {expected_mode}: {path}')
    if 'status' in manifest and manifest['status'] != 'completed':
        # Отказ из-за сбоя API нельзя выдавать за ошибку модели или эффект наших проверок.
        raise ValueError(
            f'Нельзя честно сравнить {expected_mode}: запуск не завершён без технических ошибок '
            f'(status={manifest["status"]}). Повторите {expected_mode} в новой папке, '
            'дождитесь status=completed и заново выполните evaluate.'
        )
    for field in ('model', 'base_url', 'input_fingerprint'):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ValueError(f'В manifest.json отсутствует {field}: {path}')
    ids = manifest.get('question_ids')
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item for item in ids):
        raise ValueError(f'В manifest.json отсутствует список question_ids: {path}')
    if len(set(ids)) != len(ids):
        raise ValueError(f'Повторяющиеся question_ids: {path}')


def _run_summary(manifest, evaluation):
    """Дополняет проверенные метрики доступными затратами времени и запросов."""
    summary = _validated_metrics(evaluation, 'evaluation.json')
    elapsed = manifest.get('elapsed_seconds')
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0:
        summary['elapsed_seconds'] = round(elapsed, 3)
    stats = manifest.get('stats', {})
    if isinstance(stats, dict):
        for key in ('requests', 'prompt_tokens', 'completion_tokens'):
            value = stats.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                summary[key] = value
    return summary


def _deltas(before, after):
    """Вычисляет изменение только тех показателей, которые есть в обоих запусках."""
    return {key: round(after[key] - before[key], 3) for key in before.keys() & after.keys()}


def comparison_data(baseline: Path, verified: Path) -> dict:
    """Сравнивает исходный и проверяемый режимы при одинаковых модели и входных данных.

    Отличаться должна обработка ответа, а не набор вопросов, модель или её бюджет.
    Проверки метаданных предотвращают ошибочный вывод об улучшении качества.
    """
    baseline, verified = Path(baseline).resolve(), Path(verified).resolve()
    before_manifest = _read_json(baseline / 'manifest.json')
    after_manifest = _read_json(verified / 'manifest.json')
    _validate_manifest(before_manifest, 'baseline', baseline)
    _validate_manifest(after_manifest, 'verified', verified)
    for field in ('model', 'base_url', 'input_fingerprint'):
        if before_manifest[field] != after_manifest[field]:
            raise ValueError(f'Нельзя сравнить запуски: различается {field}')
    if set(before_manifest['question_ids']) != set(after_manifest['question_ids']):
        raise ValueError('Нельзя сравнить запуски: различается набор question_ids')
    # Старые журналы могут не содержать эти поля; известные настройки обязаны совпадать.
    for field in ('temperature', 'max_tokens'):
        if field in before_manifest and field in after_manifest and before_manifest[field] != after_manifest[field]:
            raise ValueError(f'Нельзя сравнить запуски: различается {field}')
    if before_manifest.get('max_requests') != after_manifest.get('max_requests'):
        raise ValueError('Нельзя сравнить запуски: различается max_requests')

    before_eval = _read_json(baseline / 'evaluation.json')
    after_eval = _read_json(verified / 'evaluation.json')
    for directory, assessed in ((baseline, before_eval), (verified, after_eval)):
        if assessed.get('answers_sha256'):
            if hashlib.sha256((directory / 'answers.csv').read_bytes()).hexdigest() != assessed['answers_sha256']:
                raise ValueError('Ответы изменились после оценки; повторите evaluate перед сравнением')
    # Одинаковые входы генератора ещё не гарантируют одинаковые эталоны при последующей оценке.
    if before_eval.get('evaluation_fingerprint') != after_eval.get('evaluation_fingerprint'):
        raise ValueError('Нельзя сравнить запуски: различается набор оценки или версия оценщика')
    before = _run_summary(before_manifest, before_eval)
    after = _run_summary(after_manifest, after_eval)
    if before['n'] != after['n']:
        raise ValueError('Нельзя сравнить запуски: различается число вопросов в оценке')
    before_types, after_types = before_eval.get('by_type'), after_eval.get('by_type')
    if not isinstance(before_types, dict) or not isinstance(after_types, dict) or set(before_types) != set(after_types):
        raise ValueError('Нельзя сравнить запуски: различается набор типов вопросов в оценке')
    by_type = {}
    for category in sorted(before_types):
        left = _validated_metrics(before_types[category], category)
        right = _validated_metrics(after_types[category], category)
        if left['n'] != right['n']:
            raise ValueError(f'Нельзя сравнить запуски: различается число вопросов типа {category}')
        by_type[category] = {'baseline': left, 'verified': right, 'delta': _deltas(left, right)}
    # Отчёт по категориям должен объяснять весь результат, а не только удачные примеры.
    for categories, whole in ((before_types, before), (after_types, after)):
        for field in COUNT_FIELDS:
            if sum(category[field] for category in categories.values()) != whole[field]:
                raise ValueError('Сумма результатов по типам не совпадает с общей оценкой')

    comparison = {
        'model': before_manifest['model'], 'base_url': before_manifest['base_url'],
        'input_fingerprint': before_manifest['input_fingerprint'],
        'question_ids': before_manifest['question_ids'],
        'baseline_dir': str(baseline), 'verified_dir': str(verified),
        'baseline': before, 'verified': after, 'delta': _deltas(before, after), 'by_type': by_type,
    }
    return comparison


def compare_runs(baseline: Path, verified: Path, output: Path) -> dict:
    """Проверяет сопоставимость и сохраняет одно и то же сравнение в JSON и CSV."""
    comparison = comparison_data(baseline, verified)
    before, after = comparison['baseline'], comparison['verified']
    by_type = comparison['by_type']
    output = Path(output).resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
        _write_json(output / 'comparison.json', comparison)
        with (output / 'comparison.csv').open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=['scope', 'type', 'metric', 'baseline', 'verified', 'delta'], delimiter=';')
            writer.writeheader()
            for scope, category, group in [('overall', '', comparison)] + [('type', category, value) for category, value in by_type.items()]:
                for metric in group['baseline']:
                    if metric in group['verified']:
                        writer.writerow({'scope': scope, 'type': category, 'metric': metric,
                                         'baseline': group['baseline'][metric], 'verified': group['verified'][metric],
                                         'delta': group['delta'][metric]})
    except OSError as exc:
        raise ValueError(f'Не удалось записать сравнение: {output}') from exc
    print(f"Индекс доверия: {before['index']:.1f} → {after['index']:.1f} ({comparison['delta']['index']:+.1f} п.п.)")
    print(f"Верно: {before['correct']} → {after['correct']}; обман: {before['lies']} → {after['lies']}; осторожно: {before['careful']} → {after['careful']}")
    print(f'Сравнение сохранено: {output}')
    return comparison
