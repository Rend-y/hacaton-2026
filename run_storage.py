"""Сохранение JSON и CSV запуска без зависимости от командной строки."""
import csv
import json

from answers import FIELDS, csv_row


def write_json(path, value):
    """Сохраняет читаемый JSON с русским текстом в UTF-8."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def save_answers(output, questions, answers):
    """Сохраняет результат в порядке вопросов; CSV заменяется только после записи."""
    ordered = [answers[q['id']] for q in questions if q['id'] in answers]
    temporary = output / 'answers.csv.tmp'
    with temporary.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=';')
        writer.writeheader()
        writer.writerows(csv_row(a) for a in ordered)
    temporary.replace(output / 'answers.csv')
    write_json(output / 'answers.json', ordered)
