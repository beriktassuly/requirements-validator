from copy import deepcopy
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
import pytest

from reqvalidator.config import Settings
from reqvalidator.document import Document, load_document
from reqvalidator.llm import ReviewError
from reqvalidator.report import render_json
from reqvalidator.review import ReviewEngine
from reqvalidator.rewrite import main, propose_rewrite
from reqvalidator.segment import segment_document
from tests.fakes import ScriptedReviewer, TWO_RULES


T = TypeVar("T", bound=BaseModel)
RewriteCase = tuple[Path, Document, dict[str, Any], dict[str, Any]]


class RewriteReviewer:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = iter(answers)
        self.prompts: list[str] = []

    def ask(self, system: str, prompt: str, schema: type[T]) -> T:
        assert "target_block_ids" in system
        self.prompts.append(prompt)
        return schema.model_validate(next(self.answers), strict=True)


@pytest.fixture
def rewrite_case(tmp_path: Path) -> RewriteCase:
    source = tmp_path / "требования.md"
    source.write_text(
        TWO_RULES
        + "\n## Общие сведения\n"
        "Служебный текст вне соседних блоков.\n"
        "\n## Критерии приёмки\n"
        "* **AC-1.** Проверяется число уведомлений после покупки.\n",
        encoding="utf-8",
    )
    document = load_document(source)
    report = ReviewEngine(ScriptedReviewer()).run(document, model="offline", provider="offline", version="test")
    inventory = {block.source_id: block for block in segment_document(document) if block.source_id}
    first, second, acceptance = (inventory[key].id for key in ("Ф-1", "Ф-2", "AC-1"))
    variants = []
    for label, assumption, expected in (
        ("A", "Push разрешён.", "Сервис отправляет один push."),
        ("B", "Push запрещён.", "Сервис не отправляет push."),
    ):
        variants.append({
            "label": label,
            "assumption": assumption,
            "target_block_ids": [first, second, acceptance],
            "proposed_text": f"После подтверждения покупки выполняется правило варианта {label}. {expected}",
            "acceptance_criteria": [{
                "given": "Оповещения включены.",
                "when": "Подтверждается покупка.",
                "then": expected,
            }],
            "expected_result": expected,
        })
    answer = {
        "needs_business_decision": True,
        "question_to_analyst": "При подтверждении покупки нужно отправлять push?",
        "variants": variants,
    }
    return source, document, json.loads(render_json(report)), answer


@pytest.mark.parametrize("problem", ["hash", "unknown_id", "duplicate_id", "changed_evidence"])
def test_rewrite_rejects_invalid_report_before_asking_model(rewrite_case: RewriteCase, problem: str) -> None:
    _, document, report, _ = rewrite_case
    finding_id = "D-001"
    if problem == "hash":
        report["document"]["sha256"] = "different"
        message = "SHA-256"
    elif problem == "unknown_id":
        finding_id = "D-999"
        message = "нет замечания"
    elif problem == "duplicate_id":
        report["findings"].append(deepcopy(report["findings"][0]))
        message = "несколько замечаний"
    else:
        report["findings"][0]["evidence"][0]["start_col"] += 1
        message = "не совпадают"

    reviewer = RewriteReviewer()
    with pytest.raises(ValueError, match=message):
        propose_rewrite(reviewer, document, report, finding_id)
    assert reviewer.prompts == []


@pytest.mark.parametrize("problem", ["target", "one_variant", "assumption", "result", "quote", "attribution"])
def test_rewrite_retries_invalid_content_and_rejects_it(rewrite_case: RewriteCase, problem: str) -> None:
    _, document, report, answer = rewrite_case
    first, second = answer["variants"]
    if problem == "target":
        first["target_block_ids"] = ["B9999"]
    elif problem == "one_variant":
        answer["variants"] = [first]
    elif problem == "assumption":
        second["assumption"] = first["assumption"].upper().removesuffix(".")
    elif problem == "result":
        second["expected_result"] = first["expected_result"]
    elif problem == "quote":
        quote = report["findings"][0]["evidence"][0]["quote"]
        first["proposed_text"] = f"«{quote}»"
    else:
        first["proposed_text"] = "Цитата из ТЗ: Сервис отправляет 999 push."

    reviewer = RewriteReviewer(answer, answer)
    with pytest.raises(ReviewError, match="формулировкой не прошёл проверку"):
        propose_rewrite(reviewer, document, report, "D-001")
    assert len(reviewer.prompts) == 2
    assert reviewer.prompts[1].startswith(reviewer.prompts[0] + "\n\nПовтор:")


def test_rewrite_recovers_after_an_unknown_edit_target(rewrite_case: RewriteCase) -> None:
    _, document, report, answer = rewrite_case
    invalid = deepcopy(answer)
    invalid["variants"][0]["target_block_ids"].append("B9999")
    reviewer = RewriteReviewer(invalid, answer)
    result = propose_rewrite(reviewer, document, report, "D-001")
    assert result.model_dump(mode="json") == answer
    assert len(reviewer.prompts) == 2


def test_rewrite_accepts_one_variant_for_already_decided_rule(rewrite_case: RewriteCase) -> None:
    _, document, report, answer = rewrite_case
    answer["needs_business_decision"] = False
    answer["question_to_analyst"] = None
    answer["variants"] = answer["variants"][:1]
    reviewer = RewriteReviewer(answer)

    result = propose_rewrite(reviewer, document, report, "D-001")
    assert result.model_dump(mode="json") == answer
    assert len(reviewer.prompts) == 1


def test_rewrite_cli_writes_separate_json_and_keeps_original(
    rewrite_case: RewriteCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    source, document, report, answer = rewrite_case
    input_report = tmp_path / "отчёт.json"
    original = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    input_report.write_bytes(original)
    output = tmp_path / "результаты" / "формулировка.json"
    reviewer = RewriteReviewer(answer)
    monkeypatch.setattr(Settings, "from_env", lambda **_: Settings("openai", "offline", "offline-key"))

    assert main(
        [str(input_report), "--finding", "D-001", "--document", str(source), "--output", str(output)],
        reviewer_factory=lambda _: reviewer,
    ) == 0
    streams = capsys.readouterr()
    assert streams.out == f"JSON сохранён: {output}\n"
    assert streams.err == ""
    assert input_report.read_bytes() == original
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "schema_version": 1, "finding_id": "D-001", "document_sha256": document.sha256, **answer,
    }
    (prompt,) = reviewer.prompts
    finding_json, _, document_json = prompt.partition("\n\nДокумент JSON:\n")
    detail = json.loads(finding_json)
    assert detail["evidence"] == report["findings"][0]["evidence"]
    assert "Служебный текст вне соседних блоков" not in prompt
    assert json.loads(document_json)["context_only"] == []
    (acceptance_id,) = detail["acceptance_locations"].values()
    assert acceptance_id in answer["variants"][0]["target_block_ids"]
    assert acceptance_id not in {item["block_id"] for item in detail["evidence"]}


def test_rewrite_cli_refuses_to_overwrite_report(
    rewrite_case: RewriteCase, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, _, report, _ = rewrite_case
    input_report = tmp_path / "отчёт.json"
    original = json.dumps(report, ensure_ascii=False).encode("utf-8")
    input_report.write_bytes(original)

    assert main([
        str(input_report), "--finding", "D-001", "--document", str(source), "--output", str(input_report),
    ]) == 2
    assert capsys.readouterr().out == ""
    assert input_report.read_bytes() == original
