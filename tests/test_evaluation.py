from dataclasses import asdict
import json
from pathlib import Path

import pytest

from evaluation.score import score, verify_anchor
from reqvalidator.document import load_document
from reqvalidator.ground import locate_quote
from reqvalidator.segment import segment_document
from tests.fakes import write_rule_fixture


def test_metrics_require_all_distinct_verified_places(tmp_path: Path) -> None:
    path = write_rule_fixture(tmp_path / "input.md")
    document = load_document(path)
    inventory = {block.source_id: block for block in segment_document(document) if block.source_id}
    first = locate_quote(document, inventory["Ф-1"], "сервис всегда отправляет push")
    second = locate_quote(document, inventory["Ф-2"], "сервис никогда не отправляет push")
    assert first is not None and second is not None
    anchors = [asdict(first), asdict(second)]
    one_place = [anchors[0], anchors[0]]
    wrong_place = [{**anchors[0], "start_line": 1}, anchors[1]]

    def finding(identifier: str, evidence: list[dict]) -> dict:
        return {
            "id": identifier, "criterion": "contradiction", "title": "Противоречие при push",
            "description": "После покупки отправка одновременно обязательна и запрещена.",
            "scenario": "Одна подтверждённая покупка.", "evidence": evidence,
        }

    gold = {
        "defects": [{
            "id": "EX-01", "core": True, "title": "Один триггер с обратными предписаниями",
            "anchors": [["всегда отправляет"], ["никогда не отправляет"]],
            "concepts": [["push"], ["запрещена", "запрещено"]],
        }],
    }
    report = {
        "document": {"sha256": document.sha256},
        "coverage_complete": True,
        "findings": [
            finding("D-001", anchors), finding("D-002", anchors),
            finding("D-003", one_place), finding("D-004", wrong_place),
        ],
    }
    metrics = score(document, report, gold, {"D-004": "false_positive"})
    assert metrics["recall"] == metrics["core_recall"] == 1.0
    assert metrics["gold_results"][0]["finding_ids"] == ["D-001", "D-002"]
    assert metrics["duplicates"] == 1
    assert metrics["duplicate_ids"] == ["D-002"]
    assert metrics["confirmed_anchor_ratio"] == 7 / 8
    assert metrics["fully_grounded_finding_ratio"] == 3 / 4
    assert metrics["unmatched_count"] == 2
    assert metrics["potential_false_positives"] == 1
    assert metrics["confirmed_false_positives"] == 1
    assert verify_anchor(document, anchors[0])
    assert not verify_anchor(document, {**anchors[0], "quote": "разрешено заменять цитаты"})

    no_full_hit = {**report, "findings": report["findings"][2:]}
    assert score(document, no_full_hit, gold)["recall"] == 0.0
    with pytest.raises(ValueError, match="SHA-256"):
        score(document, {**report, "document": {"sha256": "other"}}, gold)


def test_reference_has_extra_targets_and_unique_identifiers() -> None:
    path = Path(__file__).parents[1] / "evaluation" / "gold.json"
    gold = json.loads(path.read_text(encoding="utf-8"))
    defects = gold["defects"]
    assert len(defects) >= 12
    assert len({item["id"] for item in defects}) == len(defects)
    assert {"G01", "G02", "G03", "G04"} <= {item["id"] for item in defects if item["core"]}
    assert sum(not item["core"] for item in defects) >= 8


def test_semantic_matching_uses_verified_block_candidates(tmp_path: Path) -> None:
    from evaluation.score import SemanticMatch, SemanticMatches

    document = load_document(write_rule_fixture(tmp_path / "input.md"))
    inventory = {block.source_id: block for block in segment_document(document) if block.source_id}
    left = locate_quote(document, inventory["Ф-1"], "сервис всегда отправляет push")
    right = locate_quote(document, inventory["Ф-2"], "сервис никогда не отправляет push")
    assert left and right
    gold = {"defects": [{
        "id": "EX-01", "core": True, "title": "Несовместимые условия оповещения",
        "anchors": [["всегда отправляет"], ["никогда не отправляет"]],
        "concepts": [["неосуществимо одновременно"]],
    }]}
    report = {
        "document": {"sha256": document.sha256},
        "findings": [
            {"id": "D-001", "title": "Уведомление одновременно нужно и запрещено",
             "evidence": [asdict(left), asdict(right)]},
            {"id": "D-002", "title": "Похожее замечание с одной ссылкой", "evidence": [asdict(left)]},
        ],
    }

    class Reviewer:
        def ask(self, system, prompt, schema):
            (entry,) = json.loads(prompt)
            assert [candidate["id"] for candidate in entry["candidates"]] == ["D-001"]
            return SemanticMatches(matches=[SemanticMatch(gold_id="EX-01", finding_ids=["D-001"])])

    metrics = score(document, report, gold, semantic_reviewer=Reviewer())
    assert metrics["gold_found"] == 1 and metrics["lexical_gold_found"] == 0
