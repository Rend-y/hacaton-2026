"use strict";

const byId = (id) => document.getElementById(id);
const page = new Map([["/new", "new"], ["/settings", "settings"], ["/tests", "tests"]]).get(location.pathname) ?? "runs";
const statuses = new Map([
  ["running", "В процессе"],
  ["completed", "Завершена"],
  ["completed_with_errors", "Завершена с ошибками"],
  ["failed", "Ошибка"],
  ["unknown", "Статус неизвестен"],
]);
const runKinds = new Map([["manual", "Вручную"], ["cli", "CLI"], ["benchmark", "check-all"], ["test", "Тестовый набор"]]);
const dateFormat = new Intl.DateTimeFormat("ru", { dateStyle: "short", timeStyle: "short" });
let previous = "";

byId(`${page}-page`).hidden = false;
byId(`${page}-link`).setAttribute("aria-current", "page");
if (page === "new") {
  document.title = "Новая обработка · Ответы по документам";
  byId("launch-form").addEventListener("submit", submitRun);
  const mode = byId("mode");
  const descriptions = {
    verified: byId("mode-hint").textContent,
    baseline: "Сохраняет ответ модели как есть, без локальной сверки цитат, чисел и дат и без попыток исправления.",
  };
  const showMode = () => { byId("mode-hint").textContent = descriptions[mode.value]; };
  mode.addEventListener("change", showMode);
  showMode();
} else if (page === "settings") {
  document.title = "Настройки · Ответы по документам";
  byId("settings-form").addEventListener("submit", saveSettings);
  loadSettings();
} else if (page === "runs") {
  refreshRuns();
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function runRow(run) {
  const row = document.createElement("tr");
  const mode = run.mode === "verified" ? "С проверкой" : run.mode === "baseline" ? "Прямой ответ" : "";
  const label = row.insertCell();
  label.append(node("span", run.label, "run-name"));
  const meta = [mode, run.file_count > 1 ? `Файлов: ${run.file_count}` : ""].filter(Boolean);
  if (meta.length) label.append(node("span", meta.join(" · "), "run-meta"));
  if (run.error) label.append(node("span", run.error, "run-error"));

  const kind = row.insertCell();
  kind.className = "run-kind";
  kind.textContent = runKinds.get(run.kind) ?? "—";

  const status = statuses.has(run.status) ? run.status : "unknown";
  row.insertCell().append(node("span", statuses.get(status), `status ${status}`));
  const progress = run.total ? `${run.completed} / ${run.total}` : "—";
  const questions = node("span", progress);
  if (run.total) questions.setAttribute("aria-label", `Обработано ${run.completed} из ${run.total} вопросов`);
  row.insertCell().append(questions);

  const date = new Date(run.created_at ?? "");
  const time = row.insertCell();
  time.className = "run-time";
  if (Number.isNaN(date.getTime())) {
    time.textContent = "—";
  } else {
    const timestamp = node("time", dateFormat.format(date));
    timestamp.dateTime = date.toISOString();
    time.append(timestamp);
  }

  const result = row.insertCell();
  if (run.has_report) {
    const link = node("a", "Открыть отчёт", "report-link");
    link.href = `/runs/${encodeURIComponent(run.id)}/report.html`;
    link.target = "_blank";
    link.rel = "noopener";
    result.append(link);
  } else {
    result.append(node("span", "—", "placeholder"));
  }
  return row;
}

async function refreshRuns() {
  const error = byId("list-error");
  try {
    const response = await fetch("/api/runs");
    if (!response.ok) throw new Error();
    const runs = await response.json();
    const current = JSON.stringify(runs);
    if (current !== previous) {
      const rows = byId("run-rows");
      const focusedReport = rows.contains(document.activeElement) ? document.activeElement.href : null;
      rows.replaceChildren(...runs.map(runRow));
      if (focusedReport) {
        Array.from(rows.querySelectorAll("a")).find((link) => link.href === focusedReport)?.focus();
      }
      byId("run-count").textContent = runs.length;
      byId("run-count").hidden = false;
      byId("run-table").hidden = runs.length === 0;
      byId("list-empty").hidden = runs.length > 0;
      byId("list-empty").textContent = "Пока нет обработок. Запустите первую, чтобы увидеть её здесь.";
      previous = current;
    }
    byId("refresh-note").hidden = false;
    error.hidden = true;
  } catch {
    byId("list-empty").hidden = true;
    byId("refresh-note").hidden = true;
    error.textContent = "Не удалось обновить список. Повторим попытку автоматически.";
    error.hidden = false;
    previous = "";
  } finally {
    window.setTimeout(refreshRuns, 3000);
  }
}

async function submitRun(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const label = button.textContent;
  const error = byId("form-error");
  error.hidden = true;
  button.disabled = true;
  button.textContent = "Отправляем документы…";
  try {
    const response = await fetch(form.action, { method: "POST", body: new FormData(form) });
    const result = await response.json();
    if (!response.ok) {
      error.textContent = result.error;
      error.hidden = false;
      return;
    }
    location.assign("/");
  } catch {
    error.textContent = "Не удалось связаться с дашбордом. Проверьте, что он запущен.";
    error.hidden = false;
  } finally {
    if (!error.hidden) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

function showSettings(form, data) {
  for (const input of form.querySelectorAll("input")) {
    input.value = data.values[input.name] ?? "";
    input.disabled = data.overridden.includes(input.name);
  }
  byId("api-key").placeholder = data.api_key_set ? "Ключ задан" : "Введите API-ключ";
  byId("settings-overrides").hidden = data.overridden.length === 0;
}

async function loadSettings() {
  const form = byId("settings-form");
  try {
    const response = await fetch(form.action);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    showSettings(form, data);
    byId("settings-fields").disabled = false;
    form.querySelector("button").disabled = false;
  } catch (failure) {
    const error = byId("settings-error");
    error.textContent = failure.message || "Не удалось загрузить настройки.";
    error.hidden = false;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = JSON.stringify(Object.fromEntries(new FormData(form)));
  const fields = byId("settings-fields");
  const button = form.querySelector("button");
  const error = byId("settings-error");
  const success = byId("settings-success");
  error.hidden = true;
  success.hidden = true;
  fields.disabled = true;
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    showSettings(form, data);
    success.hidden = false;
  } catch (failure) {
    error.textContent = failure.message || "Не удалось сохранить настройки.";
    error.hidden = false;
  } finally {
    fields.disabled = false;
    button.disabled = false;
  }
}
