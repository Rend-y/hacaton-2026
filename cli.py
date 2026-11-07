#!/usr/bin/env python3
"""Точка входа: проверяет установку CLI-библиотеки и передаёт управление командам."""
import sys


def main(argv=None):
    """Возвращает код завершения; обработка документов находится в других модулях."""
    try:
        import cyclopts  # Проверяем зависимость до загрузки команд.
    except ModuleNotFoundError as exc:
        if exc.name != 'cyclopts':
            raise
        print('Не установлены зависимости CLI. Выполните .\\setup.ps1 или '
              'python -m pip install -r requirements.txt.', file=sys.stderr)
        return 1
    from cli_commands import main as execute
    return execute(argv)


if __name__ == '__main__':
    sys.exit(main())
