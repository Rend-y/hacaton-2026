"""Собирает проверяемые данные для автономного HTML-отчёта без вызова модели.

Статус ответа модели, локальная проверка и официальный вердикт — разные сущности.
Индекс доверия появляется только после оценки по эталону; пропуски не становятся
нулевыми измерениями, а технические сбои остаются видимыми.
"""
import csv
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.evaluation.runs import _metrics_for_rows, _validated_metrics, comparison_data


def _json(path, expected_type=dict, optional=False):
    """Читает файл ожидаемого типа; отсутствие необязательного файла допустимо."""
    if optional and not path.exists():
        return expected_type()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError('Не удалось прочитать данные отчёта: ' + path.name) from exc
    if not isinstance(data, expected_type):
        raise ValueError('Некорректная структура данных: ' + path.name)
    return data


def _csv(path, fields):
    """Читает CSV и не допускает тихой потери строк из-за повторяющихся ID."""
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        if not set(fields) <= set(reader.fieldnames or []):
            raise ValueError('В CSV не хватает колонок: ' + path.name)
        rows = list(reader)
    ids = [r.get('id') for r in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError('Некорректные или повторяющиеся ID: ' + path.name)
    return rows


def _flat(value):
    return re.sub(r'\s+', ' ', value).strip() if isinstance(value, str) else ''


def _projection(answer):
    """Сверяет подробный JSON с форматом CSV, который реально сдаётся организатору."""
    status = answer.get('status') if isinstance(answer.get('status'), str) else ''
    evidence = answer.get('evidence')
    first = evidence[0] if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict) else {}
    empty = status in ('не знаю', 'нет в документах')
    return {'id': answer.get('id'), 'status': status,
            'answer': '' if empty else _flat(answer.get('answer')),
            'quote': '' if empty else _flat(first.get('quote')),
            'file': '' if empty else (first.get('file') if isinstance(first.get('file'), str) else '')}


def _trace(path, warnings):
    """Читает только события проверок; внутренние рассуждения не нужны отчёту."""
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            warnings.append('В журнале есть незавершённая строка; история проверок может быть неполной.')
            continue
        if isinstance(event, dict):
            result.append(event)
    return result


def _number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def build_run_model(run_dir: Path) -> dict:
    """Загружает один запуск и сверяет источники перед отображением чисел."""
    root = Path(run_dir).resolve()
    manifest = _json(root / 'manifest.json')
    exported = _csv(root / 'answers.csv', ('id', 'status', 'answer', 'quote', 'file'))
    detailed = _json(root / 'answers.json', list, optional=True)
    warnings = []
    detail_map = {}
    for item in detailed:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in detail_map:
            raise ValueError('Некорректные или повторяющиеся ответы в answers.json')
        detail_map[item['id']] = item
    if (root / 'answers.json').exists():
        if set(detail_map) != {r['id'] for r in exported}:
            raise ValueError('Набор ответов answers.json не совпадает с answers.csv')
        for row in exported:
            if any(_projection(detail_map[row['id']])[k] != row[k] for k in ('id', 'status', 'answer', 'quote', 'file')):
                raise ValueError('Ответы answers.json и answers.csv различаются: ' + row['id'])
    inputs = _json(root / 'inputs.json', optional=True)
    question_map = {q['id']: q for q in inputs.get('questions', []) if isinstance(q, dict) and isinstance(q.get('id'), str)}
    selected_ids = manifest.get('question_ids', [r['id'] for r in exported])
    if not isinstance(selected_ids, list) or any(not isinstance(item, str) for item in selected_ids) or len(selected_ids) != len(set(selected_ids)):
        raise ValueError('Некорректные question_ids в manifest.json')

    quality = None
    verdicts = {}
    if (root / 'evaluation.json').exists():
        assessed = _json(root / 'evaluation.json')
        digest = assessed.get('answers_sha256')
        if digest and digest != hashlib.sha256((root / 'answers.csv').read_bytes()).hexdigest():
            raise ValueError('Ответы изменились после оценки; повторите evaluate')
        if not digest:
            warnings.append('Старая оценка не содержит отпечатка ответов. Повторите evaluate для подтверждения актуальности.')
        metrics = _validated_metrics(assessed, 'evaluation.json')
        rows = _csv(root / 'evaluation.csv', ('id', 'type', 'verdict', 'why'))
        if _metrics_for_rows(rows) != metrics:
            raise ValueError('Итоги evaluation.csv не совпадают с evaluation.json')
        by_type = {category: _metrics_for_rows([r for r in rows if r['type'] == category]) for category in sorted({r['type'] for r in rows})}
        if assessed.get('by_type') != by_type:
            raise ValueError('Разбивка по типам не совпадает с evaluation.csv')
        quality = {**metrics, 'by_type': by_type}
        if assessed.get('scope') == 'matched_query_questions':
            warnings.append('Оценена выбранная группа. Вопросов в оценке: %s; во всём публичном наборе: %s. Индекс относится только к этой группе.' %
                            (metrics['n'], assessed.get('source_question_count', 'неизвестное число')))
        verdicts = {r['id']: r for r in rows}
        if set(verdicts) != set(selected_ids):
            warnings.append('Оценщик проверил весь эталон: %s вопросов; в запуск выбрано %s. Пропуски входят в знаменатель.' % (metrics['n'], len(selected_ids)))
        outside = set(selected_ids) - set(verdicts)
        if outside:
            warnings.append('Вопросов вне эталона: %s. Их качество официальным оценщиком не измерено.' % len(outside))
    else:
        warnings.append('Оценка по эталону не выполнена. Статусы модели и локальные проверки не являются измеренной точностью.')

    trace = _trace(root / 'trace.jsonl', warnings)
    answers = []
    technical_errors = 0
    for row in exported:
        detail = detail_map.get(row['id'], {})
        question = question_map.get(row['id'], {})
        verdict = verdicts.get(row['id'], {})
        reason = detail.get('reason', '')
        error_type = detail.get('error_type', '')
        # Первые версии CLI записывали техническую причину без отдельного поля типа.
        if not error_type and isinstance(reason, str) and reason.startswith(('Model API', 'Internal package failure')):
            error_type = 'technical'
        if error_type:
            technical_errors += 1
        evidence = detail.get('evidence', [])
        if not isinstance(evidence, list):
            evidence = []
        evidence = [e for e in evidence if isinstance(e, dict) and isinstance(e.get('file'), str) and isinstance(e.get('quote'), str)]
        if not evidence and row.get('quote'):
            evidence = [{'file': row['file'], 'quote': row['quote']}]
        answers.append({**row, 'question': question.get('question', 'Текст вопроса отсутствует в снимке'),
                        'package': question.get('package', row['id'].split('-')[0]),
                        'reason': reason if isinstance(reason, str) else '', 'evidence': evidence,
                        'verdict': verdict.get('verdict'), 'why': verdict.get('why', ''),
                        'type': verdict.get('type', ''), 'error_type': str(error_type or '')})
    exported_ids = {r['id'] for r in exported}
    for identifier, row in verdicts.items():
        if identifier not in exported_ids:
            answers.append({'id': identifier, 'question': 'Вопрос из эталона: строка ответа отсутствует',
                            'package': identifier.split('-')[0], 'status': 'нет строки', 'answer': '',
                            'reason': '', 'evidence': [], 'verdict': row['verdict'], 'why': row['why'],
                            'type': row['type'], 'error_type': ''})
    if manifest.get('status') != 'completed' or technical_errors:
        problem = ('Есть технические отказы по %s вопросам.' % technical_errors if technical_errors
                   else 'Успешное завершение запуска не подтверждено.')
        warnings.append(problem + ' Метрики описывают сохранённую выгрузку. Для честного сравнения «было/стало» повторите запуск без технических ошибок.')
    final_answers = {r['id']: r for r in answers}
    checks = []
    for event in trace:
        if event.get('event') == 'validation' and isinstance(event.get('errors'), list) and event['errors']:
            checks.append({'id': str(event.get('id', '')), 'attempt': event.get('attempt', 0),
                           'errors': [str(e) for e in event['errors']],
                           'before': event.get('answer') if isinstance(event.get('answer'), dict) else {},
                           'after': final_answers.get(event.get('id'), {})})
    # Одна итерация исправления может содержать несколько HTTP-повторов. Считаем
    # её один раз, включая неуспешные обращения; старые журналы содержат response.
    repair_requests = len({(e.get('package'), e.get('batch', 'root'), e.get('attempt')) for e in trace
                           if isinstance(e.get('attempt'), int) and e['attempt'] > 0
                           and (e.get('event') in ('api_request', 'api_error') or 'response' in e)})
    stats = manifest.get('stats') if isinstance(manifest.get('stats'), dict) else {}
    operations = {name: _number(stats.get(name)) for name in ('requests', 'prompt_tokens', 'completion_tokens')}
    operations.update(elapsed_seconds=_number(manifest.get('elapsed_seconds')),
                      technical_errors=technical_errors, repair_requests=repair_requests)
    config_fields = ('model', 'mode', 'temperature', 'max_tokens', 'context_token_limit',
                     'max_questions_per_request', 'timeout_seconds', 'workers', 'repair_attempts',
                     'created_at', 'input_fingerprint')
    run = {'label': 'Текущий запуск', 'mode': manifest.get('mode', ''), 'model': manifest.get('model', ''),
           'status': manifest.get('status', ''), 'n_questions': len(selected_ids), 'quality': quality,
           'operations': operations, 'config': {key: manifest[key] for key in config_fields if key in manifest},
           'answers': answers, 'checks': checks}
    files = ['answers.csv', 'answers.json', 'evaluation.csv', 'evaluation.json', 'manifest.json', 'prompts.json', 'progress.log']
    return {'title': 'Отчёт о проверке ответов ИИ', 'kind': 'run', 'generated_at': _now(),
            'warnings': list(dict.fromkeys(warnings)), 'runs': [run], 'comparison': None,
            'source_files': [{'name': name, 'href': name} for name in files if (root / name).is_file()]}


def _now():
    return datetime.now(timezone(timedelta(hours=3), 'MSK')).strftime('%d.%m.%Y %H:%M МСК')


def build_comparison_model(comparison_dir: Path) -> dict:
    """Повторно проверяет сопоставимость источников, не доверяя старому снимку метрик."""
    root = Path(comparison_dir).resolve()
    saved = _json(root / 'comparison.json')
    directories = []
    for key in ('baseline_dir', 'verified_dir'):
        if not isinstance(saved.get(key), str) or not saved[key]:
            raise ValueError('В сравнении отсутствует ' + key)
        path = Path(saved[key])
        directories.append(path if path.is_absolute() else root / path)
    current = comparison_data(*directories)
    for key in ('model', 'base_url', 'input_fingerprint', 'question_ids', 'baseline', 'verified', 'delta', 'by_type'):
        if saved.get(key) != current[key]:
            raise ValueError('Источники сравнения изменились; повторите compare')
    models = [build_run_model(path) for path in directories]
    runs = []
    warnings = []
    for label, model in zip(('Было · baseline', 'Стало · verified'), models):
        run = model['runs'][0]
        run['label'] = label
        runs.append(run)
        warnings.extend(label + ': ' + message for message in model['warnings'])
    return {'title': 'Было и стало: качество ответов ИИ', 'kind': 'comparison', 'generated_at': _now(),
            'warnings': warnings, 'runs': runs, 'comparison': current,
            'source_files': [{'name': name, 'href': name} for name in ('comparison.csv', 'comparison.json') if (root / name).is_file()]}


def _save(root, model):
    """Файл самодостаточен: CSS и сценарии включены, интернет для просмотра не нужен."""
    from src.reporting.report_view import render_report
    path = root / 'report.html'
    temporary = root / 'report.html.tmp'
    temporary.write_text(render_report(model), encoding='utf-8')
    temporary.replace(path)
    return path


def write_run_report(run_dir: Path) -> Path:
    root = Path(run_dir).resolve()
    return _save(root, build_run_model(root))


def write_comparison_report(comparison_dir: Path) -> Path:
    root = Path(comparison_dir).resolve()
    return _save(root, build_comparison_model(root))
