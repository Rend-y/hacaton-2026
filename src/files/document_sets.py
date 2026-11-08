"""Общий каталог загруженных документов для CLI и MCP."""

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import re
from shutil import rmtree
from uuid import uuid4

from src.files.input_loader import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, SERVICE_FILES, SUPPORTED_SUFFIXES
from src.files.run_storage import write_json


DEMO_SETS = {
    'P01': 'Чат-бот для обращений',
    'P02': 'Лицензии на офисное ПО',
    'P03': 'Доработка конфигурации 1С',
    'P04': 'Обслуживание и защита информации',
    'P05': 'Мобильное приложение для записи',
    'P06': 'Серверное оборудование',
}
UPLOAD_ID = re.compile(r'[a-f0-9]{32}', re.ASCII)


def validate_document_name(name):
    """Проверяет имя загружаемого файла перед сохранением в каталоге MCP."""
    if (not name or name.startswith('.') or PureWindowsPath(name).is_reserved()
            or any(char in '<>:"/\\|?*' or not char.isprintable() for char in name)
            or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES or name.casefold() in SERVICE_FILES):
        raise ValueError('Выберите документы поддерживаемого формата с обычными именами.')


def local_documents(paths):
    for value in paths:
        path = Path(value).expanduser()
        validate_document_name(path.name)
        if not path.is_file():
            raise ValueError(f'Укажите существующий файл документа: {path.name}')
        with path.open('rb') as stream:
            yield path.name, stream.read(MAX_FILE_BYTES + 1)


def _attachment_id(name, content):
    digest = sha256(name.encode('utf-8') + b'\0')
    digest.update(content)
    return digest.hexdigest()


def _write_documents(directory, files, known_attachments=None):
    directory.mkdir(parents=True)
    total = 0
    written = {}
    seen = set()
    for count, (name, content) in enumerate(files, 1):
        validate_document_name(name)
        total += len(content)
        if not content:
            raise ValueError(f'Пустой файл: {name}')
        if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or count > MAX_FILES:
            raise ValueError('Допустимо до 500 файлов, 20 МиБ на файл и 80 МиБ суммарно.')
        identifier = None
        if known_attachments is not None:
            identifier = _attachment_id(name, content)
            if identifier in known_attachments:
                continue
            known_attachments.add(identifier)
        if name.casefold() in seen:
            raise ValueError(f'Имя документа повторяется: {name}')
        (directory / name).write_bytes(content)
        written[name] = identifier
        seen.add(name.casefold())
    if not written and known_attachments is None:
        raise ValueError('Добавьте хотя бы один документ.')
    return written


def _document_paths(folder):
    if not folder.is_dir() or folder.resolve() != folder:
        raise ValueError('Комплект документов не найден.')
    paths = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
    if not paths or any(not path.is_file() or path.resolve() != path for path in paths):
        raise ValueError('В комплекте нет доступных документов или его содержимое изменилось.')
    return paths


@contextmanager
def _new_batch(parent):
    batch = parent / uuid4().hex
    batch.mkdir(parents=True)
    try:
        yield batch
    except BaseException:
        if batch.resolve() == batch:
            rmtree(batch, ignore_errors=True)
        raise


def _set_entry(folder):
    metadata = folder / 'set.json'
    if metadata.resolve() != metadata:
        return None
    try:
        entry = json.loads(metadata.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        # Загрузка ещё не опубликована или процесс остановился при записи.
        return None
    return entry if isinstance(entry, dict) and entry.get('document_set_id') == folder.name else None


class DocumentSets:
    def __init__(self, root):
        root = root.resolve()
        self.uploads = root / 'runs' / '.agent' / 'documents'
        self.demos = root / 'data' / 'public' / 'packages'

    def save(self, title, files):
        title = (title or '').strip()
        if not 0 < len(title) <= 200 or not title.isprintable():
            raise ValueError('Укажите название комплекта длиной от 1 до 200 символов.')
        with _new_batch(self.uploads) as folder:
            written = _write_documents(folder / 'files', files)
            entry = {'document_set_id': folder.name, 'title': title, 'files': list(written)}
            write_json(folder / 'set.json', entry)
        return entry

    def paths(self, set_id):
        if set_id in DEMO_SETS:
            folder = self.demos / set_id
        elif UPLOAD_ID.fullmatch(set_id) and _set_entry(self.uploads / set_id) is not None:
            folder = self.uploads / set_id / 'files'
        else:
            raise ValueError('Комплект документов не найден.')
        return _document_paths(folder)

    def list(self):
        entries = [{'document_set_id': set_id, 'title': title, 'files': [p.name for p in self.paths(set_id)]}
                   for set_id, title in DEMO_SETS.items() if (self.demos / set_id).is_dir()]
        if self.uploads.is_dir():
            for folder in sorted(self.uploads.iterdir()):
                if UPLOAD_ID.fullmatch(folder.name) and (entry := _set_entry(folder)) is not None:
                    entries.append(entry)
        return entries


class DocumentChats:
    """Файлы каждого MCP-чата, сгруппированные по неизменяемым загрузкам."""

    def __init__(self, root):
        self.root = root.resolve() / 'runs' / '.agent' / 'chats'

    def create(self):
        chat_id = uuid4().hex
        (self.root / chat_id).mkdir(parents=True)
        return {'chat_id': chat_id, 'files': [], 'attachments': []}

    def _folder(self, chat_id):
        if UPLOAD_ID.fullmatch(chat_id):
            folder = self.root / chat_id
            if folder.is_dir() and folder.resolve() == folder:
                return folder
        raise ValueError('Контекст чата не найден.')

    def _attachments(self, chat_id):
        folder = self._folder(chat_id)
        batches = sorted(path for path in folder.iterdir()
                         if UPLOAD_ID.fullmatch(path.name) and (path / '.ready').is_file())
        attachments = {}
        for batch in batches:
            index = batch / 'attachments.json'
            ids = json.loads(index.read_text(encoding='utf-8')) if index.is_file() else {}
            for path in _document_paths(batch / 'files'):
                identifier = ids.get(path.name) or _attachment_id(path.name, path.read_bytes())
                attachments.setdefault(identifier, path)
        return attachments

    def paths(self, chat_id, attachment_ids=None):
        attachments = self._attachments(chat_id)
        if attachment_ids is None:
            return list(attachments.values())
        if not attachment_ids or any(identifier not in attachments for identifier in attachment_ids):
            raise ValueError('Выберите файлы текущего чата по attachment_ids из get_document_chat.')
        return [attachments[identifier] for identifier in dict.fromkeys(attachment_ids)]

    def get(self, chat_id):
        attachments = self._attachments(chat_id)
        return {'chat_id': chat_id, 'files': [path.name for path in attachments.values()], 'attachments': [
            {'attachment_id': identifier, 'name': path.name} for identifier, path in attachments.items()
        ]}

    def append(self, chat_id, files):
        known = set(self._attachments(chat_id))
        with _new_batch(self._folder(chat_id)) as batch:
            written = _write_documents(batch / 'files', files, known)
            if written:
                write_json(batch / 'attachments.json', written)
                # Вопросы видят пакет только после записи и закрытия всех его файлов.
                (batch / '.ready').touch(exist_ok=False)
            else:
                (batch / 'files').rmdir()
                batch.rmdir()
        return {**self.get(chat_id), 'added_files': list(written)}
