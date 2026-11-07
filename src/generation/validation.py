"""Проверки структуры ответа, цитат и числовых значений без обращения к модели.

Успешные проверки подтверждают согласованность данных, но не гарантируют верность
смысла: модель по-прежнему определяет нужное условие, отрицание и редакцию.
Исходный ответ и документы не изменяются, чтобы сохранить основания проверки.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from difflib import get_close_matches
from typing import Any


STATUSES = frozenset(('ответ', 'расхождение', 'нет в документах', 'не знаю'))
_DATE = re.compile(r'(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)')
_ISO_DATE = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)')
_TIME = re.compile(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)')
_NUMBER = re.compile(
    r'(?<![\w])[-+]?\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?(?![\w])'
)
_PARAGRAPH = re.compile(r'^\s*\d+(?:\.\d+)*[.)]\s+', re.MULTILINE)
_CASE_ENDING = re.compile(r'\b[а-я]{5,}([еы])\b')


def norm(text: str) -> str:
    """Нормализовать цитату по правилам официального оценщика."""
    text = (text or '').lower().replace('ё', 'е').replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[«»„“”"\']', '"', text)
    text = re.sub(r'[–—−]', '-', text)
    return re.sub(r'\s+', ' ', text).strip()


def _quote_hint(normalized_quote: str, document: str) -> str | None:
    """Найти одну похожую строку для подсказки, сохранив исходный текст."""
    lines = (line.strip() for line in document.splitlines())
    candidates = {norm(line): line for line in lines if 8 <= len(line) <= 350}
    matches = get_close_matches(normalized_quote, candidates, n=2, cutoff=0.7)
    return candidates[matches[0]] if len(matches) == 1 else None


def _same_quote_with_case_ending(quote: str, source: str) -> bool:
    # Модель может опустить номер пункта, но не заменить его другим номером.
    if not _PARAGRAPH.match(quote):
        source = _PARAGRAPH.sub('', source)
    if len(quote) != len(source):
        return False
    differences = [i for i, (original, exact) in enumerate(zip(quote, source)) if original != exact]
    if len(differences) != 1:
        return False
    index = differences[0]
    return source[index] in 'еы' and any(match.start(1) == index for match in _CASE_ENDING.finditer(quote))


def restore_quote_endings(answer: dict, docs: dict[str, str]) -> dict:
    """Копирует из файла однозначную строку с одной ошибкой в падежном окончании."""
    evidence = answer.get('evidence')
    status = answer.get('status')
    if not isinstance(status, str) or norm(status) != 'ответ' or not isinstance(evidence, list):
        return answer
    restored = evidence[:]
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            continue
        filename, quote = item.get('file'), item.get('quote')
        if not isinstance(filename, str) or filename not in docs or not isinstance(quote, str):
            continue
        document, normalized_quote = docs[filename], norm(quote)
        if len(normalized_quote) < 40 or normalized_quote in norm(document):
            continue
        source = _quote_hint(normalized_quote, document)
        if source and _same_quote_with_case_ending(normalized_quote, norm(source)):
            restored[index] = {**item, 'quote': source}
    return {**answer, 'evidence': restored} if restored != evidence else answer


def _decimal(value: Any) -> Decimal | None:
    """Прочитать обычное десятичное число без вычисления выражений из ответа."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    raw = str(value).strip().replace('\xa0', ' ').replace('\u202f', ' ')
    # Ограничение длины и формата защищает расчёт от выражений и чрезмерных чисел.
    if len(raw) > 80 or not re.fullmatch(r'[-+]?\d+(?: \d{3})*(?:[.,]\d+)?', raw):
        return None
    try:
        result = Decimal(raw.replace(' ', '').replace(',', '.'))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _values(text: str) -> tuple[set[Decimal], set[str], set[str]]:
    """Отделить даты и время: год или час не подтверждают денежную сумму."""
    text = _PARAGRAPH.sub('', text)
    dates: set[str] = set()
    times: set[str] = set()

    def extract_date(match: re.Match[str], iso: bool = False) -> str:
        parts = [int(part) for part in match.groups()]
        year, month, day = parts if iso else (parts[2], parts[1], parts[0])
        try:
            dates.add(date(year, month, day).isoformat())
        except ValueError:
            dates.add('invalid:' + match.group())
        return ' '

    def extract_time(match: re.Match[str]) -> str:
        hour, minute, second = match.groups()
        times.add(f'{int(hour):02d}:{minute}:{second or "00"}')
        return ' '

    text = _DATE.sub(extract_date, text)
    text = _ISO_DATE.sub(lambda match: extract_date(match, True), text)
    text = _TIME.sub(extract_time, text)
    # Номера пунктов удалены до склейки строк: они не являются значениями условий.
    text = norm(text)
    numbers = set()
    for match in _NUMBER.finditer(text):
        value = _decimal(match.group())
        if value is not None:
            numbers.add(value)
    return numbers, dates, times


def _display(value: Decimal) -> str:
    """Показать число в ошибке обычной записью, без экспоненциального формата."""
    return format(value, 'f')


def _validate_calculation(calculation: Any, evidence: list[Any], answer_numbers: set[Decimal]) -> list[str]:
    """Проверить разрешённый расчёт процента и цитаты для обоих операндов."""
    errors: list[str] = []
    if not isinstance(calculation, dict) or calculation.get('operation') != 'percent':
        return ['calculation: поддерживается только operation="percent".']
    base = _decimal(calculation.get('base'))
    rate = _decimal(calculation.get('rate'))
    if base is None or rate is None or base < 0 or rate < 0:
        return ['calculation: base и rate должны быть конечными неотрицательными числами.']
    for field, value in (('base', base), ('rate', rate)):
        index = calculation.get(field + '_evidence')
        if type(index) is not int or not 0 <= index < len(evidence):
            errors.append(f'calculation: {field}_evidence должен указывать индекс существующей цитаты (с нуля).')
            continue
        item = evidence[index]
        quote = item.get('quote') if isinstance(item, dict) else None
        if not isinstance(quote, str) or value not in _values(quote)[0]:
            errors.append(f'calculation: значение {field}={_display(value)} отсутствует в указанной цитате.')
    # Decimal исключает погрешности двоичной арифметики; сумму округляем до копеек.
    with localcontext() as context:
        context.prec = max(28, len(base.as_tuple().digits) + len(rate.as_tuple().digits) + 12)
        result = (base * rate / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if result not in answer_numbers:
        errors.append(f'calculation: результат должен быть {_display(result)} руб. (base × rate / 100).')
    allowed = {base, rate, result, Decimal(100)}
    unsupported = answer_numbers - allowed
    if unsupported:
        errors.append('calculation: лишние неподтверждённые числа в ответе: ' + ', '.join(_display(n) for n in sorted(unsupported)) + '.')
    return errors


def validate_answer(answer: dict, docs: dict[str, str], read_errors: list[str] | None = None) -> list[str]:
    """Вернуть причины ошибок; пустой список означает прохождение этих проверок.

    «Не знаю» допустимо как осторожный отказ. Утверждать отсутствие сведений
    нельзя, если пакет прочитан не полностью. Цитату ищем именно в указанном
    файле: совпадение в другом документе не подтверждает заявленный источник.
    Смысл показателя, его единицы и применимость редакции оценивает модель;
    проверка чисел не заменяет эту смысловую оценку.
    """
    if not isinstance(answer, dict):
        return ['Ответ должен быть JSON-объектом.']
    status = answer.get('status')
    if not isinstance(status, str) or norm(status) not in STATUSES:
        return ['Недопустимый status; допустимы: ответ, расхождение, нет в документах, не знаю.']
    status = norm(status)
    if status == 'не знаю':
        return []
    if status == 'нет в документах':
        # Ошибка чтения — пробел во входных данных, а не доказательство отсутствия.
        if read_errors:
            return ['Нельзя утверждать «нет в документах»: некоторые файлы не прочитаны; используйте «не знаю».']
        if not any(isinstance(text, str) and text.strip() for text in docs.values()):
            return ['Нельзя утверждать «нет в документах»: нет читаемых документов; используйте «не знаю».']
        return []

    errors: list[str] = []
    text = answer.get('answer')
    if not isinstance(text, str) or not text.strip():
        errors.append('Поле answer должно содержать непустой текст ответа.')
        text = ''
    evidence = answer.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        return errors + ['Требуется непустой список evidence с file и дословной quote.']

    valid_evidence: list[str] = []
    evidence_files: set[str] = set()
    for index, item in enumerate(evidence):
        label = f'evidence[{index}]'
        if not isinstance(item, dict):
            errors.append(f'{label}: требуется объект с file и quote.')
            continue
        filename, quote = item.get('file'), item.get('quote')
        if not isinstance(filename, str) or filename not in docs:
            errors.append(f'{label}: файл не найден в текущей закупке.')
            continue
        normalized_quote = norm(quote) if isinstance(quote, str) else ''
        if len(normalized_quote) < 8:
            errors.append(f'{label}: дословная цитата должна содержать не менее 8 символов после нормализации.')
            continue
        # Сначала проверяем принадлежность цитаты файлу, затем используем её числа.
        document = docs[filename]
        if normalized_quote not in norm(document):
            error = f'{label}: цитата не найдена в указанном файле {filename}.'
            hint = _quote_hint(normalized_quote, document)
            if hint:
                error += f' Похожий фрагмент из файла: {hint!r}. Если он подтверждает ответ, скопируй его дословно.'
            errors.append(error)
            continue
        valid_evidence.append(quote)
        evidence_files.add(filename)

    if status == 'расхождение' and len(evidence_files) < 2:
        errors.append('Для расхождения нужны подтверждённые цитаты как минимум из двух разных файлов.')

    numbers, dates, times = _values(text)
    source_numbers: set[Decimal] = set()
    source_dates: set[str] = set()
    source_times: set[str] = set()
    for quote in valid_evidence:
        values, quote_dates, quote_times = _values(quote)
        source_numbers.update(values)
        source_dates.update(quote_dates)
        source_times.update(quote_times)

    calculation = answer.get('calculation')
    if calculation is not None:
        if status == 'расхождение':
            errors.append('Не сводите расхождение к одному расчёту: покажите оба действующих значения.')
        errors.extend(_validate_calculation(calculation, evidence, numbers))
    else:
        unsupported = numbers - source_numbers
        if unsupported:
            errors.append('Числа из ответа не подтверждены цитатами: ' + ', '.join(_display(n) for n in sorted(unsupported)) + '.')

    if dates - source_dates:
        errors.append('Дата из ответа не подтверждена цитатами: ' + ', '.join(sorted(dates - source_dates)) + '.')
    if times - source_times:
        errors.append('Время из ответа не подтверждено цитатами: ' + ', '.join(sorted(times - source_times)) + '.')
    return errors
