"use strict";

if (page === "compare") {
  document.title = "Сравнение запусков · Ответы по документам";
  const form = byId("comparison-form");
  const submit = form.querySelector("button");
  const selects = [form.elements.baseline, form.elements.verified];
  const state = byId("comparison-state");
  const error = byId("comparison-error");
  const results = byId("comparison-results");
  const changedOnly = byId("comparison-changed");
  const number = new Intl.NumberFormat("ru", { maximumFractionDigits: 2 });
  const signed = new Intl.NumberFormat("ru", { maximumFractionDigits: 2, signDisplay: "exceptZero" });
  const modeLabels = { baseline: "Чистый запрос", verified: "Улучшенный запрос" };
  const metricHeaders = ["Показатель", ...Object.values(modeLabels), "Разница"];
  let questions = [];
  let scored = false;

  function table(headers, rows, highlight = () => false) {
    const element = document.createElement("table");
    const heading = element.createTHead().insertRow();
    for (const title of headers) {
      const cell = node("th", title);
      cell.scope = "col";
      heading.append(cell);
    }
    const body = element.createTBody();
    rows.forEach(([label, ...values], rowIndex) => {
      const row = body.insertRow();
      const cell = node("th", label);
      cell.scope = "row";
      row.append(cell);
      values.forEach((value, columnIndex) => {
        const item = node("td", value);
        if (highlight(rowIndex, columnIndex)) item.classList.add("active-transition");
        row.append(item);
      });
    });
    return element;
  }

  function metricRow(label, baseline, verified, showDelta = true) {
    return [label, baseline == null ? "—" : number.format(baseline), verified == null ? "—" : number.format(verified),
      !showDelta || baseline == null || verified == null ? "—" : signed.format(verified - baseline)];
  }

  function displayedIndex(value) {
    return Number.isFinite(value) ? value : null;
  }

  function svgElement(tag, attributes) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    return element;
  }

  function chartBar(label, value, max, tone) {
    const row = node("div", "", "comparison-chart-row");
    const svg = svgElement("svg", { viewBox: "0 0 200 12", preserveAspectRatio: "none", "aria-hidden": "true" });
    const rect = (x, width, style) => svgElement("rect", { x, width, y: 2, height: 8, rx: 2, class: style });
    svg.append(rect(0, 200, "comparison-chart-track"));
    if (value != null) {
      svg.append(rect(0, 200 * value / max, `comparison-chart-${tone}`));
    }
    row.append(node("span", label), svg, node("strong", value == null ? "—" : number.format(value)));
    return row;
  }

  function renderTrustCard(mode, report) {
    const card = byId(`comparison-trust-${mode}`);
    const index = displayedIndex(report?.index);
    const bar = card.querySelector(".comparison-trust-bar");
    const counts = card.querySelector(".comparison-trust-counts");
    card.querySelector(".comparison-trust-score").textContent = index == null ? "—" : number.format(index);
    card.querySelector(".comparison-trust-scale").hidden = index == null;
    bar.hidden = index == null || !report.n;
    bar.replaceChildren();
    if (index == null) {
      counts.textContent = "Нет оценки по эталону";
      return;
    }
    const results = [["correct", "Верно"], ["careful", "Осторожно"], ["lies", "Ошибки"]];
    counts.replaceChildren();
    for (const [key, label] of results) {
      if (counts.hasChildNodes()) counts.append(" · ");
      counts.append(`${label} `, node("strong", number.format(report[key]), `comparison-count-${key}`));
      if (report[key] > 0) {
        const segment = node("span", "", `comparison-trust-${key}`);
        segment.style.flexGrow = report[key];
        bar.append(segment);
      }
    }
  }

  function renderReportQuality(data) {
    const before = data.baseline.quality;
    const after = data.verified.quality;
    renderTrustCard("baseline", before);
    renderTrustCard("verified", after);
    const note = [data.quality_note];
    for (const [mode, label] of Object.entries(modeLabels)) {
      const conflicts = data[mode].source_audit_conflicts;
      if (conflicts) note.push(`${label}: верных по старой оценке с неподтверждёнными источниками — ${number.format(conflicts)}.`);
    }
    const qualityNote = byId("comparison-quality-note");
    qualityNote.textContent = note.filter(Boolean).join(" ");
    qualityNote.hidden = !qualityNote.textContent;

    const categories = [...new Set([...Object.keys(before?.by_type ?? {}), ...Object.keys(after?.by_type ?? {})])].sort();
    byId("comparison-categories").hidden = !categories.length;
    byId("comparison-category-table").replaceChildren(table(metricHeaders, categories.map((category) => metricRow(
      category, displayedIndex(before?.by_type?.[category]?.index), displayedIndex(after?.by_type?.[category]?.index),
      Boolean(data.quality),
    ))));
  }

  function renderOverlap(data, verdictChanges) {
    const { paired } = data.coverage;
    const chart = byId("comparison-overlap-chart");
    const note = byId("comparison-overlap-note");
    if (!paired) {
      chart.replaceChildren();
      note.textContent = "Общих однозначно сопоставленных вопросов нет.";
      return;
    }
    const unchanged = data.questions.filter((item) => !item.changed).length;
    const textOnly = data.questions.filter((item) => item.changed && item.baseline.status === item.verified.status).length;
    const statusOnly = data.questions.filter((item) => item.changed && item.baseline.answer === item.verified.answer).length;
    const both = paired - unchanged - textOnly - statusOnly;
    chart.replaceChildren(
      chartBar("Ответы совпадают", unchanged, paired, "unchanged"),
      chartBar("Только текст", textOnly, paired, "baseline"),
      chartBar("Только статус", statusOnly, paired, "status"),
      chartBar("Текст и статус", both, paired, "both"),
    );
    const share = (total) => `${number.format(100 * paired / total)} %`;
    const coverage = `Сопоставлено ${share(data.baseline.n_questions)} вопросов чистого запроса и ${share(data.verified.n_questions)} вопросов улучшенного запроса.`;
    const better = verdictChanges.filter((change) => change > 0).length;
    const worse = verdictChanges.filter((change) => change < 0).length;
    note.textContent = verdictChanges.length ? `${coverage} По эталону: улучшилось ${better}, ухудшилось ${worse}, совпало ${verdictChanges.length - better - worse}.` : coverage;
  }

  function renderAnswerSummary(transitions, paired) {
    const summary = byId("comparison-answer-summary");
    const chart = byId("comparison-status-chart");
    chart.hidden = !paired;
    if (!paired) {
      summary.replaceChildren(node("p", "Сводка появится при наличии общих вопросов.", "hint"));
      return;
    }
    const counts = new Map([
      ["ответ", [0, 0]], ["расхождение", [0, 0]], ["нет в документах", [0, 0]], ["не знаю", [0, 0]],
    ]);
    for (const { baseline, verified, count } of transitions) {
      if (!counts.has(baseline)) counts.set(baseline, [0, 0]);
      if (!counts.has(verified)) counts.set(verified, [0, 0]);
      counts.get(baseline)[0] += count;
      counts.get(verified)[1] += count;
    }
    const rows = Array.from(counts, ([status, values]) => metricRow(`«${status}»`, ...values));
    rows.push(metricRow("Всего общих вопросов", paired, paired));
    summary.replaceChildren(table(metricHeaders, rows));
    chart.replaceChildren(...Array.from(counts).filter(([, values]) => values.some(Boolean)).map(([status, [baseline, verified]]) => {
      const group = node("div", "", "comparison-status-group");
      group.append(node("strong", `«${status}»`), chartBar(modeLabels.baseline, baseline, paired, "baseline"),
        chartBar(modeLabels.verified, verified, paired, "verified"));
      return group;
    }));
  }

  function renderQuestions() {
    const selected = changedOnly.checked ? questions.filter((item) => item.changed) : questions;
    const rows = selected.map((item) => {
      const details = node("details", "", "comparison-question");
      const summary = node("summary", item.question);
      summary.append(node("span", item.changed ? "Есть различия" : "Совпадает", "run-meta"));
      const answers = node("div", "", "comparison-grid");
      for (const [mode, label] of Object.entries(modeLabels)) {
        const answer = item[mode];
        const card = node("div", "", "comparison-answer");
        card.append(node("strong", label), node("span", answer.status, "run-meta"),
          node("p", answer.answer || "Ответ отсутствует"));
        if (scored && answer.verdict) card.append(node("span", `По эталону: ${answer.verdict}`, "run-meta"));
        answers.append(card);
      }
      details.append(summary, answers);
      return details;
    });
    const empty = questions.length ? "Различий в статусах и тексте нет." : "Общих однозначно сопоставленных вопросов нет.";
    byId("comparison-questions").replaceChildren(...(rows.length ? rows : [node("p", empty, "hint")]));
  }

  function render(data, params) {
    const coverage = data.coverage;
    const ranks = new Map([["обман", -1], ["осторожно", 0], ["верно", 1]]);
    const verdictChanges = data.quality ? data.questions
      .filter((row) => ranks.has(row.baseline.verdict) && ranks.has(row.verified.verdict))
      .map((row) => ranks.get(row.verified.verdict) - ranks.get(row.baseline.verdict)) : [];
    byId("comparison-verdict-changes").textContent = verdictChanges.length
      ? number.format(verdictChanges.filter((change) => change !== 0).length) : "—";
    byId("comparison-document-warning").hidden = data.documents_match !== false;
    const scope = [];
    if (coverage.baseline_only || coverage.verified_only) {
      scope.push(`Вопросов без пары: в чистом запросе — ${number.format(coverage.baseline_only)}, в улучшенном — ${number.format(coverage.verified_only)}.`);
    }
    if (coverage.ambiguous) scope.push(`Повторяющиеся формулировки исключены (${coverage.ambiguous}).`);
    if (data.settings_changed.length) scope.push(`Различаются настройки генерации: ${data.settings_changed.join(", ")}.`);
    const scopeNote = byId("comparison-scope");
    scopeNote.textContent = scope.join(" ");
    scopeNote.hidden = !scope.length;
    byId("comparison-links").replaceChildren(...Object.entries(modeLabels).map(([mode, label]) => {
      const link = node("a", `Отчёт «${label}»`, "button secondary");
      link.href = `/runs/${encodeURIComponent(params.get(mode))}/report.html`;
      link.target = "_blank";
      link.rel = "noopener";
      return link;
    }));

    renderReportQuality(data);
    renderOverlap(data, verdictChanges);
    const rows = [["Модель", data.baseline.model || "—", data.verified.model || "—", "—"],
      metricRow("Вопросов в запуске", data.baseline.n_questions, data.verified.n_questions)];
    const addMetrics = (fields, baseline, verified, showDelta = true) => {
      for (const [key, label] of fields) {
        rows.push(metricRow(label, baseline?.[key], verified?.[key], showDelta));
      }
    };
    addMetrics([["n", "Оценено вопросов"], ["correct", "Верные"],
      ["careful", "Осторожные"], ["lies", "Ошибочные"]], data.baseline.quality, data.verified.quality, Boolean(data.quality));
    addMetrics([["elapsed_seconds", "Время, с"], ["requests", "Запросы к API"], ["prompt_tokens", "Токены входа"],
      ["completion_tokens", "Токены выхода"]], data.baseline.operations, data.verified.operations);
    byId("comparison-metrics").replaceChildren(table(metricHeaders, rows));
    renderAnswerSummary(data.transitions, coverage.paired);

    const labels = [...new Set(data.transitions.flatMap((cell) => [cell.baseline, cell.verified]))];
    const counts = new Map(data.transitions.map((cell) => [JSON.stringify([cell.baseline, cell.verified]), cell.count]));
    const matrix = labels.map((from) => [from, ...labels.map((to) => counts.get(JSON.stringify([from, to])) ?? 0)]);
    byId("comparison-matrix").replaceChildren(matrix.length
      ? table(["Чистый / улучшенный", ...labels], matrix, (row, column) => row !== column && matrix[row][column + 1] > 0)
      : node("p", "Матрица появится при наличии общих вопросов.", "hint"));
    questions = data.questions;
    scored = Boolean(data.quality);
    renderQuestions();
    results.hidden = false;
  }

  async function loadLegacyQuality(data, params) {
    // Старый сервер читает новый JS с диска, но не включает оценки каждого отчёта в /api/compare.
    await Promise.all(["baseline", "verified"].map(async (mode) => {
      if (Object.hasOwn(data[mode], "quality")) return;
      data[mode].quality = data.quality?.[mode] ?? null;
      try {
        const id = encodeURIComponent(params.get(mode));
        const response = await fetch(`/runs/${id}/evaluation.json`);
        if (!response.ok) return;
        const quality = await response.json();
        if (Number.isFinite(quality?.index)) data[mode].quality = quality;
      } catch {
        // Сравнение ответов остаётся доступным, если отдельный отчёт не удалось загрузить.
      }
    }));
  }

  async function compare() {
    const params = new URLSearchParams(new FormData(form));
    submit.disabled = true;
    selects.forEach((select) => { select.disabled = true; });
    error.hidden = results.hidden = true;
    state.textContent = "Сопоставляем вопросы…";
    try {
      const response = await fetch(`/api/compare?${params}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      await loadLegacyQuality(data, params);
      render(data, params);
      history.replaceState(null, "", `/compare?${params}`);
    } catch (failure) {
      error.textContent = failure.message || "Не удалось сравнить запуски.";
      error.hidden = false;
    } finally {
      state.textContent = "";
      selects.forEach((select) => { select.disabled = false; });
      submit.disabled = selects.some((select) => !select.value);
    }
  }

  async function loadComparisonRuns() {
    try {
      const response = await fetch("/api/runs");
      if (!response.ok) throw new Error();
      const runs = (await response.json()).filter((run) => run.has_report
        && ["completed", "completed_with_errors"].includes(run.status));
      const params = new URLSearchParams(location.search);
      for (const select of selects) {
        const matching = runs.filter((run) => run.mode === select.name);
        const options = matching.map((run) => {
          const date = new Date(run.created_at ?? "");
          const stamp = Number.isNaN(date.getTime()) ? run.id : dateFormat.format(date);
          return new Option(`${run.label} · ${stamp} · ${run.total} вопр.`, run.id);
        });
        select.replaceChildren(new Option(matching.length ? "Выберите запуск" : "Нет готовых запусков", ""), ...options);
        select.value = matching.some((run) => run.id === params.get(select.name)) ? params.get(select.name) : "";
      }
      const available = selects.every((select) => select.options.length > 1);
      state.textContent = available ? "" : "Для сравнения нужен хотя бы один готовый запуск каждого режима.";
      if (selects.every((select) => select.value)) await compare();
    } catch {
      state.textContent = "";
      error.textContent = "Не удалось загрузить историю запусков.";
      error.hidden = false;
    }
  }

  form.addEventListener("change", () => { submit.disabled = selects.some((select) => !select.value); });
  form.addEventListener("submit", (event) => { event.preventDefault(); compare(); });
  changedOnly.addEventListener("change", renderQuestions);
  loadComparisonRuns();
}
