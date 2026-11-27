(() => {
  "use strict";

  const scriptUrl = document.currentScript?.src;
  const severityLabels = {
    critical: "Критический",
    major: "Существенный",
    minor: "Незначительный",
  };
  const severitySymbols = { critical: "◆", major: "▲", minor: "●" };
  const criterionLabels = {
    contradiction: "Согласованность",
    completeness: "Полнота",
    ambiguity: "Однозначность",
    verifiability: "Проверяемость",
    atomicity: "Единичность",
    duplication: "Дублирование с риском расхождения",
    traceability: "Трассируемость",
  };
  const readinessLabels = {
    ready: "Можно начинать",
    conditional: "Готово с условиями",
    not_ready: "Есть блокирующие замечания",
    not_confirmed: "Готовность не подтверждена",
  };
  const checkLabels = {
    pass: "Без замечаний",
    issue: "Есть замечание",
    uncertain: "Требует уточнения",
    not_applicable: "Неприменимо",
  };
  const relationLabels = {
    functional_acceptance: "Функциональность и приёмка",
    business_api: "Бизнес-правила и API",
    data_api: "Данные и API",
    status_examples: "Статусы и примеры",
  };
  const externalLabels = {
    confirmed: "Подтверждено",
    contradicts: "Источник противоречит утверждению",
    version_mismatch: "Совпадение версии не подтверждено",
    not_found: "Подтверждение не найдено",
    unavailable: "Источник недоступен",
  };
  const stageLabels = {
    extract: "Извлечение текста",
    segment: "Разбиение на блоки",
    local: "Проверка локальных пакетов",
    set: "Проверка документа целиком / ожидание результата",
    set_model: "Фоновый запрос общей проверки",
    evidence: "Сверка цитат",
    reviewers: "Дополнительные ревьюеры",
    external_docs: "Проверка внешних источников",
    merge: "Первый проход слияния",
    merge_consolidated: "Второй проход слияния",
    clarify: "Уточнение оценок",
    write: "Запись отчёта",
  };
  const noSource = "Исходный текст не приложен. Переход к документу недоступен. Точные цитаты доступны в карточках.";
  const hashMismatch = "SHA-256 исходника не совпадает с паспортом отчёта или отсутствует. Переход к документу недоступен.";
  const coordinateMismatch = "Координаты или текст цитаты не совпадают с исходником. Сверьте дословную цитату в карточке.";
  const compactScreen = window.matchMedia("(max-width: 760px)");
  const formatNumber = new Intl.NumberFormat("ru", { maximumFractionDigits: 1 });
  let mountCount = 0;

  function element(tag, className = "", content) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (content !== undefined) result.textContent = content;
    return result;
  }

  function button(label, className, action) {
    const result = element("button", className, label);
    result.type = "button";
    result.addEventListener("click", action);
    return result;
  }

  function items(value) {
    return Array.isArray(value) ? value : [];
  }

  function text(value) {
    return typeof value === "string" ? value : "";
  }

  function label(mapping, key) {
    const safeKey = text(key);
    return Object.hasOwn(mapping, safeKey) ? mapping[safeKey] : safeKey || "Нет данных";
  }

  function positiveInteger(value) {
    return Number.isSafeInteger(value) && value > 0;
  }

  function numeric(value) {
    return typeof value === "number" && Number.isFinite(value) ? formatNumber.format(value) : "";
  }

  function titleFor(data) {
    return text(data.title) || text(data.report?.document?.name) || "Отчёт о проверке";
  }

  function severityBadge(severity, suffix = "") {
    const known = Object.hasOwn(severityLabels, text(severity));
    const badge = element("span", known ? `rv-badge rv-badge-${severity}` : "rv-badge");
    const symbol = element("span", "rv-severity-symbol", known ? severitySymbols[severity] : "○");
    symbol.setAttribute("aria-hidden", "true");
    badge.append(symbol, document.createTextNode(`${label(severityLabels, severity)}${suffix}`));
    return badge;
  }

  function coordinates(anchor) {
    const { start_line: first, end_line: last, start_col: start, end_col: end,
      page_start: pageFirst, page_end: pageLast } = anchor;
    if (![first, last, start, end].every(positiveInteger)) return "Координаты отсутствуют";
    const pages = !positiveInteger(pageFirst) ? "" : pageFirst === pageLast || !positiveInteger(pageLast)
      ? `Стр. ${pageFirst} · `
      : `Стр. ${pageFirst}–${pageLast} · `;
    const lines = first === last
      ? `строка ${first}, символы ${start}–${end}`
      : `строки ${first}–${last}, от символа ${start} до ${end}`;
    return pages + lines;
  }

  function locate(anchor, sourceRows) {
    const { start_line: first, end_line: last, start_col: start, end_col: end } = anchor;
    if (![first, last, start, end].every(positiveInteger) || last < first || last - first >= sourceRows.size) {
      return null;
    }
    const startRow = sourceRows.get(first);
    const endRow = sourceRows.get(last);
    if (!startRow || !endRow ||
        (anchor.page_start ?? null) !== startRow.page || (anchor.page_end ?? null) !== endRow.page) {
      return null;
    }
    const spans = [];
    for (let number = first; number <= last; number += 1) {
      const row = sourceRows.get(number);
      if (!row) return null;
      // Python counts Unicode code points from one and includes the final column.
      const characters = Array.from(row.text);
      const from = number === first ? start - 1 : 0;
      const until = number === last ? end : characters.length;
      if ((number === first && from >= characters.length) || until > characters.length || from > until ||
          (first === last && from === until)) return null;
      spans.push({
        row,
        before: characters.slice(0, from).join(""),
        match: characters.slice(from, until).join(""),
        after: characters.slice(until).join(""),
      });
    }
    if (spans.map(({ match }) => match).join("\n") !== anchor.quote) return null;
    return spans;
  }

  function facts(entries) {
    const result = element("dl", "rv-facts");
    for (const [caption, value] of entries) {
      if (value === "" || value === undefined || value === null) continue;
      result.append(element("dt", "", caption), element("dd", "", value));
    }
    if (!result.childElementCount) result.append(element("dt", "", "Нет данных"));
    return result;
  }

  function detail(title, ...content) {
    const result = element("details", "rv-detail");
    const body = element("div", "rv-detail-body");
    body.append(...content);
    result.append(element("summary", "", title), body);
    return result;
  }

  function textList(values) {
    const result = element("ul", "rv-text-list");
    for (const value of values) result.append(element("li", "", value));
    return result;
  }

  function reportDate(value) {
    if (!text(value)) return "";
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
    if (!match) return value;
    const [, year, month, day, clock, offset] = match;
    return `${day}.${month}.${year}, ${clock} ${offset === "Z" ? "UTC" : `UTC${offset}`}`;
  }

  function timingDetails(timings) {
    const seconds = (value) => numeric(value) ? `${numeric(value)} с` : "";
    const totals = facts([
      ["Всего", seconds(timings.total_seconds)],
      ["Вызовы модели", numeric(timings.model_calls)],
      ["Попытки HTTP", numeric(timings.sdk_requests)],
      ["Ожидание ответов", seconds(timings.model_wait_seconds)],
      ["Ожидание ограничителя", seconds(timings.limiter_wait_seconds)],
      ["Паузы при повторах SDK", seconds(timings.sdk_retry_wait_seconds)],
      ["Входные токены", numeric(timings.usage?.input_tokens)],
      ["Выходные токены", numeric(timings.usage?.output_tokens)],
      ["Всего токенов", numeric(timings.usage?.total_tokens)],
    ]);
    const stages = Object.entries(timings.stages_seconds ?? {});
    const timedStages = stages.length
      ? facts(stages.map(([key, value]) => [label(stageLabels, key), seconds(value)]))
      : element("p", "rv-meta-text", "Времени по отдельным этапам нет.");
    return detail("Замеры прогона", totals, element("h3", "", "Время по этапам"), timedStages);
  }

  function matrix(report) {
    const requirements = items(report.requirements).filter((row) => row?.block);
    const criteria = [...new Set(requirements.flatMap((row) =>
      items(row.checks).map((check) => text(check?.criterion)).filter(Boolean)))];
    const table = element("table", "rv-matrix");
    table.createCaption().textContent = "Проверка отдельных требований";
    const head = table.createTHead().insertRow();
    for (const title of ["Требование", ...criteria.map((key) => label(criterionLabels, key))]) {
      const cell = element("th", "", title);
      cell.scope = "col";
      head.append(cell);
    }
    const body = table.createTBody();
    for (const requirement of requirements) {
      const row = body.insertRow();
      const block = requirement.block;
      const id = element("th", "", text(block.id) || "Нет ID");
      id.scope = "row";
      if (text(block.section)) id.append(element("span", "rv-meta-text", block.section));
      row.append(id);
      for (const key of criteria) {
        const check = items(requirement.checks).find((entry) => entry?.criterion === key);
        row.insertCell().textContent = check ? label(checkLabels, check.status) : "—";
      }
    }
    const wrapper = element("div", "rv-matrix-scroll");
    wrapper.tabIndex = 0;
    wrapper.setAttribute("role", "region");
    wrapper.setAttribute("aria-label", "Таблица проверки отдельных требований");
    wrapper.append(table);
    const result = [];
    if (requirements.length) result.push(wrapper);
    if (items(report.set_checks).length) {
      result.push(element("h3", "", "Проверка набора"), facts(items(report.set_checks).filter(Boolean)
        .map((check) => [label(criterionLabels, check.criterion), label(checkLabels, check.status)])));
    }
    if (items(report.link_checks).length) {
      result.push(element("h3", "", "Связи между разделами"), facts(items(report.link_checks).filter(Boolean)
        .map((check) => [label(relationLabels, check.relation), label(checkLabels, check.status)])));
    }
    return detail("Матрица проверок", ...(result.length ? result : [element("p", "rv-meta-text", "Данные матрицы отсутствуют.")]));
  }

  function reportDetails(report) {
    const meta = report.document ?? {};
    const questions = items(report.clarifications).filter((entry) => Array.isArray(entry) && text(entry[1]));
    const warnings = items(report.warnings).filter((warning) => text(warning));
    const failedReviewers = items(report.failed_reviewers).filter((value) => text(value)).join(", ");
    const passport = facts([
      ["Документ", text(meta.name)],
      ["Формат", text(meta.format).toUpperCase()],
      ["Строк в документе", numeric(meta.line_count)],
      ["Страниц PDF", numeric(meta.page_count)],
      ["SHA-256", text(meta.sha256)],
      ["Время прогона", reportDate(report.reviewed_at)],
      ["Провайдер", text(report.provider)],
      ["Модель", text(report.model)],
      ["Версия проекта", text(report.version)],
    ]);
    const diagnostics = facts([
      ["Этапов с ошибкой", numeric(report.failed_stages)],
      ["Отброшено предложений", numeric(report.discarded_proposals)],
      ["Слито повторных предложений", numeric(report.merged_proposals)],
      ["Ревьюеры с ошибками", failedReviewers],
    ]);
    const content = [
      detail("Паспорт", passport),
      matrix(report),
      detail("Вопросы к аналитику", questions.length ? textList(questions.map(([key, question]) =>
        text(key) ? `${key} — ${question}` : question)) : element("p", "rv-meta-text", "Вопросов в отчёте нет.")),
      detail("Диагностика", diagnostics, ...(warnings.length ? [textList(warnings)] : [])),
    ];
    if (report.timings && typeof report.timings === "object") content.push(timingDetails(report.timings));
    if (Array.isArray(report.external_references)) {
      content.push(detail("Внешняя сверка", report.external_references.length
        ? externalReferenceList(report.external_references)
        : element("p", "rv-meta-text", "Внешних ссылок в отчёте нет.")));
    }
    return detail("Паспорт, матрица и диагностика", ...content);
  }

  function appendField(body, caption, value) {
    if (!text(value)) return;
    body.append(element("h4", "", caption), element("p", "rv-prose", value));
  }

  function externalReferenceList(references) {
    return textList(references.filter(Boolean).map((reference) => [
      text(reference.technology),
      label(externalLabels, reference.status),
      text(reference.question),
      text(reference.url),
    ].filter(Boolean).join(" · ")));
  }

  function criteriaFor(finding) {
    return [finding.criterion, ...items(finding.related_criteria)].filter((key) => text(key));
  }

  function selectFilter(labelText, firstLabel, keys, labels) {
    const result = element("select", "rv-control");
    result.setAttribute("aria-label", labelText);
    result.add(new Option(firstLabel, ""));
    for (const key of keys) result.add(new Option(label(labels, key), key));
    return result;
  }

  function saveBlob(content, contentType, name) {
    const url = URL.createObjectURL(new Blob([content], { type: contentType }));
    const link = element("a");
    link.href = url;
    link.download = name;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  function fileStem(data) {
    return (titleFor(data).replace(/\.[^.]+$/, "").replace(/[^\p{L}\p{N}._-]+/gu, "-")
      .replace(/^[-.]+|[-.]+$/g, "").slice(0, 90) || "review") + "-report";
  }

  async function createHtml(data) {
    if (!scriptUrl) throw new Error("Ресурсы экспорта недоступны.");
    const cssUrl = new URL("report.css", scriptUrl);
    const [viewer, styles] = await Promise.all([scriptUrl, cssUrl].map(async (url) => {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error("Не удалось загрузить ресурсы экспорта.");
      return response.text();
    }));
    const json = JSON.stringify({
      report: data.report,
      source: data.source ?? null,
      title: titleFor(data),
      partial: data.partial === true,
      standalone: true,
    }).replace(/[<>&\u2028\u2029]/g, (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`);
    const script = viewer.replace(/<\/script/gi, "<\\/script");
    const css = styles.replace(/<\/style/gi, "<\\/style");
    return `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Отчёт · Requirements Validator</title>
<style>${css}</style>
</head>
<body class="rv-standalone">
<main id="rv-export"></main>
<noscript>Для фильтров и переходов к цитатам разрешите JavaScript в этом файле.</noscript>
<script id="rv-data" type="application/json">${json}</script>
<script>${script}</script>
<script>
const review = JSON.parse(document.getElementById("rv-data").textContent);
document.title = review.title + " · Requirements Validator";
window.RequirementsReport.mount(document.getElementById("rv-export"), review);
</script>
</body>
</html>`;
  }

  async function downloadHtml(data) {
    saveBlob(await createHtml(data), "text/html;charset=utf-8", `${fileStem(data)}.html`);
  }

  function reportHeader(data) {
    const header = element("header", "rv-heading");
    const name = element("div", "rv-heading-name");
    name.append(element("h1", "", titleFor(data)));
    const meta = data.report.document ?? {};
    const descriptor = [text(meta.format).toUpperCase(), reportDate(data.report.reviewed_at)].filter(Boolean);
    if (descriptor.length) name.append(element("p", "rv-meta-text", descriptor.join(" · ")));
    header.append(name);
    if (data.standalone) return header;
    const controls = element("div", "rv-downloads");
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "Скачивание отчёта");
    for (const [key, format] of [["md", "MD"], ["json", "JSON"]]) {
      const url = data.downloadUrls?.[key];
      if (text(url)) {
        const link = element("a", "rv-button", `Скачать ${format}`);
        link.href = url;
        link.download = `${fileStem(data)}.${key}`;
        controls.append(link);
      } else if (key === "json") {
        controls.append(button("Скачать JSON", "rv-button", () =>
          saveBlob(JSON.stringify(data.report, null, 2) + "\n", "application/json", `${fileStem(data)}.json`)));
      }
    }
    const status = element("p", "rv-meta-text rv-download-status");
    status.setAttribute("role", "status");
    const html = button("Скачать HTML", "rv-button rv-primary", async () => {
      if (html.getAttribute("aria-disabled") === "true") return;
      html.setAttribute("aria-disabled", "true");
      status.textContent = "Подготовка HTML…";
      try {
        await downloadHtml(data);
        status.textContent = "";
      } catch {
        status.textContent = "Не удалось подготовить HTML. Повторите скачивание.";
      } finally {
        html.removeAttribute("aria-disabled");
      }
    });
    controls.append(html, status);
    header.append(controls);
    return header;
  }

  function reportSummary(report, findings, partial) {
    const summary = element("section", "rv-summary");
    summary.setAttribute("aria-label", "Итог проверки");
    const states = [
      ["Разработка", label(readinessLabels, report.readiness?.development)],
      ["Тестирование", label(readinessLabels, report.readiness?.testing)],
      ["Покрытие", partial || report.coverage_complete === false ? "Неполное"
        : report.coverage_complete === true ? "Полное" : "Нет данных"],
    ];
    for (const [caption, value] of states) {
      const entry = element("div", "rv-verdict");
      entry.append(element("p", "", caption), element("strong", "", value));
      summary.append(entry);
    }
    const counts = element("div", "rv-counts");
    counts.setAttribute("aria-label", "Замечания по серьёзности");
    for (const key of Object.keys(severityLabels)) {
      const count = findings.filter((finding) => finding.severity === key).length;
      counts.append(severityBadge(key, ` · ${count}`));
    }
    summary.append(counts);
    return summary;
  }

  function mount(root, data) {
    const report = data.report;
    const findings = items(report.findings).filter((finding) => finding && typeof finding === "object");
    const partial = data.partial === true || report.coverage_complete === false;
    const id = `rv-${++mountCount}`;
    root.classList.add("rv-report");
    const workspace = element("div", "rv-workspace");
    workspace.dataset.pane = "findings";
    const documentPanel = element("section", "rv-panel rv-document");
    documentPanel.id = `${id}-document`;
    const findingsPanel = element("section", "rv-panel rv-findings");
    findingsPanel.id = `${id}-findings`;
    const docHeading = element("h2", "", "Документ");
    docHeading.id = `${id}-document-heading`;
    documentPanel.setAttribute("aria-labelledby", docHeading.id);
    const documentHeader = element("header", "rv-panel-header");
    const sourceNotice = element("p", "rv-anchor-status rv-meta-text");
    sourceNotice.setAttribute("role", "status");
    documentHeader.append(docHeading, sourceNotice);

    const paneNavigation = element("div", "rv-mobile-panes");
    paneNavigation.setAttribute("role", "group");
    paneNavigation.setAttribute("aria-label", "Панели отчёта");
    const documentTab = button("Документ", "rv-button", () => showPane("document"));
    const findingsTab = button("Замечания", "rv-button", () => {
      showPane("findings");
      if (!activeCard) return;
      const target = activeQuote ?? activeCard.title;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "nearest" });
    });
    documentTab.setAttribute("aria-controls", documentPanel.id);
    findingsTab.setAttribute("aria-controls", findingsPanel.id);
    paneNavigation.append(documentTab, findingsTab);

    function showPane(pane) {
      workspace.dataset.pane = pane;
      documentTab.setAttribute("aria-pressed", String(pane === "document"));
      findingsTab.setAttribute("aria-pressed", String(pane === "findings"));
    }
    showPane("findings");

    const sourceRows = new Map();
    const sourceList = element("div", "rv-source-list");
    sourceList.tabIndex = 0;
    sourceList.setAttribute("role", "region");
    sourceList.setAttribute("aria-label", "Строки исходного документа");
    let lastPage = null;
    let sourceHasDuplicates = false;
    for (const line of items(data.source?.lines)) {
      if (!line || !positiveInteger(line.number) || typeof line.text !== "string") continue;
      if (sourceRows.has(line.number)) {
        sourceHasDuplicates = true;
        continue;
      }
      const page = positiveInteger(line.page) ? line.page : null;
      if (page !== lastPage && page !== null) {
        sourceList.append(element("h3", "rv-page-heading", `Страница ${page}`));
      }
      lastPage = page;
      const row = element("div", "rv-source-line");
      row.dataset.line = String(line.number);
      const number = element("span", "rv-line-number", String(line.number));
      number.setAttribute("aria-label", `Строка ${line.number}`);
      const body = element("span", "rv-line-text", line.text);
      row.append(number, body);
      sourceList.append(row);
      sourceRows.set(line.number, { text: line.text, page, row, body });
    }
    const sourceHash = text(data.source?.sha256);
    const reportHash = text(report.document?.sha256);
    let sourceIssue = "";
    if (!sourceRows.size) sourceIssue = noSource;
    else if (!/^[a-f0-9]{64}$/i.test(sourceHash) || sourceHash.toLowerCase() !== reportHash.toLowerCase()) {
      sourceIssue = hashMismatch;
    } else if (sourceHasDuplicates) sourceIssue = coordinateMismatch;
    sourceNotice.textContent = sourceIssue || "Выберите замечание, чтобы увидеть цитату в документе.";
    documentPanel.append(documentHeader);
    if (sourceRows.size) documentPanel.append(sourceList);

    const findingsHeading = element("h2", "", "Замечания");
    findingsHeading.id = `${id}-findings-heading`;
    findingsPanel.setAttribute("aria-labelledby", findingsHeading.id);
    const filterHeader = element("header", "rv-panel-header");
    const filters = element("div", "rv-filters");
    const search = element("input", "rv-control");
    search.type = "search";
    search.placeholder = "Поиск по замечаниям";
    search.setAttribute("aria-label", "Поиск замечаний");
    const levels = [...new Set(findings.map((finding) => text(finding.severity)).filter(Boolean))];
    const severity = selectFilter("Серьёзность", "Все уровни", [
      ...Object.keys(severityLabels).filter((key) => levels.includes(key)),
      ...levels.filter((key) => !Object.hasOwn(severityLabels, key)),
    ], severityLabels);
    const criterion = selectFilter("Критерий", "Все критерии", [...new Set(findings.flatMap(criteriaFor))], criterionLabels);
    const reset = button("Сбросить", "rv-button", () => {
      search.value = severity.value = criterion.value = "";
      applyFilters();
      search.focus();
    });
    const count = element("p", "rv-filter-count rv-meta-text");
    count.setAttribute("role", "status");
    const evidenceStatus = element("p", "rv-evidence-status");
    evidenceStatus.setAttribute("role", "status");
    filters.append(search, severity, criterion, reset);
    filterHeader.append(findingsHeading, filters, count, evidenceStatus);
    const results = element("ul", "rv-results");
    const empty = element("p", "rv-empty");
    const resultsArea = element("div", "rv-results-area");
    resultsArea.append(results, empty);
    findingsPanel.append(filterHeader, resultsArea);
    workspace.append(documentPanel, findingsPanel);

    let activeSpans = [];
    let activeCard = null;
    let activeQuote = null;
    let activeQuoteWarning = null;

    function clearHighlight() {
      for (const { row } of activeSpans) {
        row.body.textContent = row.text;
        row.row.classList.remove("rv-highlighted");
      }
      activeSpans = [];
      if (activeQuote) activeQuote.removeAttribute("aria-current");
      if (activeQuoteWarning) activeQuoteWarning.textContent = "";
      activeQuote = null;
      activeQuoteWarning = null;
    }

    function followQuote(anchor, quoteControl) {
      clearHighlight();
      const spans = sourceIssue ? null : locate(anchor, sourceRows);
      if (!spans) {
        const warning = sourceIssue || coordinateMismatch;
        sourceNotice.textContent = warning;
        if (quoteControl) {
          activeQuoteWarning = quoteControl.warning;
          activeQuoteWarning.textContent = warning;
        }
        evidenceStatus.textContent = quoteControl ? "" : warning;
        return;
      }
      activeSpans = spans;
      activeQuote = quoteControl?.button;
      activeQuote?.setAttribute("aria-current", "location");
      evidenceStatus.textContent = "";
      sourceNotice.textContent = `Подсвечена цитата · ${coordinates(anchor)}`;
      for (const { row, before, match, after } of spans) {
        row.row.classList.add("rv-highlighted");
        row.body.replaceChildren(document.createTextNode(before), element("mark", "", match), document.createTextNode(after));
      }
      const first = spans[0].row.row;
      if (compactScreen.matches) {
        showPane("document");
        first.tabIndex = -1;
        first.focus({ preventScroll: true });
        first.scrollIntoView({ block: "start" });
      } else {
        const viewport = sourceList.getBoundingClientRect();
        const target = first.getBoundingClientRect();
        sourceList.scrollTop += target.top - viewport.top - 20;
      }
    }

    function activate(card, anchor, quoteControl) {
      if (activeCard !== card) {
        if (activeCard) {
          activeCard.title.setAttribute("aria-expanded", "false");
          activeCard.body.hidden = true;
          activeCard.root.classList.remove("rv-selected");
        }
        activeCard = card;
        card.title.setAttribute("aria-expanded", "true");
        card.body.hidden = false;
        card.root.classList.add("rv-selected");
      }
      if (anchor) followQuote(anchor, quoteControl);
      else {
        clearHighlight();
        evidenceStatus.textContent = "Для замечания нет координат цитаты.";
        sourceNotice.textContent = sourceIssue || evidenceStatus.textContent;
      }
    }

    const cards = findings.map((finding, index) => {
      const card = { root: element("li", "rv-card"), body: element("div", "rv-card-body") };
      const criteria = criteriaFor(finding);
      const evidence = items(finding.evidence).filter((anchor) => anchor && typeof anchor === "object");
      const quoteControls = [];
      card.body.id = `${id}-card-${index}`;
      card.body.hidden = true;
      const metadata = element("div", "rv-card-meta");
      metadata.append(severityBadge(finding.severity));
      if (text(finding.id)) metadata.append(element("span", "rv-meta-text rv-card-id", finding.id));
      if (criteria.length) metadata.append(element("span", "rv-meta-text", criteria.map((key) => label(criterionLabels, key)).join(" · ")));
      const title = button(text(finding.title) || "Замечание без заголовка", "rv-card-title",
        () => activate(card, evidence[0], quoteControls[0]));
      title.setAttribute("aria-expanded", "false");
      title.setAttribute("aria-controls", card.body.id);
      card.title = title;
      const heading = element("h3", "");
      heading.append(title);
      card.root.append(metadata, heading, card.body);
      appendField(card.body, "Объяснение", finding.description);
      appendField(card.body, "Сценарий", finding.scenario);
      card.body.append(element("h4", "", "Цитаты из отчёта"));
      if (!evidence.length) card.body.append(element("p", "rv-meta-text", "Цитаты отсутствуют."));
      const citations = element("ol", "rv-citations");
      evidence.forEach((anchor, quoteIndex) => {
        const item = element("li", "rv-citation");
        const reference = [text(anchor.block_id), coordinates(anchor)].filter(Boolean).join(" · ");
        item.append(element("p", "rv-meta-text", reference));
        item.append(element("blockquote", "", text(anchor.quote) || "Текст цитаты отсутствует."));
        if (sourceRows.size) {
          const warning = element("p", "rv-quote-warning");
          warning.setAttribute("role", "status");
          const jump = button(`Показать цитату ${quoteIndex + 1}`, "rv-button rv-quote-button",
            () => activate(card, anchor, quoteControls[quoteIndex]));
          jump.setAttribute("aria-label", `Показать цитату ${quoteIndex + 1} · ${reference}`);
          quoteControls.push({ button: jump, warning });
          item.append(jump, warning);
        }
        citations.append(item);
      });
      if (evidence.length) card.body.append(citations);
      appendField(card.body, "Исправление", finding.recommendation);
      const alternatives = items(finding.additional_recommendations).filter((value) => text(value));
      if (alternatives.length) card.body.append(detail("Дополнительные формулировки", textList(alternatives)));
      const external = items(finding.external_references);
      if (external.length) card.body.append(detail("Внешние источники", externalReferenceList(external)));
      const searchTerms = [
        finding.id, finding.title, finding.description, finding.scenario, finding.recommendation,
        ...alternatives, ...items(finding.requirement_ids), ...criteria.map((key) => label(criterionLabels, key)),
        ...evidence.flatMap((anchor) => [anchor.block_id, anchor.quote]),
      ].filter((value) => text(value)).join("\n").toLocaleLowerCase("ru");
      results.append(card.root);
      return { ...card, criteria, severity: finding.severity, searchTerms };
    });

    function applyFilters() {
      const query = search.value.trim().toLocaleLowerCase("ru");
      let visible = 0;
      for (const card of cards) {
        const matches = (!severity.value || card.severity === severity.value)
          && (!criterion.value || card.criteria.includes(criterion.value))
          && (!query || card.searchTerms.includes(query));
        card.root.hidden = !matches;
        if (matches) visible += 1;
      }
      if (activeCard?.root.hidden) {
        activeCard.title.setAttribute("aria-expanded", "false");
        activeCard.body.hidden = true;
        activeCard.root.classList.remove("rv-selected");
        activeCard = null;
        clearHighlight();
        sourceNotice.textContent = sourceIssue || "Выберите замечание, чтобы увидеть цитату в документе.";
        evidenceStatus.textContent = "";
      }
      count.textContent = `Показано ${visible} из ${findings.length}`;
      empty.hidden = visible > 0;
      empty.textContent = findings.length
        ? "По этому запросу замечания не найдены. Измените или сбросьте фильтры."
        : "В отчёте нет подтверждённых замечаний.";
    }
    search.addEventListener("input", applyFilters);
    severity.addEventListener("change", applyFilters);
    criterion.addEventListener("change", applyFilters);
    applyFilters();

    const details = reportDetails(report);
    details.classList.add("rv-report-details");
    const top = [];
    if (!data.hideHeader) top.push(reportHeader(data));
    top.push(reportSummary(report, findings, partial));
    if (partial) top.push(element("p", "rv-partial", "Частичный отчёт. Проверка завершилась с неполным покрытием."));
    if (sourceIssue) top.push(element("p", "rv-source-warning", sourceIssue));
    top.push(details, paneNavigation, workspace);
    root.replaceChildren(...top);
  }

  window.RequirementsReport = Object.freeze({ mount, createHtml, downloadHtml });
})();
