"""Изолированные запуски MCP: только verified и модель текущего агента."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sys
import threading
from uuid import uuid4

from src.files.input_loader import load_documents
from src.files.run_storage import write_json
from src.generation.mcp_client import McpModelClient
from src.settings import DEFAULT_MAX_QUESTIONS_PER_REQUEST, RunOptions
from src.workflows.pipeline import run_groups
from src.workflows.query import question_rows


RUN_ID = re.compile(r'\d{8}-\d{6}-[a-f0-9]{12}-(?:mcp|web)', re.ASCII)


class DocumentRuns:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.metadata = self.root / '.mcp'
        self.active = set()
        self.lock = threading.Lock()

    def start(self, paths, questions, *, label, chat_id=None):
        loaded = load_documents(paths)
        if not loaded['documents']:
            raise ValueError('Не найдено читаемых документов. Проверьте формат и содержимое файлов.')
        rows = question_rows(questions)
        now = datetime.now(timezone.utc)
        run_id = now.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:12] + '-mcp'
        client = McpModelClient()
        # Режим, повторы и модель не берутся из .env и не задаются MCP-клиентом.
        options = RunOptions(mode='verified', output=self.root / run_id, repair_attempts=2)
        job = {'status': 'running', 'mode': 'verified', 'label': label, 'chat_id': chat_id,
               'created_at': now.isoformat()}
        with self.lock:
            self.metadata.mkdir(parents=True, exist_ok=True)
            write_json(self.metadata / (run_id + '.json'), job)
            self.active.add(run_id)
            try:
                threading.Thread(target=self._run, args=(run_id, job, options, rows, loaded, client),
                                 daemon=True).start()
            except RuntimeError:
                self.active.remove(run_id)
                write_json(self.metadata / (run_id + '.json'),
                           {**job, 'status': 'failed', 'error': 'Не удалось запустить обработку.'})
                raise ValueError('Не удалось запустить обработку.') from None
        return run_id, client

    def _run(self, run_id, job, options, rows, loaded, client):
        try:
            code = run_groups(options, rows, {'INPUT': (loaded['documents'], loaded['read_errors'])},
                              source_metadata={'sources': loaded['sources'], 'skipped': loaded['skipped']},
                              batch_limit=DEFAULT_MAX_QUESTIONS_PER_REQUEST, environ={}, client=client,
                              log=lambda text: print(text, file=sys.stderr, flush=True))
            result = {'status': 'completed' if code == 0 else 'completed_with_errors'}
        except (ValueError, OSError) as exc:
            result = {'status': 'failed', 'error': str(exc)}
        except Exception:
            logging.exception('Ошибка обработки MCP %s', run_id)
            result = {'status': 'failed', 'error': 'Внутренняя ошибка обработки.'}
        with self.lock:
            try:
                write_json(self.metadata / (run_id + '.json'), {**job, **result})
            finally:
                self.active.remove(run_id)

    def get(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            return None
        path = self.metadata / (run_id + '.json')
        # Читаем ранее созданные результаты без переноса или изменения файлов.
        if not path.exists():
            path = self.root / '.dashboard' / run_id / 'job.json'
        if path.resolve() != path:
            return None
        with self.lock:
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
            if not isinstance(job, dict):
                return None
            if job.get('status') == 'running' and run_id not in self.active:
                job['status'] = 'unknown'
            return job
