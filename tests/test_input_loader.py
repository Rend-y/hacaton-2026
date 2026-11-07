"""Проверки произвольных входов: настоящие временные файлы, без модели и сети."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import input_loader as loader


class InputLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='inputs-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative, content='Гарантийный срок составляет 12 месяцев.', encoding='utf-8'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding=encoding)
        return path

    def test_arbitrary_nested_directory_and_overlapping_files(self):
        document = self.write('Произвольная папка/вложенная/условия.txt')
        self.write('Произвольная папка/README.md', 'Описание закупки и условий.')
        result = loader.load_documents([self.root / 'Произвольная папка', document])
        self.assertEqual(len(result['documents']), 2)
        self.assertEqual(len(result['sources']), 2)
        self.assertTrue(any('README.md' in name for name in result['documents']))
        self.assertFalse(result['read_errors'])
        self.assertTrue(any('уже добавлен' in entry['reason'] for entry in result['skipped']))

    def test_colliding_names_keep_both_documents_and_source_mapping(self):
        first = self.write('a/contract.txt', 'Первое значение 12 месяцев.')
        second = self.write('b/contract.txt', 'Второе значение 18 месяцев.')
        result = loader.load_documents([first, second])
        self.assertEqual(len(result['documents']), 2)
        self.assertEqual(set(result['documents'].values()), {'Первое значение 12 месяцев.', 'Второе значение 18 месяцев.'})
        self.assertEqual({row['path'] for row in result['sources']}, {str(first.resolve()), str(second.resolve())})

    def test_text_encodings_and_html_have_no_network_or_script_execution(self):
        self.write('windows.txt', encoding='cp1251')
        self.write('unicode.txt', encoding='utf-16')
        self.write('page.html', '<p>Срок <b>12 месяцев</b></p><script>steal()</script><style>bad{}</style><p>Цена &amp; налог</p>')
        result = loader.load_documents([self.root])
        self.assertEqual(len(result['documents']), 3)
        html = next(text for name, text in result['documents'].items() if name.endswith('.html'))
        self.assertIn('Срок 12 месяцев', html)
        self.assertIn('Цена & налог', html)
        self.assertNotIn('steal', html)
        self.assertNotIn('bad{}', html)

    def test_unknown_binary_and_empty_documents_are_visible_read_errors(self):
        self.write('valid.txt')
        self.write('scan.png', b'PNG\x00binary')
        self.write('fake.txt', b'text\x00binary')
        self.write('empty.md', '')
        result = loader.load_documents([self.root])
        self.assertEqual(len(result['documents']), 1)
        self.assertEqual(len(result['read_errors']), 3)
        self.assertEqual(len(result['skipped']), 3)

    def test_gold_secret_outputs_and_questions_are_not_documents(self):
        self.write('valid.txt')
        for name in ('.env', 'answers_public.json', 'answers.csv', 'questions.csv'):
            self.write(name, 'DO_NOT_SEND')
        self.write('.git/info.txt', 'DO_NOT_SEND')
        self.write('old-run/manifest.json', '{}')
        self.write('old-run/answers.csv', 'DO_NOT_SEND')
        self.write('old-run/extra.txt', 'DO_NOT_SEND')
        result = loader.load_documents([self.root])
        self.assertEqual(len(result['documents']), 1)
        self.assertNotIn('DO_NOT_SEND', ''.join(result['documents'].values()))
        self.assertFalse(result['read_errors'])
        with self.assertRaisesRegex(ValueError, 'Нельзя передать'):
            loader.load_documents([self.root / 'answers_public.json'])

    def test_explicit_exclusion_supports_question_file_and_output_directory(self):
        self.write('valid.txt')
        questions = self.write('my-list.txt', 'Вопрос?')
        excluded = self.root / 'output'
        self.write('output/raw.txt', 'Старый ответ')
        result = loader.load_documents([self.root], exclude_paths=[questions, excluded])
        self.assertEqual(len(result['documents']), 1)
        self.assertFalse(result['read_errors'])

    def test_symbolic_link_or_junction_is_skipped_and_blocks_full_read_claim(self):
        self.write('valid.txt')
        linked = self.write('linked.txt')
        original = loader._is_link
        with patch.object(loader, '_is_link', side_effect=lambda path: path == linked or original(path)):
            result = loader.load_documents([self.root])
        self.assertEqual(len(result['documents']), 1)
        self.assertEqual(len(result['read_errors']), 1)
        self.assertIn('junction', result['read_errors'][0])

    def test_size_limits_are_visible_and_missing_paths_fail_early(self):
        self.write('valid.txt', 'OK')
        self.write('large.txt', 'x' * 200)
        with patch.object(loader, 'MAX_FILE_BYTES', 100):
            result = loader.load_documents([self.root])
        self.assertEqual(len(result['documents']), 1)
        self.assertTrue(result['read_errors'])
        with self.assertRaisesRegex(ValueError, 'Путь не существует'):
            loader.load_documents([self.root / 'missing'])
        with self.assertRaisesRegex(ValueError, 'Укажите путь'):
            loader.load_documents([])

    def test_docx_tables_headers_and_images_have_honest_extraction_scope(self):
        document = self.root / 'terms.docx'
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>Гарантия</w:t><w:tab/><w:t>12 месяцев</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Таблица: 50 рублей</w:t></w:r></w:p></w:tc>'
               '</w:tr></w:tbl></w:body></w:document>')
        with zipfile.ZipFile(document, 'w') as archive:
            archive.writestr('word/document.xml', xml)
            archive.writestr('word/header1.xml', xml.replace('Гарантия', 'Заголовок'))
            archive.writestr('word/media/image.png', b'not-read')
        result = loader.load_documents([document])
        text = next(iter(result['documents'].values()))
        self.assertIn('Гарантия\t12 месяцев', text)
        self.assertIn('Таблица: 50 рублей', text)
        self.assertIn('Заголовок', text)
        self.assertTrue(any('OCR' in error for error in result['read_errors']))

    def test_docx_rejects_xml_entities_and_archive_bomb(self):
        document = self.root / 'broken.docx'
        with zipfile.ZipFile(document, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE a [<!ENTITY x "secret">]><a>&x;</a>')
        with self.assertRaisesRegex(ValueError, 'XML-сущности'):
            loader.load_documents([document])
        with patch.object(loader, 'MAX_TOTAL_BYTES', 10), self.assertRaisesRegex(ValueError, '80 МиБ'):
            loader.load_documents([document])

    def test_pdf_blank_page_is_explicitly_not_an_absence_result(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('Необязательный pypdf не установлен')
        document = self.root / 'scan.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        with document.open('wb') as stream:
            writer.write(stream)
        with self.assertRaisesRegex(ValueError, 'OCR'):
            loader.load_documents([document])

    def test_pdf_dependency_is_optional_and_failure_has_install_command(self):
        document = self.write('document.pdf', b'%PDF-1.4')
        with patch.dict(sys.modules, {'pypdf': None}), self.assertRaisesRegex(ValueError, 'requirements-pdf.txt'):
            loader.load_documents([document])

    def test_pdf_keeps_readable_pages_but_marks_blank_page_as_incomplete(self):
        try:
            from pypdf import PdfWriter
            from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        except ImportError:
            self.skipTest('Необязательный pypdf не установлен')
        document = self.root / 'mixed.pdf'
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 50 Td (Warranty: 12 months) Tj ET')
        page[NameObject('/Contents')] = stream
        writer.add_blank_page(width=300, height=300)
        with document.open('wb') as target:
            writer.write(target)
        result = loader.load_documents([document])
        self.assertIn('Warranty: 12 months', next(iter(result['documents'].values())))
        self.assertEqual(len(result['read_errors']), 1)
        self.assertIn('2', result['read_errors'][0])
        self.assertIn('OCR', result['read_errors'][0])

    def test_questions_text_csv_and_json_preserve_question_contents(self):
        text = self.write('list.txt', 'Первый вопрос?\n\nВторой вопрос?\n', encoding='cp1251')
        self.assertEqual(loader.load_questions(text), ['Первый вопрос?', 'Второй вопрос?'])
        csv = self.write('list.csv', 'id;package;question\nQ1;CUSTOM;"Сумма; в рублях?"\nQ2;OTHER;"Строка\nвнутри?"')
        self.assertEqual(loader.load_questions(csv), ['Сумма; в рублях?', 'Строка\nвнутри?'])
        russian = self.write('ru.csv', 'Вопрос,Пояснение\n"Сумма, рубли?",comment')
        self.assertEqual(loader.load_questions(russian), ['Сумма, рубли?'])
        data = self.write('list.json', json.dumps({'questions': ['Первый?', {'question': 'Второй?'}]}, ensure_ascii=False))
        self.assertEqual(loader.load_questions(data), ['Первый?', 'Второй?'])

    def test_invalid_question_files_do_not_silently_drop_rows(self):
        for name, content in [('empty.txt', ''), ('bad.csv', 'question\n""\n'),
                              ('bad.json', '{"questions":[1]}'), ('wrong.csv', 'answer\n42')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                loader.load_questions(self.write(name, content))
        with patch.object(loader, 'MAX_QUESTIONS', 1), self.assertRaisesRegex(ValueError, 'Допустимо'):
            loader.load_questions(self.write('many.txt', 'Первый?\nВторой?'))


if __name__ == '__main__':
    unittest.main()
