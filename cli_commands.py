"""Типизированные команды Cyclopts: параметры, справка и вызовы прикладных модулей."""
import json
from pathlib import Path
import sys
from typing import Annotated, Literal

from cyclopts import App, Group, Parameter
from cyclopts.exceptions import CycloptsError
from cyclopts.validators import LimitedChoice

from client import ApiError
import settings
from progress import emit


app = App(name='cli.py', help='Ответы по вашим документам: файлы, вопросы, проверки и HTML-отчёт.',
          help_format='plaintext', group_commands='Команды', group_parameters='Параметры',
          version_flags=[], suppress_keyboard_interrupt=False)


@app.command
def query(
    *,
    atacment: Annotated[list[str], Parameter(consume_multiple=1, negative=False, json_list=False,
        help='Файл, несколько файлов или папки с документами. Папки читаются рекурсивно.')],
    promt: Annotated[list[str], Parameter(consume_multiple=1, negative=False, json_list=False,
        help='Один или несколько вопросов в кавычках либо файлы вопросов TXT/MD/RST/CSV/JSON.')],
    mode: Annotated[Literal['baseline', 'verified'], Parameter(
        help='baseline — прямой ответ; verified — локальные проверки и исправления.')] = 'verified',
    evaluate: Annotated[bool, Parameter(negative=False,
        help='После генерации оценить вопросы, строго сопоставленные с известными эталонами.')] = False,
) -> int:
    """Задать вопросы к произвольным документам, без обязательной структуры папок."""
    from query import run_query
    return run_query(settings.QueryOptions(atacment=atacment, promt=promt, mode=mode, evaluate=evaluate))


@app.command
def evaluate(
    *,
    run: Annotated[Path, Parameter(help='Папка сохранённого запуска с ответами.')],
    data: Annotated[Path | None, Parameter(show_default=False,
        help='Каталог эталонов только для оценки; по умолчанию data/public внутри проекта.')] = None,
) -> int:
    """Оценить сохранённые ответы и обновить HTML без обращения к модели."""
    from evaluation import evaluate_saved_run
    from report import write_run_report
    evaluate_saved_run(run.resolve(), (data or settings.ROOT / 'data' / 'public').resolve())
    emit('HTML-отчёт: ' + str(write_run_report(run)))
    return 0


@app.command
def compare(
    *,
    baseline: Annotated[Path, Parameter(help='Оценённый запуск в режиме baseline.')],
    verified: Annotated[Path, Parameter(help='Оценённый запуск verified на тех же данных.')],
    output: Annotated[Path, Parameter(help='Папка для comparison.json, comparison.csv и HTML.')],
) -> int:
    """Сравнить два совместимых оценённых запуска без повторной генерации."""
    from evaluation import compare_runs
    from report import write_comparison_report
    result = compare_runs(baseline.resolve(), verified.resolve(), output.resolve())
    emit(json.dumps(result, ensure_ascii=False, indent=2))
    emit('HTML-отчёт: ' + str(write_comparison_report(output)))
    return 0


# Ограничение источников проверяет библиотека ещё до чтения файлов отчёта.
report_source = Group('Источник отчёта: выберите один', validator=LimitedChoice(min=1, max=1))


@app.command
def report(
    *,
    run: Annotated[Path | None, Parameter(group=report_source,
        help='Папка одного сохранённого запуска.')] = None,
    comparison: Annotated[Path | None, Parameter(group=report_source,
        help='Папка с comparison.json и доступными исходными запусками.')] = None,
) -> int:
    """Пересобрать HTML по сохранённым файлам, без генерации и оценивания."""
    from report import write_run_report, write_comparison_report
    path = write_run_report(run) if run is not None else write_comparison_report(comparison)
    emit('HTML-отчёт: ' + str(path))
    return 0


def main(argv=None):
    """Сохраняет коды 0/1/2, а ошибки синтаксиса и справку оформляет Cyclopts."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        result = app(tokens or ['--help'], exit_on_error=False, print_error=True, result_action='return_value')
        return result if isinstance(result, int) else 0
    except CycloptsError:
        return 2
    except (ValueError, OSError, ApiError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Остановлено пользователем. Для нового запуска будет создана отдельная папка.', file=sys.stderr)
        return 130
