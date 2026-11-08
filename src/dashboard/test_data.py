"""Хранилище пользовательских наборов в формате локального оценщика."""
import csv
import json
from pathlib import Path, PureWindowsPath
from shutil import rmtree
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from src.evaluation.check import STATUSES, norm
from src.evaluation.query_evaluation import _question_key
from src.evaluation.reference_data import read_package, read_questions
from src.files.input_loader import MAX_QUESTION_CHARS, MAX_QUESTIONS, load_documents
from src.files.run_storage import write_json


PACKAGE = 'TEST'


def _test_name(value):
    if not isinstance(value, str):
        raise ValueError('Введите название теста.')
    name = value.strip()
    if (not name or len(name) > 80 or name.startswith('.') or name.endswith('.')
            or PureWindowsPath(name).is_reserved()
            or any(char in '<>:"/\\|?*' or not char.isprintable() for char in name)):
        raise ValueError('Введите название до 80 символов, допустимое для имени папки.')
    return name


def _reference_rows(cases, documents):
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_QUESTIONS:
        raise ValueError(f'Добавьте от 1 до {MAX_QUESTIONS} вопросов.')
    rows, seen = [], set()
    normalized_docs = {name: norm(text) for name, text in documents.items()}
    for index, case in enumerate(cases, 1):
        fields = ('question', 'expected', 'quote')
        if not isinstance(case, dict) or any(not isinstance(case.get(field, ''), str) for field in fields):
            raise ValueError(f'Некорректные данные вопроса №{index}.')
        question, expected, quote = (case.get(field, '').strip() for field in fields)
        status = case.get('status', 'ответ')
        if status not in STATUSES:
            raise ValueError(f'Укажите допустимый статус для вопроса №{index}.')
        if any(len(value) > MAX_QUESTION_CHARS for value in (question, expected, quote)):
            raise ValueError(f'Вопрос №{index} содержит поле длиннее {MAX_QUESTION_CHARS} символов.')
        key = _question_key(question)
        criteria = [line.strip() for line in expected.splitlines() if line.strip()]
        if not key or (status in ('ответ', 'расхождение') and not criteria):
            raise ValueError(f'Укажите вопрос и ожидаемый ответ в строке №{index}.')
        if key in seen:
            raise ValueError(f'Вопрос №{index} повторяет другой вопрос набора.')
        seen.add(key)
        source = ''
        if quote:
            normalized_quote = norm(quote)
            source = next((name for name, text in normalized_docs.items() if normalized_quote in text), '')
            if len(normalized_quote) < 8 or not source:
                raise ValueError(f'Цитата для вопроса №{index} должна содержать от 8 символов и встречаться в документе.')
        rows.append({'id': f'INPUT-{index:03d}', 'package': PACKAGE, 'question': question,
                     'kind': 'all_kw', 'expected': criteria, 'type': 'факт' if status == 'ответ' else status,
                     'status': status,
                     'file': source, 'quote': quote})
    return rows


class TestData:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = Lock()

    def create(self, name, files, cases):
        with self.lock:
            return self._save(_test_name(name), files, cases)

    def _path(self, name):
        path = self.root / _test_name(name)
        if not path.is_dir() or path.resolve() != path:
            raise ValueError('Тестовый набор не найден.')
        return path

    def _revision(self, path):
        metadata = path.stat()
        return f'{metadata.st_ino}-{metadata.st_mtime_ns}'

    def details(self, name):
        with self.lock:
            path = self._path(name)
            gold = json.loads((path / 'answers_public.json').read_text(encoding='utf-8'))
            return {'name': path.name, 'revision': self._revision(path),
                    'documents': sorted(file.name for file in (path / 'documents').iterdir() if file.is_file()),
                    'cases': [{'question': row['question'], 'status': row['status'],
                               'expected': '\n'.join(row['expected']),
                               'quote': row.get('quote', '')} for row in gold]}

    def update(self, original, revision, name, files, cases):
        with self.lock:
            previous = self._path(original)
            if revision != self._revision(previous):
                raise ValueError('Набор уже изменился. Откройте его заново.')
            if not files:
                originals = previous / 'documents'
                files = [(file.name, file.read_bytes()) for file in sorted(originals.iterdir())
                         if file.is_file() and file.resolve() == file]
            return self._save(_test_name(name), files, cases, previous)

    def _save(self, name, files, cases, previous=None):
        if not files:
            raise ValueError('Добавьте документы для теста.')
        if len({filename.casefold() for filename, _ in files}) != len(files):
            raise ValueError('Имена документов в одном тесте должны различаться.')
        self.root.mkdir(parents=True, exist_ok=True)
        if any(path != previous and path.name.casefold() == name.casefold() for path in self.root.iterdir()):
            raise ValueError('Тест с таким названием уже существует.')
        with TemporaryDirectory(prefix='.draft-', dir=self.root) as temporary:
            staged = Path(temporary) / name
            originals = staged / 'documents'
            originals.mkdir(parents=True)
            for filename, content in files:
                (originals / filename).write_bytes(content)
            loaded = load_documents([str(originals / filename) for filename, _ in files])
            if loaded['read_errors']:
                raise ValueError('Прочитайте все документы перед созданием теста. ' + loaded['read_errors'][0])

            package = staged / 'packages' / PACKAGE
            package.mkdir(parents=True)
            documents = {}
            for index, (filename, text) in enumerate(loaded['documents'].items(), 1):
                suffix = '' if filename.lower().endswith('.txt') else '.txt'
                normalized_name = f'{index:03d}-{filename}{suffix}'
                documents[normalized_name] = text
                (package / normalized_name).write_text(text, encoding='utf-8', newline='\n')
            rows = _reference_rows(cases, documents)
            with (staged / 'questions.csv').open('w', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream, delimiter=';')
                writer.writerow(('id', 'package', 'question'))
                writer.writerows((row['id'], row['package'], row['question']) for row in rows)
            write_json(staged / 'answers_public.json', rows)
            destination = self.root / name
            if previous is None:
                staged.rename(destination)
            else:
                backup = self.root / ('.previous-' + uuid4().hex)
                previous.rename(backup)
                try:
                    staged.rename(destination)
                except OSError:
                    backup.rename(previous)
                    raise
                rmtree(backup, ignore_errors=True)
        return {'name': name, 'documents': len(files), 'questions': len(rows)}

    def list(self):
        with self.lock:
            return self._list()

    def _list(self):
        if not self.root.is_dir():
            return []
        rows = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name.casefold()):
            if (path.name.startswith('.') or not path.is_dir() or path.resolve() != path
                    or not (path / 'answers_public.json').is_file()):
                continue
            try:
                count = len(read_questions(path))
                originals = sum(file.is_file() for file in (path / 'documents').iterdir())
            except (OSError, ValueError):
                continue
            rows.append({'name': path.name, 'documents': originals, 'questions': count})
        return rows

    def start(self, name, jobs, *, mode='verified'):
        with self.lock:
            path = self._path(name)
            documents, errors = read_package(path, PACKAGE)
            if errors:
                raise ValueError('Не удалось прочитать документы набора. ' + errors[0])
            questions = [row['question'] for row in read_questions(path)]
            files = [(filename, text.encode('utf-8')) for filename, text in documents.items()]
            return jobs.start(files, questions, mode, label=path.name, evaluation_data=path,
                              snapshot_package=PACKAGE)
