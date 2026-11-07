"""Автономное HTML-представление отчёта CLI без сети и внешних библиотек.

Модуль получает уже подготовленные данные и только оформляет их. Тексты вопросов,
ответов и документов всегда экранируются: содержимое источника не становится HTML.
"""
from html import escape
import math


def _e(value):
    """Экранирует и обычный текст, и значения атрибутов HTML."""
    return escape('' if value is None else str(value), quote=True)


def _number(value, decimals=0):
    """Отображает отсутствующее измерение отдельно от реального нуля."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 'нет данных'
    return format(value, ',.%sf' % decimals).replace(',', ' ').replace('.', ',')


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 'нет данных'
    if value < 60:
        return _number(value, 1) + ' с'
    minutes, seconds = divmod(int(round(value)), 60)
    return '%s мин %s с' % (_number(minutes), seconds)


def _numeric(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def _badge(text, category=None):
    # Класс выбирается по закрытому словарю, а не берётся из ответа модели.
    mapping = {'верно': 'good', 'обман': 'bad', 'осторожно': 'cautious',
               'ответ': 'neutral', 'расхождение': 'conflict', 'нет в документах': 'neutral',
               'не знаю': 'cautious', 'completed': 'good', 'completed_with_errors': 'bad',
               'running': 'cautious'}
    style = mapping.get(category or text, 'neutral')
    return '<span class="badge %s">%s</span>' % (style, _e(text))


def _status_label(value):
    return {'completed': 'Завершён', 'completed_with_errors': 'Есть технические ошибки',
            'running': 'Запуск не завершён'}.get(value, str(value or 'Статус не сохранён'))


def _metric(label, value, note='', accent=''):
    return '<div class="metric %s"><span class="metric-label">%s</span><strong>%s</strong><span class="metric-note">%s</span></div>' % (
        accent, _e(label), _e(value), _e(note))


def _distribution(metrics, label):
    """Полоса отражает доли от полного знаменателя официальной оценки."""
    total = _numeric(metrics.get('n'))
    parts = [('correct', 'Верно', 'good'), ('lies', 'Ошибки', 'bad'), ('careful', 'Осторожно', 'cautious')]
    labels = ['%s: %s' % (name, _number(metrics.get(key))) for key, name, _ in parts]
    if total <= 0:
        return '<p class="muted">В оценке нет вопросов; распределение не построено.</p>'
    bars = []
    for key, name, style in parts:
        count = max(0, _numeric(metrics.get(key)))
        width = min(100, count / total * 100)
        if count:
            bars.append('<span class="segment %s" style="width:%.6f%%" title="%s"></span>' % (
                style, width, _e('%s: %s из %s' % (name, _number(count), _number(total)))))
    legend = ''.join('<span><i class="dot %s" aria-hidden="true"></i>%s <b>%s</b></span>' % (
        style, _e(name), _e(_number(metrics.get(key)))) for key, name, style in parts)
    return '<div class="distribution" role="img" aria-label="%s"><div class="stack">%s</div><div class="legend">%s</div></div>' % (
        _e(label + '. ' + '; '.join(labels)), ''.join(bars), legend)


def _quality(run, index):
    quality = run.get('quality')
    label = run.get('label') or 'Запуск'
    if not isinstance(quality, dict):
        return '''<div class="panel no-quality"><span class="eyebrow">Оценка качества</span>
<h3>Оценка по эталону не выполнена</h3><p>Здесь показаны результаты работы системы, но они ещё не сравнены с правильными ответами.
Статусы модели и прохождение локальных проверок не являются измеренной точностью.</p>
<p class="muted">При наличии эталонов выполните команду <code>evaluate</code>. Она обновит этот отчёт.</p></div>'''
    total = _numeric(quality.get('n'))
    rate = lambda key: _number(_numeric(quality.get(key)) / total * 100, 1) + '% вопросов' if total else 'Набор пуст'
    cards = ''.join([
        _metric('Индекс доверия', _number(quality.get('index'), 1), 'Диапазон от −100 до 100', 'index-metric'),
        _metric('Верных ответов', _number(quality.get('correct')), rate('correct')),
        _metric('Ошибочных ответов', _number(quality.get('lies')), rate('lies')),
        _metric('Осторожных ответов', _number(quality.get('careful')), rate('careful')),
    ])
    return '<div class="metrics">%s</div><div class="panel distribution-panel"><div class="section-line"><h3>Результат по эталону</h3><span class="muted">%s вопросов в оценке</span></div>%s</div>' % (
        cards, _e(_number(quality.get('n'))), _distribution(quality, str(label)))


def _type_table(run):
    quality = run.get('quality')
    categories = quality.get('by_type') if isinstance(quality, dict) else None
    if not isinstance(categories, dict) or not categories:
        return ''
    rows = []
    for category, values in categories.items():
        if not isinstance(values, dict):
            continue
        rows.append('<tr><th scope="row">%s</th><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="strong">%s</td></tr>' % (
            _e(category), *[_e(_number(values.get(key), 1 if key == 'index' else 0)) for key in ('n', 'correct', 'lies', 'careful', 'index')]))
    return '''<section class="panel"><div class="section-line"><h3>По типам вопросов</h3><span class="muted">Классификация организаторов</span></div>
<div class="table-wrap"><table><caption class="sr-only">Официальная оценка по типам вопросов</caption><thead><tr>
<th scope="col">Тип вопроса</th><th scope="col">Всего</th><th scope="col">Верно</th><th scope="col">Ошибки</th><th scope="col">Осторожно</th><th scope="col">Индекс</th>
</tr></thead><tbody>%s</tbody></table></div></section>''' % ''.join(rows)


def _operations(run):
    operations = run.get('operations') or {}
    metrics = [
        _metric('Вопросов в запуске', _number(run.get('n_questions')), 'Объём переданного задания'),
        _metric('Время обработки', _duration(operations.get('elapsed_seconds')), 'Включая ожидание и повторы'),
        _metric('Запросов к API', _number(operations.get('requests')), 'Включая сетевые повторы'),
        _metric('Технических отказов', _number(operations.get('technical_errors')), 'Сбои API, чтения или формата'),
    ]
    secondary = '<div class="operations-detail"><span>Токены входа: <b>%s</b></span><span>Токены выхода: <b>%s</b></span><span>API-запросы исправления: <b>%s</b></span></div>' % (
        _e(_number(operations.get('prompt_tokens'))), _e(_number(operations.get('completion_tokens'))), _e(_number(operations.get('repair_requests'))))
    return '<section><h3 class="subheading">Как прошёл запуск</h3><div class="metrics operations">%s</div>%s</section>' % (''.join(metrics), secondary)


def _evidence(items):
    if not isinstance(items, list) or not items:
        return '<p class="muted">Цитаты не сохранены для этого ответа.</p>'
    pieces = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pieces.append('<figure class="evidence"><figcaption>Источник: <code>%s</code></figcaption><blockquote>%s</blockquote></figure>' % (
            _e(item.get('file')), _e(item.get('quote'))))
    return ''.join(pieces)


def _answer_detail(answer):
    metadata = []
    if answer.get('package'):
        metadata.append('Пакет: <b>%s</b>' % _e(answer['package']))
    if answer.get('type'):
        metadata.append('Тип вопроса: <b>%s</b>' % _e(answer['type']))
    body = '<p class="answer-metadata">%s</p>' % ' · '.join(metadata) if metadata else ''
    body += '<h4>Ответ системы</h4><p class="answer-value">%s</p>' % _e(answer.get('answer') or 'Содержательный ответ не предоставлен.')
    if answer.get('reason'):
        body += '<h4>Основание, сохранённое системой</h4><p>%s</p>' % _e(answer['reason'])
    if answer.get('verdict'):
        body += '<div class="assessment"><h4>Вердикт официального оценщика</h4><p>%s%s</p></div>' % (
            _badge(answer['verdict']), ' — ' + _e(answer['why']) if answer.get('why') else '')
    if answer.get('error_type'):
        body += '<p class="technical-note">Техническая причина: <code>%s</code></p>' % _e(answer['error_type'])
    body += '<h4>Доказательства ответа</h4>' + _evidence(answer.get('evidence'))
    return body


def _answers(run, index):
    answers = run.get('answers') or []
    label = run.get('label') or 'Запуск'
    statuses = sorted({str(a.get('status') or 'не указан') for a in answers})
    options = ''.join('<option value="%s">%s</option>' % (_e(status), _e(status)) for status in statuses)
    entries = []
    for answer in answers:
        status = str(answer.get('status') or 'не указан')
        verdict = str(answer.get('verdict') or '')
        question = answer.get('question') or 'Текст вопроса не сохранён'
        overview = str(answer.get('answer') or answer.get('reason') or 'Без содержательного ответа')
        overview = overview[:210] + ('…' if len(overview) > 210 else '')
        badges = '<span class="labelled-badge"><small>Статус модели</small>%s</span>' % _badge(status)
        badges += '<span class="labelled-badge"><small>Оценщик</small>%s</span>' % (
            _badge(verdict) if verdict else '<span class="not-scored">Без оценки</span>')
        entries.append('''<details class="answer-entry" data-answer data-status="%s" data-verdict="%s">
<summary><div class="answer-heading"><span class="answer-id">%s</span><span class="answer-question">%s</span><span class="expand-cue" aria-hidden="true">+</span></div>
<div class="answer-overview"><p>%s</p><div class="answer-badges">%s</div></div></summary>
<div class="answer-content">%s</div></details>''' % (
            _e(status), _e(verdict), _e(answer.get('id')), _e(question), _e(overview), badges, _answer_detail(answer)))
    return '''<section class="panel answer-section" id="answers-%s" data-answer-group>
<div class="section-line"><h3>Ответы и доказательства</h3><span class="muted">%s</span></div>
<div class="filters"><label for="search-%s">Поиск по вопросу, ответу или цитате<input id="search-%s" type="search" data-search placeholder="Например: гарантия или P01-07"></label>
<label for="status-%s">Статус модели<select id="status-%s" data-status-filter><option value="">Все статусы</option>%s</select></label>
<label for="verdict-%s">Оценка ответа<select id="verdict-%s" data-verdict-filter><option value="*">Все ответы</option><option value="верно">Верно</option><option value="обман">Ошибка</option><option value="осторожно">Осторожно</option><option value="">Без оценки</option></select></label></div>
<div class="answer-tools"><span class="result-count" role="status" aria-live="polite">Показано %s из %s ответов</span><button type="button" data-expand>Раскрыть видимые</button><button type="button" data-collapse>Свернуть</button></div>
<p class="small muted">Фильтры меняют только список. Метрики выше рассчитаны по всему оцениваемому набору.</p>
<div class="answers-list">%s</div><p class="empty-search" hidden>По выбранным условиям ответов нет.</p></section>''' % (
        index, _e(label), index, index, index, index, options, index, index,
        len(answers), len(answers), ''.join(entries) if entries else '<p class="empty-state">Ответы для просмотра не сохранены.</p>')


def _checks(run):
    checks = run.get('checks') or []
    if not checks:
        return '<section class="panel"><h3>Срабатывания локальных проверок</h3><p class="muted section-note">Ошибки проверок не зафиксированы в доступном журнале. Это не доказывает отсутствие смысловых ошибок.</p></section>'
    cards = []
    for check in checks:
        errors = ''.join('<li>%s</li>' % _e(error) for error in check.get('errors', []))
        before = check.get('before') if isinstance(check.get('before'), dict) else {}
        after = check.get('after') if isinstance(check.get('after'), dict) else {}
        attempt = _numeric(check.get('attempt')) + 1
        cards.append('''<article class="check-example"><div class="section-line"><h4>%s</h4><span class="muted">Итерация %s</span></div>
<ul class="check-errors">%s</ul><div class="before-after"><div><span class="eyebrow">Ответ, вызвавший проверку</span><p>%s</p>%s</div>
<div><span class="eyebrow">Сохранённый итог</span><p>%s</p>%s</div></div></article>''' % (
            _e(check.get('id')), _e(_number(attempt)), errors,
            _e(before.get('answer') or before.get('status') or 'Ответ отсутствует'),
            _badge(before.get('status')) if before.get('status') else '',
            _e(after.get('answer') or after.get('status') or 'Итог не сохранён'),
            _badge(after.get('status')) if after.get('status') else ''))
    visible = ''.join(cards[:3])
    remaining = '<details class="more-checks"><summary>Остальные срабатывания: %s</summary>%s</details>' % (len(cards) - 3, ''.join(cards[3:])) if len(cards) > 3 else ''
    return '<section class="panel"><div class="section-line"><h3>Срабатывания локальных проверок</h3><span class="muted">%s событий</span></div><p class="section-note muted">Показано, что именно отклонил код и какой итог был сохранён. Срабатывание проверки само по себе не является вердиктом официального оценщика.</p>%s%s</section>' % (
        len(cards), visible, remaining)


def _comparison(model):
    comparison = model.get('comparison')
    if not isinstance(comparison, dict):
        return ''
    before, after = comparison.get('baseline') or {}, comparison.get('verified') or {}
    delta = (comparison.get('delta') or {}).get('index')
    change = ('+' if _numeric(delta) > 0 else '') + _number(delta, 1) + ' п.п.'
    cards = ''.join([
        _metric('Было · baseline', _number(before.get('index'), 1), 'Индекс доверия'),
        _metric('Стало · verified', _number(after.get('index'), 1), 'Индекс доверия', 'index-metric'),
        _metric('Изменение', change, 'На одинаковых входных данных'),
    ])
    rows = []
    labels = [('correct', 'Верных ответов'), ('lies', 'Ошибочных ответов'), ('careful', 'Осторожных ответов'),
              ('elapsed_seconds', 'Время обработки, с'), ('requests', 'Запросов к API')]
    for key, name in labels:
        if key not in before and key not in after:
            continue
        digits = 1 if key == 'elapsed_seconds' else 0
        rows.append('<tr><th scope="row">%s</th><td>%s</td><td>%s</td></tr>' % (
            _e(name), _e(_number(before.get(key), digits)), _e(_number(after.get(key), digits))))
    categories = []
    for name, group in (comparison.get('by_type') or {}).items():
        left, right = group.get('baseline') or {}, group.get('verified') or {}
        diff = (group.get('delta') or {}).get('index')
        diff_label = ('+' if _numeric(diff) > 0 else '') + _number(diff, 1)
        categories.append('<tr><th scope="row">%s</th><td>%s</td><td>%s</td><td>%s</td><td class="strong">%s</td></tr>' % (
            _e(name), _e(_number(left.get('n'))), _e(_number(left.get('index'), 1)), _e(_number(right.get('index'), 1)), _e(diff_label)))
    category_table = '''<div class="panel"><h3>Изменение по типам вопросов</h3><div class="table-wrap"><table><thead><tr>
<th scope="col">Тип</th><th scope="col">Вопросов</th><th scope="col">Было</th><th scope="col">Стало</th><th scope="col">Изменение, п.п.</th>
</tr></thead><tbody>%s</tbody></table></div></div>''' % ''.join(categories) if categories else ''
    return '''<section id="comparison" class="report-section"><div class="section-title"><div><span class="eyebrow">Сопоставимые запуски</span><h2>Было и стало</h2></div></div>
<div class="metrics comparison-metrics">%s</div><div class="panel"><h3>Что изменилось</h3><div class="table-wrap"><table><thead><tr><th scope="col">Показатель</th><th scope="col">Было</th><th scope="col">Стало</th></tr></thead><tbody>%s</tbody></table></div></div>%s</section>''' % (cards, ''.join(rows), category_table)


def _configuration(run):
    config = run.get('config') or {}
    labels = {'temperature': 'Температура', 'max_tokens': 'Лимит токенов ответа', 'workers': 'Рабочих потоков',
              'repair_attempts': 'Разрешено исправлений', 'timeout_seconds': 'Таймаут HTTP, с',
              'created_at': 'Время начала запуска', 'input_fingerprint': 'Отпечаток входных данных'}
    values = [('Модель', run.get('model')), ('Режим', run.get('mode'))]
    values.extend((label, config[key]) for key, label in labels.items() if key in config)
    rows = ''.join('<div><dt>%s</dt><dd>%s</dd></div>' % (_e(label), _e(value)) for label, value in values)
    return '<details class="configuration"><summary>Настройки и воспроизводимость · %s</summary><dl>%s</dl></details>' % (_e(run.get('label') or 'Запуск'), rows)


def _sources(model):
    # Ссылки ведут только на известные соседние артефакты, не на URL из документов.
    allowed = {'answers.csv', 'answers.json', 'manifest.json', 'prompts.json', 'trace.jsonl', 'progress.log',
               'evaluation.csv', 'evaluation.json', 'comparison.csv', 'comparison.json'}
    links = []
    for source in model.get('source_files') or []:
        href = source.get('href')
        if href in allowed:
            links.append('<a class="download-link" href="%s" download>%s</a>' % (_e(href), _e(source.get('name') or href)))
    return '<div class="downloads">%s</div>' % ''.join(links) if links else ''


CSS = r'''
:root{color-scheme:light;--bg:#f5f7fb;--surface:#fff;--ink:#17283f;--muted:#53657b;--line:#dce4ef;--blue:#244cad;--navy:#142945;--good:#14705c;--good-soft:#e9f5f0;--bad:#b42d35;--bad-soft:#fff0f0;--amber:#93500b;--amber-soft:#fff4df;--focus:#2857ce}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:var(--blue);text-underline-offset:3px}button,input,select{font:inherit}button,summary,select{cursor:pointer}button{border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink);padding:9px 14px;min-height:44px;transition:background .15s}button:hover{background:#eef3fb}a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--focus);outline-offset:3px}h1,h2,h3,h4,p,figure{margin:0}h1{font-size:clamp(28px,4vw,44px);font-weight:650;line-height:1.15;letter-spacing:-.035em;max-width:850px}h2{font-size:27px;line-height:1.2;letter-spacing:-.025em}h3{font-size:19px;line-height:1.3;font-weight:650}h4{font-size:15px;line-height:1.4;font-weight:650}code{font:13px/1.5 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}b,strong{font-weight:650}.muted{color:var(--muted)}.small{font-size:13px}.eyebrow{display:block;font-size:12px;font-weight:650;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip-link{position:absolute;left:16px;top:-90px;padding:10px;background:white;z-index:10}.skip-link:focus{top:12px}.shell{max-width:1184px;margin:auto;padding:0 32px}.masthead{background:var(--navy);color:white;padding:30px 0 36px}.masthead .eyebrow{color:#c9d7ec}.brand-row{display:flex;gap:24px;justify-content:space-between;align-items:center;margin-bottom:28px}.brand{font-size:13px;font-weight:650;letter-spacing:.08em}.report-kind{font-size:12px;color:#d8e4f7;letter-spacing:.05em}.hero-note{color:#cfddf0;margin-top:16px;max-width:810px}.header-meta{display:flex;flex-wrap:wrap;gap:10px 24px;font-size:13px;margin-top:22px;color:#d9e3f1}.navigation{background:var(--surface);border-bottom:1px solid var(--line)}.navigation .shell{display:flex;gap:24px;align-items:center;flex-wrap:wrap;padding-top:12px;padding-bottom:12px}.navigation a{color:var(--muted);font-weight:550;text-decoration:none;font-size:14px;padding:6px 0}.navigation a:hover{color:var(--blue)}.navigation button{margin-left:auto;font-size:13px;min-height:36px;padding:5px 12px}main{padding-top:28px!important;padding-bottom:60px!important}.warnings{display:grid;gap:10px;margin-bottom:28px}.warning{background:var(--amber-soft);border-left:4px solid var(--amber);padding:14px 18px;font-size:14px;color:#693c12;overflow-wrap:anywhere}.report-section{display:grid;gap:20px;margin-top:34px;scroll-margin-top:20px}.report-section:first-of-type{margin-top:0}.section-title{display:flex;align-items:end;justify-content:space-between;gap:16px;flex-wrap:wrap}.section-title .eyebrow{margin-bottom:7px}.run-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}.run-heading .mode{font-size:13px;color:var(--muted);margin-top:6px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.metric{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:19px 20px;min-width:0;display:flex;flex-direction:column;align-items:start;gap:8px}.metric-label{font-size:13px;color:var(--muted);font-weight:550}.metric strong{font-size:35px;line-height:1.12;letter-spacing:-.04em;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.metric-note{font-size:12px;color:var(--muted)}.index-metric{background:var(--navy);border-color:var(--navy);color:white}.index-metric .metric-label,.index-metric .metric-note{color:#d2def0}.comparison-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:24px;min-width:0}.section-line{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.section-line>.muted{font-size:13px}.section-note{font-size:14px;margin-top:12px;margin-bottom:18px;max-width:820px}.distribution{margin-top:22px}.stack{display:flex;height:22px;background:#e8edf5;border-radius:5px;overflow:hidden}.segment{display:block;height:100%}.segment.good,.dot.good{background:var(--good)}.segment.bad,.dot.bad{background:var(--bad)}.segment.cautious,.dot.cautious{background:var(--amber)}.legend{display:flex;gap:12px 28px;flex-wrap:wrap;margin-top:13px;font-size:13px}.legend>span{display:flex;align-items:center;gap:7px}.legend b{font-variant-numeric:tabular-nums}.dot{height:9px;width:9px;border-radius:2px;display:inline-block;flex-shrink:0}.subheading{margin-bottom:13px}.operations .metric strong{font-size:26px}.operations-detail{display:flex;flex-wrap:wrap;gap:10px 26px;color:var(--muted);font-size:13px;margin-top:13px}.operations-detail b{color:var(--ink);font-variant-numeric:tabular-nums}.badge{display:inline-block;border-radius:5px;padding:4px 8px;font-size:12px;line-height:1.4;font-weight:600;overflow-wrap:anywhere}.badge.good{background:var(--good-soft);color:var(--good)}.badge.bad{background:var(--bad-soft);color:var(--bad)}.badge.cautious{background:var(--amber-soft);color:var(--amber)}.badge.neutral{background:#edf2fa;color:#365478}.badge.conflict{background:#f0ebfa;color:#614094}.no-quality{background:#eef3fb;border-color:#cddbf0}.no-quality h3{margin:10px 0}.no-quality p{font-size:14px;max-width:850px}.no-quality p+p{margin-top:12px}.table-wrap{overflow-x:auto;margin-top:18px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:12px 10px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}th:first-child,td:first-child{text-align:left;white-space:normal;min-width:180px}thead th{font-size:12px;color:var(--muted);font-weight:600;background:#f8fafc}tbody th{font-weight:500}tbody tr:last-child>*{border-bottom:0}tbody tr:hover{background:#f7f9fd}.strong{font-weight:650}.filters{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px;margin-top:22px}.filters label{display:grid;gap:7px;font-size:12px;font-weight:600;color:var(--muted)}input,select{width:100%;min-height:44px;border:1px solid #becbdb;background:white;border-radius:7px;padding:10px 11px;color:var(--ink);font-size:14px;min-width:0}input::placeholder{color:#6a7b91}.answer-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:15px;margin-bottom:10px}.result-count{font-size:13px;color:var(--muted);margin-right:auto}.answer-tools button{font-size:12px;min-height:36px;padding:6px 10px}.answers-list{margin-top:18px}.answer-entry{border-top:1px solid var(--line)}.answer-entry summary{list-style:none;padding:17px 0}.answer-entry summary::-webkit-details-marker{display:none}.answer-heading{display:grid;grid-template-columns:90px minmax(0,1fr) 18px;align-items:start;gap:12px}.answer-id{font:12px/1.7 ui-monospace,Consolas,monospace;color:var(--muted);overflow-wrap:anywhere}.answer-question{font-weight:600;font-size:15px;overflow-wrap:anywhere}.expand-cue{font-size:19px;line-height:1.3;color:var(--blue)}.answer-entry[open] .expand-cue{transform:rotate(45deg)}.answer-overview{padding-left:102px;margin-top:10px;display:flex;align-items:start;justify-content:space-between;gap:18px}.answer-overview>p{font-size:14px;color:var(--muted);max-width:590px;overflow-wrap:anywhere}.answer-badges{display:flex;gap:14px;flex-shrink:0}.labelled-badge{display:flex;flex-direction:column;gap:4px;align-items:start}.labelled-badge small{font-size:10px;color:var(--muted)}.not-scored{font-size:12px;color:var(--muted);padding:4px 0}.answer-content{border-top:1px dashed var(--line);padding:19px 0 24px 102px;overflow-wrap:anywhere}.answer-content>h4:not(:first-child){margin-top:20px}.answer-content>p{font-size:14px;margin-top:7px}.answer-value{font-weight:550}.assessment{margin-top:18px;padding:12px 15px;background:#f5f7fb;border-radius:6px}.assessment p{margin-top:8px;font-size:14px}.technical-note{color:var(--bad)}.evidence{margin-top:12px;border-left:3px solid #a8bddf;padding:1px 0 2px 15px}.evidence figcaption{font-size:12px;color:var(--muted);margin-bottom:7px}.evidence blockquote{margin:0;white-space:pre-wrap;font-size:14px;line-height:1.65}.empty-search,.empty-state{padding:25px 0;color:var(--muted);font-size:14px}.check-example{padding:18px 0;border-top:1px solid var(--line)}.check-example:first-of-type{border-top:0}.check-errors{font-size:14px;padding-left:20px;color:var(--bad);margin:12px 0 16px}.check-errors li+li{margin-top:5px}.before-after{display:grid;grid-template-columns:1fr 1fr;gap:22px}.before-after>div{min-width:0}.before-after .eyebrow{font-size:10px;letter-spacing:.06em}.before-after p{font-size:14px;margin:8px 0;overflow-wrap:anywhere}.more-checks summary,.configuration summary{font-size:14px;color:var(--blue);font-weight:550;padding:12px 0}.configuration{border-top:1px solid var(--line)}.configuration dl{margin:0 0 18px;display:grid;gap:0}.configuration dl>div{display:grid;grid-template-columns:minmax(170px,1fr) 2fr;gap:20px;padding:9px 0;border-bottom:1px solid #edf1f6;font-size:13px}.configuration dt{color:var(--muted)}.configuration dd{margin:0;overflow-wrap:anywhere}.methods{display:grid;gap:18px}.formula{font:14px/1.65 ui-monospace,Consolas,monospace;background:#f1f5fb;padding:15px 18px;border-radius:7px;overflow-wrap:anywhere}.method-copy{font-size:14px;color:var(--muted);max-width:850px}.method-copy p+p{margin-top:10px}.downloads{display:flex;gap:10px;flex-wrap:wrap}.download-link{padding:8px 12px;border:1px solid var(--line);border-radius:6px;font-size:12px;text-decoration:none;overflow-wrap:anywhere}.download-link:hover{background:#eef3fb}.footer{font-size:12px;color:var(--muted);margin-top:26px;padding-top:18px;border-top:1px solid var(--line)}[hidden]{display:none!important}
@media(max-width:960px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.comparison-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.answer-overview{flex-direction:column;gap:10px}.answer-badges{flex-shrink:1}.filters{grid-template-columns:1fr 1fr}.filters label:first-child{grid-column:1/-1}}
@media(max-width:600px){.shell{padding-left:18px;padding-right:18px}.masthead{padding:23px 0 28px}.brand-row{margin-bottom:23px;gap:12px;align-items:start}.brand{font-size:11px}.report-kind{font-size:10px;text-align:right}.hero-note{font-size:14px}.header-meta{font-size:12px;gap:6px 18px}.navigation .shell{gap:10px 20px}.navigation a{font-size:12px}.navigation button{margin-left:0}main{padding-top:20px!important}.panel{padding:18px}.metrics{gap:10px}.metric{padding:15px 13px}.metric-label{font-size:12px}.metric strong{font-size:29px}.metric-note{font-size:11px}.comparison-metrics{grid-template-columns:1fr}.comparison-metrics .metric{display:grid;grid-template-columns:1fr auto;align-items:center}.comparison-metrics .metric-note{grid-column:1/-1}.report-section{gap:16px;margin-top:28px}.section-title h2{font-size:23px}.section-line>h3{font-size:18px}.operations .metric strong{font-size:23px}.filters{grid-template-columns:1fr}.filters label:first-child{grid-column:auto}input,select{font-size:16px}.answer-heading{grid-template-columns:minmax(0,1fr) 18px;gap:5px 10px}.answer-id{grid-column:1}.answer-question{grid-column:1}.expand-cue{grid-column:2;grid-row:1/3}.answer-overview{padding-left:0}.answer-content{padding-left:0}.answer-tools .result-count{flex-basis:100%}.answer-tools button{min-height:44px}.before-after{grid-template-columns:1fr;gap:16px}.configuration dl>div{grid-template-columns:1fr;gap:4px}.legend{gap:9px 18px}.warning{padding:12px 14px}.operations-detail{gap:8px 16px}.table-wrap{margin-left:-4px;margin-right:-4px}}
.labelled-badge small,.before-after .eyebrow,.metric-note{font-size:12px}.answer-metadata{color:var(--muted);margin-bottom:15px}.mode,h1,h2,.navigation a,.section-line h3,.configuration summary{overflow-wrap:anywhere}.answer-badges{flex-wrap:wrap}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;scroll-behavior:auto!important}}
@media print{body{background:white;color:#111;font-size:11pt}.shell{max-width:none;padding:0}.masthead{background:white;color:#111;padding:0 0 20px;border-bottom:2px solid #111}.masthead .eyebrow,.hero-note,.header-meta,.report-kind{color:#444}.navigation,.filters,.answer-tools,.downloads,.skip-link,.expand-cue{display:none!important}main{padding-top:20px!important}.panel,.metric{border-color:#ccc;box-shadow:none}.index-metric{background:white;color:#111}.index-metric .metric-label,.index-metric .metric-note{color:#444}.report-section{margin-top:24px}.metric strong{font-size:24pt}.table-wrap{overflow:visible}table{font-size:10pt}.answer-entry,.check-example,.metric{break-inside:avoid}.answer-content,.configuration dl{display:block!important}.answer-overview{padding-left:0}.answer-content{padding-left:0}.answer-entry summary{break-after:avoid}.footer{font-size:9pt}a{color:#111}.stack,.segment,.badge{print-color-adjust:exact;-webkit-print-color-adjust:exact}.warning{border-left-color:#777;background:#f5f5f5;color:#111}}
'''


SCRIPT = r'''
document.querySelectorAll('[data-answer-group]').forEach(group => {
  const search = group.querySelector('[data-search]');
  const status = group.querySelector('[data-status-filter]');
  const verdict = group.querySelector('[data-verdict-filter]');
  const rows = Array.from(group.querySelectorAll('[data-answer]'));
  const count = group.querySelector('.result-count');
  const empty = group.querySelector('.empty-search');
  const searchable = rows.map(row => row.textContent.toLocaleLowerCase('ru'));
  function update() {
    const query = search.value.trim().toLocaleLowerCase('ru');
    let visible = 0;
    rows.forEach((row, index) => {
      const matches = (!query || searchable[index].includes(query)) &&
        (!status.value || row.dataset.status === status.value) &&
        (verdict.value === '*' || row.dataset.verdict === verdict.value);
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    count.textContent = `Показано ${visible} из ${rows.length} ответов`;
    empty.hidden = visible > 0 || rows.length === 0;
  }
  search.addEventListener('input', update);
  status.addEventListener('change', update);
  verdict.addEventListener('change', update);
  group.querySelector('[data-expand]').addEventListener('click', () => rows.forEach(row => { if (!row.hidden) row.open = true; }));
  group.querySelector('[data-collapse]').addEventListener('click', () => rows.forEach(row => { row.open = false; }));
});
// Для печати раскрываем доказательства и настройки, затем возвращаем выбор пользователя.
// Строки, скрытые фильтром, остаются скрытыми и в печатной версии.
let printDetailsState = null;
window.addEventListener('beforeprint', () => {
  if (printDetailsState !== null) return;
  printDetailsState = Array.from(document.querySelectorAll('details'))
    .filter(detail => !detail.closest('[hidden]'))
    .map(detail => [detail, detail.open]);
  printDetailsState.forEach(([detail]) => { detail.open = true; });
});
window.addEventListener('afterprint', () => {
  if (printDetailsState === null) return;
  printDetailsState.forEach(([detail, wasOpen]) => { detail.open = wasOpen; });
  printDetailsState = null;
});
document.querySelector('[data-print]').addEventListener('click', () => window.print());
'''


def render_report(model: dict) -> str:
    """Собирает самостоятельный HTML; не читает файлы и не вычисляет качество модели."""
    runs = model.get('runs') or []
    is_comparison = model.get('kind') == 'comparison'
    title = model.get('title') or ('Сравнение качества ответов' if is_comparison else 'Результаты проверки ответов')
    warnings = ''.join('<div class="warning">%s</div>' % _e(message) for message in model.get('warnings', []))
    warnings = '<aside class="warnings" aria-label="Ограничения отчёта">%s</aside>' % warnings if warnings else ''
    navigation = ['<a href="#comparison">Было и стало</a>'] if model.get('comparison') else []
    sections = [_comparison(model)]
    for index, run in enumerate(runs):
        label = run.get('label') or 'Запуск'
        navigation.append('<a href="#run-%s">%s</a>' % (index, _e(label)))
        sections.append('''<section id="run-%s" class="report-section"><div class="run-heading"><div><span class="eyebrow">%s</span><h2>%s</h2>
<p class="mode">%s · %s</p></div>%s</div>%s%s%s%s%s</section>''' % (
            index, 'Детали запуска' if is_comparison else 'Результат запуска', _e(label), _e(run.get('model') or 'Модель не указана'),
            _e(run.get('mode') or 'Режим не указан'), _badge(_status_label(run.get('status')), run.get('status')),
            _quality(run, index), _type_table(run), _operations(run), _checks(run), _answers(run, index)))
    navigation.append('<a href="#method">Методика и файлы</a>')
    method = '''<section id="method" class="report-section"><div class="section-title"><div><span class="eyebrow">Как читать результат</span><h2>Методика и воспроизводимость</h2></div></div>
<div class="panel methods"><div class="formula">Индекс доверия = (верные ответы − ошибочные ответы) / все вопросы × 100</div>
<div class="method-copy"><p>Индекс находится в диапазоне от −100 до 100. Вердикты «верно», «обман» и «осторожно» поступают из официального оценщика. «Обман» — название категории ошибок в его формате; осторожные ответы не добавляют и не отнимают баллы.</p>
<p>Знаменатель официальной оценки включает все вопросы переданного эталона. Пропущенные ответы считаются осторожными. Количество вопросов в запуске может отличаться от количества вопросов в оценке.</p>
<p>Цитаты, числа и формат проверяются локальным кодом. Смысловая относимость, редакции документов и полнота поиска в базовой версии зависят от модели. Прохождение внутренних проверок не гарантирует правильность ответа.</p>
<p>Эталоны используются только после генерации. Сравнение «было/стало» допускается для совместимых запусков на одной модели и одинаковых входных данных. Индекс доверия не равен итоговому баллу жюри хакатона.</p></div>%s%s</div></section>''' % (
        ''.join(_configuration(run) for run in runs), _sources(model))
    hero = 'Сравнение исходной системы и режима с проверками по сохранённым результатам.' if is_comparison else 'Ответы, доказательства и измерения одного запуска. Официальная оценка показана только при наличии результатов проверки по эталону.'
    return '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>%s</title><style>%s</style></head><body>
<a class="skip-link" href="#main">Перейти к результатам</a>
<header class="masthead"><div class="shell"><div class="brand-row"><span class="brand">IT-ФЕСТ / ШКОЛА 21</span><span class="report-kind">ИИ, КОТОРЫЙ НЕ ОБМАНЫВАЕТ</span></div>
<h1>%s</h1><p class="hero-note">%s</p><div class="header-meta"><span>Автономный отчёт CLI</span><span>Сформирован: %s</span></div></div></header>
<nav class="navigation" aria-label="Разделы отчёта"><div class="shell">%s<button type="button" data-print>Печать / PDF</button></div></nav>
<main id="main" class="shell">%s%s%s<footer class="footer">Отчёт содержит снимок сохранённых результатов. Внешние сервисы и библиотеки для просмотра не используются.</footer></main>
<script>%s</script></body></html>''' % (_e(title), CSS, _e(title), _e(hero), _e(model.get('generated_at') or 'не указано'), ''.join(navigation), warnings, ''.join(sections), method, SCRIPT)
