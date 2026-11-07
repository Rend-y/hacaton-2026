"""Читаемый журнал этапов и честный индикатор ожидания ответа сервера."""
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path


PRINT_LOCK = threading.Lock()


def emit(text):
    """Печатает прогресс без перемешивания строк с сообщениями ожидания API."""
    with PRINT_LOCK:
        print(text, flush=True)


class RunLog:
    """Пишет одинаковые строки в терминал и файл, скрывая ключи и управляющие коды."""

    def __init__(self, path, emit, secrets=()):
        self.path = Path(path) if path else None
        self.emit = emit
        self.secrets = tuple(value for value in secrets if value)
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def __call__(self, message):
        text = str(message)
        for secret in self.secrets:
            text = text.replace(secret, '[скрыто]')
        # Ответ модели не должен управлять курсором терминала или подделывать строки.
        text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        text = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        with self.lock:
            line = '[+%.1f с] %s' % (time.monotonic() - self.started, text)
            if self.path:
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(line + '\n')
            self.emit(line)


@contextmanager
def heartbeat(callback, interval=10):
    """Сообщает только прошедшее время ожидания, не выдумывая прогресс работы ИИ.

    Сигнал остановки и join гарантируют, что после получения ответа либо ошибки
    фоновые сообщения ожидания больше не появятся.
    """
    if callback is None:
        yield
        return
    stopped = threading.Event()
    started = time.monotonic()

    def notify():
        while not stopped.wait(interval):
            callback(time.monotonic() - started)

    worker = threading.Thread(target=notify, name='api-wait-progress', daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
