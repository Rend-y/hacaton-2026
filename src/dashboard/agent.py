import json
import re

from src.dashboard.jobs import FINISHED_STATUSES, WEB_RUN_NAME, _read_object
from src.files.input_loader import MAX_QUESTION_CHARS


_REPAIR = re.compile(r'Готовим исправление (\d+)/')


def _trace_events(text):
    events = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _model_text(event):
    response = event.get('response')
    if isinstance(response, str) and response.strip():
        return response.strip()
    error = event.get('parse_error') or event.get('error')
    return error.strip() if isinstance(error, str) else ''


def _check_text(events, restored):
    lines = []
    if restored:
        lines.append('Цитата восстановлена дословно из файла.')
    for event in events:
        answer = event.get('answer') if isinstance(event.get('answer'), dict) else {}
        status = answer.get('status') if isinstance(answer.get('status'), str) else ''
        if status:
            lines.append('Статус: %s.' % status)
        for item in answer.get('evidence') or []:
            if not isinstance(item, dict):
                continue
            filename = item.get('file') if isinstance(item.get('file'), str) else ''
            quote = item.get('quote') if isinstance(item.get('quote'), str) else ''
            if filename and quote:
                lines.append('Цитата в %s: %s' % (filename, quote))
            elif filename or quote:
                lines.append('Цитата: %s' % (filename or quote))
        errors = [error.strip() for error in event.get('errors') or [] if isinstance(error, str) and error.strip()]
        if errors:
            lines.extend('Не пройдено: %s' % error for error in errors)
        elif status == 'не знаю':
            lines.append('Осторожный отказ: цитаты и числа не проверялись.')
        elif status:
            lines.append('Код проверил цитаты, числа, даты и время.')
    return '\n'.join(lines)


def stages_from_trace(trace_text, progress_text=''):
    events = _trace_events(trace_text)
    responses, checks, restored = {}, {}, set()
    requested = set()
    for event in events:
        attempt = event.get('attempt')
        if not isinstance(attempt, int) or attempt < 0:
            continue
        kind = event.get('event')
        if kind == 'api_request':
            requested.add(attempt)
        elif kind == 'quote_restored':
            restored.add(attempt)
        elif kind == 'validation':
            checks.setdefault(attempt, []).append(event)
        elif kind == 'api_error':
            responses.setdefault(attempt, _model_text(event))
        elif kind is None and 'response' in event:
            responses[attempt] = _model_text(event)
    repairs = {int(match) for match in _REPAIR.findall(progress_text)}
    if 'Готовим первичный запрос' in progress_text:
        requested.add(0)
    attempts = sorted(requested | repairs | set(responses) | set(checks) | restored)
    stages = []
    for attempt in attempts:
        if attempt == 0:
            if 0 in requested or 0 in responses or 0 in checks:
                stages.append({'title': 'Запрашиваем данные...', 'detail': responses.get(0, '')})
        elif attempt in requested or attempt in repairs or attempt in responses or attempt in checks:
            stages.append({'title': 'Уточняем повторно...', 'detail': responses.get(attempt, '')})
        if attempt in checks or attempt in restored:
            stages.append({'title': 'Проверка ответа...', 'detail': _check_text(checks.get(attempt, []), attempt in restored)})
    return stages


def _answer_text(answers):
    if not isinstance(answers, list):
        return ''
    blocks = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        status = item.get('status') if isinstance(item.get('status'), str) else ''
        text = item.get('answer') if isinstance(item.get('answer'), str) else ''
        reason = item.get('reason') if isinstance(item.get('reason'), str) else ''
        if status and status != 'ответ':
            block = status + (': ' + text if text else '')
        else:
            block = text or status
        if reason and reason not in block:
            block += '\n' + reason
        quotes = []
        for evidence in item.get('evidence') or []:
            if not isinstance(evidence, dict):
                continue
            filename = evidence.get('file') if isinstance(evidence.get('file'), str) else ''
            quote = evidence.get('quote') if isinstance(evidence.get('quote'), str) else ''
            if filename and quote:
                quotes.append('%s — %s' % (filename, quote))
            elif filename or quote:
                quotes.append(filename or quote)
        if quotes:
            block += '\n\n' + '\n'.join(quotes)
        if block:
            blocks.append(block)
    return '\n\n'.join(blocks)


class AgentSessions:
    def __init__(self, jobs):
        self.jobs = jobs

    def start(self, files, prompt):
        label = ' '.join(prompt.split())[:80] or 'Агент'
        return self.jobs.start(files, [prompt], 'verified', label=label)

    def view(self, run_id):
        if not WEB_RUN_NAME.fullmatch(run_id):
            raise ValueError('Диалог не найден.')
        with self.jobs.lock:
            job = _read_object(self.jobs.metadata / run_id / 'job.json')
            if job.get('status') == 'running' and run_id not in self.jobs.active:
                job = {**job, 'status': 'unknown'}
        if not job:
            raise ValueError('Диалог не найден.')
        directory = self.jobs.root / run_id
        try:
            log = (directory / 'progress.log').read_text(encoding='utf-8') if (directory / 'progress.log').is_file() else ''
            trace = (directory / 'trace.jsonl').read_text(encoding='utf-8') if (directory / 'trace.jsonl').is_file() else ''
        except OSError:
            log, trace = '', ''
        status = job.get('status') or 'running'
        answer = None
        if status in FINISHED_STATUSES:
            answers_path = directory / 'answers.json'
            try:
                if answers_path.is_file():
                    answer = _answer_text(json.loads(answers_path.read_text(encoding='utf-8'))) or None
            except (OSError, ValueError):
                answer = None
        error = job.get('error') if isinstance(job.get('error'), str) else None
        if status == 'unknown':
            error = error or 'Обработка прервана.'
        elif status in FINISHED_STATUSES and not answer and not error:
            answer = 'Агент завершил обработку без текста ответа.'
        return {'id': run_id, 'status': status, 'stages': stages_from_trace(trace, log), 'answer': answer, 'error': error}


def agent_prompt(prompt):
    text = prompt.strip()
    if not text or len(text) > MAX_QUESTION_CHARS:
        raise ValueError('Напишите вопрос до 10 000 символов.')
    return text
