"""Score generated JSON against an independently authored reference."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

from reqvalidator.document import Document, DocumentError, load_document


DEFAULT_GOLD = Path(__file__).with_name("gold.json")


def _semantic(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", text))


def _whitespace(text: str) -> str:
    return " ".join(text.split())


def verify_anchor(document: Document, anchor: dict[str, Any]) -> bool:
    quote = anchor.get("quote")
    start, end = anchor.get("start_line"), anchor.get("end_line")
    first_col, last_col = anchor.get("start_col"), anchor.get("end_col")
    coordinates = (start, end, first_col, last_col)
    if not isinstance(quote, str) or not quote.strip() or not all(type(n) is int for n in coordinates):
        return False
    if not 1 <= start <= end <= len(document.lines):
        return False

    selected = document.lines[start - 1 : end]
    if not 1 <= first_col <= len(selected[0].text) or not 1 <= last_col <= len(selected[-1].text):
        return False
    if selected[0].page != anchor.get("page_start") or selected[-1].page != anchor.get("page_end"):
        return False
    pieces = [line.text for line in selected]
    pieces[-1] = pieces[-1][:last_col]
    pieces[0] = pieces[0][first_col - 1 :]
    return _whitespace("\n".join(pieces)) == _whitespace(quote)


def _has(text: str, terms: list[str]) -> bool:
    return any(_semantic(term) in text for term in terms)


def _has_quote(quote: str, alternatives: list[str]) -> bool:
    normalized = _semantic(quote)
    for alternative in alternatives:
        term = _semantic(alternative)
        if term and term in normalized:
            return True
        if not term and _whitespace(alternative) == _whitespace(quote):
            return True
    return False


Location = tuple[int, int, int, int]


def _overlap(left: Location, right: Location) -> bool:
    return left[:2] <= right[2:] and right[:2] <= left[2:]


def _distinct_anchors(verified_quotes: dict[Location, str], groups: list[list[str]]) -> bool:
    choices = [
        [location for location, quote in verified_quotes.items() if _has_quote(quote, alternatives)]
        for alternatives in groups
    ]
    choices.sort(key=len)

    def assign(at: int, used: tuple[Location, ...]) -> bool:
        if at == len(choices):
            return True
        return any(
            assign(at + 1, (*used, location))
            for location in choices[at] if not any(_overlap(location, previous) for previous in used)
        )

    return assign(0, ())


def _shape(group: object, *, allow_markup_only: bool = False) -> bool:
    return isinstance(group, list) and bool(group) and all(
        isinstance(term, str) and bool(_whitespace(term) if allow_markup_only else _semantic(term))
        for term in group
    )


def _reference_items(gold: dict[str, Any]) -> list[dict[str, Any]]:
    defects = gold.get("defects")
    if not isinstance(defects, list) or not defects:
        raise ValueError("В эталоне ожидается непустой массив defects.")

    ids: set[str] = set()
    for defect in defects:
        if not isinstance(defect, dict):
            raise ValueError("Запись defects должна быть объектом.")
        identifier = defect.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("Каждая запись эталона должна иметь уникальный id.")
        ids.add(identifier)
        for field in ("anchors", "concepts"):
            groups = defect.get(field)
            if not isinstance(groups, list) or not groups or not all(
                _shape(group, allow_markup_only=field == "anchors") for group in groups
            ):
                raise ValueError(f"У дефекта {identifier} не заданы группы {field}.")
    return defects


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score(
    document: Document,
    report: dict[str, Any],
    gold: dict[str, Any],
    adjudications: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Match grounded findings to the reference. Unmatched findings still need expert review."""
    source = report.get("document")
    if not isinstance(source, dict) or source.get("sha256") != document.sha256:
        raise ValueError("SHA-256 в отчёте отличается от входного файла. Передайте точный файл прогона.")
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise ValueError("В отчёте ожидается массив findings.")
    references = _reference_items(gold)

    findings_by_id: dict[str, dict[str, Any]] = {}
    matched: dict[str, list[str]] = defaultdict(list)
    matched_findings: set[str] = set()
    anchored_count = 0
    anchors_total = 0
    anchors_valid = 0
    exact_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)

    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Запись findings должна быть объектом.")
        identifier = finding.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in findings_by_id:
            raise ValueError("Каждое замечание должно иметь уникальный id.")
        findings_by_id[identifier] = finding

        evidence = finding.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(f"У замечания {identifier} ожидается массив evidence.")
        anchors_total += len(evidence)
        verified = [anchor for anchor in evidence if isinstance(anchor, dict) and verify_anchor(document, anchor)]
        anchors_valid += len(verified)
        if not evidence or len(verified) != len(evidence):
            continue
        anchored_count += 1

        content = _semantic(
            " ".join(
                str(finding.get(field) or "")
                for field in ("title", "description", "scenario")
            )
        )
        # Repeating an identical source location in a finding cannot satisfy two required anchors.
        unique_quotes = {
            (anchor["start_line"], anchor["start_col"], anchor["end_line"], anchor["end_col"]):
                anchor["quote"]
            for anchor in verified
        }
        for defect in references:
            if all(_has(content, group) for group in defect["concepts"]) and _distinct_anchors(
                unique_quotes, defect["anchors"]
            ):
                matched[defect["id"]].append(identifier)
                matched_findings.add(identifier)

        if content:
            key = (str(finding.get("criterion", "")), content, str(sorted(unique_quotes)))
            exact_groups[key].append(identifier)

    decisions = adjudications or {}
    unknown = sorted(set(decisions) - set(findings_by_id))
    if unknown or any(value not in ("valid", "false_positive") for value in decisions.values()):
        raise ValueError("Разбор замечаний должен содержать известный ID и значение valid или false_positive.")

    unmatched = [identifier for identifier in findings_by_id if identifier not in matched_findings]
    duplicates = {
        identifier
        for group in [*matched.values(), *exact_groups.values()]
        for identifier in group[1:]
    }
    core = [defect for defect in references if defect.get("core") is True]
    core_matches = sum(bool(matched[defect["id"]]) for defect in core)
    return {
        "coverage_complete": report.get("coverage_complete"),
        "recall": _ratio(sum(bool(group) for group in matched.values()), len(references)),
        "core_recall": _ratio(core_matches, len(core)),
        "gold_total": len(references),
        "gold_found": sum(bool(group) for group in matched.values()),
        "gold_core_total": len(core),
        "gold_core_found": core_matches,
        "gold_results": [
            {
                "id": defect["id"],
                "title": defect.get("title"),
                "finding_ids": matched.get(defect["id"], []),
            }
            for defect in references
        ],
        "findings_count": len(findings),
        "fully_grounded_findings": anchored_count,
        "fully_grounded_finding_ratio": _ratio(anchored_count, len(findings)),
        "anchors_count": anchors_total,
        "confirmed_anchors": anchors_valid,
        "confirmed_anchor_ratio": _ratio(anchors_valid, anchors_total),
        "duplicates": len(duplicates),
        "duplicate_ids": sorted(duplicates),
        "unmatched_count": len(unmatched),
        "unmatched_findings": [
            {"id": identifier, "title": findings_by_id[identifier].get("title")} for identifier in unmatched
        ],
        "potential_false_positives": sum(identifier not in decisions for identifier in unmatched),
        "confirmed_false_positives": (
            sum(value == "false_positive" for value in decisions.values()) if adjudications is not None else None
        ),
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Ожидается JSON-объект в {path}.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Независимая оценка экспортированного JSON-отчёта.")
    parser.add_argument("--document", type=Path, required=True, help="тот же входной файл, что у агента")
    parser.add_argument("--report", type=Path, required=True, help="JSON из --json-output")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="эталонная разметка")
    parser.add_argument("--output", type=Path, help="путь для записи JSON-метрик")
    parser.add_argument("--adjudications", type=Path, help="объект JSON {id: valid | false_positive}")
    args = parser.parse_args()
    try:
        if args.output and args.output.resolve() in {
            path.resolve() for path in (args.document, args.report, args.gold, args.adjudications) if path is not None
        }:
            raise ValueError("Файл метрик должен отличаться от входных файлов.")
        metrics = score(
            load_document(args.document),
            _read_json(args.report),
            _read_json(args.gold),
            _read_json(args.adjudications) if args.adjudications else None,
        )
        encoded = json.dumps(metrics, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
    except (DocumentError, OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
