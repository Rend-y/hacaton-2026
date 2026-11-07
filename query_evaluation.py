"""Официальная оценка выбранных вопросов query, когда их эталоны известны.

Сопоставление до генерации использует только тексты вопросов и документов.
Правильные ответы читаются исключительно после сохранения ответа модели.
"""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

import evaluation


def _question_key(value):
    """Убирает оформление, но сохраняет слова, числа, отрицания и единицы измерения."""
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    return ' '.join(re.findall(r'\w+', value, flags=re.UNICODE))


# Это две явно проверенные короткие формулировки из пользовательского примера.
# Единицы в произвольных вопросах не удаляются: рабочие дни нельзя принять за календарные.
QUESTION_ALIASES = {
    _question_key('Какой срок выполнения работ?'):
        _question_key('Какой срок выполнения работ, в календарных днях?'),
    _question_key('Какой гарантийный срок на результаты работ?'):
        _question_key('Какой гарантийный срок на результаты работ, в месяцах?'),
}


def _document_signature(documents):
    """Сверяет содержимое всего комплекта; переименование файлов не меняет документы."""
    if not isinstance(documents, dict) or not documents:
        raise ValueError('Для оценки нужен непустой снимок документов.')
    hashes = []
    for name, text in documents.items():
        if not isinstance(name, str) or not name or not isinstance(text, str) or not text.strip():
            raise ValueError('Некорректный документ в снимке запуска.')
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        hashes.append(hashlib.sha256(normalized.encode('utf-8')).hexdigest())
    # Counter сохраняет число одинаковых файлов, поэтому неполный комплект не совпадёт.
    return Counter(hashes)


def _signature_digest(signature):
    return hashlib.sha256(json.dumps(sorted(signature.items())).encode('utf-8')).hexdigest()


def plan_query_evaluation(questions, documents, read_errors, data_dir):
    """Находит однозначные соответствия, не читая answers_public.json и не вызывая ИИ."""
    from reference_data import read_questions, read_package

    if read_errors:
        raise ValueError('Нельзя оценить query по эталону: документы прочитаны не полностью.')
    if not isinstance(questions, list) or not questions:
        raise ValueError('В снимке нет вопросов для оценки.')
    ids = []
    for item in questions:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                or not isinstance(item.get('question'), str) or not item['question'].strip()):
            raise ValueError('Некорректные вопросы в снимке запуска.')
        ids.append(item['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('В снимке повторяются ID вопросов.')

    data_dir = Path(data_dir).resolve()
    reference_questions = read_questions(data_dir)
    wanted = _document_signature(documents)
    packages = []
    for package in sorted({item['package'] for item in reference_questions}):
        candidate, errors = read_package(data_dir, package)
        if candidate and not errors and _document_signature(candidate) == wanted:
            packages.append(package)
    if len(packages) != 1:
        raise ValueError('Для --evaluate весь комплект документов должен однозначно совпадать с одной закупкой эталонного набора. Оценка произвольных документов без эталона недоступна.')
    package = packages[0]
    candidates = [item for item in reference_questions if item['package'] == package]
    matches, used = [], set()
    for item in questions:
        key = _question_key(item['question'])
        found = [row for row in candidates if _question_key(row['question']) == key]
        method = 'normalized'
        if not found and key in QUESTION_ALIASES:
            found = [row for row in candidates if _question_key(row['question']) == QUESTION_ALIASES[key]]
            method = 'explicit_alias'
        if len(found) != 1:
            raise ValueError('Для вопроса %s не найден единственный эталонный вопрос в %s. Используйте точную формулировку из questions.csv: %s' % (item['id'], package, item['question']))
        reference = found[0]
        if reference['id'] in used:
            raise ValueError('Несколько вопросов соответствуют одному эталону %s; уберите повтор для честного знаменателя.' % reference['id'])
        used.add(reference['id'])
        matches.append({'id': item['id'], 'reference_id': reference['id'], 'question': item['question'],
                        'reference_question': reference['question'], 'match': method})
    return {'package': package, 'source_question_count': len(reference_questions),
            'matches': matches, 'document_fingerprint': _signature_digest(wanted)}


def _load_snapshot(run_dir):
    """Проверяет завершённый запуск и привязку его вопросов к использованному контексту."""
    snapshot = evaluation._read_json(run_dir / 'inputs.json')
    manifest = evaluation._read_json(run_dir / 'manifest.json')
    if manifest.get('status') not in ('completed', 'completed_with_errors'):
        raise ValueError('Сначала дождитесь завершения query; текущий запуск ещё нельзя оценить.')
    questions = snapshot.get('questions')
    packages = snapshot.get('packages')
    if (not isinstance(questions, list) or not questions or not isinstance(packages, dict)
            or set(packages) != {'INPUT'}):
        raise ValueError('Ожидался снимок query с одним комплектом INPUT.')
    ids = []
    for item in questions:
        if not isinstance(item, dict) or item.get('package') != 'INPUT' or not isinstance(item.get('id'), str):
            raise ValueError('Некорректная привязка вопросов query к документам.')
        ids.append(item['id'])
    if len(ids) != len(set(ids)) or manifest.get('question_ids') != ids:
        raise ValueError('Вопросы inputs.json не совпадают с manifest.json.')
    package_data = packages['INPUT']
    if (not isinstance(package_data, list) or len(package_data) != 2
            or not isinstance(package_data[0], dict) or not isinstance(package_data[1], list)):
        raise ValueError('Некорректный снимок документов INPUT.')
    fingerprint = hashlib.sha256(json.dumps({'questions': questions, 'packages': packages},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if manifest.get('input_fingerprint') != fingerprint:
        raise ValueError('Снимок вопросов или документов изменился после генерации; исходная оценка недостоверна.')
    return questions, package_data[0], package_data[1]


def _validate_answer_ids(run_dir, questions):
    """Оценщик не должен тихо игнорировать лишние или повторяющиеся строки выгрузки."""
    with (run_dir / 'answers.csv').open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        if not {'id', 'status', 'answer', 'quote', 'file'} <= set(reader.fieldnames or []):
            raise ValueError('В answers.csv отсутствуют обязательные колонки.')
        ids = [row.get('id') for row in reader]
    expected = {item['id'] for item in questions}
    if len(ids) != len(set(ids)) or any(item not in expected for item in ids):
        raise ValueError('В answers.csv есть лишние или повторяющиеся ID.')


def evaluate_query_run(run_dir, data_dir):
    """Оценивает сохранённые ответы только по найденной выборке официального эталона."""
    from reference_data import read_package

    run_dir, data_dir = Path(run_dir).resolve(), Path(data_dir).resolve()
    questions, documents, read_errors = _load_snapshot(run_dir)
    plan = plan_query_evaluation(questions, documents, read_errors, data_dir)
    _validate_answer_ids(run_dir, questions)

    # Только здесь, после генерации и проверки снимка, читаются правильные ответы.
    try:
        gold = json.loads((data_dir / 'answers_public.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ValueError('Не удалось прочитать answers_public.json для оценки query.') from exc
    if not isinstance(gold, list):
        raise ValueError('Ожидался список эталонных ответов в answers_public.json.')
    by_id = {}
    for item in gold:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                or item['id'] in by_id):
            raise ValueError('Некорректные или повторяющиеся ID в эталонных ответах.')
        by_id[item['id']] = item
    selected = []
    for match in plan['matches']:
        reference = by_id.get(match['reference_id'])
        if (reference is None or reference.get('package') != plan['package']
                or not isinstance(reference.get('question'), str)
                or _question_key(reference['question']) != _question_key(match['reference_question'])):
            raise ValueError('Эталонные ответы и questions.csv расходятся для %s.' % match['reference_id'])
        # Меняется лишь ID для связи с сохранённым INPUT-ответом; эталонное значение неизменно.
        selected.append({**reference, 'id': match['id']})

    # Официальный скрипт оценивает весь переданный gold. Передаём ему ровно выбранные
    # вопросы и полный неизменённый комплект документов, не весь набор из 71 вопроса.
    with tempfile.TemporaryDirectory(prefix='query-evaluation-', dir=run_dir) as temporary:
        subset = Path(temporary)
        evaluation._write_json(subset / 'answers_public.json', selected)
        target = subset / 'packages' / plan['package']
        target.mkdir(parents=True)
        for path in sorted((data_dir / 'packages' / plan['package']).glob('*.txt')):
            if path.is_symlink():
                raise ValueError('Документы эталонного набора изменились во время оценки.')
            shutil.copyfile(path, target / path.name)
        copied, errors = read_package(subset, plan['package'])
        if errors or _document_signature(copied) != _document_signature(documents):
            raise ValueError('Документы эталонного набора изменились во время оценки.')
        result = evaluation.evaluate_run(run_dir, subset)
    if result['n'] != len(questions):
        raise ValueError('Оценщик вернул неверное число вопросов выборки.')
    result.update(scope='matched_query_questions', source_question_count=plan['source_question_count'],
                  source_package=plan['package'], question_mapping=plan['matches'],
                  document_fingerprint=plan['document_fingerprint'])
    evaluation._write_json(run_dir / 'evaluation.json', result)
    print('Оценена выборка: %s из %s вопросов публичного набора (%s). Индекс относится только к этой выборке.' %
          (result['n'], plan['source_question_count'], plan['package']))
    return result
