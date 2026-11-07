"""Чтение произвольных документов и вопросов для CLI без структуры packages/P01.

Файлы не исполняются и не изменяются. Непрочитанные документы перечисляются
отдельно, чтобы модель не выдавала неполный поиск за доказанное отсутствие данных.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


MAX_FILES = 500
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_PDF_PAGES = 300
MAX_PDF_STREAM_BYTES = 8 * 1024 * 1024
MAX_QUESTIONS = 200
MAX_QUESTION_CHARS = 10_000
TEXT_SUFFIXES = {'.txt', '.md', '.rst', '.csv', '.json', '.html', '.htm'}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {'.docx', '.pdf'}
SERVICE_DIRS = {'__pycache__', 'node_modules', 'venv', '.venv'}
# Эти файлы являются результатами/эталонами, а не доказательствами для ответа.
SERVICE_FILES = {'answers_public.json', 'answers.json', 'answers.csv', 'evaluation.json',
                 'evaluation.csv', 'manifest.json', 'inputs.json', 'prompts.json',
                 'trace.jsonl', 'progress.log', 'report.html', 'comparison.json',
                 'comparison.csv', 'questions.csv'}


def _safe_label(value):
    """Убирает управляющие коды из имён, показываемых в терминале и модели."""
    return ''.join(char if char.isprintable() else '_' for char in str(value))


def _is_link(path):
    """На Windows junction также является ссылкой, хотя is_symlink может дать False."""
    details = path.lstat()
    return path.is_symlink() or bool(getattr(details, 'st_file_attributes', 0) &
                                     getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _decode(raw):
    """Поддерживает русские текстовые кодировки, не превращая бинарные данные в текст."""
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            text = raw.decode('utf-16')
        except UnicodeError as exc:
            raise ValueError('повреждённый текст UTF-16') from exc
    else:
        if b'\x00' in raw:
            raise ValueError('двоичный файл или UTF-16 без BOM; нужен текстовый документ')
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeError:
            try:
                text = raw.decode('cp1251')
            except UnicodeError as exc:
                raise ValueError('неподдерживаемая кодировка; используйте UTF-8') from exc
    if any(ord(char) < 32 and char not in '\n\r\t\f' for char in text):
        raise ValueError('обнаружены двоичные управляющие символы')
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('слишком большой извлечённый текст; разделите документ')
    return text.replace('\r\n', '\n').replace('\r', '\n')


class _HtmlText(HTMLParser):
    """Берёт текст HTML, не выполняя сценарии и не загружая внешние ресурсы."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1
        elif not self.hidden and tag in ('p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'li', 'tr', 'td', 'th'):
            self.parts.append('\n' if tag not in ('td', 'th') else '\t')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _docx_text(path):
    """Извлекает абзацы и таблицы DOCX без распаковки архива на диск."""
    namespace = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    parts, warnings = [], []
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(item.file_size for item in entries) > MAX_TOTAL_BYTES:
                raise ValueError('DOCX превышает лимит распакованных данных')
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError('зашифрованный DOCX не поддерживается')
            names = set(archive.namelist())
            if 'word/document.xml' not in names:
                raise ValueError('файл не является DOCX')
            selected = ['word/document.xml'] + sorted(name for name in names
                if re.fullmatch(r'word/(?:header\d*|footer\d*|footnotes|endnotes)\.xml', name))
            for name in selected:
                entry = archive.getinfo(name)
                if entry.file_size > MAX_FILE_BYTES:
                    raise ValueError('текстовая часть DOCX слишком велика')
                raw = archive.read(name)
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise ValueError('DOCX содержит неподдерживаемые XML-сущности')
                root = ET.fromstring(raw)
                for paragraph in root.iter(namespace + 'p'):
                    words = []
                    for element in paragraph.iter():
                        if element.tag == namespace + 't':
                            words.append(element.text or '')
                        elif element.tag in (namespace + 'tab', namespace + 'br', namespace + 'cr'):
                            words.append('\t' if element.tag.endswith('tab') else '\n')
                    if words:
                        parts.append(''.join(words))
            if any(name.startswith(('word/media/', 'word/embeddings/')) for name in names):
                warnings.append('DOCX содержит изображения или вложения: их содержимое не прочитано, OCR не выполняется')
    except (zipfile.BadZipFile, ET.ParseError, KeyError, RuntimeError) as exc:
        raise ValueError('повреждённый или неподдерживаемый DOCX') from exc
    return '\n'.join(parts), warnings


def _pdf_text(path):
    """Читает текстовый слой PDF; отсутствие OCR не скрывается от системы проверок."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError('для PDF установите pypdf: python -m pip install -r requirements-pdf.txt') from exc
    warnings, pages = [], []
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF защищён паролем')
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError('PDF содержит более %s страниц; разделите файл' % MAX_PDF_PAGES)
        for index, page in enumerate(reader.pages, 1):
            try:
                # Ограничение потока уменьшает риск расхода памяти на огромную страницу.
                content = page.get_contents()
                if content is not None and len(content.get_data()) > MAX_PDF_STREAM_BYTES:
                    raise ValueError('слишком большой поток страницы')
                text = page.extract_text() or ''
                if not text.strip():
                    warnings.append('PDF, страница %s: нет текстового слоя; возможен скан, нужен OCR' % index)
                else:
                    pages.append(text)
            except Exception as exc:
                # Сохраняем читаемые страницы, но не объявляем поиск полным.
                warnings.append('PDF, страница %s: не удалось извлечь текст (%s)' % (index, type(exc).__name__))
            if sum(len(text) for text in pages) > MAX_TEXT_CHARS:
                raise ValueError('слишком большой извлечённый текст; разделите PDF')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('не удалось прочитать PDF (%s)' % type(exc).__name__) from exc
    return '\n\n'.join(pages), warnings


def _read_document(path):
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        text, warnings = _pdf_text(path)
    elif suffix == '.docx':
        text, warnings = _docx_text(path)
    else:
        text, warnings = _decode(path.read_bytes()), []
        if suffix in ('.html', '.htm'):
            parser = _HtmlText()
            parser.feed(text)
            parser.close()
            text = ''.join(parser.parts)
    if not text.strip():
        raise ValueError('; '.join(warnings) if warnings else 'документ пуст или не содержит читаемого текста')
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('слишком большой извлечённый текст; разделите документ')
    return text, warnings


def load_documents(paths, exclude_paths=()):
    """Собирает один корпус из файлов и папок, возвращая тексты и полный журнал пропусков."""
    if not paths:
        raise ValueError('Укажите путь к файлу или папке с документами')
    excluded = {os.path.normcase(str(Path(path).expanduser().absolute())) for path in exclude_paths}
    documents, sources, skipped, errors = {}, [], [], []
    candidates, seen = [], set()
    visited = 0

    def skip(path, reason, incomplete=False):
        label = _safe_label(path)
        skipped.append({'path': label, 'reason': reason})
        if incomplete:
            errors.append(label + ': ' + reason)

    def visit(path, base, explicit=False):
        nonlocal visited
        visited += 1
        if visited > MAX_FILES * 10:
            raise ValueError('В выбранных папках слишком много объектов; выберите более узкую директорию')
        key = os.path.normcase(str(path.absolute()))
        if any(key == other or key.startswith(other + os.sep) for other in excluded):
            skip(path, 'исключён из документов: отдельный вход или папка результата')
            return
        try:
            if _is_link(path):
                skip(path, 'символическая ссылка или junction не обходится', True)
                return
            if path.name.startswith('.') or path.name.lower() in SERVICE_DIRS:
                skip(path, 'скрытый или служебный объект')
                return
            if path.is_dir():
                if (path / 'manifest.json').is_file() and ((path / 'answers.csv').is_file() or (path / 'trace.jsonl').is_file()):
                    skip(path, 'папка результатов предыдущего запуска')
                    return
                for child in sorted(path.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
                    visit(child, base)
                return
            if not path.is_file():
                skip(path, 'не является обычным файлом', True)
                return
            if path.name.casefold() in SERVICE_FILES:
                reason = 'эталон, вопросы или служебный результат; не является исходным документом'
                if explicit:
                    raise ValueError('Нельзя передать как документ ' + _safe_label(path.name) + ': ' + reason)
                skip(path, reason)
                return
            physical = os.path.normcase(str(path.resolve()))
            if physical in seen:
                skip(path, 'файл уже добавлен через другой входной путь')
                return
            seen.add(physical)
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                skip(path, 'неподдерживаемый формат ' + (path.suffix or '(без расширения)'), True)
                return
            if len(candidates) >= MAX_FILES:
                raise ValueError('Найдено более %s документов; выберите меньший набор' % MAX_FILES)
            candidates.append((path, _safe_label(path.relative_to(base).as_posix())))
        except OSError as exc:
            skip(path, 'не удалось прочитать путь (%s)' % type(exc).__name__, True)

    roots = sorted({str(Path(value).expanduser().absolute()) for value in paths}, key=str.casefold)
    for root in roots:
        path = Path(root)
        if not path.exists() and not path.is_symlink():
            raise ValueError('Путь не существует: ' + _safe_label(path))
        visit(path, path.parent, explicit=True)
    total = 0
    for path, label in candidates:
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError('файл больше 20 МиБ; разделите документ')
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError('общий размер документов превышает 80 МиБ; уменьшите набор')
            text, warnings = _read_document(path)
            original_label, count = label, 1
            # Одинаковые имена в разных выбранных папках не перезаписывают друг друга.
            while label.casefold() in {name.casefold() for name in documents}:
                count += 1
                label = '%s [%s]' % (original_label, count)
            documents[label] = text
            sources.append({'file': label, 'path': str(path.resolve()), 'format': path.suffix.lower().lstrip('.')})
            errors.extend(label + ': ' + warning for warning in warnings)
        except (OSError, ValueError) as exc:
            skip(path, _safe_label(str(exc)), True)
    if not documents:
        detail = '; '.join(item['reason'] for item in skipped[:3]) or 'папка пуста'
        raise ValueError('Не найдено читаемых документов. ' + detail)
    return {'documents': documents, 'read_errors': errors, 'sources': sources, 'skipped': skipped}


def load_questions(path):
    """Читает вопросы из строк TXT/MD, колонки CSV или массива JSON; ID создаёт CLI."""
    path = Path(path).expanduser()
    if not path.is_file() or _is_link(path):
        raise ValueError('Файл вопросов отсутствует или является ссылкой: ' + _safe_label(path))
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Файл вопросов слишком большой')
    text = _decode(path.read_bytes())
    suffix = path.suffix.lower()
    if suffix in ('.txt', '.md', '.rst'):
        questions = [line.strip() for line in text.splitlines() if line.strip()]
    elif suffix == '.csv':
        header = text.splitlines()[0] if text else ''
        delimiter = ';' if header.count(';') >= header.count(',') else ','
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        columns = {name.strip().casefold(): name for name in (reader.fieldnames or [])}
        column = columns.get('question') or columns.get('вопрос')
        if not column:
            raise ValueError('В CSV вопросов нужна колонка question или вопрос')
        questions = [(row.get(column) or '').strip() for row in reader]
        if any(not value for value in questions):
            raise ValueError('В CSV есть строка без вопроса')
    elif suffix == '.json':
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ValueError('Файл вопросов содержит некорректный JSON') from exc
        questions = value.get('questions') if isinstance(value, dict) else value
        if isinstance(questions, list):
            questions = [item.get('question') if isinstance(item, dict) else item for item in questions]
    else:
        raise ValueError('Файл вопросов должен быть TXT, MD, RST, CSV или JSON')
    if not isinstance(questions, list) or not questions or any(not isinstance(item, str) or not item.strip() for item in questions):
        raise ValueError('Не найдены непустые текстовые вопросы')
    if len(questions) > MAX_QUESTIONS or any(len(item) > MAX_QUESTION_CHARS for item in questions):
        raise ValueError('Допустимо до %s вопросов, каждый до %s символов' % (MAX_QUESTIONS, MAX_QUESTION_CHARS))
    return [item.strip() for item in questions]
