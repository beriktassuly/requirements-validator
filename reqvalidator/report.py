"""Russian Markdown and machine-readable report rendering."""

from dataclasses import asdict
from collections import Counter
import html
import json
import re

from pydantic import BaseModel

from .model import LOCAL_CRITERIA, Anchor, ReviewReport
from .prompts import CRITERIA, RELATION_LABELS


CRITERION_LABELS = {
    "contradiction": "Согласованность",
    "completeness": "Полнота",
    "ambiguity": "Однозначность",
    "verifiability": "Проверяемость",
    "atomicity": "Единичность",
    "duplication": "Дублирование с риском расхождения",
    "traceability": "Трассируемость",
}
STATUS_LABELS = {
    "pass": "Без подтверждённых замечаний",
    "issue": "Есть замечание",
    "uncertain": "Требует уточнения",
    "not_applicable": "Неприменимо",
}
SEVERITY_LABELS = {"critical": "Критический", "major": "Существенный", "minor": "Незначительный"}
DECISIONS = {
    "ready": "Можно начинать.",
    "conditional": "Условно можно после согласования указанных исправлений.",
    "not_ready": "Начинать нельзя до устранения блокирующих замечаний.",
    "not_confirmed": "Готовность не подтверждена. Нужно завершить проверку.",
}


def _plain(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_[\]{}#~|])", r"\\\1", escaped)


def _cell(value: str) -> str:
    return _plain(" ".join(value.split())).replace(r"\|", "&#124;")


def _location(anchor: Anchor) -> str:
    lines = (
        f"строка {anchor.start_line}, символы {anchor.start_col}–{anchor.end_col}"
        if anchor.start_line == anchor.end_line
        else f"строки {anchor.start_line}–{anchor.end_line} "
        f"(от символа {anchor.start_col} до символа {anchor.end_col})"
    )
    if anchor.page_start is None:
        return lines
    pages = (
        f"страница PDF {anchor.page_start}"
        if anchor.page_start == anchor.page_end
        else f"страницы PDF {anchor.page_start}–{anchor.page_end}"
    )
    return f"{pages}, {lines}"


def _fenced(text: str) -> str:
    # An extracted code sample may itself contain a Markdown fence.
    run = max((len(match) for match in re.findall(r"`+", text)), default=0)
    delimiter = "`" * max(3, run + 1)
    return f"{delimiter}text\n{text}\n{delimiter}"


def render_markdown(report: ReviewReport) -> str:
    doc = report.document
    by_requirement: dict[tuple[str, str], list[str]] = {}
    for finding in report.findings:
        if "local" in finding.scopes:
            for requirement_id in finding.requirement_ids:
                for criterion in (finding.criterion, *finding.related_criteria):
                    by_requirement.setdefault((requirement_id, criterion), []).append(finding.id)

    severity_counts = Counter(f.severity for f in report.findings)
    critical = [f for f in report.findings if f.severity == "critical"]
    lines = [
        "# Отчёт о проверке требований",
        "",
        "## Паспорт прогона",
        "",
        f"- Документ — {_plain(doc.name)}",
        f"- Формат — {_plain(doc.format.upper())}",
        f"- SHA-256 — `{doc.sha256}`",
        f"- Дата прогона — {_plain(report.reviewed_at)}",
        f"- Провайдер и модель — {_plain(report.provider)} / {_plain(report.model)}",
        f"- Версия агента — {_plain(report.version)}",
        f"- Строк в извлечённом тексте — {doc.line_count}",
    ]
    if doc.page_count is not None:
        lines.append(f"- Страниц PDF — {doc.page_count}")
    lines.extend([
        "",
        "## Сводка",
        "",
        f"- Выделено требований и нормативных элементов — {len(report.requirements)}.",
        f"- Подтверждено уникальных замечаний — {len(report.findings)}.",
        f"- Критические — {severity_counts['critical']}, существенные — {severity_counts['major']}, "
        f"незначительные — {severity_counts['minor']}.",
        f"- Оценок, которые требуют уточнения — {len(report.clarifications)}.",
        f"- Покрытие проверок — {'полное' if report.coverage_complete else 'неполное'}.",
        "",
        "### Критические дефекты",
        "",
    ])
    if critical:
        lines.extend(f"- [{f.id}](#{f.id.lower()}) — {_plain(f.title)}." for f in critical)
    else:
        lines.append("Подтверждённых критических дефектов нет.")

    lines.extend([
        "",
        "## Как нарезаны требования",
        "",
        "Идентификаторы B… присвоены блокам исходника. Пропуски в нумерации "
        "относятся к контексту, примерам и служебным фрагментам.",
        "",
        "| ID в отчёте | ID в документе | Место | Краткая цитата |",
        "|---|---|---|---|",
    ])
    for row in report.requirements:
        block = row.block
        sample = " ".join(block.text.split())
        if len(sample) > 220:
            sample = sample[:217].rstrip() + "…"
        section = f"{block.section}, " if block.section else ""
        location = f"{section}строки {block.start_line}–{block.end_line}"
        lines.append(
            f"| {block.id} | {_cell(block.source_id or '—')} | {_cell(location)} | {_cell(sample)} |"
        )

    if report.context_blocks:
        lines.extend([
            "", "### Контекстные блоки", "",
            "| ID | Тип и раздел | Строки | Краткая цитата |",
            "|---|---|---|---|",
        ])
        for block in report.context_blocks:
            sample = " ".join(block.text.split())
            if len(sample) > 220:
                sample = sample[:217].rstrip() + "…"
            kind = {
                "example": "Пример", "governance": "Служебная часть",
                "heading": "Заголовок",
            }.get(block.kind, "Контекст")
            location = f"{kind} / {block.section}" if block.section else kind
            lines.append(
                f"| {block.id} | {_cell(location)} | {block.start_line}–{block.end_line} | {_cell(sample)} |"
            )

    labels = [CRITERION_LABELS[criterion] for criterion in LOCAL_CRITERIA]
    lines.extend([
        "", "## Проверка каждого требования", "",
        "Рубрика отдельных требований основана на ISO/IEC/IEEE 29148:2018, п. 5.2.5.",
        "", "✓ без замечаний · ✗ есть замечание · ? требует уточнения · — неприменимо", "",
        "| ID | " + " | ".join(labels) + " |",
        "|" + "---|" * (len(labels) + 1),
    ])
    marks = {"pass": "✓", "issue": "✗", "uncertain": "?", "not_applicable": "—"}
    for row in report.requirements:
        checks = {check.criterion: check.status for check in row.checks}
        cells = [row.block.id]
        for criterion in LOCAL_CRITERIA:
            status = checks.get(criterion, "uncertain")
            ids = by_requirement.get((row.block.id, criterion), []) if status == "issue" else []
            links = " ".join(f"[{i}](#{i.lower()})" for i in ids)
            cells.append(f"{marks[status]} {links}".rstrip())
        lines.append("| " + " | ".join(cells) + " |")

    lines.extend([
        "",
        "## Проверка набора целиком",
        "",
        "Рубрика набора основана на ISO/IEC/IEEE 29148:2018, п. 5.2.6, и IEEE 830-1998, п. 4.3.",
        "",
        "| Критерий | Что проверено | Статус | Замечания уровня набора |",
        "|---|---|---|---|",
    ])
    for check in report.set_checks:
        ids = ", ".join(
            f"[{f.id}](#{f.id.lower()})"
            for f in report.findings
            if "set" in f.scopes and check.criterion in (f.criterion, *f.related_criteria)
        ) or "—"
        lines.append(
            f"| {CRITERION_LABELS[check.criterion]} | {CRITERIA[check.criterion]} "
            f"| {STATUS_LABELS[check.status]} | {ids} |"
        )
    lines.extend([
        "",
        "### Связи между разделами",
        "",
        "| Сопоставление | Статус |",
        "|---|---|",
    ])
    lines.extend(
        f"| {RELATION_LABELS[check.relation]} | {STATUS_LABELS[check.status]} |"
        for check in report.link_checks
    )

    if report.clarifications:
        lines.extend(["", "### Вопросы к аналитику", ""])
        for key, question in report.clarifications:
            target, criterion = key.split("/", 1)
            label = CRITERION_LABELS.get(criterion, RELATION_LABELS.get(criterion, criterion))
            lines.append(f"- {_plain(target)} · {_plain(label)}. {_plain(question)}")

    lines.extend([
        "", "## Реестр замечаний", "",
        "**Критический** — основной сценарий нельзя однозначно реализовать или проверить. "
        "**Существенный** — есть риск переделки реализации или тестов. "
        "**Незначительный** — оформление или уточнение с ограниченным влиянием.", "",
    ])
    if not report.findings:
        lines.append("Подтверждённых замечаний нет.")
    for finding in report.findings:
        labels = ", ".join("отдельное требование" if scope == "local" else "набор" for scope in finding.scopes)
        lines.extend([
            f'<a id="{finding.id.lower()}"></a>',
            f"### {finding.id} — {_plain(finding.title)}",
            "",
            f"- Уровень — {SEVERITY_LABELS[finding.severity]}",
            f"- Критерии — {', '.join(CRITERION_LABELS[key] for key in (finding.criterion, *finding.related_criteria))}",
            f"- Обнаружено при проверке — {labels}",
            f"- Требования — {', '.join(finding.requirement_ids) or 'служебный или опорный блок'}",
            "",
            _plain(finding.description),
            "",
        ])
        if finding.scenario:
            lines.extend([f"**Сценарий.** {_plain(finding.scenario)}", ""])
        lines.extend([f"**Исправление.** {_plain(finding.recommendation)}", ""])
        if finding.additional_recommendations:
            lines.append("<details><summary>Дополнительные формулировки исправления</summary>")
            lines.append("")
            lines.extend(f"- {_plain(action)}" for action in finding.additional_recommendations)
            lines.extend(["", "</details>", ""])
        lines.extend(["**Доказательства**", ""])
        for evidence in finding.evidence:
            lines.extend([
                f"- {evidence.block_id}, {_location(evidence)}",
                "",
                _fenced(evidence.quote),
                "",
            ])

    decisions = DECISIONS
    if report.coverage_complete and report.clarifications:
        decisions = {**DECISIONS, "not_confirmed": "Готовность зависит от ответов на вопросы к аналитику."}
    lines.extend([
        "",
        "## Готовность к разработке и тестированию",
        "",
        f"- Разработка — {decisions[report.readiness.development]}",
        f"- Тестирование — {decisions[report.readiness.testing]}",
        "",
        "## Приоритет исправлений",
        "",
    ])
    if report.findings:
        lines.extend(
            f"{i}. [{f.id}](#{f.id.lower()}) ({SEVERITY_LABELS[f.severity].lower()}) "
            f"— {_plain(f.recommendation)}"
            for i, f in enumerate(report.findings[:8], start=1)
        )
    else:
        lines.append("В реестре нет подтверждённых исправлений.")
    lines.extend([
        "",
        "## Диагностика",
        "",
        f"- Отброшено предложений без достаточной буквальной опоры или с неполными полями — "
        f"{report.discarded_proposals}.",
        f"- Подтверждено замечаний до слияния — {len(report.findings) + report.merged_proposals}.",
        f"- Слито повторных замечаний — {report.merged_proposals}.",
        f"- Этапов с ошибкой вызова или схемы — {report.failed_stages}.",
    ])
    lines.extend(f"- {_plain(warning)}" for warning in report.warnings)
    return "\n".join(lines).rstrip() + "\n"


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Неподдерживаемый тип в отчёте: {type(value).__name__}")


def render_json(report: ReviewReport) -> str:
    return json.dumps(
        {"schema_version": 1, **asdict(report)},
        ensure_ascii=False,
        indent=2,
        default=_json_value,
    ) + "\n"
