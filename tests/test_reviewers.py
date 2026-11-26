import json
from pathlib import Path
from typing import cast

import pytest

from reqvalidator.cli import main
from reqvalidator.document import load_document
from reqvalidator.model import Clarification, ClarificationResponse, EvidenceHint, Proposal
from reqvalidator.review import ReviewEngine, ReviewLimitError, _payload
from reqvalidator.reviewers import SpecialistResponse
from reqvalidator.segment import segment_document
from tests.fakes import ScriptedReviewer, T, write_rule_fixture
from tests.test_review_cli import _configure_offline


class SpecialistStub(ScriptedReviewer):
    def __init__(self, *, reject_first: bool = False) -> None:
        super().__init__()
        self.reject_first = reject_first
        self.specialists: list[tuple[str, tuple[str, ...]]] = []

    def ask(self, system: str, prompt: str, schema: type[T]) -> T:
        if schema is ClarificationResponse:
            targets, _ = json.JSONDecoder().raw_decode(prompt)
            return cast(T, ClarificationResponse(items=[
                Clarification(target=target, resolved=False, question=None) for target in targets
            ]))
        if schema is not SpecialistResponse:
            return super().ask(system, prompt, schema)
        source = prompt.partition("Документ JSON:\n")[2]
        inventory, _ = json.JSONDecoder().raw_decode(source)
        if prompt.startswith("Передан фрагмент документа."):
            assert inventory["context_only"] == []
        blocks = inventory["blocks"]
        self.specialists.append((system, tuple(block["id"] for block in blocks)))
        rules = {block["source_id"]: block["id"] for block in blocks if block["source_id"]}
        if "Роль QA." not in system or not {"Ф-1", "Ф-2"} <= rules.keys():
            return cast(T, SpecialistResponse(findings=[]))
        if self.reject_first and len(self.specialists) == 1:
            assert "Лимит находок" in system
        conflict = Proposal(
            criterion="contradiction", severity="critical",
            title="Правила отправки push противоречат друг другу",
            description="В одном событии операция одновременно требует отправить push и запрещает это.",
            recommendation="Согласовать одно правило отправки push после подтверждения покупки.",
            scenario="Приходит подтверждённая покупка по отслеживаемой карте.",
            evidence=[
                EvidenceHint(block_id=rules["Ф-1"], quote="сервис всегда отправляет push"),
                EvidenceHint(block_id=rules["Ф-2"], quote="сервис никогда не отправляет push"),
            ],
        )
        bogus = conflict.model_copy(update={
            "evidence": [EvidenceHint(block_id=rules["Ф-1"], quote="цитата, которой нет")],
        })
        findings = [conflict, bogus]
        if self.reject_first and len(self.specialists) == 1:
            findings.append(bogus)
        return cast(T, SpecialistResponse(findings=findings))


def test_cli_reviewers_labels_merge_and_reject_unproven_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_offline(monkeypatch, tmp_path)
    source = write_rule_fixture(tmp_path / "input.md")
    markdown = tmp_path / "review.md"
    data = tmp_path / "review.json"
    reviewer = SpecialistStub()
    assert main(
        [str(source), str(markdown), "--json-output", str(data), "--reviewers", "qa,api"],
        reviewer_factory=lambda _: reviewer,
    ) == 0
    (call,) = reviewer.specialists
    assert "Роль QA." in call[0]
    assert call[1] == tuple(block.id for block in segment_document(load_document(source)))
    payload = json.loads(data.read_text(encoding="utf-8"))
    (finding,) = payload["findings"]
    assert finding["origins"] == ["main", "qa"]
    assert len(finding["evidence"]) == 2
    assert payload["discarded_proposals"] == 1
    assert "Ревьюеры — main, qa" in markdown.read_text(encoding="utf-8")


def test_api_reviewer_runs_only_for_a_contract_and_qa_cap_retries(tmp_path: Path) -> None:
    source = write_rule_fixture(tmp_path / "input.md")
    with source.open("a", encoding="utf-8") as stream:
        stream.write("\n## API\n\nGET /settings возвращает состояние оповещений.\n")
    reviewer = SpecialistStub(reject_first=True)
    report = ReviewEngine(reviewer, reviewers=("qa", "api"), reviewer_max_findings=2).run(
        load_document(source), model="offline", provider="offline", version="test",
    )
    assert [next(role for role in ("QA", "интегратора") if f"Роль {role}." in system)
            for system, _ in reviewer.specialists] == ["QA", "QA", "интегратора"]
    assert report.discarded_proposals == 1
    assert any(finding.origins == ("main", "qa") for finding in report.findings)


def test_specialist_falls_back_to_existing_blocks_without_full_context(tmp_path: Path) -> None:
    source = write_rule_fixture(tmp_path / "input.md")
    with source.open("a", encoding="utf-8") as stream:
        for index in range(12):
            stream.write(f"\n* **Ф-{index + 3}.** " + ("Значение обязательно. " * 25) + "\n")
    document = load_document(source)
    blocks = segment_document(document)
    payload = _payload(document, blocks)
    reviewer = SpecialistStub()
    limits = {"max_input_chars": len(payload) // 2, "local_batch_size": 2}
    passport = {"model": "offline", "provider": "offline", "version": "test"}
    with pytest.raises(ReviewLimitError, match="Полный документ не помещается"):
        ReviewEngine(reviewer, **limits).run(document, **passport)
    report = ReviewEngine(reviewer, **limits, reviewers=("qa",)).run(document, **passport)

    assert len(reviewer.specialists) > 1
    assert tuple(identifier for _, ids in reviewer.specialists for identifier in ids) == tuple(
        block.id for block in blocks
    )
    assert not report.coverage_complete and report.failed_stages == 0
    assert reviewer.calls["LocalResponse"] == reviewer.calls["SetResponse"] == 0
    assert report.findings[0].origins == ("qa",)
