"""Проверки защиты от ложных цитат и чисел без сети и эталонных ответов.

Отдельные случаи проверяют отказ при неполных данных, конфликты документов,
различие дат и сумм, а также расчёт процента по подтверждённым значениям.
"""

import copy
import unittest

from src.generation.validation import norm, restore_quote_endings, validate_answer


class ValidationTests(unittest.TestCase):
    """Фиксировать поведение валидатора на правильных и ошибочных доказательствах."""

    def fact(self, text, quote, filename='notice.txt'):
        return {'id': 'P01-01', 'status': 'ответ', 'answer': text, 'evidence': [{'file': filename, 'quote': quote}]}

    def test_exact_file_is_required(self):
        answer = self.fact('60 дней', 'Срок работ 60 календарных дней.')
        errors = validate_answer(answer, {'notice.txt': 'Иные условия закупки.', 'contract.txt': answer['evidence'][0]['quote']})
        self.assertTrue(any('не найдена в указанном' in error for error in errors))

    def test_wrong_package_or_filename_is_rejected(self):
        answer = self.fact('60 дней', 'Срок работ 60 календарных дней.', '../P02/notice.txt')
        self.assertTrue(validate_answer(answer, {'notice.txt': 'Срок работ 60 календарных дней.'}))

    def test_citation_normalization_and_input_immutability(self):
        answer = self.fact('12 месяцев', '«Гарантия» – 12 месяцев для трёх изделий.')
        original = copy.deepcopy(answer)
        self.assertEqual(validate_answer(answer, {'notice.txt': '"гарантия" — 12\n месяцев для трех изделий.'}), [])
        self.assertEqual(original, answer)
        self.assertEqual(norm(' Ё\u202f«ДА»—\nНет '), 'е "да"- нет')

    def test_fake_quote_rejected(self):
        answer = self.fact('12 месяцев', 'Гарантия составляет 12 месяцев.')
        self.assertTrue(validate_answer(answer, {'notice.txt': 'Гарантия составляет 24 месяца.'}))

    def test_case_ending_repair_leaves_different_or_ambiguous_sources_untouched(self):
        source = 'Заказчик проверяет сведения о лицензии по технической защите информации в течение 12 дней.'
        answer = self.fact('12 дней', source.replace('защите', 'защиты'))
        candidates = (source.replace('12', '24'), source.replace('проверяет', 'не проверяет'),
                      source + '\n' + source.replace('12', '24'))
        for candidate in candidates:
            with self.subTest(source=candidate):
                docs = {'notice.txt': candidate}
                self.assertIs(restore_quote_endings(answer, docs), answer)
                self.assertTrue(validate_answer(answer, docs))

    def test_too_short_quote_rejected(self):
        answer = self.fact('12 месяцев', '12')
        self.assertTrue(validate_answer(answer, {'notice.txt': '12 месяцев'}))

    def test_hallucinated_number_rejected_even_with_real_quote(self):
        answer = self.fact('12 или 999 месяцев', 'Гарантия составляет 12 месяцев.')
        self.assertTrue(any('999' in error for error in validate_answer(answer, {'notice.txt': answer['evidence'][0]['quote']})))

    def test_thousands_decimal_and_parenthetical_values(self):
        quote = 'Не менее 3 (трёх) контрактов стоимостью 250 000,00 рублей каждый.'
        self.assertEqual(validate_answer(self.fact('3 контракта, каждый от 250000.00 руб.', quote), {'notice.txt': quote}), [])

    def test_date_and_time_order(self):
        quote = 'Заявки принимаются до 10:00 по московскому времени 27.11.2026.'
        self.assertEqual(validate_answer(self.fact('27.11.2026 10:00', quote), {'notice.txt': quote}), [])
        self.assertTrue(validate_answer(self.fact('27.11.2026 11:00', quote), {'notice.txt': quote}))

    def test_iso_date_equivalence(self):
        quote = 'Приём заявок заканчивается 27.11.2026 в 10:00.'
        self.assertEqual(validate_answer(self.fact('2026-11-27 10:00:00', quote), {'notice.txt': quote}), [])

    def test_date_digits_do_not_prove_money(self):
        quote = 'Приём заявок заканчивается 27.11.2026 в 10:00.'
        self.assertTrue(validate_answer(self.fact('2026 рублей', quote), {'notice.txt': quote}))

    def test_paragraph_numbers_do_not_prove_duration(self):
        quote = '3.16. Исполнитель выполняет работы в течение 15 календарных дней.'
        self.assertTrue(validate_answer(self.fact('3.16 дня', quote), {'notice.txt': quote}))
        self.assertEqual(validate_answer(self.fact('15 календарных дней', quote), {'notice.txt': quote}), [])

    def test_explicit_negative_is_answer(self):
        quote = 'Требование об обеспечении заявок не установлено.'
        self.assertEqual(validate_answer(self.fact('Не требуется', quote), {'notice.txt': quote}), [])

    def test_safe_refusal(self):
        self.assertEqual(validate_answer({'status': 'не знаю'}, {}, ['read error']), [])

    def test_absence_requires_readable_package(self):
        answer = {'status': 'нет в документах'}
        self.assertTrue(validate_answer(answer, {}))
        self.assertTrue(validate_answer(answer, {'file.txt': '  '}))
        self.assertTrue(validate_answer(answer, {'file.txt': 'Документ'}, ['read error']))
        self.assertEqual(validate_answer(answer, {'file.txt': 'Документ'}), [])

    def test_conflict_requires_two_distinct_files(self):
        notice = 'Исполнитель выполняет работы в течение 60 календарных дней.'
        contract = 'Срок работ: семьдесят пять (75) календарных дней.'
        answer = {'status': 'расхождение', 'answer': '60 и 75 календарных дней', 'evidence': [
            {'file': 'notice.txt', 'quote': notice}, {'file': 'contract.txt', 'quote': contract}]}
        self.assertEqual(validate_answer(answer, {'notice.txt': notice, 'contract.txt': contract}), [])
        answer['evidence'][1]['file'] = 'notice.txt'
        self.assertTrue(validate_answer(answer, {'notice.txt': notice + '\n' + contract}))

    def calculation(self):
        base_quote = 'Начальная (максимальная) цена контракта: 1 800 000,00 руб.'
        rate_quote = 'Обеспечение исполнения контракта: 5 % от цены контракта.'
        docs = {'notice.txt': base_quote + '\n' + rate_quote}
        answer = {'status': 'ответ', 'answer': '90000 руб.', 'evidence': [
            {'file': 'notice.txt', 'quote': base_quote}, {'file': 'notice.txt', 'quote': rate_quote}],
            'calculation': {'operation': 'percent', 'base': '1 800 000,00', 'rate': '5', 'base_evidence': 0, 'rate_evidence': 1}}
        return answer, docs

    def test_calculation_result_need_not_be_in_source(self):
        answer, docs = self.calculation()
        self.assertEqual(validate_answer(answer, docs), [])

    def test_calculation_result_is_verified(self):
        answer, docs = self.calculation()
        answer['answer'] = '90001 руб.'
        self.assertTrue(any('90000.00' in error for error in validate_answer(answer, docs)))

    def test_calculation_operands_must_match_assigned_evidence(self):
        answer, docs = self.calculation()
        answer['calculation']['base_evidence'] = 1
        self.assertTrue(any('base=1800000.00' in error for error in validate_answer(answer, docs)))

    def test_calculation_wrong_rate_rejected_even_with_matching_result(self):
        answer, docs = self.calculation()
        answer['calculation']['rate'] = '10'
        answer['answer'] = '180000 руб.'
        self.assertTrue(any('rate=10' in error for error in validate_answer(answer, docs)))

    def test_arbitrary_calculation_rejected(self):
        answer, docs = self.calculation()
        answer['calculation']['operation'] = 'eval'
        self.assertTrue(validate_answer(answer, docs))

    def test_malformed_evidence_and_calculation_indices(self):
        for value in (None, 'text', {}, [None], [{'file': 1, 'quote': 2}]):
            with self.subTest(evidence=value):
                self.assertTrue(validate_answer({'status': 'ответ', 'answer': '10', 'evidence': value}, {}))
        answer, docs = self.calculation()
        answer['calculation']['base_evidence'] = True
        self.assertTrue(validate_answer(answer, docs))

    def test_malformed_status_and_answer(self):
        self.assertTrue(validate_answer(None, {}))
        self.assertTrue(validate_answer({'status': 'правильно'}, {}))
        self.assertTrue(validate_answer({'status': 'ответ', 'answer': 123, 'evidence': []}, {}))


if __name__ == '__main__':
    unittest.main()
