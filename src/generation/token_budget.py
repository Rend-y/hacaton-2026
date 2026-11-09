"""Local token counting; only public tokenizer data is downloaded, never documents."""
from functools import lru_cache
import hashlib
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading
import urllib.request

from src.settings import ROOT


QWEN_SHA256 = 'aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4'
QWEN_URL = 'https://huggingface.co/Qwen/Qwen3-8B/resolve/main/tokenizer.json'
TOKENIZER_CACHE = ROOT / '.cache' / 'tokenizers'
_LOAD_LOCK = threading.Lock()


class TokenCounter:
    def __init__(self, tokenizer, name):
        self.tokenizer, self.name = tokenizer, name
        # A tokenizer.json may have saved inference-time truncation/padding.
        # Counting must always see the entire input.
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def input_tokens(self, messages):
        # Allow for chat framing even when a provider uses a custom template.
        return 16 + sum(len(self.tokenizer.encode(message['content'], add_special_tokens=False).ids) + 32
                        for message in messages)

    def estimate(self, messages, max_tokens):
        # Reserve output separately; the 2% margin applies only to input.
        return math.ceil(self.input_tokens(messages) * 1.02) + max_tokens


def _qwen_path():
    path = TOKENIZER_CACHE / ('qwen3-' + QWEN_SHA256[:12] + '.json')
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == QWEN_SHA256:
        return path
    TOKENIZER_CACHE.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Bound public downloads. No model API credentials are used here.
        with urllib.request.urlopen(QWEN_URL, timeout=30) as response:
            data = response.read(16 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != QWEN_SHA256:
            raise ValueError('Контрольная сумма tokenizer.json Qwen3 не совпадает.')
        with NamedTemporaryFile(dir=TOKENIZER_CACHE, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(path)
        return path
    except OSError as exc:
        raise ValueError('Не удалось загрузить токенизатор Qwen3. Проверьте доступ к Hugging Face '
                         'или задайте TOKENIZER_PATH к локальному tokenizer.json.') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@lru_cache(maxsize=8)
def _load(kind, path):
    try:
        from tokenizers import Tokenizer
    except ImportError:
        raise ValueError('Для подсчёта токенов установите зависимости: pip install -r requirements.txt') from None
    resolved = _qwen_path() if kind == 'qwen3' else Path(path).expanduser()
    try:
        return TokenCounter(Tokenizer.from_file(str(resolved)), 'Qwen3' if kind == 'qwen3' else str(resolved))
    except Exception as exc:
        raise ValueError('Не удалось прочитать tokenizer.json: ' + str(resolved)) from exc


def get_token_counter(model, tokenizer_path=''):
    """Qwen3 aliases use the family tokenizer; custom models can supply their own.

    Unknown models retain the safe byte bound instead of pretending a different
    model's tokenizer is exact. Explicit paths always take precedence.
    """
    kind = 'local' if tokenizer_path else 'qwen3' if model.lower().rsplit('/', 1)[-1].startswith('qwen3-') else None
    if kind is None:
        return None
    # Avoid duplicate downloads/loads when concurrent worker threads start.
    with _LOAD_LOCK:
        return _load(kind, str(tokenizer_path))
