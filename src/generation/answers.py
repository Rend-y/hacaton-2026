"""Формат ответа модели, безопасный расчёт и представление для CSV."""
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

FIELDS = ['id', 'status', 'answer', 'quote', 'file']
BASELINE_FIELDS = frozenset(FIELDS)


def _response_json(content):
    text = re.sub(r'<think>.*?</think>', '', content, flags=re.S).strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def parse_answers(content):
    """Разбирает JSON модели; дубли ID считаются ошибкой, а не скрыто заменяются."""
    parsed = _response_json(content)
    rows = parsed.get('answers') if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError('Expected JSON object with answers list')
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise ValueError('Answer must be an object with string id')
        if row['id'] in found:
            raise ValueError('Duplicate answer ID')
        found[row['id']] = row
    return found


def parse_baseline_answers(content):
    """Приводит плоские ответы к формату выгрузки, пропуская некорректные строки."""
    answers = {}
    for question_id, row in parse_answers(content).items():
        if row.keys() != BASELINE_FIELDS or not all(isinstance(value, str) for value in row.values()):
            continue
        quote, file = row['quote'], row['file']
        answers[question_id] = {'id': question_id, 'status': row['status'], 'answer': row['answer'],
                                'evidence': [{'quote': quote, 'file': file}] if quote or file else []}
    return answers


def calculate(answer):
    """Считает процент через Decimal; основания операндов затем проверяет валидатор."""
    calculation = answer.get('calculation')
    if not isinstance(calculation, dict) or calculation.get('operation') != 'percent' or answer.get('status') != 'ответ':
        return answer
    try:
        values = []
        for key in ('base', 'rate'):
            value = str(calculation.get(key, ''))
            if len(value) > 80 or not re.fullmatch(r'\d+(?:\.\d+)?', value):
                return answer
            values.append(Decimal(value))
        with localcontext() as context:
            context.prec = 180
            total = (values[0] * values[1] / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return {**answer, 'answer': format(total, 'f') + ' руб.'}
    except (InvalidOperation, ValueError):
        return answer


def abstain(question, reason):
    """Формирует осторожный отказ с причиной вместо неподтверждённого ответа."""
    return {'id': question['id'], 'status': 'не знаю', 'answer': '', 'evidence': [], 'reason': reason}


def csv_row(answer):
    """Преобразует внутренний ответ в пять колонок формата организаторов."""
    # В CSV помещается одна цитата; остальные основания остаются в JSON и журнале.
    evidence = answer.get('evidence')
    first = evidence[0] if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict) else {}
    status = answer.get('status', '')
    empty = status in ('не знаю', 'нет в документах')
    text = answer.get('answer', '')
    quote = first.get('quote', '')
    return {'id': answer['id'], 'status': status if isinstance(status, str) else '',
            'answer': re.sub(r'\s+', ' ', text).strip() if isinstance(text, str) and not empty else '',
            'quote': re.sub(r'\s+', ' ', quote).strip() if isinstance(quote, str) and not empty else '',
            'file': first.get('file', '') if isinstance(first.get('file', ''), str) and not empty else ''}
