from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from reqvalidator.cli import main
from reqvalidator.document import load_document
from reqvalidator.model import Readiness
from reqvalidator.report import render_markdown
from reqvalidator.review import ReviewEngine, assess_readiness
from tests.fakes import ScriptedReviewer, write_rule_fixture


REQUIRED_HEADINGS = (
    "Паспорт прогона", "Сводка", "Как нарезаны требования", "Проверка каждого требования",
    "Проверка набора целиком", "Реестр замечаний", "Готовность к разработке и тестированию",
    "Приоритет исправлений",
)


def test_review_retries_incomplete_stage_and_drops_unproven_findings(tmp_path: Path) -> None:
    path = write_rule_fixture(tmp_path / "input.md")
    reviewer = ScriptedReviewer(reject_first_local=True, add_bad_proposal=True)
    report = ReviewEngine(reviewer).run(
        load_document(path), model="offline-fixture", provider="offline", version="test-v1"
    )

    assert reviewer.calls["LocalResponse"] == 2
    assert reviewer.calls["SetResponse"] == 1
    assert {item.block.source_id for item in report.requirements} == {"Ф-1", "Ф-2"}
    assert report.discarded_proposals == 3
    assert report.merged_proposals == 1
    (finding,) = report.findings
    assert finding.criterion == "contradiction"
    assert finding.scopes == ("local", "set")
    assert len(finding.requirement_ids) == len(finding.evidence) == 2
    assert {anchor.start_line for anchor in finding.evidence} == {3, 4}
    assert finding.scenario == "Приходит подтверждённая покупка по отслеживаемой карте."
    assert report.readiness == Readiness("not_ready", "not_ready")

    report_text = render_markdown(report)
    assert all(f"## {heading}" in report_text for heading in REQUIRED_HEADINGS)
    for required in ("offline-fixture", "test-v1", "Ф-1", "Ф-2", "D-001", "сервис никогда не отправляет push"):
        assert required in report_text
    assert "магический номер 9000" not in report_text


def test_verdict_is_deterministic_and_differentiates_testing(tmp_path: Path) -> None:
    path = write_rule_fixture(tmp_path / "input.md")
    report = ReviewEngine(ScriptedReviewer()).run(
        load_document(path), model="offline-fixture", provider="offline", version="test-v1"
    )
    (critical,) = report.findings
    major_verifiability = replace(critical, severity="major", criterion="verifiability")
    major_completeness = replace(critical, severity="major", criterion="completeness")
    minor = replace(critical, severity="minor")

    assert assess_readiness((), True) == Readiness("ready", "ready")
    assert assess_readiness((minor,), True) == Readiness("ready", "ready")
    assert assess_readiness((major_completeness,), True) == Readiness("conditional", "conditional")
    assert assess_readiness((major_verifiability,), True) == Readiness("conditional", "not_ready")
    assert assess_readiness((minor, critical), False) == Readiness("not_ready", "not_ready")
    assert assess_readiness((critical, minor), True) == Readiness("not_ready", "not_ready")
    assert assess_readiness((), False) == Readiness("not_confirmed", "not_confirmed")


def _configure_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("REQVALIDATOR_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("REQVALIDATOR_PROVIDER", "openai")
    monkeypatch.setenv("REQVALIDATOR_MODEL", "offline-fixture")
    monkeypatch.setenv("REQVALIDATOR_API_KEY", "offline-fixture-secret")
    monkeypatch.setenv("REQVALIDATOR_MAX_INPUT_CHARS", "100000")


def test_cli_accepts_paths_and_writes_markdown_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_offline(monkeypatch, tmp_path)
    source = write_rule_fixture(tmp_path / "входной файл.md")
    markdown = tmp_path / "results" / "отчёт.md"
    data = tmp_path / "results" / "отчёт.json"

    exit_code = main([str(source), str(markdown), "--json-output", str(data)], reviewer_factory=lambda _: ScriptedReviewer())
    assert exit_code == 0
    assert "## Паспорт прогона" in markdown.read_text(encoding="utf-8")
    payload = json.loads(data.read_text(encoding="utf-8"))
    assert payload["document"]["sha256"] == load_document(source).sha256
    assert payload["model"] == "offline-fixture"
    assert payload["readiness"] == {"development": "not_ready", "testing": "not_ready"}
    assert len(payload["findings"]) == 1
    assert "offline-fixture-secret" not in data.read_text(encoding="utf-8")


def test_cli_rejects_missing_input_without_opening_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure_offline(monkeypatch, tmp_path)
    missing = tmp_path / "missing.pdf"
    output = tmp_path / "report.md"

    def should_not_create_provider(_: object) -> ScriptedReviewer:
        pytest.fail("Вызов модели при несуществующем входном файле.")

    assert main([str(missing), str(output)], reviewer_factory=should_not_create_provider) == 2
    assert not output.exists()
    assert "не найден" in capsys.readouterr().err.lower()


def test_uncertainty_has_question_without_marking_coverage_incomplete(tmp_path: Path) -> None:
    from reqvalidator.model import Clarification, ClarificationResponse, LocalResponse, SetResponse

    class UncertainReviewer(ScriptedReviewer):
        def ask(self, system, prompt, schema):
            if schema is ClarificationResponse:
                targets, _ = json.JSONDecoder().raw_decode(prompt)
                return ClarificationResponse(items=[
                    Clarification(target=target, resolved=False, question="Как измерять задержку?")
                    for target in targets
                ])
            response = super().ask(system, prompt, schema)
            if schema is LocalResponse:
                for item in response.items:
                    for check in item.checks:
                        if check.criterion == "verifiability":
                            check.status = "uncertain"
            if schema is SetResponse:
                for check in response.links:
                    check.status = "uncertain"
            return response

    source = write_rule_fixture(tmp_path / "input.md")
    report = ReviewEngine(UncertainReviewer()).run(
        load_document(source), model="offline", provider="offline", version="test",
    )
    rendered = render_markdown(report)
    matrix = rendered.split("## Проверка каждого требования", 1)[1].split("## Проверка набора", 1)[0]
    rows = [line for line in matrix.splitlines() if line.startswith("| B")]
    assert report.coverage_complete and report.failed_stages == 0
    assert len(rows) == len(report.requirements) == 2
    assert "[D-001](#d-001)" in matrix and "| ? |" in matrix
    assert "Покрытие проверок — полное" in rendered
    assert "Вопросы к аналитику" in rendered and "Как измерять задержку?" in rendered


def test_markdown_lists_cited_context_outside_requirement_registry(tmp_path: Path) -> None:
    from reqvalidator.segment import Block

    source = write_rule_fixture(tmp_path / "input.md")
    report = ReviewEngine(ScriptedReviewer()).run(
        load_document(source), model="offline", provider="offline", version="test",
    )
    example = Block("B109", "Примеры API", "example", 10, 10, "result: ERROR")
    rendered = render_markdown(replace(report, context_blocks=(example,)))
    context = rendered.split("### Контекстные блоки", 1)[1].split("## Проверка каждого", 1)[0]
    assert "| B109 | Пример / Примеры API | 10–10 | result: ERROR |" in context
