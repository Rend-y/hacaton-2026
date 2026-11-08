"use strict";

if (page === "agent") {
  const root = byId("agent-page");
  const log = byId("agent-log");
  const form = byId("agent-form");
  const drop = byId("agent-drop");
  const fileList = byId("agent-files");
  const prompt = byId("agent-prompt");
  const attach = byId("agent-attach");
  const send = byId("agent-send");
  const picker = byId("agent-file-input");
  const error = byId("agent-error");
  let attachments = [];
  let busy = false;

  function showError(message) {
    error.hidden = !message;
    error.textContent = message || "";
  }

  function setBusy(value) {
    busy = value;
    prompt.disabled = value;
    attach.disabled = value;
    send.disabled = value;
    picker.disabled = value;
    form.setAttribute("aria-busy", String(value));
    drop.classList.toggle("is-locked", value);
    for (const button of fileList.querySelectorAll("button")) button.disabled = value;
  }

  function renderFiles() {
    fileList.replaceChildren();
    attachments.forEach((file, index) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = file.name;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", `Убрать ${file.name}`);
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("fill", "none");
      icon.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M18 6 6 18M6 6l12 12");
      icon.append(path);
      remove.append(icon);
      remove.disabled = busy;
      remove.addEventListener("click", () => {
        attachments.splice(index, 1);
        renderFiles();
      });
      item.append(name, remove);
      fileList.append(item);
    });
    fileList.hidden = attachments.length === 0;
  }

  function addFiles(list) {
    const rejected = [];
    for (const file of list) {
      if (file.name.toLowerCase().endsWith(".txt")) attachments.push(file);
      else rejected.push(file.name);
    }
    renderFiles();
    showError(rejected.length ? "Можно прикрепить только txt-файлы." : "");
  }

  function message(className) {
    const item = document.createElement("article");
    item.className = `agent-message ${className}`;
    log.append(item);
    root.classList.remove("is-empty");
    log.scrollTop = log.scrollHeight;
    return item;
  }

  function userMessage(text, names) {
    const item = message("agent-user");
    const body = document.createElement("p");
    body.textContent = text;
    item.append(body);
    if (names.length) {
      const list = document.createElement("ul");
      list.className = "agent-attached";
      for (const name of names) {
        const entry = document.createElement("li");
        entry.textContent = name;
        list.append(entry);
      }
      item.append(list);
    }
  }

  const doneTitles = {
    "Запрашиваем данные": "Данные получены",
    "Проверка ответа": "Ответ проверен",
    "Уточняем повторно": "Уточнение получено",
  };

  function stageBase(title) {
    return String(title || "").replace(/\s*\.{1,3}\s*$/, "").trim();
  }

  function visibleStages(data) {
    const stages = data.stages?.length ? data.stages.map((stage) => ({ ...stage })) : [];
    if (data.status !== "running") return stages;
    if (!stages.length) return [{ title: "Запрашиваем данные...", detail: "" }];
    const last = stages[stages.length - 1];
    const base = stageBase(last.title);
    if (!last.detail) return stages;
    if (base === "Запрашиваем данные" || base === "Уточняем повторно") {
      stages.push({ title: "Проверка ответа...", detail: "" });
    }
    return stages;
  }

  function stageTitle(stage, pending) {
    const base = stageBase(stage.title);
    if (pending || !stage.detail) return base;
    return doneTitles[base] || base;
  }

  function pendingDots() {
    const dots = document.createElement("span");
    dots.className = "agent-dots";
    dots.setAttribute("aria-hidden", "true");
    const delay = `-${Date.now() % 900}ms`;
    for (let count = 0; count < 3; count += 1) {
      const dot = document.createElement("span");
      dot.textContent = ".";
      dot.style.animationDelay = delay;
      dots.append(dot);
    }
    return dots;
  }

  function renderAgent(turn, data) {
    const activity = turn.querySelector(".agent-activity");
    const previousSteps = [...activity.querySelectorAll(".agent-step")];
    const openStages = new Set(previousSteps.filter((node) => node.open).map((node) => node.dataset.stage));
    const seenStages = new Set(previousSteps.map((node) => node.dataset.stage));
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    activity.replaceChildren();
    const stages = visibleStages(data);
    stages.forEach((stage, index) => {
      const key = `${index}:${stageBase(stage.title)}`;
      const pending = data.status === "running" && !stage.detail && index === stages.length - 1;
      const title = stageTitle(stage, pending);
      const entering = seenStages.has(key) ? "" : " agent-enter";
      if (!stage.detail) {
        const row = document.createElement("p");
        row.className = (pending ? "agent-step is-pending" : "agent-step") + entering;
        row.dataset.stage = key;
        row.append(title);
        if (pending) row.append(pendingDots());
        activity.append(row);
        return;
      }
      const details = document.createElement("details");
      details.className = `agent-step${entering}`;
      details.dataset.stage = key;
      details.open = openStages.has(key);
      const summary = document.createElement("summary");
      summary.textContent = title;
      const detail = document.createElement("pre");
      detail.textContent = stage.detail;
      details.append(summary, detail);
      activity.append(details);
    });
    let answer = turn.querySelector(".agent-message");
    if (data.answer || data.error) {
      if (!answer) {
        answer = document.createElement("article");
        answer.className = "agent-message agent-agent";
        turn.append(answer);
      }
      answer.replaceChildren();
      if (data.answer) {
        const body = document.createElement("p");
        body.className = "agent-answer";
        body.textContent = data.answer;
        answer.append(body);
      }
      if (data.error) {
        const failure = document.createElement("p");
        failure.className = "notice";
        failure.textContent = data.error;
        answer.append(failure);
      }
    }
    if (nearBottom) log.scrollTop = log.scrollHeight;
  }

  async function follow(id, item) {
    try {
      const response = await fetch(`/api/agent/${id}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось получить ответ агента.");
      renderAgent(item, data);
      if (data.status === "running") {
        setTimeout(() => follow(id, item), 1000);
        return;
      }
    } catch (reason) {
      renderAgent(item, { status: "failed", stages: [], error: reason.message || "Не удалось получить ответ агента." });
    }
    setBusy(false);
    prompt.focus();
  }

  attach.addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    addFiles(picker.files);
    picker.value = "";
  });
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (event) => {
      event.preventDefault();
      if (!busy) drop.classList.add("is-dragover");
    });
  }
  drop.addEventListener("dragleave", () => drop.classList.remove("is-dragover"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("is-dragover");
    if (!busy) addFiles(event.dataTransfer.files);
  });
  root.addEventListener("dragover", (event) => event.preventDefault());
  root.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("is-dragover");
    if (!busy && event.target === root) addFiles(event.dataTransfer.files);
  });
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const text = prompt.value.trim();
    if (!text) {
      showError("Напишите вопрос.");
      return;
    }
    if (!attachments.length) {
      showError("Прикрепите хотя бы один txt-файл.");
      return;
    }
    const files = attachments.slice();
    const names = files.map((file) => file.name);
    prompt.value = "";
    attachments = [];
    renderFiles();
    showError("");
    userMessage(text, names);
    const pending = document.createElement("div");
    pending.className = "agent-turn";
    const activity = document.createElement("div");
    activity.className = "agent-activity";
    pending.append(activity);
    log.append(pending);
    renderAgent(pending, { status: "running", stages: [] });
    setBusy(true);
    const body = new FormData();
    body.append("prompt", text);
    for (const file of files) body.append("documents", file, file.name);
    try {
      const response = await fetch("/api/agent", { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось отправить запрос.");
      follow(data.id, pending);
    } catch (reason) {
      renderAgent(pending, { status: "failed", stages: [], error: reason.message || "Не удалось отправить запрос." });
      setBusy(false);
      prompt.focus();
    }
  });
}
