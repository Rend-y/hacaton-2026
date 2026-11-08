"""Адаптер публичного эталонного набора для проверки качества.

Формат эталонного набора обрабатывает этот адаптер. Обычная генерация принимает
произвольные документы через input_loader и не использует этот модуль.
"""
import csv
import io
import re


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
