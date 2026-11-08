"""HTTP-интерфейс на loopback, без внешних веб-зависимостей."""
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path, PureWindowsPath
import re
import threading
from urllib.parse import parse_qs, urlsplit

from src import settings
from src.dashboard.comparison import compare_history_runs
from src.dashboard.config import DashboardConfig
from src.dashboard.jobs import Jobs
from src.dashboard.test_data import TestData
from src.evaluation.reference_data import read_package, read_questions
from src.files.input_loader import (
    MAX_FILE_BYTES, MAX_FILES, MAX_QUESTION_CHARS, MAX_QUESTIONS, MAX_TOTAL_BYTES,
    SERVICE_FILES, SUPPORTED_SUFFIXES,
)
from src.progress import emit


STATIC = Path(__file__).with_name('static')
ASSETS = {'/': 'index.html', '/new': 'index.html', '/settings': 'index.html', '/tests': 'index.html',
          '/tests/create': 'index.html', '/compare': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js',
          '/test-data.js': 'test-data.js', '/comparison.js': 'comparison.js'}
REPORT_FILES = {
    'report.html', 'answers.csv', 'answers.json', 'evaluation.csv', 'evaluation.json',
    'manifest.json', 'prompts.json', 'progress.log',
}
BODY_LIMIT = MAX_TOTAL_BYTES + 10 * 1024 * 1024
JSON_LIMIT = 32 * 1024
INPUT_CLEANUP_INTERVAL_SECONDS = 15 * 60


def _multipart_data(content_type, body, field_names):
    header = ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode('ascii')
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if message.get_content_type() != 'multipart/form-data' or not message.is_multipart() or message.defects:
        raise ValueError('Не удалось прочитать форму запуска.')
    files, fields = [], {}
    total = 0
    for part in message.iter_parts():
        field = part.get_param('name', header='Content-Disposition')
        if part.get_content_disposition() != 'form-data' or part.is_multipart():
            raise ValueError('Не удалось прочитать форму запуска.')
        content = part.get_payload(decode=True)
        if field == 'documents':
            name = part.get_filename() or ''
            if (not name or name.startswith('.') or PureWindowsPath(name).is_reserved()
                    or any(char in '<>:"/\\|?*' or not char.isprintable() for char in name)
                    or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES or name.lower() in SERVICE_FILES):
                raise ValueError('Выберите документы поддерживаемого формата с обычными именами.')
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                raise ValueError('Допустимо до 500 файлов, 20 МиБ на файл и 80 МиБ суммарно.')
            files.append((name, content))
        elif field in field_names and field not in fields:
            fields[field] = content.decode('utf-8')
        else:
            raise ValueError('Не удалось прочитать форму запуска.')
    return files, fields


def _form_data(content_type, body):
    files, fields = _multipart_data(content_type, body, ('questions', 'mode'))
    questions = [line.strip() for line in fields.get('questions', '').splitlines() if line.strip()]
    mode = fields.get('mode')
    if not files:
        raise ValueError('Добавьте хотя бы один документ.')
    if not questions or len(questions) > MAX_QUESTIONS or any(len(line) > MAX_QUESTION_CHARS for line in questions):
        raise ValueError('Укажите от 1 до 200 вопросов, каждый до 10 000 символов.')
    if mode not in ('baseline', 'verified'):
        raise ValueError('Выберите режим обработки.')
    return files, questions, mode


def _p01_inputs():
    data = settings.ROOT / 'data' / 'public'
    documents, errors = read_package(data, 'P01')
    questions = [row['question'] for row in read_questions(data) if row['package'] == 'P01']
    if errors or not questions:
        raise ValueError('Не удалось загрузить документы и вопросы теста P01.')
    return data, [(name, text.encode('utf-8')) for name, text in documents.items()], questions


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, port, root):
        super().__init__(('127.0.0.1', port), DashboardHandler)
        root = root.resolve()
        self.config = DashboardConfig(root.parent / '.env')
        self.jobs = Jobs(root, self.config)
        self.tests = TestData(root.parent / 'test-data')
        self.hosts = {f'127.0.0.1:{self.server_port}', f'localhost:{self.server_port}'}
        if self.server_port == 80:
            self.hosts.update(('127.0.0.1', 'localhost'))


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if content_type.startswith('text/html') and ASSETS.get(urlsplit(self.path).path) == 'index.html':
            self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

    def _file(self, path):
        try:
            body = path.read_bytes()
        except OSError:
            self._json(404, {'error': 'Файл не найден.'})
            return
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix == '.js':
            content_type = 'text/javascript'
        self._send(200, body, content_type + '; charset=utf-8')

    def do_GET(self):
        if self.headers.get('Host') not in self.server.hosts:
            self._json(403, {'error': 'Откройте локальный адрес дашборда.'})
            return
        request = urlsplit(self.path)
        path = request.path
        if path in ASSETS:
            self._file(STATIC / ASSETS[path])
        elif re.fullmatch(r'/tests/[^/]+/edit', path):
            self._file(STATIC / 'index.html')
        elif path == '/api/runs':
            self._json(200, self.server.jobs.list())
        elif path == '/api/compare':
            params = parse_qs(request.query)
            try:
                baseline, verified = (params.get(mode, [''])[0] for mode in ('baseline', 'verified'))
                self._json(200, compare_history_runs(self.server.jobs.root, baseline, verified))
            except ValueError as exc:
                self._json(400, {'error': str(exc)})
            except OSError:
                self._json(400, {'error': 'Не удалось прочитать сохранённые данные выбранных запусков.'})
        elif path == '/api/tests':
            try:
                self._json(200, self.server.tests.list())
            except OSError:
                self._json(500, {'error': 'Не удалось прочитать список тестовых наборов.'})
        elif path == '/api/tests/details':
            try:
                name = parse_qs(request.query).get('name', [''])[0]
                self._json(200, self.server.tests.details(name))
            except (ValueError, OSError) as exc:
                self._json(400, {'error': str(exc)})
        elif path == '/api/settings':
            try:
                self._json(200, self.server.config.public())
            except (OSError, UnicodeError):
                self._json(500, {'error': 'Не удалось прочитать локальный файл .env.'})
        else:
            match = re.fullmatch(r'/runs/([A-Za-z0-9_-]+)/([a-z.]+)', path)
            if match and match[2] in REPORT_FILES:
                root = self.server.jobs.root
                artifact = root / match[1] / match[2]
                if artifact.resolve() == artifact:
                    self._file(artifact)
                    return
            self._json(404, {'error': 'Страница не найдена.'})

    def do_POST(self):
        host = self.headers.get('Host')
        if host not in self.server.hosts or self.headers.get('Origin') != f'http://{host}':
            self._json(403, {'error': 'Запрос доступен с локальной страницы дашборда.'})
            return
        is_settings = self.path == '/api/settings'
        is_p01 = self.path == '/api/tests/p01'
        is_test_create = self.path == '/api/tests'
        is_test_edit = self.path == '/api/tests/edit'
        is_test_run = self.path == '/api/tests/run'
        is_json = is_settings or is_p01 or is_test_run
        if not is_json and not is_test_create and not is_test_edit and self.path != '/api/runs':
            self._json(404, {'error': 'Страница не найдена.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= (JSON_LIMIT if is_json else BODY_LIMIT):
                self._json(413, {'error': 'Форма пуста или превышает допустимый размер.'})
                return
            content_type = 'application/json' if is_json else 'multipart/form-data'
            if self.headers.get_content_type() != content_type:
                raise ValueError('Некорректный формат формы.')
            self.connection.settimeout(30)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Форма передана не полностью.')
            if is_settings:
                self.server.config.save(json.loads(body.decode('utf-8')))
                self._json(200, self.server.config.public())
                return
            if is_test_create or is_test_edit:
                field_names = ('name', 'cases', 'original', 'revision') if is_test_edit else ('name', 'cases')
                files, fields = _multipart_data(self.headers['Content-Type'], body, field_names)
                args = (fields.get('name'), files, json.loads(fields.get('cases', '[]')))
                if is_test_edit:
                    dataset = self.server.tests.update(fields.get('original'), fields.get('revision'), *args)
                    self._json(200, dataset)
                else:
                    self._json(201, self.server.tests.create(*args))
                return
            if is_test_run or is_p01:
                payload = json.loads(body.decode('utf-8'))
                if not isinstance(payload, dict):
                    raise ValueError('Некорректные параметры запуска теста.')
                mode = payload.get('mode', 'verified')
                if mode not in ('baseline', 'verified'):
                    raise ValueError('Выберите режим обработки.')
                if is_test_run:
                    run_id = self.server.tests.start(payload.get('name'), self.server.jobs, mode=mode)
                else:
                    data, files, questions = _p01_inputs()
                    run_id = self.server.jobs.start(files, questions, mode,
                                                    label='Контрольный тест P01', evaluation_data=data)
            else:
                files, questions, mode = _form_data(self.headers['Content-Type'], body)
                run_id = self.server.jobs.start(files, questions, mode)
        except UnicodeError:
            self._json(400, {'error': 'Не удалось прочитать текст формы.'})
        except ValueError as exc:
            self._json(400, {'error': str(exc)})
        except OSError:
            error = 'Не удалось сохранить настройки в .env.' if is_settings else 'Не удалось получить форму или записать документы.'
            self._json(500, {'error': error})
        else:
            self._json(202, {'id': run_id})


def serve(port):
    if not 1 <= port <= 65535:
        raise ValueError('Укажите порт от 1 до 65535.')
    with DashboardServer(port, settings.ROOT / 'runs') as server:
        stopped = threading.Event()

        def cleanup_inputs():
            while not stopped.is_set():
                try:
                    server.jobs.cleanup_inputs(server.config.input_retention_hours())
                except (OSError, ValueError) as exc:
                    logging.warning('Очистка временных файлов пропущена: %s', exc)
                stopped.wait(INPUT_CLEANUP_INTERVAL_SECONDS)

        cleaner = threading.Thread(target=cleanup_inputs, name='run-input-cleanup', daemon=True)
        cleaner.start()
        emit(f'Дашборд открыт на http://127.0.0.1:{server.server_port}')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            emit('Дашборд остановлен.')
        finally:
            stopped.set()
            cleaner.join()
