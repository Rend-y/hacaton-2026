"""HTTP-интерфейс на loopback, без внешних веб-зависимостей."""
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import urlsplit

from src import settings
from src.dashboard.jobs import Jobs
from src.files.input_loader import (
    MAX_FILE_BYTES, MAX_FILES, MAX_QUESTION_CHARS, MAX_QUESTIONS, MAX_TOTAL_BYTES,
    SERVICE_FILES, SUPPORTED_SUFFIXES,
)
from src.progress import emit


STATIC = Path(__file__).with_name('static')
ASSETS = {'/': 'index.html', '/new': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js'}
REPORT_FILES = {
    'report.html', 'answers.csv', 'answers.json', 'evaluation.csv', 'evaluation.json',
    'manifest.json', 'prompts.json', 'progress.log',
}
BODY_LIMIT = MAX_TOTAL_BYTES + 10 * 1024 * 1024


def _form_data(content_type, body):
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
        elif field in ('questions', 'mode', 'evaluate') and field not in fields:
            fields[field] = content.decode('utf-8')
        else:
            raise ValueError('Не удалось прочитать форму запуска.')
    questions = [line.strip() for line in fields.get('questions', '').splitlines() if line.strip()]
    mode = fields.get('mode')
    if not files:
        raise ValueError('Добавьте хотя бы один документ.')
    if not questions or len(questions) > MAX_QUESTIONS or any(len(line) > MAX_QUESTION_CHARS for line in questions):
        raise ValueError('Укажите от 1 до 200 вопросов, каждый до 10 000 символов.')
    if mode not in ('baseline', 'verified'):
        raise ValueError('Выберите режим обработки.')
    if 'evaluate' in fields and fields['evaluate'] != 'on':
        raise ValueError('Некорректное значение итоговой проверки.')
    return files, questions, mode, 'evaluate' in fields


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, port, root):
        super().__init__(('127.0.0.1', port), DashboardHandler)
        self.jobs = Jobs(root.resolve())
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
        if content_type.startswith('text/html') and urlsplit(self.path).path in ('/', '/new'):
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
        path = urlsplit(self.path).path
        if path in ASSETS:
            self._file(STATIC / ASSETS[path])
        elif path == '/api/runs':
            self._json(200, self.server.jobs.list())
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
            self._json(403, {'error': 'Запуск доступен с локальной страницы дашборда.'})
            return
        if self.path != '/api/runs':
            self._json(404, {'error': 'Страница не найдена.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= BODY_LIMIT:
                self._json(413, {'error': 'Форма пуста или превышает допустимый размер.'})
                return
            if self.headers.get_content_type() != 'multipart/form-data':
                raise ValueError('Ожидалась форма с документами.')
            self.connection.settimeout(30)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Форма передана не полностью.')
            files, questions, mode, evaluate = _form_data(self.headers['Content-Type'], body)
            run_id = self.server.jobs.start(files, questions, mode, evaluate=evaluate)
        except UnicodeError:
            self._json(400, {'error': 'Не удалось прочитать текст формы.'})
        except ValueError as exc:
            self._json(400, {'error': str(exc)})
        except OSError:
            self._json(500, {'error': 'Не удалось получить форму или записать документы.'})
        else:
            self._json(202, {'id': run_id})


def serve(port):
    if not 1 <= port <= 65535:
        raise ValueError('Укажите порт от 1 до 65535.')
    with DashboardServer(port, settings.ROOT / 'runs') as server:
        emit(f'Дашборд открыт на http://127.0.0.1:{server.server_port}')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            emit('Дашборд остановлен.')
