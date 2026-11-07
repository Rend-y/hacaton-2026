"""Запрос к модели, локальная проверка и исправление неподтверждённых ответов.

Движок получает готовые тексты. Он не знает, где лежали исходные файлы,
и не выбирает вопросы из эталонного набора.
"""
import re
import time

from src.generation.answers import abstain, calculate, parse_answers
from src.generation.client import ApiError, ResponseTruncated
from src.progress import RunLog, emit
from src.generation.prompts import messages
from src.settings import MAX_QUESTIONS_PER_REQUEST
from src.generation.validation import norm, validate_answer


def process_questions(client, mode, package, questions, documents, read_errors, attempts, trace, progress=None,
                      *, _start_attempt=0, _correction=None, _batch='root'):
    """Обрабатывает общий комплект документов; повторяет только ответы с ошибками проверок.

    В baseline содержательные ошибки сохраняются для честного исходного замера.
    В verified неподтверждённый ответ исправляется либо заменяется на «не знаю».
    """
    accepted = {}
    pending = questions[:]
    correction = _correction
    original_trace = trace

    def trace(event):
        original_trace({'batch': _batch, **event})
    log = progress if progress is not None else RunLog(None, emit)

    def say(message):
        log('[%s] %s' % (package, message))

    def api_progress(event):
        """Связывает безопасные события API с текущим контекстом, терминалом и журналом JSON."""
        kind = event['event']
        if kind == 'rate_limit_wait':
            say('Локальный лимит запросов/токенов: ожидание %.0f с.' % event['wait_seconds'])
        elif kind == 'api_request':
            say('API: отправляем запрос, HTTP-попытка %s/3; таймаут %s с.' %
                (event['http_attempt'], event['timeout_seconds']))
        elif kind == 'api_wait':
            say('API: ожидаем ответ сервера уже %.0f с; локальная проверка ещё не началась.' % event['elapsed_seconds'])
        elif kind == 'api_response':
            say('API: ответ получен за %.1f с; токены входа: %s, выхода: %s.' %
                (event['elapsed_seconds'], event['prompt_tokens'], event['completion_tokens']))
        elif kind == 'api_error':
            say('API: HTTP %s.' % event['http_status'] if 'http_status' in event else 'API: сетевая ошибка или таймаут.')
        elif kind == 'api_retry':
            say('API: повтор того же запроса через %.0f с, HTTP-попытка %s/3. Это сетевой повтор, не исправление ответа.' %
                (event['wait_seconds'], event['next_http_attempt']))
        trace({'package': package, 'attempt': attempt, **event})

    say('Загружено документов: %s; символов: %s; вопросов: %s.' %
        (len(documents), sum(len(text) for text in documents.values()), len(questions)))
    if documents:
        say('Файлы: ' + ', '.join(documents))
    for error in read_errors:
        say('Проблема чтения: ' + error)
    for question in questions:
        say('%s — вопрос: %s' % (question['id'], question['question'][:200]))
    if not documents:
        say('Нет читаемых документов: API не вызываем, возвращаем «не знаю».')
        trace({'package': package, 'event': 'unreadable', 'read_errors': read_errors})
        return {q['id']: {**abstain(q, '; '.join(read_errors)), 'error_type': 'input_read'} for q in questions}
    if len(pending) > MAX_QUESTIONS_PER_REQUEST:
        # Ограничение общее для любых документов и режимов, без привязки к пакету.
        groups = [pending[i:i + MAX_QUESTIONS_PER_REQUEST]
                  for i in range(0, len(pending), MAX_QUESTIONS_PER_REQUEST)]
        say('Заранее делим %s вопросов на %s групп: %s; максимум %s вопросов в запросе.' %
            (len(pending), len(groups), ', '.join(str(len(group)) for group in groups),
             MAX_QUESTIONS_PER_REQUEST))
        trace({'package': package, 'event': 'batch_plan', 'limit': MAX_QUESTIONS_PER_REQUEST,
               'sizes': [len(group) for group in groups]})
        for number, subset in enumerate(groups, 1):
            say('Группа %s/%s: отправляем %s вопросов со всем комплектом документов.' %
                (number, len(groups), len(subset)))
            ids = {q['id'] for q in subset}
            subset_correction = [item for item in correction or [] if item['id'] in ids] or None
            accepted.update(process_questions(client, mode, package, subset, documents, read_errors,
                attempts, trace, log, _start_attempt=_start_attempt, _correction=subset_correction,
                _batch=_batch + '.' + str(number)))
        return accepted
    for attempt in range(_start_attempt, 1 + (attempts if mode == 'verified' else 0)):
        requested_ids = {q['id'] for q in pending}
        request_messages = messages(mode, documents, pending, read_errors, correction)
        started = time.monotonic()
        stage = 'первичный запрос' if attempt == 0 else 'исправление %s/%s' % (attempt, attempts)
        say('Готовим %s: весь комплект документов и %s вопросов. Эталоны модели не передаются.' % (stage, len(pending)))
        try:
            content, meta = client.complete(request_messages, progress=api_progress)
            say('Локально: разбираем JSON и проверяем идентификаторы вопросов.')
            try:
                candidates = parse_answers(content)
                if set(candidates) - requested_ids:
                    raise ValueError('Response contains unrequested question IDs')
                parse_error = None
            except (ValueError, TypeError):
                candidates = {}
                parse_error = 'Response was not valid answers JSON or IDs were invalid'
                say('Локально: JSON или идентификаторы некорректны; ответы текущего запроса нужно исправить.')
            if parse_error is None:
                say('Локально: JSON разобран, записей ответа: %s.' % len(candidates))
            trace({'package': package, 'attempt': attempt, 'question_ids': sorted(requested_ids),
                   'elapsed_seconds': round(time.monotonic() - started, 3),
                   'response': re.sub(r'<think>.*?</think>', '', content, flags=re.S),
                   'api': meta, 'parse_error': parse_error})
        except ApiError as exc:
            reason = str(exc)
            trace({'package': package, 'attempt': attempt, 'event': 'api_error', 'error': reason})
            if isinstance(exc, ResponseTruncated) and len(pending) > 1:
                middle = len(pending) // 2
                say('Ответ обрезан по лимиту токенов. Делим %s вопросов на группы %s и %s; итоговая выгрузка остаётся общей.' %
                    (len(pending), middle, len(pending) - middle))
                trace({'package': package, 'attempt': attempt, 'event': 'batch_split',
                       'question_ids': [q['id'] for q in pending], 'sizes': [middle, len(pending) - middle]})
                for number, subset in enumerate((pending[:middle], pending[middle:]), 1):
                    ids = {q['id'] for q in subset}
                    subset_correction = [item for item in correction or [] if item['id'] in ids] or None
                    # Не теряем принятые ответы и не обнуляем бюджет исправлений.
                    accepted.update(process_questions(client, mode, package, subset, documents, read_errors,
                        attempts, trace, log, _start_attempt=attempt, _correction=subset_correction,
                        _batch=_batch + '.' + str(number)))
                return accepted
            if isinstance(exc, ResponseTruncated):
                say('Обрезан ответ даже на один вопрос: дальнейшее деление невозможно, фиксируем технический отказ.')
            say('API не дал пригодного ответа: %s. Ожидающим вопросам назначаем «не знаю» с технической причиной.' % reason)
            for q in pending:
                accepted[q['id']] = {**abstain(q, reason), 'error_type': 'api'}
            return accepted
        next_pending, correction = [], []
        for question in pending:
            raw = candidates.get(question['id'])
            errors = []
            if raw is None:
                say('%s: ответа в JSON нет; фиксируем ошибку формата.' % question['id'])
                errors = [parse_error or 'Missing answer ID']
                answer = abstain(question, errors[0])
            elif mode == 'verified':
                status = norm(raw['status']) if isinstance(raw.get('status'), str) else ''
                say('%s: статус модели — «%s».' % (question['id'], status or 'не указан'))
                if status == 'нет в документах':
                    say('%s: модель сообщает об отсутствии сведений. Локально проверяем читаемость документов; отсутствие ответа кодом не доказано.' % question['id'])
                elif status == 'не знаю':
                    say('%s: осторожный отказ модели; факт и цитаты не проверяем.' % question['id'])
                else:
                    say('%s: локально проверяем статус, цитаты в указанных файлах, числа, даты и время; для расхождения нужны два файла.' % question['id'])
                answer = calculate(raw)
                if answer is not raw:
                    say('%s: процент пересчитан кодом: %s. Теперь проверяем основания обоих операндов.' %
                        (question['id'], answer['answer']))
                errors = validate_answer(answer, documents, read_errors)
                if not errors:
                    answer = {**answer, 'status': norm(answer['status'])}
                    if status == 'нет в документах':
                        say('%s: статус допустим по локальным условиям; полнота поиска остаётся оценкой модели.' % question['id'])
                    elif status != 'не знаю':
                        say('%s: локальные проверки пройдены, подтверждённых цитат: %s. Это не независимая проверка смысла.' %
                            (question['id'], len(answer.get('evidence') or [])))
            else:
                answer = raw
                say('%s: baseline — сохраняем ответ модели без содержательных проверок и исправлений.' % question['id'])
            for error in errors:
                say('%s: проверка не пройдена — %s' % (question['id'], error))
            trace({'package': package, 'id': question['id'], 'attempt': attempt,
                   'event': 'validation' if mode == 'verified' else 'baseline_answer',
                   'errors': errors, 'answer': answer})
            if errors and mode == 'verified' and attempt < attempts:
                next_pending.append(question)
                correction.append({'id': question['id'], 'answer': raw, 'errors': errors})
                say('%s: отправим вопрос на исправление вместе с причинами ошибок.' % question['id'])
            elif errors:
                accepted[question['id']] = abstain(question, '; '.join(errors))
                if raw is None:
                    accepted[question['id']]['error_type'] = 'model_format'
                say('%s: попытки исправления закончились либо отключены; итог — «не знаю».' % question['id'])
            else:
                accepted[question['id']] = answer
                say('%s: итог — «%s»%s.' % (question['id'], str(answer.get('status', ''))[:60],
                    '; ' + str(answer['answer'])[:200] if answer.get('answer') else ''))
        say('Итерация %s завершена: зафиксировано %s/%s ответов, на исправление — %s.' %
            (attempt + 1, len(accepted), len(questions), len(next_pending)))
        pending = next_pending
        if not pending:
            break
    return accepted
