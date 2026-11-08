"""Команда запроса к документам и коды завершения CLI."""
import json
import sys
from typing import Annotated, Literal

from cyclopts import App, Parameter
from cyclopts.exceptions import CycloptsError

from src.files.document_sets import DocumentChats, DocumentSets, local_documents
from src.generation.client import ApiError
from src import settings
from src.settings import QueryOptions


app = App(name='python -m src', help='Ответы по вашим документам: файлы, вопросы, проверки и HTML-отчёт.',
          help_format='plaintext', group_commands='Команды', group_parameters='Параметры',
          version_flags=[], suppress_keyboard_interrupt=False)


@app.command
def query(
    *,
    attachment: Annotated[list[str], Parameter(consume_multiple=1, negative=False, json_list=False,
        help='Один или несколько файлов документов. Укажите путь к каждому файлу.')],
    prompt: Annotated[list[str], Parameter(consume_multiple=1, negative=False, json_list=False,
        help='Один или несколько вопросов. Передайте каждый вопрос отдельным аргументом.')],
    mode: Annotated[Literal['baseline', 'verified'], Parameter(
        help='baseline — прямой ответ; verified — локальные проверки и исправления.')] = 'verified',
) -> int:
    """Ответить на вопросы по общему комплекту переданных файлов."""
    from src.workflows.query import run_query
    return run_query(QueryOptions(attachment=attachment, prompt=prompt, mode=mode))


@app.command
def upload_documents(
    *,
    attachment: Annotated[list[str], Parameter(consume_multiple=1, negative=False, json_list=False,
        help='Пути к файлам для сохранения в комплекте.')],
    title: Annotated[str | None, Parameter(help='Название отдельного комплекта.')] = None,
    chat_id: Annotated[str | None, Parameter(help='ID чата MCP, к которому нужно добавить файлы.')] = None,
) -> int:
    """Сохранить отдельный комплект или добавить документы в текущий MCP-чат."""
    if (title is None) == (chat_id is None):
        print('Укажите --title для отдельного комплекта или --chat-id для текущего чата.', file=sys.stderr)
        return 2
    files = local_documents(attachment)
    result = (DocumentChats(settings.ROOT).append(chat_id, files) if chat_id is not None
              else DocumentSets(settings.ROOT).save(title, files))
    print(json.dumps(result, ensure_ascii=False))
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
