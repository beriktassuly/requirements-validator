"""Create an ordered registry of stable source spans and requirement units."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from .document import Document, SourceLine


@dataclass(frozen=True, slots=True)
class Block:
    id: str
    section: str
    kind: str
    start_line: int
    end_line: int
    text: str
    source_id: str | None = None

    @property
    def is_requirement(self) -> bool:
        return self.kind in _REQUIREMENT_KINDS


_REQUIREMENT_KINDS = frozenset(
    {"functional", "nonfunctional", "acceptance", "api", "data", "integration", "process", "generic"}
)

_ATX = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?[ \t]*$")
_PART = re.compile(r"^(?:часть|part)\s+[a-zа-яё0-9]{1,4}(?:\s*[.:\-–—]\s*.{1,92})?$", re.IGNORECASE)
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_DIVIDER = re.compile(r"^(?:-{3,}|_{3,}|\*{3,})$")
_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+•]\s+|\d{1,3}[.)]\s+)")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?$")
_COLUMNS = re.compile(r"(?:[ \t]{2,}|\t+)")
_SOURCE_ID = re.compile(
    r"^(?P<id>[A-ZА-ЯЁ]{1,8}(?:[-_–][A-ZА-ЯЁ0-9]{1,8})*[-_–]\d+(?:[.\-]\d+)*"
    r"|(?:REQ|FR|NFR|Ф|ФТ|НФ|НФТ|AC|US)\s*\d+(?:[.\-]\d+)*)"
    r"(?=[.\s:)\]_*`-]|$)",
    re.IGNORECASE,
)
_STATUS_RULE = re.compile(r"^(?:HTTP\s+)?[1-5]\d{2}\s*[.:\-–—]\s*\S", re.IGNORECASE)
_STATUS_LABEL = re.compile(r"^(?:HTTP\s+)?[1-5]\d{2}$", re.IGNORECASE)
_MODAL = re.compile(
    r"\b(?:shall|must|required|may not|will not|will provide|should|forbidden|must not|"
    r"долж(?:ен|на|но|ны)|обязан[аоы]?|требуется|запрещ(?:ено|ается)|допускается|"
    r"необходимо|не\s+допускается|подлежит)\b",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(
    r"^(?:todo|tbd|n/a|не\s+заполнен[аоы]?|заполня\w*\s+(?:аналитик\w*|тестировщик\w*)|"
    r"заполнить|уточнить|to\s+be\s+(?:defined|filled))[\s:.;…—-]*$",
    re.IGNORECASE,
)
_CROSS_REFERENCE = re.compile(
    r"^(?:дочерний документ части|этот раздел описывает|this section describes)\b", re.IGNORECASE
)
_EXAMPLE_WORDS = frozenset({"или", "or", "пример", "example"})
_TABLE_LABELS = frozenset(
    {
        "поле", "field", "параметр", "parameter", "проверка", "подразделение",
        "исходные условия", "precondition", "preconditions", "статус", "status", "id",
        "идентификатор",
    }
)
_COLUMN_LABELS = frozenset(
    {
        "значение", "value", "формат", "format", "тип", "type", "проверено", "подпись",
        "действие", "action", "описание", "description", "ожидаемый результат",
        "expected result", "требование", "requirement", "код", "code", "фио ответственного",
    }
)
_ADMIN_FIELDS = frozenset(
    {
        "gitlab project", "github project", "jira", "author", "developer", "framework",
        "автор", "разработчик", "задача в jira",
    }
)
_EXAMPLE_FIELDS = frozenset({"curl", "пример", "пример запроса", "пример ответа", "example"})
_GOVERNANCE = re.compile(
    r"ревью требований|блок согласующ\w*|согласовани\w*|история изменени\w*|"
    r"контроль верси\w*|чек-лист(?: ревью)?|журнал изменени\w*|метаданные|"
    r"\brequirements review\b|\bapprovals?\b|\bsign[- ]?off\b|\brevision history\b|"
    r"\bchange log\b|\breview checklist\b",
    re.IGNORECASE,
)
_SCOPES = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        ("governance", _GOVERNANCE.pattern),
        ("example", r"^(?:примеры?(?:\s+(?:запросов|ответов))?|(?:request |response )?examples?)$"),
        ("context", r"^общие сведения$|^цел[ьи]$|архитектур\w*\s+(?:решения|системы|обзор)|"
         r"^(?:введение|вводная|глоссарий|оглавление|резюме|introduction|overview|context|"
         r"background|architecture|glossary|objectives?|goals?|scope|general information)$"),
        ("acceptance", r"критери\w*\s+при[её]мк\w*|\bacceptance criteria\b"),
        ("nonfunctional", r"нефункциональн\w+|\bnon[- ]?functional\b|"
         r"требовани\w*\s+к\s+(?:производительности|тестированию|безопасности)"),
        ("data", r"требовани\w*\s+к\s+данным|^данные$|\bdata requirements\b|\bdata model\b"),
        ("integration", r"требовани\w*\s+к\s+интеграци\w*|\bintegration requirements\b|^интеграци\w*$"),
        ("functional", r"функциональн\w+\s+требовани\w+|\bfunctional requirements\b|"
         r"дизайн процесса ui|\buser interface\b"),
        ("process", r"\bbpmn\b|\bsequence diagram\b|\bworkflow\b|схема процесса|"
         r"сценари\w*\s+использования"),
        ("api", r"\bapi\b|^логика работы метода$|^параметры запроса$|^особые правила$|"
         r"^request body$|^(?:path|query)[- ]параметры$|^статусы$|^ответ$"),
        ("generic", r"требовани\w*|\brequirements?\b|\bspecification\b"),
    )
)
_PLAIN_HEADINGS = frozenset(
    {
        "общие сведения", "общие параметры", "цели", "архитектура решения", "дизайн процесса ui",
        "требования к изменениям", "bpmn схема процесса", "функциональные требования",
        "sequence diagram", "нефункциональные требования", "требования к данным",
        "требования к интеграции", "требования к тестированию", "тестирование требований",
        "критерии приемки", "ревью требований", "блок согласующих", "согласование",
        "история изменений", "введение", "глоссарий", "логика работы метода", "особые правила",
        "схема", "параметры запроса", "path-параметры", "query-параметры", "request body",
        "ответ", "статусы", "requirements", "specification", "functional requirements",
        "non-functional requirements", "nonfunctional requirements", "acceptance criteria",
        "integration requirements", "data requirements", "user interface", "overview", "context",
        "revision history", "review checklist", "requirements review", "approvals", "sign-off",
        "request parameters", "response", "response codes", "rest api", "api",
        "примеры запросов", "примеры ответов", "request examples", "response examples", "examples",
    }
)
_SUBHEADINGS = frozenset(
    {"bpmn схема процесса", "функциональные требования", "тестирование требований",
     "path-параметры", "query-параметры", "request body"}
)


def segment_document(document: Document) -> tuple[Block, ...]:
    lines = document.lines
    headings: list[tuple[int, str]] = []
    blocks: list[Block] = []
    fallback_candidates: list[int] = []
    pos = 0

    while pos < len(lines):
        line = lines[pos]
        raw = line.text.strip()
        if not raw or _DIVIDER.fullmatch(raw):
            pos += 1
            continue

        finish = pos + 1
        source_id = None
        heading = _heading(line, document.format)
        if heading is not None:
            level, label = heading
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, label))
            kind = "governance" if _scope(headings) == "governance" else "context"
        else:
            scope = _scope(headings)
            fence = _FENCE.match(line.text)
            fields = _table_fields(line, scope, document.format)
            if fence is not None:
                finish = _fenced_end(lines, pos, fence[1])
                kind = "example"
            elif fields:
                separator_next = pos + 1 < len(lines) and _TABLE_SEPARATOR.fullmatch(lines[pos + 1].text.strip())
                if separator_next:
                    finish += 1
                source_id = _id(fields[0])
                kind = _table_kind(scope, fields, bool(separator_next), source_id)
            elif _TABLE_SEPARATOR.fullmatch(raw):
                kind = "governance" if scope == "governance" else "context"
            elif _json_start(raw, scope):
                finish = _json_end(lines, pos, document.format, scope)
                kind = "example"
            else:
                finish = _paragraph_end(lines, pos, scope, document.format)
                source_id = _id(raw)
                span = "\n".join(item.text for item in lines[pos:finish])
                kind = _body_kind(scope, span, source_id)
                if kind == "context" and scope is None and len(span.split()) >= 5:
                    fallback_candidates.append(len(blocks))

        blocks.append(
            Block(
                id=f"B{len(blocks) + 1:03d}",
                section=" / ".join(label for _, label in headings) or "Без раздела",
                kind=kind,
                start_line=line.number,
                end_line=lines[finish - 1].number,
                text="\n".join(item.text for item in lines[pos:finish]),
                source_id=source_id,
            )
        )
        pos = finish

    if not any(block.is_requirement for block in blocks):
        for index in fallback_candidates:
            blocks[index] = replace(blocks[index], kind="generic")
    return tuple(blocks)


def _plain_label(text: str) -> str:
    return text.strip().strip("*_`").strip()


def _heading(line: SourceLine, file_format: str) -> tuple[int, str] | None:
    atx = _ATX.match(line.text)
    if atx is not None:
        return len(atx[1]), _plain_label(atx[2])
    label = _plain_label(line.text)
    if line.heading_level is not None:
        return line.heading_level, label
    if file_format == "md" or line.table_row or not label:
        return None
    if _PART.fullmatch(label):
        return 2, label
    folded = label.lower().replace("ё", "е").rstrip(":")
    if folded in _PLAIN_HEADINGS:
        return (4 if folded in _SUBHEADINGS else 3), label
    return None


def _scope(headings: list[tuple[int, str]]) -> str | None:
    for _, label in reversed(headings):
        for kind, pattern in _SCOPES:
            if pattern.search(label):
                return kind
    return None


def _id(text: str) -> str | None:
    cleaned = _plain_label(_LIST_MARKER.sub("", text, count=1))
    found = _SOURCE_ID.match(cleaned)
    return found["id"] if found else None


def _table_fields(line: SourceLine, scope: str | None, file_format: str) -> list[str]:
    raw = line.text.strip()
    if not raw or _TABLE_SEPARATOR.fullmatch(raw):
        return []
    pipes = raw.startswith("|") or raw.endswith("|") or " | " in raw or raw.count("|") > 1
    if line.table_row or (pipes and "|" in raw and not raw.startswith(("{", "["))):
        return [_plain_label(cell) for cell in re.split(r"(?<!\\)\|", raw.strip("|"))]
    if (
        file_format == "pdf" and scope in {"acceptance", "api", "data", "governance", "context"}
        and not _LIST_MARKER.match(raw) and not raw.startswith(("{", "[", "`", "~"))
    ):
        fields = _COLUMNS.split(raw)
        if len(fields) > 1:
            return [_plain_label(cell) for cell in fields]
    return []


def _table_kind(
    scope: str | None, fields: list[str], is_header: bool, source_id: str | None
) -> str:
    if scope == "governance":
        return "governance"
    if not any(fields):
        return "governance"
    first = fields[0].lower().strip(":")
    label_header = first in _TABLE_LABELS and any(field.lower() in _COLUMN_LABELS for field in fields[1:])
    if is_header or label_header or first in _ADMIN_FIELDS:
        return "context"
    if scope == "example" or first in _EXAMPLE_FIELDS:
        return "example"
    if scope is not None and scope in _REQUIREMENT_KINDS:
        return scope
    if source_id or _MODAL.search(" ".join(fields)):
        return "generic"
    return "context"


def _fenced_end(lines: tuple[SourceLine, ...], start: int, fence: str) -> int:
    close = re.compile(r"^[ \t]{0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*$")
    for pos in range(start + 1, len(lines)):
        if close.fullmatch(lines[pos].text):
            return pos + 1
    return len(lines)


def _json_start(raw: str, scope: str | None) -> bool:
    return scope == "api" and (raw.startswith("{") or raw.startswith("[{") or raw == "[")


def _json_end(
    lines: tuple[SourceLine, ...], start: int, file_format: str, scope: str | None
) -> int:
    finish = _paragraph_end(lines, start, scope, file_format)
    candidate = "\n".join(line.text for line in lines[start:finish]).lstrip()
    try:
        result, offset = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return finish
    if isinstance(result, (list, dict)):
        return start + candidate.count("\n", 0, offset) + 1
    return finish


def _paragraph_end(
    lines: tuple[SourceLine, ...], start: int, scope: str | None, file_format: str
) -> int:
    for pos in range(start + 1, len(lines)):
        line = lines[pos]
        raw = line.text.strip()
        label = _plain_label(raw)
        if (
            not raw or line.page != lines[start].page or _DIVIDER.fullmatch(raw)
            or _heading(line, file_format) is not None or _FENCE.match(line.text)
            or _table_fields(line, scope, file_format) or _TABLE_SEPARATOR.fullmatch(raw)
            or _LIST_MARKER.match(raw) or _id(raw)
            or _STATUS_RULE.match(label) or _STATUS_LABEL.fullmatch(label)
        ):
            return pos
    return len(lines)


def _body_kind(scope: str | None, text: str, source_id: str | None) -> str:
    label = _plain_label(_LIST_MARKER.sub("", text, count=1))
    if scope == "governance" or _PLACEHOLDER.fullmatch(label):
        return "governance"
    if scope == "example" or label.lower().rstrip(":") in _EXAMPLE_WORDS or _STATUS_LABEL.fullmatch(label):
        return "example"
    if _CROSS_REFERENCE.match(label):
        return "context"
    if scope is not None and scope in _REQUIREMENT_KINDS:
        return scope
    if source_id or _MODAL.search(label):
        return "generic"
    return "context"
