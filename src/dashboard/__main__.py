"""Отдельная точка входа локального дашборда."""
import argparse
import sys

from src.dashboard.server import serve


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(prog='python -m src.dashboard', description='Локальный дашборд.')
    parser.add_argument('--port', type=int, default=8765, help='Порт локального сервера.')
    options = parser.parse_args(argv)
    try:
        serve(options.port)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Ошибка запуска дашборда: {exc}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
