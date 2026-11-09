"""Проверяет генерацию моделью MCP-клиента, каталог и реальный workflow без HTTP API."""

import asyncio
from base64 import b64encode
from contextlib import redirect_stdout
from datetime import timedelta
from importlib.util import find_spec
import io
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src import cli, settings
from src.files.document_sets import DocumentChats, DocumentSets


@unittest.skipUnless(find_spec('mcp'), 'Для проверки протокола установите requirements-mcp.txt')
class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_launcher_from_another_working_directory(self):
        from mcp import ClientSession, StdioServerParameters, stdio_client

        with TemporaryDirectory(prefix='MCP клиент ', dir=Path(__file__).parent) as directory:
            config = StdioServerParameters(command=sys.executable, args=[str(settings.ROOT / 'run_mcp.py')],
                                           cwd=directory)
            async with stdio_client(config) as streams, ClientSession(
                    *streams, read_timeout_seconds=timedelta(seconds=8)) as client:
                initialized = await client.initialize()
                self.assertEqual(initialized.serverInfo.name, 'document-agent')
                result = await client.call_tool('list_document_sets')
                self.assertFalse(result.isError, result.content)
                self.assertIn('P01', [item['document_set_id'] for item in result.structuredContent['document_sets']])

    async def test_upload_and_query_over_stdio(self):
        from mcp import ClientSession, StdioServerParameters, stdio_client

        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / '.env').write_text('MODEL=unused\nAPI_KEY=unused\nMAX_WORKERS=99\nMAX_TOKENS=invalid\n', encoding='utf-8')
            source = '01_izveshchenie.txt'
            package = Path('data/public/packages/P01')
            (root / package).mkdir(parents=True)
            shutil.copyfile(settings.ROOT / package / source, root / package / source)
            quote = 'Способ определения поставщика: электронный аукцион.'
            question = 'Какой способ определения поставщика указан в извещении?'
            script = f'''
from pathlib import Path
from unittest.mock import patch
from src import settings
from src.mcp_server import main

with patch('src.workflows.pipeline.Client', side_effect=AssertionError('MCP must not create an API client')), \
     patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('MCP must not send HTTP requests')), \
     patch.object(settings, 'ROOT', Path({str(root)!r})):
    main([])
'''
            config = StdioServerParameters(command=sys.executable, args=['-c', script], cwd=settings.ROOT)

            async def call(name, args=None):
                result = await client.call_tool(name, args or {})
                self.assertFalse(result.isError, result.content)
                return result.structuredContent

            async def check_answer(context, filename, *, repair=False, reject=False):
                run = await call('ask_documents', {**context, 'questions': [question]})
                arguments = {'run_id': run['run_id']}
                if 'chat_id' in context:
                    arguments['chat_id'] = context['chat_id']
                result = run
                requests = []
                for _ in range(100):
                    if result['status'] == 'requires_response':
                        request = result['model_request']
                        self.assertNotIn(request['request_id'], requests)
                        payload = json.loads(request['messages'][1]['content'])
                        self.assertEqual(max(doc['file'] for doc in payload['documents']), filename)
                        if requests:
                            self.assertTrue(payload['previous_answers_and_validation_errors'][0]['errors'])
                        bad = reject or (repair and not requests)
                        answer = 'Выдуманный ответ.' if bad else 'Электронный аукцион'
                        evidence = 'Такой цитаты в документах нет.' if bad else quote
                        response = {'answers': [{'id': payload['questions'][0]['id'], 'status': 'ответ',
                                                'answer': answer, 'evidence': [{'file': filename, 'quote': evidence}]}]}
                        if 'chat_id' in context and not requests:
                            foreign = await client.call_tool('submit_document_response', {
                                'run_id': run['run_id'], 'request_id': request['request_id'], 'response': '{}'})
                            self.assertTrue(foreign.isError)
                        requests.append(request['request_id'])
                        result = await call('submit_document_response', {**arguments,
                            'request_id': request['request_id'], 'response': json.dumps(response, ensure_ascii=False)})
                        if result['status'] == 'requires_response':
                            stale = await client.call_tool('submit_document_response', {**arguments,
                                'request_id': request['request_id'], 'response': '{}'})
                            self.assertTrue(stale.isError)
                    elif result['status'] == 'running':
                        await asyncio.sleep(0.025)
                        result = await call('get_document_run', arguments)
                    else:
                        break
                self.assertEqual(result['status'], 'completed', result)
                answer, = result['answers']
                self.assertEqual(answer['status'], 'не знаю' if reject else 'ответ')
                self.assertEqual(answer['evidence'], [] if reject else [{'file': filename, 'quote': quote}])
                self.assertEqual(len(requests), 3 if reject else 2 if repair else 1)
                stale = await client.call_tool('submit_document_response', {**arguments,
                    'request_id': requests[0], 'response': '{}'})
                self.assertTrue(stale.isError)
                manifest = json.loads((root / 'runs/.agent/queries' / run['run_id'] / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['model'], 'mcp-client')
                self.assertEqual(manifest['mode'], 'verified')
                self.assertEqual(manifest['repair_attempts'], 2)
                self.assertEqual(manifest['workers'], 1)
                self.assertEqual(manifest['stats']['requests'], len(requests))
                return run['run_id']

            async with stdio_client(config) as streams, ClientSession(
                    *streams, read_timeout_seconds=timedelta(seconds=8)) as client:
                await client.initialize()
                tool_list = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tool_list},
                                 {'list_document_sets', 'upload_documents', 'ask_documents', 'get_document_run',
                                  'create_document_chat', 'get_document_chat', 'submit_document_response'})
                ask_schema = next(tool.inputSchema for tool in tool_list if tool.name == 'ask_documents')
                self.assertTrue({'mode', 'repair_attempts', 'api_key', 'base_url', 'model'}.isdisjoint(ask_schema['properties']))
                initial = await call('list_document_sets')
                self.assertEqual(initial['document_sets'][0]['document_set_id'], 'P01')
                await check_answer({'document_set_id': 'P01'}, source, repair=True)
                await check_answer({'document_set_id': 'P01'}, source, reject=True)

                batch_run = await call('ask_documents', {'document_set_id': 'P01', 'questions': [question] * 7})
                batch_result = batch_run
                sizes = []
                for _ in range(100):
                    if batch_result['status'] == 'requires_response':
                        request = batch_result['model_request']
                        payload = json.loads(request['messages'][1]['content'])
                        sizes.append(len(payload['questions']))
                        response = {'answers': [{'id': row['id'], 'status': 'ответ', 'answer': quote,
                                                'evidence': [{'file': source, 'quote': quote}]}
                                               for row in payload['questions']]}
                        batch_result = await call('submit_document_response', {
                            'run_id': batch_run['run_id'], 'request_id': request['request_id'],
                            'response': json.dumps(response, ensure_ascii=False)})
                    elif batch_result['status'] == 'running':
                        batch_result = await call('get_document_run', {'run_id': batch_run['run_id']})
                    else:
                        break
                self.assertEqual(batch_result['status'], 'completed')
                self.assertEqual(sizes, [5, 2])
                self.assertEqual(len(batch_result['answers']), 7)

                upload = {'name': 'Извещение.txt', 'content_base64': b64encode(quote.encode()).decode()}
                invalid = await client.call_tool('upload_documents', {
                    'title': 'Неполная загрузка', 'files': [upload, {**upload, 'name': '../secret.txt'}],
                })
                self.assertTrue(invalid.isError)
                self.assertEqual(await call('list_document_sets'), initial)
                saved = await call('upload_documents', {'title': 'Мои документы', 'files': [upload]})
                set_id = saved['document_set_id']
                run_id = await check_answer({'document_set_id': set_id}, upload['name'])

                chat = await call('create_document_chat')
                context = {'chat_id': chat['chat_id']}
                self.assertEqual(chat['files'], [])
                first = await call('upload_documents', {**context, 'files': [{**upload, 'name': 'a-first.txt'},
                                                                           {**upload, 'name': 'b-first.txt'}]})
                self.assertCountEqual(first['files'], ['a-first.txt', 'b-first.txt'])
                first_run = await check_answer(context, 'b-first.txt')
                extra = root / 'z-later.txt'
                extra.write_bytes(quote.encode())
                with patch.object(settings, 'ROOT', root), redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['upload-documents', '--chat-id', chat['chat_id'],
                                               '--attachment', str(extra)]), 0)
                second = await call('upload_documents', {**context, 'files': [
                    {**upload, 'name': 'a-first.txt'}, {**upload, 'name': 'c-later.txt'},
                ]})
                all_files = ['a-first.txt', 'b-first.txt', 'c-later.txt', 'z-later.txt']
                self.assertCountEqual(second['files'], all_files)
                self.assertEqual(second['added_files'], ['c-later.txt'])
                repeated = await call('upload_documents', {**context, 'files': [{**upload, 'name': 'a-first.txt'}]})
                self.assertEqual(repeated['added_files'], [])
                self.assertEqual(repeated['attachments'], second['attachments'])
                second_run = await check_answer(context, 'z-later.txt')
                selected = next(item['attachment_id'] for item in second['attachments'] if item['name'] == 'a-first.txt')
                selected_run = await check_answer({**context, 'attachment_ids': [selected]}, 'a-first.txt')
                runs = root / 'runs' / '.agent' / 'queries'
                for run, expected in [(first_run, ['a-first.txt', 'b-first.txt']), (second_run, all_files),
                                      (selected_run, ['a-first.txt'])]:
                    snapshot = json.loads((runs / run / 'inputs.json').read_text(encoding='utf-8'))
                    self.assertCountEqual(snapshot['packages']['INPUT'][0], expected)

                other = await call('create_document_chat')
                other_context = {'chat_id': other['chat_id']}
                self.assertEqual((await call('get_document_chat', other_context))['files'], [])
                empty = await client.call_tool('ask_documents', {**other_context, 'questions': [question]})
                self.assertTrue(empty.isError)
                foreign_attachment = await client.call_tool('ask_documents', {
                    **other_context, 'questions': [question], 'attachment_ids': [selected],
                })
                self.assertTrue(foreign_attachment.isError)
                foreign = await client.call_tool('get_document_run', {**other_context, 'run_id': second_run})
                self.assertTrue(foreign.isError)
                content = b'x' * (3 * 1024 * 1024 + 1)
                large = await call('upload_documents', {**other_context, 'files': [{
                    'name': 'large.txt', 'content_base64': b64encode(content).decode(),
                }]})
                self.assertEqual(large['files'], ['large.txt'])
                saved_path, = DocumentChats(root).paths(other['chat_id'])
                self.assertEqual(saved_path.read_bytes(), content)
                self.assertEqual(await call('list_document_sets'), {'document_sets': [*initial['document_sets'], saved]})
                parallel_runs = await asyncio.gather(check_answer(context, 'z-later.txt'),
                                                    check_answer({'document_set_id': 'P01'}, source))
                self.assertEqual(len(set(parallel_runs)), 2)
                interrupted = await call('ask_documents', {**context, 'questions': [question]})
                self.assertEqual(interrupted['status'], 'requires_response')

            original, = DocumentSets(root).paths(set_id)
            self.assertEqual(original.read_bytes(), quote.encode())

            output = io.StringIO()
            with patch.object(settings, 'ROOT', root), redirect_stdout(output):
                self.assertEqual(cli.main(['upload-documents', '--attachment', str(original), '--title', 'Из CLI']), 0)
            cli_set_id = json.loads(output.getvalue())['document_set_id']
            with patch.object(settings, 'ROOT', root), patch('src.workflows.query.run_query', return_value=0) as run:
                self.assertEqual(cli.main(['query', '--attachment', str(original), '--prompt', question]), 0)
                self.assertEqual(run.call_args.args[0].attachment, [str(original)])

            async with stdio_client(config) as streams, ClientSession(
                    *streams, read_timeout_seconds=timedelta(seconds=8)) as client:
                await client.initialize()
                listing = await call('list_document_sets')
                self.assertEqual({row['document_set_id'] for row in listing['document_sets']},
                                 {'P01', set_id, cli_set_id})
                self.assertEqual((await call('get_document_run', {'run_id': run_id}))['status'], 'completed')
                self.assertCountEqual((await call('get_document_chat', context))['files'], all_files)
                lost = await call('get_document_run', {**context, 'run_id': interrupted['run_id']})
                self.assertEqual(lost['status'], 'unknown')
                self.assertNotIn('model_request', lost)
