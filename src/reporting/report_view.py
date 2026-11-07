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
<div class="answer-tools"><span class="result-count" role="status" aria-live="polite" aria-atomic="true">Показано %s из %s ответов</span><button type="button" data-expand>Раскрыть видимые</button><button type="button" data-collapse>Свернуть</button></div>
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
              'context_token_limit': 'Лимит контекста',
              'repair_attempts': 'Разрешено исправлений', 'timeout_seconds': 'Таймаут HTTP, с',
              'max_questions_per_request': 'Максимум вопросов в запросе',
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
/* Единые токены сохраняют контраст и одинаковую иерархию во всех видах отчёта. */
:root{color-scheme:light;--bg:#f7f8fa;--surface:#fff;--ink:#20252d;--muted:#5e6571;--line:#e0e3e8;--line-strong:#a0a7b1;--blue:#2348b5;--blue-soft:#eef2fd;--navy:#20252d;--good:#176044;--good-soft:#edf6f0;--bad:#a72e38;--bad-soft:#fff0f1;--amber:#83530d;--amber-soft:#fff7e7;--focus:#2348b5;--radius:8px;--mono:ui-monospace,"Cascadia Mono",Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}a{color:var(--blue);text-underline-offset:4px}button,input,select{font:inherit}button,summary,select{cursor:pointer}button{border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink);padding:10px 15px;min-height:44px;font-size:13px;font-weight:600;transition:background .18s,border-color .18s}button:hover{background:var(--blue-soft);border-color:var(--blue)}button:active{background:#e1e8fc}a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--focus);outline-offset:3px}h1,h2,h3,h4,p,figure{margin:0}h1{font-size:clamp(28px,3.2vw,40px);font-weight:650;line-height:1.18;letter-spacing:-.035em;max-width:920px;text-wrap:balance}h2{font-size:25px;font-weight:650;line-height:1.25;letter-spacing:-.025em}h3{font-size:18px;line-height:1.4;font-weight:650;letter-spacing:-.015em}h4{font-size:14px;line-height:1.5;font-weight:650}code{font:13px/1.55 var(--mono);overflow-wrap:anywhere}b,strong{font-weight:650}.muted{color:var(--muted)}.small{font-size:13px}.eyebrow{display:block;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip-link{position:absolute;left:16px;top:-90px;padding:10px;background:var(--surface);z-index:10}.skip-link:focus{top:12px}.shell{max-width:1248px;margin:auto;padding:0 40px}.masthead{background:var(--surface);padding:24px 0 28px}.hero-note{color:var(--muted);font-size:15px;margin-top:13px;max-width:830px}.header-meta{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:12px;margin-top:19px;color:var(--muted)}.header-meta>span{display:flex;align-items:center;gap:8px}.meta-dot{width:6px;height:6px;border-radius:50%;background:var(--blue);display:inline-block;flex-shrink:0}.navigation{background:var(--surface);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.navigation .shell{display:flex;gap:24px;align-items:center;flex-wrap:wrap;padding-top:8px;padding-bottom:8px}.navigation a{display:flex;align-items:center;min-height:44px;color:var(--muted);font-weight:550;text-decoration:none;font-size:13px;padding:8px 0;transition:color .18s}.navigation a:hover{color:var(--blue)}.navigation button{margin-left:auto;display:flex;align-items:center;gap:8px}.button-icon{width:16px;height:16px;flex-shrink:0}main{padding-top:28px!important;padding-bottom:48px!important}.warnings{display:grid;gap:10px;margin-bottom:28px}.warning{background:var(--amber-soft);border:1px solid #ead8b3;border-radius:6px;border-left:3px solid var(--amber);padding:13px 17px;font-size:13px;line-height:1.65;color:#6d4912;overflow-wrap:anywhere}.report-section{display:grid;gap:22px;margin-top:48px;scroll-margin-top:24px;min-width:0}.report-section:first-of-type{margin-top:0}.section-title{display:flex;align-items:end;justify-content:space-between;gap:16px;flex-wrap:wrap}.section-title .eyebrow{margin-bottom:7px}.run-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}.run-heading .eyebrow{margin-bottom:6px}.run-heading .mode{font:12px/1.6 var(--mono);color:var(--muted);margin-top:8px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;background:var(--surface)}.metric{padding:22px 24px;min-width:0;display:flex;flex-direction:column;align-items:start;gap:12px;border-left:1px solid var(--line)}.metric:first-child{border-left:0}.metric-label{font-size:13px;color:var(--muted);font-weight:550}.metric strong{font-size:43px;line-height:1.1;letter-spacing:-.045em;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;font-weight:600}.metric-note{font-size:12px;color:var(--muted)}.index-metric{background:var(--blue-soft);box-shadow:inset 0 3px var(--blue)}.index-metric strong{color:var(--blue)}.index-metric .metric-label{color:var(--blue)}.comparison-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:24px;min-width:0}.section-line{display:flex;align-items:center;justify-content:space-between;gap:12px 16px;flex-wrap:wrap}.section-line>.muted{font-size:12px}.section-note{font-size:14px;margin-top:10px;margin-bottom:18px;max-width:850px}.distribution-panel{padding-top:20px;padding-bottom:20px}.distribution{margin-top:18px}.stack{display:flex;height:9px;background:var(--line);border-radius:3px;overflow:hidden}.segment{display:block;height:100%}.segment.good,.dot.good{background:var(--good)}.segment.bad,.dot.bad{background:var(--bad)}.segment.cautious,.dot.cautious{background:var(--amber)}.legend{display:flex;gap:12px 30px;flex-wrap:wrap;margin-top:15px;font-size:12px;color:var(--muted)}.legend>span{display:flex;align-items:center;gap:8px}.legend b{font-variant-numeric:tabular-nums;color:var(--ink)}.dot{height:7px;width:7px;border-radius:2px;display:inline-block;flex-shrink:0}.subheading{margin-bottom:13px}.operations .metric{padding:18px 22px;gap:10px}.operations .metric strong{font-size:27px;letter-spacing:-.025em}.operations-detail{display:flex;flex-wrap:wrap;gap:9px 26px;color:var(--muted);font-size:12px;margin-top:13px}.operations-detail b{color:var(--ink);font-variant-numeric:tabular-nums}.badge{display:inline-flex;align-items:center;border:1px solid transparent;border-radius:4px;padding:4px 8px;font-size:12px;line-height:1.4;font-weight:600;overflow-wrap:anywhere}.badge.good{background:var(--good-soft);color:var(--good)}.badge.bad{background:var(--bad-soft);color:var(--bad)}.badge.cautious{background:var(--amber-soft);color:var(--amber)}.badge.neutral{background:#f0f2f5;color:#4a5464}.badge.conflict{background:#f2eef9;color:#63468a}.no-quality{background:var(--surface);border-left:3px solid var(--blue);padding:24px 28px}.no-quality .eyebrow{color:var(--blue)}.no-quality h3{margin:10px 0}.no-quality p{font-size:14px;max-width:850px}.no-quality p+p{margin-top:12px}.table-wrap{overflow-x:auto;margin-top:17px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:13px 12px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}th:first-child,td:first-child{text-align:left;white-space:normal;min-width:180px}thead th{font-size:12px;color:var(--muted);font-weight:550;background:var(--bg);border-top:1px solid var(--line)}tbody th{font-weight:500}tbody tr:last-child>*{border-bottom:0}tbody tr:hover{background:var(--bg)}.strong{font-weight:650}.filters{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px;margin-top:22px;padding:18px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.filters label{display:grid;gap:8px;font-size:12px;font-weight:550;color:var(--muted);min-width:0}input,select{width:100%;min-height:44px;border:1px solid var(--line-strong);background:var(--surface);border-radius:5px;padding:10px 12px;color:var(--ink);font-size:14px;min-width:0}input::placeholder{color:var(--muted)}.answer-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:15px;margin-bottom:9px}.result-count{font-size:13px;color:var(--muted);margin-right:auto}.answer-tools button{font-size:12px;min-height:44px;padding:9px 12px}.answers-list{margin-top:21px}.answer-entry{border-top:1px solid var(--line)}.answer-entry:last-child{border-bottom:1px solid var(--line)}.answer-entry summary{list-style:none;padding:20px 12px;margin:0 -12px;border-radius:5px;transition:background .18s}.answer-entry summary:hover{background:var(--bg)}.answer-entry summary::-webkit-details-marker{display:none}.answer-heading{display:grid;grid-template-columns:92px minmax(0,1fr) 24px;align-items:start;gap:14px}.answer-id{font:12px/1.8 var(--mono);color:var(--muted);overflow-wrap:anywhere}.answer-question{font-weight:600;font-size:15px;line-height:1.55;overflow-wrap:anywhere}.expand-cue{display:grid;place-items:center;width:24px;height:24px;font-size:20px;line-height:1;color:var(--muted);transition:transform .18s}.answer-entry[open] .expand-cue{transform:rotate(45deg);color:var(--blue)}.answer-entry[open] .answer-question{color:var(--blue)}.answer-overview{padding-left:106px;margin-top:9px;display:flex;align-items:start;justify-content:space-between;gap:20px}.answer-overview>p{font-size:14px;color:var(--muted);max-width:600px;overflow-wrap:anywhere}.answer-badges{display:flex;gap:14px;flex-wrap:wrap;flex-shrink:0}.labelled-badge{display:flex;flex-direction:column;gap:4px;align-items:start}.labelled-badge small{font-size:12px;color:var(--muted)}.not-scored{font-size:12px;color:var(--muted);padding:5px 0}.answer-content{border-top:1px solid var(--line);padding:22px 0 28px 106px;overflow-wrap:anywhere}.answer-content>h4:not(:first-child){margin-top:23px}.answer-content>p{font-size:14px;margin-top:7px;max-width:850px}.answer-value{font-size:17px!important;line-height:1.6;font-weight:550}.answer-metadata{color:var(--muted);margin-bottom:18px;font-size:12px!important}.assessment{margin-top:20px;padding:14px 16px;background:var(--bg);border:1px solid var(--line);border-radius:5px}.assessment p{margin-top:8px;font-size:14px}.technical-note{color:var(--bad)}.evidence{margin-top:13px;border:1px solid var(--line);border-left:3px solid var(--line-strong);border-radius:0 5px 5px 0;padding:15px 18px;background:var(--surface)}.evidence figcaption{font-size:12px;color:var(--muted);margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--line)}.evidence figcaption code{font-size:12px;color:var(--ink)}.evidence blockquote{margin:0;white-space:pre-wrap;font-size:14px;line-height:1.75}.empty-search,.empty-state{padding:32px 16px;color:var(--muted);font-size:14px;text-align:center;background:var(--bg);border-radius:5px}.check-example{padding:20px 0;border-top:1px solid var(--line)}.check-example:first-of-type{border-top:0}.check-errors{font-size:14px;padding-left:20px;color:var(--bad);margin:12px 0 16px}.check-errors li+li{margin-top:5px}.before-after{display:grid;grid-template-columns:1fr 1fr;gap:20px}.before-after>div{min-width:0;padding:16px;background:var(--bg);border:1px solid var(--line);border-radius:5px}.before-after .eyebrow{font-size:12px;letter-spacing:.02em;text-transform:none}.before-after p{font-size:14px;margin:10px 0;overflow-wrap:anywhere}.more-checks summary,.configuration summary{font-size:14px;color:var(--blue);font-weight:550;padding:13px 0;min-height:48px}.configuration{border-top:1px solid var(--line)}.configuration dl{margin:0 0 18px;display:grid;gap:0}.configuration dl>div{display:grid;grid-template-columns:minmax(170px,1fr) 2fr;gap:20px;padding:11px 0;border-bottom:1px solid var(--line);font-size:13px}.configuration dt{color:var(--muted)}.configuration dd{margin:0;overflow-wrap:anywhere;font-family:var(--mono);font-size:12px}.methods{display:grid;gap:20px}.formula{font:14px/1.7 var(--mono);background:var(--bg);padding:17px 20px;border-left:3px solid var(--blue);overflow-wrap:anywhere}.method-copy{font-size:14px;color:var(--muted);max-width:850px}.method-copy p+p{margin-top:12px}.downloads{display:flex;gap:9px;flex-wrap:wrap}.download-link{display:flex;align-items:center;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:5px;font:12px/1.5 var(--mono);text-decoration:none;overflow-wrap:anywhere;transition:background .18s,border-color .18s}.download-link:hover{background:var(--blue-soft);border-color:var(--blue)}.footer{display:flex;gap:20px;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:36px;padding-top:20px;border-top:1px solid var(--line)}.mode,h1,h2,.navigation a,.section-line h3,.configuration summary{overflow-wrap:anywhere}[hidden]{display:none!important}
/* На планшете сохраняем сравнимость метрик, на телефоне перестраиваем элементы управления. */
@media(max-width:1000px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric:nth-child(3){border-left:0}.metric:nth-child(n+3){border-top:1px solid var(--line)}.comparison-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.comparison-metrics .metric:nth-child(3){border-left:1px solid var(--line);border-top:0}.answer-overview{flex-direction:column;gap:12px}.answer-badges{flex-shrink:1}.filters{grid-template-columns:1fr 1fr}.filters label:first-child{grid-column:1/-1}.shell{padding-left:28px;padding-right:28px}}
@media(max-width:600px){.shell{padding-left:18px;padding-right:18px}.masthead{padding:20px 0 24px}.hero-note{font-size:14px;line-height:1.65}.header-meta{font-size:12px;gap:7px 18px}.navigation .shell{gap:4px 18px}.navigation a{font-size:12px}.navigation button{margin-left:0;font-size:12px;padding-left:10px;padding-right:10px}main{padding-top:20px!important}.panel{padding:18px}.metric{padding:19px 16px;gap:11px}.metric-label{font-size:12px}.metric strong{font-size:34px}.metric-note{font-size:12px}.comparison-metrics{grid-template-columns:1fr}.comparison-metrics .metric{display:grid;grid-template-columns:1fr auto;align-items:center;border-left:0;border-top:1px solid var(--line)}.comparison-metrics .metric:first-child{border-top:0}.comparison-metrics .metric:nth-child(3){border-left:0;border-top:1px solid var(--line)}.comparison-metrics .metric-note{grid-column:1/-1}.report-section{gap:18px;margin-top:36px}.section-title h2,h2{font-size:23px}.section-line>h3{font-size:17px}.operations .metric{padding:16px}.operations .metric strong{font-size:23px}.filters{grid-template-columns:1fr;padding:14px;margin-top:18px;gap:14px}.filters label:first-child{grid-column:auto}input,select{font-size:16px}.answer-heading{grid-template-columns:minmax(0,1fr) 24px;gap:5px 10px}.answer-id{grid-column:1}.answer-question{grid-column:1;font-size:16px}.expand-cue{grid-column:2;grid-row:1/3}.answer-overview{padding-left:0}.answer-content{padding-left:0}.answer-entry summary{margin-left:-6px;margin-right:-6px;padding-left:6px;padding-right:6px}.answer-overview>p,.answer-content>p,.evidence blockquote{font-size:16px}.answer-content>h4{font-size:15px}.answer-tools .result-count{flex-basis:100%;margin-bottom:4px}.answer-tools button{min-height:44px}.before-after{grid-template-columns:1fr;gap:12px}.configuration dl>div{grid-template-columns:1fr;gap:5px}.legend{gap:10px 22px}.warning{padding:12px 14px}.operations-detail{gap:8px 16px}.table-wrap{margin-left:-3px;margin-right:-3px}.evidence{padding:13px 14px}.no-quality{padding:20px}.formula{padding:14px;font-size:13px}.footer{display:block;line-height:1.65}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;scroll-behavior:auto!important}}
/* Печать сохраняет доказательства и числа, убирая только элементы управления. */
@media print{body{background:white;color:#111;font-size:11pt}.shell{max-width:none;padding:0}.masthead{background:white;color:#111;padding:0 0 20px;border-bottom:2px solid #111}.hero-note,.header-meta{color:#444}.navigation,.filters,.answer-tools,.downloads,.skip-link,.expand-cue{display:none!important}main{padding-top:20px!important}.panel,.metrics,.metric{border-color:#ccc;box-shadow:none}.metrics{grid-template-columns:repeat(4,minmax(0,1fr))}.comparison-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.metric{border-top:0!important;border-left:1px solid #ccc!important;padding:16px}.metric:first-child{border-left:0!important}.index-metric{background:white;color:#111;box-shadow:none}.index-metric strong,.index-metric .metric-label,.index-metric .metric-note{color:#111}.report-section{margin-top:24px}.metric strong{font-size:24pt}.table-wrap{overflow:visible}table{font-size:10pt}th,td{padding:8px}.answer-entry,.check-example,.metric,.evidence{break-inside:avoid}.answer-content,.configuration dl{display:block!important}.answer-overview{padding-left:0}.answer-content{padding-left:0}.answer-entry summary{break-after:avoid}.footer{font-size:9pt}a{color:#111}.stack,.segment,.badge{print-color-adjust:exact;-webkit-print-color-adjust:exact}.warning{border-left-color:#777;background:#f5f5f5;color:#111}.before-after{grid-template-columns:1fr 1fr}}
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
    if not is_comparison and runs:
        navigation.append('<a href="#answers-0">Ответы и доказательства</a>')
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
<header class="masthead"><div class="shell">
<h1>%s</h1><p class="hero-note">%s</p><div class="header-meta"><span><i class="meta-dot" aria-hidden="true"></i>Автономный отчёт CLI</span><span>Сформирован: %s</span></div></div></header>
<nav class="navigation" aria-label="Разделы отчёта"><div class="shell">%s<button type="button" data-print><svg class="button-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M6 9V3h12v6M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 14h12v7H6zM17 12h2"/></svg>Печать / PDF</button></div></nav>
<main id="main" class="shell">%s%s%s<footer class="footer">Снимок сохранённых результатов. Для просмотра не нужны внешние сервисы и библиотеки.</footer></main>
<script>%s</script></body></html>''' % (_e(title), CSS, _e(title), _e(hero), _e(model.get('generated_at') or 'не указано'), ''.join(navigation), warnings, ''.join(sections), method, SCRIPT)
