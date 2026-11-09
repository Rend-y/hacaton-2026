"""Token-based packing, with a real local tokenizer and no network/model calls."""
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tokenizers import Tokenizer, models, pre_tokenizers

from src.generation import token_budget
from src.generation.client import ApiError, Client, estimate_request_tokens
from src.generation.context_loop import DocumentLoop, validate_context
from src.generation.token_budget import TokenCounter, get_token_counter
from src.settings import RunOptions


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tokenizer.json'
        tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'документ': 1, 'longword': 2}, unk_token='[UNK]'))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.save(str(self.path))
        self.counter = TokenCounter(tokenizer, 'test-local')
        self.question = {'id': 'Q1', 'question': 'Что указано в документе?'}

    def plan(self, text, limit=4000, counter=True):
        return DocumentLoop('verified', {'a.txt': text}, self.question, [], None,
                            max_tokens=256, context_token_limit=limit,
                            token_counter=self.counter if counter else None)

    def test_counts_tokens_and_reserves_output_separately(self):
        messages = [{'role': 'user', 'content': 'документ ' * 100}]
        self.assertEqual(self.counter.input_tokens(messages), 148)
        self.assertEqual(estimate_request_tokens(messages, 256, self.counter), math.ceil(148 * 1.02) + 256)
        self.assertGreater(estimate_request_tokens(messages, 256), self.counter.estimate(messages, 256) * 3)

    def test_loading_disables_saved_truncation_and_padding(self):
        tokenizer = self.counter.tokenizer
        tokenizer.enable_truncation(max_length=4)
        tokenizer.enable_padding(length=100)
        tokenizer.save(str(self.path))
        counter = get_token_counter('custom', str(self.path))
        self.assertEqual(counter.input_tokens([{'content': 'документ ' * 150}]), 198)

    def test_alias_resolution_and_explicit_override_never_download_unnecessarily(self):
        with patch.object(token_budget, '_qwen_path', side_effect=AssertionError('No download')):
            self.assertIsNone(get_token_counter('unknown-model'))
            self.assertIsNotNone(get_token_counter('qwen3-custom', str(self.path)))
        with patch.object(token_budget, '_load', return_value=self.counter) as load:
            self.assertIs(get_token_counter('Qwen/Qwen3-6-practice'), self.counter)
            load.assert_called_once_with('qwen3', '')

    def test_invalid_explicit_tokenizer_does_not_silently_revert_to_bytes(self):
        with self.assertRaisesRegex(ValueError, 'tokenizer.json'):
            get_token_counter('qwen3-custom', str(self.path.parent / 'missing.json'))

    def test_public_tokenizer_download_is_verified_and_reused_offline(self):
        data = self.path.read_bytes()
        with patch.object(token_budget, 'TOKENIZER_CACHE', self.path.parent / 'cache'), \
             patch.object(token_budget, 'QWEN_SHA256', hashlib.sha256(data).hexdigest()), \
             patch.object(token_budget.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as download:
            target = token_budget._qwen_path()
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(token_budget._qwen_path(), target)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(list(target.parent.glob('*.tmp')), [])
        with patch.object(token_budget, 'TOKENIZER_CACHE', self.path.parent / 'bad'), \
             patch.object(token_budget.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(ValueError, 'сумма'):
                token_budget._qwen_path()

    def test_full_chunks_use_at_least_97_percent_of_available_input_budget(self):
        text = 'документ ' * 14000
        plan = self.plan(text)
        chunks = list(plan.chunks())
        self.assertGreater(len(chunks), 2)
        for chunk in chunks[:-1]:
            payload = plan.summary_messages('map', chunk, retry=True)
            estimate = estimate_request_tokens(payload, 256, self.counter)
            self.assertLessEqual(estimate, 4000)
            self.assertGreater((estimate - 256) / (4000 - 256), 0.97)
        # The previous character cap would have stopped at 4,000 characters.
        self.assertGreater(len(chunks[0][0]['text']), 5000)
        self.assertEqual(chunks[-1][-1]['end'], len(text))

    def test_early_paragraph_does_not_throw_away_half_a_request(self):
        unbroken = self.plan('longword ' * 14000)
        first_end = next(unbroken.chunks())[0]['end']
        text = 'longword ' * 14000
        position = int(first_end * 0.6)
        text = text[:position] + '\n\n' + text[position:]
        plan = self.plan(text)
        first = next(plan.chunks())
        self.assertGreater(first[-1]['end'], first_end * 0.95)
        self.assertTrue(plan.fits(plan.summary_messages('map', first, retry=True)))

    def test_no_tokenizer_still_packs_byte_budget_nearly_full(self):
        plan = self.plan('документ ' * 14000, limit=12000, counter=False)
        for chunk in list(plan.chunks())[:-1]:
            self.assertGreater(estimate_request_tokens(plan.summary_messages('map', chunk, retry=True), 256), 11500)

    def test_preflight_and_http_guard_use_same_tokenizer(self):
        client = Client('key', 'https://example.test/v1', 'custom', max_tokens=256,
                        context_token_limit=4000, tokenizer_path=str(self.path))
        client.limiter.acquire = Mock()
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps({
            'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}).encode())
        payload = [{'role': 'user', 'content': 'документ ' * 1000}]
        self.assertGreater(estimate_request_tokens(payload, 256), 4000)
        self.assertEqual(client.complete(payload)[0], 'ok')
        self.assertEqual(client.stats['requests'], 1)
        with self.assertRaises(ApiError):
            client.complete([{'role': 'user', 'content': 'документ ' * 5000}])
        self.assertEqual(client.stats['requests'], 1)
        validate_context('verified', {'a.txt': 'документ ' * 1000}, [self.question], [],
                         max_tokens=256, context_token_limit=4000, token_counter=client.token_counter)

    def test_tokenizer_setting_survives_environment_parsing(self):
        options = RunOptions.from_env('verified', self.path.parent, environ={'TOKENIZER_PATH': str(self.path)})
        self.assertEqual(options.tokenizer_path, str(self.path))


if __name__ == '__main__':
    unittest.main()
