(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const workspace = byId("workspace");
  const lastRunKey = "requirements-validator.last-run";
  const runScreenKey = "requirements-validator.run-screen";
  const uploadFormats = [".pdf", ".docx", ".md", ".txt"];
  const jsonLimit = 30 * 1024 * 1024;
  const numbers = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
  const recordedSeconds = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const integers = new Intl.NumberFormat("ru-RU");
  const stageLabels = {
    extract: "Извлечение текста",
    segment: "Нарезка документа",
    local: "Проверка локальных пакетов",
    set: "Проверка документа целиком",
    set_model: "Запрос общей проверки",
    evidence: "Сверка цитат",
    reviewers: "Дополнительные ревьюеры",
    external_docs: "Проверка внешних источников",
    merge: "Первый проход слияния",
    merge_consolidated: "Второй проход слияния",
    clarify: "Уточнение оценок",
    write: "Запись отчёта",
  };
  const statusLabels = {
    queued: "В очереди",
    running: "Проверка идёт",
    completed: "Проверка завершена",
    failed: "Проверка завершилась с ошибкой",
    cancelled: "Проверка остановлена",
  };
  const terminalStatuses = new Set(["completed", "failed", "cancelled"]);

  let config = null;
  let configError = "";
  let landingSync = () => {};
  let cleanup = () => {};
  let view = null;
  let demoReport = null;
  let lastRun = readStorage(lastRunKey);

  function readStorage(key) {
    try {
      const value = sessionStorage.getItem(key);
      return value ? JSON.parse(value) : null;
    } catch {
      return null;
    }
  }

  function writeStorage(key, value) {
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Navigation and live progress also work with browser storage disabled.
    }
  }

  function rememberRun(id, filename = "") {
    lastRun = id ? { id, filename } : null;
    writeStorage(lastRunKey, lastRun);
  }

  function showMessage(element, message) {
    if (element.textContent !== message) element.textContent = message;
    element.hidden = !message;
  }

  async function jsonRequest(path, options) {
    let response;
    try {
      response = await fetch(path, { cache: "no-store", ...options });
    } catch {
      throw new Error("Нет связи с локальным сервером. Проверьте соединение и попробуйте снова.");
    }
    const body = await response.json().catch(() => null);
    if (!response.ok || !body) {
      const problem = new Error(typeof body?.error === "string" ? body.error : "Сервер не смог обработать запрос.");
      problem.status = response.status;
      throw problem;
    }
    return body;
  }

  function upload(path, file, extra = {}) {
    const params = new URLSearchParams({ filename: file.name, ...extra });
    return jsonRequest(`${path}?${params}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
  }

  function changeView(name, templateId, properties = {}) {
    cleanup();
    cleanup = () => {};
    landingSync = () => {};
    view = { name, ...properties };
    showMessage(byId("app-alert"), "");
    byId("return-home").hidden = name === "landing";
    byId("return-to-run").hidden = !(name === "report" && view.runId);
    byId("add-demo-source").hidden = !(name === "report" && view.demo && !view.hasSource);
    byId("add-demo-source").disabled = false;
    workspace.classList.toggle("workspace-report", name === "report");
    workspace.replaceChildren();
    if (templateId) workspace.append(byId(templateId).content.cloneNode(true));
    writeStorage(runScreenKey, view.runId || null);
    return view;
  }

  function focusHeading() {
    const heading = workspace.querySelector("h1");
    if (heading) {
      if (!heading.hasAttribute("tabindex")) heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    } else {
      workspace.focus({ preventScroll: true });
    }
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function number(value) {
    return Number.isFinite(value) ? numbers.format(value) : "—";
  }

  function timeLabel(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 60) return `${number(seconds)} с`;
    const wholeSeconds = Math.floor(seconds);
    const minutes = Math.floor(wholeSeconds / 60);
    const remaining = wholeSeconds % 60;
    return `${minutes} мин ${String(remaining).padStart(2, "0")} с`;
  }

  function parseExtension(name) {
    return name.toLowerCase().match(/\.[^.]+$/)?.[0] || "";
  }

  function validateFile(file, formats) {
    if (!file) return "Выберите документ.";
    if (!formats.includes(parseExtension(file.name))) {
      const extensions = formats.map((extension) => extension.slice(1).toUpperCase()).join(", ");
      return `Поддерживаемые форматы: ${extensions}.`;
    }
    if (file.size === 0) return "Выбранный файл пуст.";
    if (Number.isFinite(config?.maxUploadBytes) && file.size > config.maxUploadBytes) {
      return `Файл превышает ${number(config.maxUploadBytes / 1048576)} МиБ.`;
    }
    return "";
  }

  async function loadConfig() {
    try {
      config = await jsonRequest("/api/config");
      configError = "";
    } catch {
      configError = "Настройки сервера недоступны. Проверьте подключение и обновите страницу.";
    }
    landingSync();
  }

  function showLanding(focus = true) {
    const currentView = changeView("landing", "landing-template");
    const form = byId("analysis-form");
    const fileInput = byId("document-input");
    const choose = byId("choose-file");
    const clear = byId("clear-file");
    const dropZone = byId("drop-zone");
    const start = byId("start-run");
    const consent = byId("consent");
    const concurrency = byId("concurrency");
    const uploadError = byId("upload-error");
    let file = null;
    let sending = false;
    let initializedProfile = false;

    const formats = () => Array.isArray(config?.formats) ? config.formats : uploadFormats;
    const refreshForm = () => {
      start.disabled = sending || !config?.configured || !file || !consent.checked || Boolean(validateFile(file, formats()));
      start.textContent = sending ? "Загружаем документ…" : "Запустить анализ";
      choose.disabled = sending;
      clear.disabled = sending;
      consent.disabled = sending;
      concurrency.disabled = sending;
      byId("start-hint").textContent = configError || (config && !config.configured
        ? config.configurationError || "Настройте ключ в окружении сервера, чтобы запустить проверку."
        : "");
    };

    const setFile = (candidate) => {
      const issue = candidate ? validateFile(candidate, formats()) : "";
      file = issue ? null : candidate;
      byId("file-name").textContent = file?.name || "Перетащите документ сюда";
      clear.hidden = !file;
      showMessage(uploadError, issue);
      refreshForm();
    };

    landingSync = () => {
      const status = byId("configuration-status");
      status.dataset.state = config?.configured ? "ready" : "waiting";
      status.textContent = config
        ? (config.configured ? "Ключ настроен" : "Ключ не настроен")
        : (configError ? "Нет связи с сервером" : "Проверяем настройки…");
      const profile = config?.profile;
      byId("provider-profile").textContent = typeof profile === "string"
        ? profile : [profile?.provider, profile?.model].filter(Boolean).join(" · ") || "—";
      if (!initializedProfile && Number.isInteger(profile?.concurrency) && profile.concurrency >= 1 && profile.concurrency <= 8) {
        concurrency.value = String(profile.concurrency);
        initializedProfile = true;
      }
      const extensions = formats().map((extension) => extension.slice(1).toUpperCase()).join(", ");
      const size = Number.isFinite(config?.maxUploadBytes) ? ` · до ${number(config.maxUploadBytes / 1048576)} МиБ` : "";
      byId("file-formats").textContent = extensions + size;
      fileInput.accept = formats().join(",");
      byId("play-demo").disabled = !config?.demoReplay;
      byId("replay-hint").textContent = config?.demoReplay ? ""
        : config || configError ? "Запись журнала сейчас недоступна." : "Проверяем наличие записи прогона…";
      byId("replay-hint").hidden = !byId("replay-hint").textContent;
      if (file && validateFile(file, formats())) setFile(file);
      refreshForm();
    };
    landingSync();

    if (lastRun?.id) {
      byId("resume-card").hidden = false;
      byId("resume-name").textContent = lastRun.filename || "Документ из этой вкладки";
      byId("resume-run").addEventListener("click", () => showRun(lastRun.id, null, false));
    }
    choose.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      if (fileInput.files[0]) setFile(fileInput.files[0]);
      fileInput.value = "";
    });
    clear.addEventListener("click", () => {
      setFile(null);
      choose.focus();
    });
    dropZone.addEventListener("dragover", (event) => {
      if (!event.dataTransfer.types.includes("Files")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = sending ? "none" : "copy";
      if (!sending) dropZone.dataset.dragging = "true";
    });
    dropZone.addEventListener("dragleave", (event) => {
      if (!dropZone.contains(event.relatedTarget)) delete dropZone.dataset.dragging;
    });
    dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      delete dropZone.dataset.dragging;
      if (sending) return;
      if (event.dataTransfer.files.length > 1) {
        showMessage(uploadError, "Для одного анализа выберите один документ.");
        return;
      }
      if (event.dataTransfer.files[0]) setFile(event.dataTransfer.files[0]);
    });
    consent.addEventListener("change", refreshForm);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (start.disabled) return;
      const chosenFile = file;
      const selectedConcurrency = concurrency.value;
      sending = true;
      showMessage(uploadError, "");
      refreshForm();
      try {
        const state = await upload("/api/runs", chosenFile, { concurrency: selectedConcurrency, consent: "yes" });
        rememberRun(state.id, state.filename || chosenFile.name);
        if (view === currentView) showRun(state.id, state, true);
      } catch (error) {
        if (view === currentView) showMessage(uploadError, error.message);
      } finally {
        sending = false;
        if (view === currentView) refreshForm();
      }
    });
    byId("open-demo").addEventListener("click", (event) => openDemo(currentView, event.currentTarget, byId("demo-error")));
    byId("play-demo").addEventListener("click", (event) => openReplay(currentView, event.currentTarget, byId("demo-error")));

    const jsonInput = byId("json-input");
    const importButton = byId("import-json");
    const importError = byId("import-error");
    importButton.addEventListener("click", () => jsonInput.click());
    jsonInput.addEventListener("change", async () => {
      const jsonFile = jsonInput.files[0];
      jsonInput.value = "";
      if (!jsonFile) return;
      if (jsonFile.size > jsonLimit) {
        showMessage(importError, "Размер отчёта превышает 30 МиБ.");
        return;
      }
      showMessage(importError, "");
      importButton.disabled = true;
      try {
        const raw = await jsonFile.text();
        const report = JSON.parse(raw.replace(/^\uFEFF/, ""));
        if (!report || !Array.isArray(report.findings) || !report.findings.every((finding) => finding && typeof finding === "object" && !Array.isArray(finding))) {
          throw new Error("Отсутствует список замечаний");
        }
        if (view === currentView) showReport({ report, source: null, partial: report.coverage_complete === false }, { title: documentTitle(report, jsonFile.name) });
      } catch {
        if (view === currentView) showMessage(importError, "Файл не удалось прочитать как JSON-отчёт Requirements Validator.");
      } finally {
        if (view === currentView) importButton.disabled = false;
      }
    });
    if (focus) focusHeading();
  }

  function documentTitle(report, fallback) {
    const documentInfo = report?.document;
    const title = documentInfo?.name || documentInfo?.filename || documentInfo?.source_name;
    return typeof title === "string" && title.trim() ? title : fallback;
  }

  function showReport(data, { title, demo = false, runId = null } = {}) {
    const reportTitle = title || documentTitle(data.report, "Отчёт проверки");
    const rootPath = demo ? "/api/demo" : runId ? `/api/runs/${encodeURIComponent(runId)}` : null;
    const downloadUrls = rootPath ? { md: `${rootPath}/download/md`, json: `${rootPath}/download/json` } : undefined;
    changeView("report", null, { demo, runId, hasSource: Boolean(data.source) });
    const host = document.createElement("div");
    host.id = "report-view";
    workspace.append(host);
    try {
      window.RequirementsReport.mount(host, { report: data.report, source: data.source, partial: data.partial, title: reportTitle, downloadUrls });
    } catch {
      showMessage(byId("app-alert"), "Отчёт получен, но его не удалось отобразить. Вернитесь к запуску и попробуйте снова.");
    }
    focusHeading();
  }

  async function openDemo(currentView, button, errorElement) {
    button.disabled = true;
    showMessage(errorElement, "");
    try {
      demoReport = demoReport || await jsonRequest("/api/demo/report");
      if (view === currentView) showReport(demoReport, { title: "Банковское ТЗ", demo: true });
    } catch (error) {
      if (view === currentView) showMessage(errorElement, error.message);
    } finally {
      if (view === currentView) button.disabled = false;
    }
  }

  byId("add-demo-source").addEventListener("click", () => byId("demo-source-input").click());
  byId("demo-source-input").addEventListener("change", async (event) => {
    const currentView = view;
    const file = event.currentTarget.files[0];
    event.currentTarget.value = "";
    if (!file || !currentView?.demo) return;
    const alert = byId("app-alert");
    const issue = validateFile(file, [".pdf"]);
    showMessage(alert, issue);
    if (issue) return;
    byId("add-demo-source").disabled = true;
    try {
      const source = await upload("/api/demo/source", file);
      if (view !== currentView) return;
      demoReport = source;
      showReport(demoReport, { title: "Банковское ТЗ", demo: true });
    } catch (error) {
      if (view === currentView) showMessage(alert, error.message);
    } finally {
      if (view === currentView) byId("add-demo-source").disabled = false;
    }
  });

  function renderStages(snapshot) {
    const stageList = byId("stage-list");
    stageList.replaceChildren();
    const markers = {
      running: ["●", "Выполняется"],
      completed: ["✓", "Завершён"],
      failed: ["!", "Прерван"],
    };
    const active = Array.isArray(snapshot.activeStages) ? snapshot.activeStages : [];
    const history = new Map(Object.entries(snapshot.stageHistory || {}));
    for (const key of active) history.set(key, "running");
    if (snapshot.backgroundSet && markers[snapshot.backgroundSet]) {
      // The independent provider request can be active while the foreground is checking local packages.
      const before = history.has("local") ? "local" : "set";
      const entries = Array.from(history);
      const index = entries.findIndex(([key]) => key === before);
      entries.splice(index === -1 ? entries.length : index, 0, ["set_model", snapshot.backgroundSet]);
      history.clear();
      for (const [key, state] of entries) history.set(key, state);
    }
    for (const [key, state] of history) {
      if (!stageLabels[key] || !markers[state]) continue;
      const label = key === "set_model" ? "Фоновый запрос общей проверки"
        : key === "set" && snapshot.backgroundSet ? "Ожидание результата общей проверки" : stageLabels[key];
      const item = document.createElement("li");
      item.dataset.state = state;
      const marker = document.createElement("span");
      marker.className = "stage-marker";
      marker.textContent = markers[state][0];
      marker.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.textContent = label;
      const status = document.createElement("span");
      status.className = "stage-status";
      status.textContent = markers[state][1];
      item.append(marker, text, status);
      stageList.append(item);
    }
    const stage = Array.isArray(snapshot.activeStages) ? active[active.length - 1] : snapshot.stage;
    const stageLabel = stage === "set" && snapshot.backgroundSet ? "Ожидание результата общей проверки"
      : stageLabels[stage] || (stage && snapshot.stageLabel);
    const current = snapshot.status === "running" ? (stageLabel || (history.size ? "Ожидаем следующий этап" : "Ожидаем первый этап"))
      : snapshot.status === "queued" ? "Ожидаем запуск"
      : snapshot.reportAvailable ? "Отчёт доступен" : "Проверка остановлена";
    if (byId("current-stage").textContent !== current) byId("current-stage").textContent = current;
  }

  function appendEvent(list, event) {
    const item = document.createElement("li");
    const timestamp = document.createElement("span");
    timestamp.className = "event-time";
    timestamp.textContent = Number.isFinite(event.elapsed) ? `${recordedSeconds.format(event.elapsed)} с` : "—";
    const message = document.createElement("span");
    message.textContent = event.text;
    item.append(timestamp, message);
    list.append(item);
  }

  function renderMeasures(snapshot) {
    byId("run-elapsed").textContent = timeLabel(snapshot.elapsed);
    const packagesKnown = Number.isInteger(snapshot.processed) && Number.isInteger(snapshot.total) && snapshot.total > 0;
    const localActive = Array.isArray(snapshot.activeStages) ? snapshot.activeStages.includes("local") : snapshot.stage === "local";
    const estimateKnown = localActive && Number.isFinite(snapshot.remaining) && snapshot.remaining >= 0
      && snapshot.status === "running" && (!packagesKnown || snapshot.processed < snapshot.total);
    byId("run-packages").textContent = packagesKnown
      ? `${integers.format(snapshot.processed)} из ${integers.format(snapshot.total)}` : "—";
    byId("run-estimate-row").hidden = !estimateKnown;
    if (estimateKnown) byId("run-estimate").textContent = `≈ ${number(snapshot.remaining)} мин`;
    byId("run-progress-track").hidden = !packagesKnown;
    if (packagesKnown) {
      byId("run-progress").max = snapshot.total;
      byId("run-progress").value = snapshot.processed;
    }

    const timings = snapshot.timings;
    byId("final-timings").hidden = !timings;
    if (!timings) return;
    const lines = byId("timings-list");
    lines.replaceChildren();
    const values = [
      ["Общая длительность", timings.total_seconds, timeLabel],
      ["Вызовы модели", timings.model_calls, number],
      ["Попытки HTTP", timings.sdk_requests, number],
      ["Ожидание ответов, сумма по вызовам", timings.model_wait_seconds, timeLabel],
      ["Ожидание ограничителя", timings.limiter_wait_seconds, timeLabel],
    ];
    for (const [name, seconds] of Object.entries(timings.stages_seconds || {})) {
      if (stageLabels[name]) values.push([stageLabels[name], seconds, timeLabel]);
    }
    for (const [label, value, format] of values) {
      if (!Number.isFinite(value)) continue;
      const term = document.createElement("dt");
      const measurement = document.createElement("dd");
      term.textContent = label;
      measurement.textContent = format(value);
      lines.append(term, measurement);
    }
  }

  function showRun(id, initialSnapshot = null, automaticallyOpenReport = false) {
    const currentView = changeView("run", "run-template", { runId: id });
    const runPath = `/api/runs/${encodeURIComponent(id)}`;
    const reportButton = byId("open-run-report");
    const stop = byId("cancel-run");
    const notice = byId("run-notice");
    const errorElement = byId("run-error");
    let events;
    let retry;
    let snapshot;
    let reconnecting = false;
    let pollingOnly = false;
    let polling = false;
    let unavailable = false;
    let reportPending = false;
    let cancellationPending = false;
    let actionError = "";
    let previousLog = "";

    byId("progress-title").textContent = initialSnapshot?.filename || lastRun?.filename || "Анализ документа";
    cleanup = () => {
      events?.close();
      clearTimeout(retry);
    };

    const stopStream = () => {
      events?.close();
      clearTimeout(retry);
      retry = undefined;
    };

    const renderNotice = () => {
      if (reconnecting) {
        showMessage(notice, "Соединение прервано. Восстанавливаем состояние проверки…");
      } else if (snapshot?.partial && snapshot.reportAvailable) {
        showMessage(notice, "Доступен частичный отчёт. Покрытие проверки неполное.");
      } else {
        const retentionMinutes = Number.isFinite(config?.availabilitySeconds) ? Math.round(config.availabilitySeconds / 60) : 0;
        const downloadHint = snapshot?.reportAvailable && retentionMinutes > 0
          ? `Отчёт доступен на сервере ${integers.format(retentionMinutes)} мин после завершения. Скачайте его из просмотра.`
          : "";
        showMessage(notice, downloadHint || (pollingOnly && !terminalStatuses.has(snapshot?.status)
          ? "Периодически запрашиваем состояние проверки." : ""));
      }
    };

    const loadReport = async () => {
      if (!snapshot?.reportAvailable || reportPending) return;
      reportPending = true;
      reportButton.disabled = true;
      actionError = "";
      try {
        const payload = await jsonRequest(`${runPath}/report`);
        if (view === currentView) showReport(payload, { title: snapshot.filename, runId: id });
      } catch (error) {
        if (view === currentView) {
          actionError = error.message;
          showMessage(errorElement, actionError);
        }
      } finally {
        reportPending = false;
        if (view === currentView) reportButton.disabled = false;
      }
    };

    const render = (nextState) => {
      if (view !== currentView || unavailable || nextState?.id !== id || !statusLabels[nextState.status]) return;
      const terminal = terminalStatuses.has(nextState.status);
      if (snapshot && !terminal && (terminalStatuses.has(snapshot.status)
          || snapshot.status === "running" && nextState.status === "queued"
          || snapshot.status === nextState.status && Number.isFinite(snapshot.elapsed)
            && Number.isFinite(nextState.elapsed) && nextState.elapsed < snapshot.elapsed)) return;
      snapshot = nextState;
      rememberRun(id, snapshot.filename || lastRun?.filename);
      const status = byId("run-state");
      if (snapshot.filename) byId("progress-title").textContent = snapshot.filename;
      status.dataset.state = snapshot.status;
      status.textContent = snapshot.cancelling && !terminal ? "Останавливаем проверку" : statusLabels[snapshot.status];
      renderNotice();
      showMessage(errorElement, actionError || snapshot.error || "");
      reportButton.hidden = !snapshot.reportAvailable;
      reportButton.textContent = snapshot.partial ? "Открыть частичный отчёт" : "Открыть отчёт";
      const stopHadFocus = document.activeElement === stop;
      stop.hidden = terminal;
      stop.disabled = cancellationPending || Boolean(snapshot.cancelling);
      stop.textContent = stop.disabled ? "Останавливаем…" : "Остановить";
      if (stopHadFocus && terminal) (snapshot.reportAvailable ? reportButton : byId("progress-title")).focus();
      renderStages(snapshot);
      renderMeasures(snapshot);
      const log = Array.isArray(snapshot.log) ? snapshot.log : [];
      const newLog = JSON.stringify(log);
      if (previousLog !== newLog) {
        const list = byId("run-log");
        const nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 15;
        list.replaceChildren();
        for (const event of log) appendEvent(list, event);
        if (nearEnd) list.scrollTop = list.scrollHeight;
        list.hidden = log.length === 0;
        byId("run-log-empty").hidden = log.length > 0;
        previousLog = newLog;
      }
      if (terminal) {
        stopStream();
        if (automaticallyOpenReport && snapshot.status === "completed" && snapshot.reportAvailable) {
          automaticallyOpenReport = false;
          loadReport();
        }
      }
    };

    const schedulePoll = (delay) => {
      if (!retry && !polling && !unavailable && !terminalStatuses.has(snapshot?.status)) {
        retry = setTimeout(() => {
          retry = undefined;
          reload();
        }, delay);
      }
    };

    const reload = async () => {
      if (view !== currentView || polling || unavailable || terminalStatuses.has(snapshot?.status)) return;
      polling = true;
      try {
        const data = await jsonRequest(runPath);
        if (view === currentView) {
          if (pollingOnly) reconnecting = false;
          render(data);
        }
      } catch (error) {
        if (view !== currentView) return;
        if (error.status === 404 || error.status === 410) {
          unavailable = true;
          stopStream();
          reconnecting = false;
          byId("run-state").textContent = "Результат больше недоступен";
          byId("current-stage").textContent = "Можно запустить новую проверку";
          stop.hidden = true;
          reportButton.hidden = true;
          showMessage(notice, "");
          showMessage(errorElement, "Проверка не найдена. Возможно, срок хранения истёк или сервер был перезапущен.");
          if (lastRun?.id === id) rememberRun(null);
          writeStorage(runScreenKey, null);
          return;
        }
        reconnecting = true;
        renderNotice();
      } finally {
        polling = false;
      }
      if (view === currentView && (reconnecting || pollingOnly)) {
        schedulePoll(3500);
      }
    };

    const connect = () => {
      if (typeof EventSource !== "function") {
        pollingOnly = true;
        reload();
        return;
      }
      events = new EventSource(`${runPath}/events`);
      events.addEventListener("snapshot", (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          reconnecting = true;
          renderNotice();
          schedulePoll(1000);
          return;
        }
        if (data?.id !== id || unavailable) return;
        if (reconnecting) {
          reconnecting = false;
          clearTimeout(retry);
          retry = undefined;
          renderNotice();
        }
        render(data);
      });
      events.addEventListener("error", () => {
        if (view !== currentView || unavailable || terminalStatuses.has(snapshot?.status)) return;
        reconnecting = true;
        renderNotice();
        schedulePoll(700);
      });
    };

    reportButton.addEventListener("click", loadReport);
    stop.addEventListener("click", async () => {
      if (cancellationPending || !snapshot || terminalStatuses.has(snapshot.status)) return;
      cancellationPending = true;
      stop.disabled = true;
      stop.textContent = "Останавливаем…";
      actionError = "";
      showMessage(errorElement, "");
      try {
        const state = await jsonRequest(`${runPath}/cancel`, { method: "POST" });
        if (view === currentView) render(state);
      } catch (error) {
        if (view === currentView && !terminalStatuses.has(snapshot?.status)) {
          actionError = error.message;
          showMessage(errorElement, actionError);
        }
      } finally {
        cancellationPending = false;
        if (view === currentView) {
          stop.disabled = Boolean(snapshot?.cancelling);
          stop.textContent = stop.disabled ? "Останавливаем…" : "Остановить";
        }
      }
    });

    focusHeading();
    if (initialSnapshot) render(initialSnapshot);
    if (!terminalStatuses.has(snapshot?.status)) {
      connect();
      if (events) reload();
    }
  }

  async function openReplay(currentView, button, errorElement) {
    button.disabled = true;
    showMessage(errorElement, "");
    try {
      const recording = await jsonRequest("/api/demo/replay");
      if (!Array.isArray(recording.events) || !recording.events.length
          || recording.events.some((event, index, events) => !Number.isFinite(event.elapsed)
            || event.elapsed < (events[index - 1]?.elapsed ?? 0) || typeof event.text !== "string")) {
        throw new Error("Запись журнала пуста или имеет неизвестный формат.");
      }
      if (view === currentView) showReplay(recording);
    } catch (error) {
      if (view === currentView) showMessage(errorElement, error.message);
    } finally {
      if (view === currentView) button.disabled = !config?.demoReplay;
    }
  }

  function showReplay(recording) {
    const currentView = changeView("replay", "replay-template");
    const list = byId("replay-log");
    const time = byId("replay-time");
    const notice = byId("replay-notice");
    const toggle = byId("replay-toggle");
    const speed = byId("replay-rate");
    const events = recording.events;
    const duration = events[events.length - 1].elapsed;
    const passportDate = recording.reviewedAt;
    if (typeof passportDate === "string" && passportDate) {
      const label = document.createElement("time");
      label.dateTime = passportDate;
      label.textContent = passportDate;
      byId("replay-start").append("Дата прогона по паспорту ", label);
    } else {
      showMessage(byId("replay-start"), "Дата прогона в паспорте отсутствует.");
    }
    let position = 0;
    let nextEvent = 0;
    let pause = false;
    let rate = Number(speed.value);
    let anchor = performance.now();

    const redraw = () => {
      const nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 15;
      while (nextEvent < events.length && events[nextEvent].elapsed <= position) {
        appendEvent(list, events[nextEvent]);
        nextEvent += 1;
      }
      if (nearEnd) list.scrollTop = list.scrollHeight;
      const ended = nextEvent === events.length;
      time.textContent = `${timeLabel(position)} из ${timeLabel(duration)} по записи`;
      toggle.textContent = ended ? "Воспроизвести снова" : pause ? "Продолжить" : "Пауза";
      const state = ended ? "Запись окончена." : pause ? "Воспроизведение приостановлено." : "Воспроизводится запись журнала.";
      if (notice.textContent !== state) notice.textContent = state;
    };
    const update = () => {
      const now = performance.now();
      if (!pause && position < duration) position = Math.min(duration, position + ((now - anchor) / 1000) * rate);
      anchor = now;
      redraw();
    };
    const timer = setInterval(update, 130);
    cleanup = () => clearInterval(timer);
    speed.addEventListener("change", () => {
      update();
      rate = Number(speed.value);
    });
    toggle.addEventListener("click", () => {
      update();
      if (nextEvent === events.length) {
        position = 0;
        nextEvent = 0;
        pause = false;
        list.replaceChildren();
      } else {
        pause = !pause;
      }
      redraw();
    });
    byId("replay-report").addEventListener("click", (event) => openDemo(currentView, event.currentTarget, byId("app-alert")));
    redraw();
    focusHeading();
  }

  byId("return-home").addEventListener("click", () => showLanding());
  byId("return-to-run").addEventListener("click", () => {
    if (view?.runId) showRun(view.runId, null, false);
  });

  const storedRunScreen = readStorage(runScreenKey);
  loadConfig();
  if (typeof storedRunScreen === "string" && /^[a-zA-Z0-9_-]{1,80}$/.test(storedRunScreen)) {
    showRun(storedRunScreen, null, true);
  } else {
    showLanding(false);
  }
})();
