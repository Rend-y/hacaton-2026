"use strict";

if (page === "tests" || page === "test-editor") {
  const form = byId("test-form");
  const saveButton = byId("save-test");
  const cases = byId("test-cases");
  const addButton = byId("add-test-case");
  const documents = byId("test-documents");
  const formError = byId("test-form-error");
  let editing = null;

  function numberCases() {
    const count = cases.children.length;
    Array.from(cases.children).forEach((row, index) => {
      row.querySelector("legend").textContent = `Вопрос ${index + 1}`;
      row.querySelector(".remove-test-case").hidden = count === 1;
    });
    addButton.disabled = count >= 200;
  }

  function addCase(values = {}, focus = false) {
    const row = byId("test-case-template").content.firstElementChild.cloneNode(true);
    row.querySelectorAll("[data-field]").forEach((input) => { input.value = values[input.dataset.field] ?? input.value; });
    const status = row.querySelector('[data-field="status"]');
    const expected = row.querySelector('[data-field="expected"]');
    function updateRequired() {
      expected.required = status.value === "ответ" || status.value === "расхождение";
    }
    status.addEventListener("change", updateRequired);
    updateRequired();
    row.querySelector(".remove-test-case").addEventListener("click", () => {
      row.remove();
      numberCases();
      addButton.focus();
    });
    cases.append(row);
    numberCases();
    if (focus) row.querySelector("input").focus();
  }

  async function runTest(url, payload, button) {
    const error = byId("test-list-error");
    error.hidden = true;
    button.disabled = true;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      location.assign("/");
    } catch (failure) {
      error.textContent = failure.message || "Не удалось запустить тест.";
      error.hidden = false;
      button.disabled = false;
    }
  }

  function runButtons(url, name, payload) {
    return [["baseline", "Чистый запрос"], ["verified", "Улучшенный запрос"]].map(([mode, label]) => {
      const button = node("button", "", "button test-run");
      button.append(node("span", "Запустить", "test-run-verb"), node("span", label));
      button.type = "button";
      button.dataset.mode = mode;
      button.setAttribute("aria-label", `Запустить «${label}» для набора «${name}»`);
      button.addEventListener("click", () => runTest(url, { ...payload, mode }, button));
      return button;
    });
  }

  function fillForm(test = null) {
    editing = test;
    (test?.cases ?? [{}]).forEach((values) => addCase(values));
    byId("test-name").value = test?.name ?? "";
    documents.required = !test;
    const current = byId("test-current-documents");
    current.hidden = !test;
    current.textContent = test
      ? `Текущие файлы: ${test.documents.join(", ")}. Чтобы заменить их, выберите полный новый комплект.`
      : "";
    form.hidden = false;
  }

  async function loadEditor() {
    const title = byId("test-editor-title");
    if (testEditRoute) title.textContent = "Редактирование тестового набора";
    document.title = `${title.textContent} · Ответы по документам`;
    if (!testEditRoute) {
      fillForm();
      return;
    }
    const status = byId("test-editor-status");
    status.hidden = false;
    try {
      const params = new URLSearchParams({ name: decodeURIComponent(testEditRoute[1]) });
      const response = await fetch(`/api/tests/details?${params}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      fillForm(data);
    } catch (failure) {
      const error = byId("test-editor-error");
      error.textContent = failure instanceof URIError ? "Некорректный адрес тестового набора."
        : failure.message || "Не удалось открыть набор для редактирования.";
      error.hidden = false;
    } finally {
      status.hidden = true;
    }
  }

  async function loadTests() {
    const list = byId("test-list");
    const empty = byId("test-list-empty");
    const error = byId("test-list-error");
    try {
      const response = await fetch("/api/tests");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      const rows = data.map((test) => {
        const item = document.createElement("li");
        const details = document.createElement("div");
        details.append(node("strong", test.name), node("span", `Файлов: ${test.documents} · Вопросов: ${test.questions}`, "run-meta"));
        const edit = node("a", "Редактировать", "button secondary");
        edit.href = `/tests/${encodeURIComponent(test.name)}/edit`;
        edit.setAttribute("aria-label", `Редактировать набор «${test.name}»`);
        const actions = document.createElement("div");
        actions.className = "test-actions";
        actions.append(...runButtons("/api/tests/run", test.name, { name: test.name }), edit);
        item.append(details, actions);
        return item;
      });
      list.replaceChildren(...rows);
      list.hidden = rows.length === 0;
      empty.hidden = rows.length > 0;
      empty.textContent = "Сохранённых наборов пока нет.";
      error.hidden = true;
    } catch (failure) {
      error.textContent = failure.message || "Не удалось загрузить тесты.";
      error.hidden = false;
      empty.hidden = true;
    }
  }

  async function saveTest(event) {
    event.preventDefault();
    const body = new FormData(form);
    if (editing) {
      body.set("original", editing.name);
      body.set("revision", editing.revision);
      if (!documents.files.length) body.delete("documents");
    }
    const rows = Array.from(cases.children, (row) => Object.fromEntries(
      Array.from(row.querySelectorAll("[data-field]"), (input) => [input.dataset.field, input.value]),
    ));
    body.set("cases", JSON.stringify(rows));
    formError.hidden = true;
    saveButton.disabled = true;
    try {
      const response = await fetch(editing ? "/api/tests/edit" : form.action, { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      location.assign("/tests?saved=1");
    } catch (failure) {
      formError.textContent = failure.message || "Не удалось сохранить тест.";
      formError.hidden = false;
    } finally {
      saveButton.disabled = false;
    }
  }

  if (page === "tests") {
    document.title = "Тестовые наборы · Ответы по документам";
    if (new URLSearchParams(location.search).get("saved") === "1") {
      byId("test-list-success").hidden = false;
      history.replaceState(null, "", "/tests");
    }
    byId("p01-actions").append(...runButtons("/api/tests/p01", "Контрольный тест P01", {}));
    loadTests();
  } else {
    addButton.addEventListener("click", () => addCase({}, true));
    form.addEventListener("submit", saveTest);
    loadEditor();
  }
}
