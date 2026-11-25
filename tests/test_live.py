from pathlib import Path
from typing import Literal

import pytest

from reqvalidator.config import Settings
from reqvalidator.document import load_document
from reqvalidator.llm import OpenAIReviewer
from reqvalidator.model import StrictSchema
from reqvalidator.review import ReviewEngine
from tests.fakes import write_rule_fixture


pytestmark = pytest.mark.live


class _ConnectionProbe(StrictSchema):
    status: Literal["ok", "error"]


def test_live_provider_returns_validated_schema() -> None:
    settings = Settings.from_env()
    with OpenAIReviewer(settings) as reviewer:
        response = reviewer.ask(
            "Проверяй только возможность обмена структурированным ответом.",
            'Верни JSON-объект со значением "ok" в поле status.',
            _ConnectionProbe,
        )
    assert response.status == "ok"


def test_live_review_finds_a_plain_conflict_and_grounds_both_rules(tmp_path: Path) -> None:
    settings = Settings.from_env()
    path = write_rule_fixture(tmp_path / "review.md")
    with OpenAIReviewer(settings) as reviewer:
        output = ReviewEngine(reviewer, max_input_chars=settings.max_input_chars).run(
            load_document(path), model=settings.model, provider=settings.provider, version="live-test"
        )

    assert output.failed_stages == 0
    assert {item.block.source_id for item in output.requirements} == {"Ф-1", "Ф-2"}
    assert any(
        finding.criterion == "contradiction"
        and finding.scenario
        and {3, 4} <= {anchor.start_line for anchor in finding.evidence}
        for finding in output.findings
    )
