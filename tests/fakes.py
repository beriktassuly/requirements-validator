from collections import Counter
import json
from pathlib import Path
import re
from typing import TypeVar, cast

from pydantic import BaseModel

from reqvalidator.model import (
    LOCAL_CRITERIA,
    RELATIONS,
    SET_CRITERIA,
    Check,
    Clarification,
    ClarificationResponse,
    EvidenceHint,
    LinkCheck,
    LocalItem,
    LocalResponse,
    MergeResponse,
    Proposal,
    SetResponse,
)


T = TypeVar("T", bound=BaseModel)
TWO_RULES = (
    "# Оповещения по карте\n"
    "## Функциональные требования\n"
    "* **Ф-1.** После подтверждённой покупки сервис всегда отправляет push.\n"
    "* **Ф-2.** После подтверждённой покупки сервис никогда не отправляет push.\n"
)


def write_rule_fixture(path: Path) -> Path:
    path.write_text(TWO_RULES, encoding="utf-8")
    return path


class ScriptedReviewer:
    """Respond with typed data and never call a network client."""

    def __init__(self, *, reject_first_local: bool = False, add_bad_proposal: bool = False) -> None:
        self.reject_first_local = reject_first_local
        self.add_bad_proposal = add_bad_proposal
        self.calls: Counter[str] = Counter()

    def __enter__(self) -> "ScriptedReviewer":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def ask(self, system: str, prompt: str, schema: type[T]) -> T:
        assert system.strip()
        self.calls[schema.__name__] += 1
        if schema is MergeResponse:
            return cast(T, MergeResponse(pairs=[]))

        if schema is ClarificationResponse:
            targets, _ = json.JSONDecoder().raw_decode(prompt)
            return cast(T, ClarificationResponse(items=[
                Clarification(target=target, resolved=False, question=question)
                for target, question in targets.items()
            ]))

        source = prompt.partition("Документ JSON:\n")[2]
        inventory, _ = json.JSONDecoder().raw_decode(source.lstrip())
        first = next(block["id"] for block in inventory["blocks"] if block["source_id"] == "Ф-1")
        second = next(block["id"] for block in inventory["blocks"] if block["source_id"] == "Ф-2")
        conflict = Proposal(
            criterion="contradiction",
            severity="critical",
            title="Правила отправки push противоречат друг другу",
            description="В одном событии операция одновременно требует отправить push и запрещает это.",
            recommendation="Согласовать одно правило отправки push после подтверждения покупки.",
            scenario="Приходит подтверждённая покупка по отслеживаемой карте.",
            evidence=[
                EvidenceHint(block_id=first, quote="сервис всегда отправляет push"),
                EvidenceHint(block_id=second, quote="сервис никогда не отправляет push"),
            ],
        )
        if schema is LocalResponse:
            if self.reject_first_local and self.calls[schema.__name__] == 1:
                return cast(T, LocalResponse(items=[]))
            match = re.search(r"Целевые ID:\s*(\[[^\n]*\])", prompt)
            assert match is not None
            target_ids = json.loads(match[1])
            return cast(T, LocalResponse(items=[
                LocalItem(
                    requirement_id=identifier,
                    checks=[
                        Check(criterion=criterion, status=(
                            "issue" if criterion == "contradiction" and identifier == first else "pass"
                        ))
                        for criterion in LOCAL_CRITERIA
                    ],
                    findings=[conflict] if identifier == first else [],
                )
                for identifier in target_ids
            ]))
        if schema is SetResponse:
            findings = [conflict]
            if self.add_bad_proposal:
                findings.append(Proposal(
                    criterion="completeness", severity="critical", title="Нет магического номера",
                    description="В тексте якобы есть число 9000.", recommendation="Убрать число.",
                    scenario=None,
                    evidence=[EvidenceHint(block_id=first, quote="магический номер 9000")],
                ))
                findings.append(conflict.model_copy(update={"scenario": None}))
                findings.append(conflict.model_copy(update={"evidence": [conflict.evidence[0]]}))
            return cast(T, SetResponse(
                checks=[
                    Check(criterion=criterion, status="issue" if criterion == "contradiction" else "pass")
                    for criterion in SET_CRITERIA
                ],
                links=[LinkCheck(relation=relation, status="not_applicable") for relation in RELATIONS],
                findings=findings,
            ))
        raise AssertionError(f"Неожиданный ответ {schema.__name__}")
