"""Source anchoring and conservative finding consolidation."""

from bisect import bisect_right
from dataclasses import replace
from itertools import combinations
import re
from typing import Iterable

from .document import Document
from .model import Anchor, Finding, SEVERITY_RANK
from .segment import Block


def _normalize(text: str) -> tuple[str, list[int]]:
    parts: list[str] = []
    positions: list[int] = []
    for match in re.finditer(r"\S+", text):
        if parts:
            parts.append(" ")
            positions.append(positions[-1] + 1)
        parts.append(match.group())
        positions.extend(range(match.start(), match.end()))
    return "".join(parts), positions


def locate_quote(document: Document, block: Block, quote: str) -> Anchor | None:
    """Find a unique, case-sensitive quote, allowing only whitespace differences."""
    lines = document.lines[block.start_line - 1:block.end_line]
    if not lines:
        return None
    raw = "\n".join(line.text for line in lines)
    normalized, positions = _normalize(raw)
    expected, _ = _normalize(quote)
    if not expected:
        return None
    offset = normalized.find(expected)
    if offset < 0 or normalized.find(expected, offset + 1) >= 0:
        return None

    first = positions[offset]
    last = positions[offset + len(expected) - 1]
    line_starts = [0]
    for line in lines[:-1]:
        line_starts.append(line_starts[-1] + len(line.text) + 1)
    start = bisect_right(line_starts, first) - 1
    end = bisect_right(line_starts, last) - 1
    return Anchor(
        block_id=block.id,
        quote=raw[first:last + 1],
        start_line=lines[start].number,
        end_line=lines[end].number,
        start_col=first - line_starts[start] + 1,
        end_col=last - line_starts[end] + 1,
        page_start=lines[start].page,
        page_end=lines[end].page,
    )


def anchor_key(anchor: Anchor) -> tuple[int, int, int, int, str]:
    return anchor.start_line, anchor.start_col, anchor.end_line, anchor.end_col, anchor.block_id


def anchors_overlap(left: Anchor, right: Anchor) -> bool:
    return (
        (left.start_line, left.start_col) <= (right.end_line, right.end_col)
        and (right.start_line, right.start_col) <= (left.end_line, left.end_col)
    )


def _matched_anchors(left: Finding, right: Finding) -> bool:
    return any(anchors_overlap(a, b) for a in left.evidence for b in right.evidence)


def _all_anchors_overlap(left: Finding, right: Finding) -> bool:
    return all(any(anchors_overlap(a, b) for b in right.evidence) for a in left.evidence)


def _eligible(left: Finding, right: Finding) -> bool:
    cross_scope = set(left.scopes) != set(right.scopes)
    shared_ids = set(left.requirement_ids) & set(right.requirement_ids)
    if left.criterion != right.criterion:
        broad = {"contradiction", "ambiguity", "completeness", "verifiability"}
        if {left.criterion, right.criterion} - broad or not cross_scope:
            return False
    if left.criterion == right.criterion == "contradiction":
        return (
            _all_anchors_overlap(left, right) or _all_anchors_overlap(right, left)
        ) or (cross_scope and len(shared_ids) >= 2)
    return _matched_anchors(left, right) or (cross_scope and bool(shared_ids))


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def _exact_duplicate(left: Finding, right: Finding) -> bool:
    if left.criterion != right.criterion or not _matched_anchors(left, right):
        return False
    if (
        left.title.casefold().strip() == right.title.casefold().strip()
        and left.description.casefold().strip() == right.description.casefold().strip()
    ):
        return True
    if not (_all_anchors_overlap(left, right) and _all_anchors_overlap(right, left)):
        return False
    first = _words(f"{left.title} {left.description}")
    second = _words(f"{right.title} {right.description}")
    return len(first & second) >= 5 and len(first & second) / len(first | second) >= 0.86


def merge_candidates(findings: Iterable[Finding]) -> tuple[tuple[int, int], ...]:
    """Return one-based overlap pairs for which wording alone does not justify a merge."""
    return tuple(
        (i, j)
        for (i, left), (j, right) in combinations(enumerate(findings, start=1), 2)
        if _eligible(left, right) and not _exact_duplicate(left, right)
    )


def _merge_group(group: list[Finding]) -> Finding:
    primary = min(
        group,
        key=lambda f: (
            {"contradiction": 0, "verifiability": 1}.get(f.criterion, 2),
            SEVERITY_RANK[f.severity],
            -len(f.evidence),
        ),
    )
    anchors = tuple(sorted({anchor for f in group for anchor in f.evidence}, key=anchor_key))
    scopes = tuple(sorted({scope for f in group for scope in f.scopes}))
    criteria = tuple(sorted(
        {criterion for f in group for criterion in (f.criterion, *f.related_criteria)}
        - {primary.criterion}
    ))
    return replace(
        primary,
        severity=min((f.severity for f in group), key=SEVERITY_RANK.__getitem__),
        requirement_ids=tuple(sorted({rid for f in group for rid in f.requirement_ids})),
        evidence=anchors,
        scopes=scopes,
        related_criteria=criteria,
    )


def deduplicate(
    findings: Iterable[Finding],
    semantic_pairs: Iterable[tuple[int, int]] = (),
) -> tuple[Finding, ...]:
    items = tuple(findings)
    semantic = {tuple(sorted(pair)) for pair in semantic_pairs}
    compatible = {
        (i, j)
        for (i, left), (j, right) in combinations(enumerate(items, start=1), 2)
        if _eligible(left, right) and (_exact_duplicate(left, right) or (i, j) in semantic)
    }
    groups: list[list[int]] = []
    for index in range(1, len(items) + 1):
        group = next((g for g in groups if all((other, index) in compatible for other in g)), None)
        if group is None:
            groups.append([index])
        else:
            group.append(index)

    merged = [_merge_group([items[i - 1] for i in group]) for group in groups]
    merged.sort(
        key=lambda f: (SEVERITY_RANK[f.severity], anchor_key(f.evidence[0]), f.criterion, f.title)
    )
    return tuple(replace(f, id=f"D-{i:03d}") for i, f in enumerate(merged, start=1))
