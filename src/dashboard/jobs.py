"""Запуск обработки в фоне и чтение истории из runs."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from shutil import copyfile
import threading
import time
from uuid import uuid4

from src.files.run_storage import write_json
from src.generation.client import ApiError
from src.settings import QueryOptions
from src.workflows.query import run_query


def _read_object(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cli_status(directory, manifest):
    status = manifest.get('status')
    if status == 'running':
        try:
            # Во время ожидания API progress.log получает heartbeat каждые 10 секунд.
            if time.time() - (directory / 'progress.log').stat().st_mtime < 60:
                return 'running'
        except OSError:
            pass
    if status in ('completed', 'completed_with_errors'):
        return status
    return 'unknown'


class Jobs:
    def __init__(self, root: Path, config):
        self.root = root
        self.config = config
        self.metadata = root / '.dashboard'
        self.active = set()
        self.lock = threading.Lock()

    def start(self, files, questions, mode, *, label=None, evaluation_data=None, snapshot_package=None):
        environ = self.config.snapshot()
        now = datetime.now(timezone.utc)
        run_id = now.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:12] + '-web'
        work = self.metadata / run_id
        work.mkdir(parents=True)
        snapshot = work / 'evaluation'
        if snapshot_package:
            package = snapshot / 'packages' / snapshot_package
            package.mkdir(parents=True)
            for filename in ('questions.csv', 'answers_public.json'):
                copyfile(evaluation_data / filename, snapshot / filename)
            evaluation_data = snapshot
        documents = []
        for index, (name, content) in enumerate(files):
            # Отдельные папки сохраняют оригинальные имена, включая дубликаты.
            directory = package if snapshot_package else work / str(index)
            directory.mkdir(exist_ok=True)
            path = directory / name
            path.write_bytes(content)
            documents.append(str(path))
        job = {'status': 'running', 'mode': mode, 'created_at': now.isoformat(),
               'kind': 'test' if evaluation_data else 'manual',
               'total': len(questions), 'label': label or files[0][0], 'file_count': len(files)}
        args = QueryOptions(attachment=documents, prompt=questions, mode=mode)
        with self.lock:
            write_json(work / 'job.json', job)
            self.active.add(run_id)
            try:
                threading.Thread(target=self._run, args=(run_id, job, args, environ, evaluation_data), daemon=True).start()
            except RuntimeError:
                self.active.remove(run_id)
                write_json(work / 'job.json', {**job, 'status': 'failed', 'error': 'Не удалось запустить обработку.'})
        return run_id

    def _run(self, run_id, job, args, environ, evaluation_data):
        try:
            code = run_query(args, output=self.root / run_id, environ=environ, evaluation_data=evaluation_data)
            result = {'status': 'completed' if code == 0 else 'completed_with_errors'}
        except (ValueError, OSError, ApiError) as exc:
            message = str(exc)
            key = environ['API_KEY']
            result = {'status': 'failed', 'error': message.replace(key, '[скрыто]') if key else message}
        except Exception:
            logging.exception('Ошибка обработки %s', run_id)
            result = {'status': 'failed', 'error': 'Внутренняя ошибка обработки.'}
        with self.lock:
            try:
                write_json(self.metadata / run_id / 'job.json', {**job, **result})
            finally:
                self.active.remove(run_id)

    def list(self):
        directories = list(self.root.iterdir()) if self.root.is_dir() else []
        manifests = {}
        for directory in directories:
            if not directory.name.startswith('.') and directory.is_dir() and directory.resolve() == directory:
                manifest = _read_object(directory / 'manifest.json')
                if manifest:
                    manifests[directory.name] = manifest
        with self.lock:
            jobs = {}
            if self.metadata.is_dir():
                for directory in self.metadata.iterdir():
                    job = _read_object(directory / 'job.json')
                    if job:
                        if job.get('status') == 'running' and directory.name not in self.active:
                            job['status'] = 'unknown'
                        jobs[directory.name] = job
        rows = []
        for run_id in manifests.keys() | jobs.keys():
            directory = self.root / run_id
            manifest = manifests.get(run_id, {})
            job = jobs.get(run_id)
            if job or run_id.endswith('-web'):
                kind = job.get('kind', 'manual') if job else 'manual'
            else:
                kind = 'benchmark' if '-all-' in run_id else 'cli'
            question_ids = manifest.get('question_ids')
            row = job or {'status': _cli_status(directory, manifest), 'mode': manifest.get('mode'),
                          'created_at': manifest.get('created_at'),
                          'label': {'manual': 'Обработка документов', 'benchmark': 'Демонстрационный набор',
                                    'cli': 'Запуск из CLI'}[kind],
                          'total': len(question_ids) if isinstance(question_ids, list) else 0}
            rows.append({**row, 'id': run_id, 'kind': kind, 'completed': manifest.get('completed_questions', 0),
                         'has_report': (directory / 'report.html').is_file()})
        return sorted(rows, key=lambda row: (str(row['created_at'] or ''), row['id']), reverse=True)
