"""Model-agnostic review pipeline. It never imports the evaluation corpus."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
from typing import TypeVar

from pydantic import BaseModel

from . import prompts
from .config import ReasoningEffort
from .document import Document
from .external import check_external_docs
from .ground import anchors_overlap, deduplicate, locate_quote, merge_candidates, prune_anchors
from .llm import OpenAIReviewer, Reviewer, ReviewError
from .model import (
    LOCAL_CRITERIA, RELATIONS, SET_CRITERIA, Check, ClarificationResponse, Criterion, DocumentInfo,
    EvidenceHint, Finding, LinkCheck, LocalResponse, MergeResponse, Proposal,
    Readiness, Relation, RequirementResult, ReviewReport, ReviewRole, Scope, SetResponse,
)
from .reviewers import SPECIALISTS, SpecialistResponse
from .segment import Block, segment_document
from .timing import RetryKind, RunTimings


class ReviewLimitError(ValueError):
    """A complete review is impossible for the supplied input and budget."""


T = TypeVar("T", bound=BaseModel)
UTC_PLUS_FIVE = timezone(timedelta(hours=5))
MISSING_ADMIN = re.compile(
    r"не заполнен[аоы]?|не указана?|не проставлен[аоы]?|заполня[а-я]+ тестировщиками|"
    r"not filled|not specified|\btbd\b",
    re.IGNORECASE,
)
ADMIN_RULES = (
    (
        re.compile(r"верси[яию]|revision|version|дата документа", re.IGNORECASE),
        "Нет версии или даты спецификации",
        "По записи нельзя определить актуальную редакцию, на которой основаны реализация и тесты.",
        "Записать версию и дату рассмотренной редакции.",
    ),
    (
        re.compile(r"чек.?лист|checklist|отметки", re.IGNORECASE),
        "Не зафиксирован результат проверки по чек-листу",
        "Шаблон не показывает, какие проверки выполнены и какие решения ещё открыты.",
        "Зафиксировать результаты проверок и объяснить пункты, которые неприменимы.",
    ),
    (
        re.compile(r"тестировщик|тестирован|test", re.IGNORECASE),
        "Раздел тестирования оставлен на заполнение",
        "Раздел не определяет, кто и по каким проверкам подтверждает готовность спецификации.",
        "Согласовать объём тестирования и сослаться на проверяемые критерии приёмки.",
    ),
)

LINK_KINDS: dict[Relation, tuple[set[str], set[str]]] = {
    "functional_acceptance": ({"functional"}, {"acceptance"}),
    "business_api": ({"functional", "integration", "process"}, {"api", "example"}),
    "data_api": ({"data"}, {"api", "example"}),
    "status_examples": ({"api"}, {"example"}),
}


def assess_readiness(findings: Iterable[Finding], coverage_complete: bool) -> Readiness:
    items = tuple(findings)
    if any(f.severity == "critical" for f in items):
        return Readiness("not_ready", "not_ready")
    if not coverage_complete:
        return Readiness("not_confirmed", "not_confirmed")
    major = [f for f in items if f.severity == "major"]
    if not major:
        return Readiness("ready", "ready")
    tests_blocked = any("verifiability" in (f.criterion, *f.related_criteria) for f in major)
    return Readiness("conditional", "not_ready" if tests_blocked else "conditional")


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _payload(
    document: Document, blocks: tuple[Block, ...], *, include_context_only: bool = True,
) -> str:
    context_only = []
    if include_context_only:
        represented = {n for block in blocks for n in range(block.start_line, block.end_line + 1)}
        context_only = [
            [line.number, line.text] for line in document.lines
            if line.number not in represented and line.text.strip()
        ]
    return _dumps({
        "blocks": [
            {
                "id": block.id, "source_id": block.source_id, "section": block.section,
                "kind": block.kind, "is_requirement": block.is_requirement,
                "lines": [block.start_line, block.end_line], "text": block.text,
            }
            for block in blocks
        ],
        "context_only": context_only,
    })


def _ground(
    proposal: Proposal,
    document: Document,
    blocks: dict[str, Block],
    scope: Scope,
    owner_id: str | None,
) -> Finding | None:
    if not all(value.strip() for value in (proposal.title, proposal.description, proposal.recommendation)):
        return None
    anchors = set()
    for hint in proposal.evidence:
        block = blocks.get(hint.block_id)
        anchor = locate_quote(document, block, hint.quote) if block else None
        if anchor is None:
            return None
        anchors.add(anchor)
    if not anchors or (owner_id is not None and not any(a.block_id == owner_id for a in anchors)):
        return None
    evidence = prune_anchors(anchors)
    if proposal.criterion in {"contradiction", "duplication"}:
        has_two_places = any(
            not anchors_overlap(a, b) for i, a in enumerate(evidence) for b in evidence[i + 1:]
        )
        if not has_two_places:
            return None
    scenario = proposal.scenario.strip() if proposal.scenario else None
    if proposal.criterion == "contradiction" and not scenario:
        return None

    severity = proposal.severity
    if all(blocks[a.block_id].kind == "governance" for a in evidence):
        severity = "minor"
    elif severity == "critical" and (not scenario or proposal.criterion in {"atomicity", "duplication"}):
        severity = "major"
    requirement_ids = tuple(sorted({a.block_id for a in evidence if blocks[a.block_id].is_requirement}))
    return Finding(
        proposal.criterion, severity, proposal.title.strip(), proposal.description.strip(),
        proposal.recommendation.strip(), scenario or None, requirement_ids, evidence, (scope,),
    )


def _governance(document: Document, blocks: dict[str, Block]) -> list[Finding]:
    findings: list[Finding] = []
    for block in blocks.values():
        if block.kind != "governance":
            continue
        for line in document.lines[block.start_line - 1:block.end_line]:
            text = line.text.strip()
            if not text:
                continue
            approval = re.search(r"согласующ|approv", block.section, flags=re.IGNORECASE)
            empty_approval = approval and text.startswith("|") and not text.replace("|", "").strip()
            for excerpt in re.split(r"(?<=[.!?])\s+", text):
                if empty_approval:
                    title = "Таблица согласования пуста"
                    description = "В таблице нет участников и результата согласования спецификации."
                    action = "Указать ответственных и зафиксировать статус согласования."
                elif MISSING_ADMIN.search(excerpt):
                    rule = next(
                        (row for row in ADMIN_RULES if row[0].search(f"{block.section} {excerpt}")),
                        None,
                    )
                    if rule is None:
                        continue
                    _, title, description, action = rule
                else:
                    continue
                proposal = Proposal(
                    criterion="completeness", severity="minor", title=title,
                    description=description, recommendation=action, scenario=None,
                    evidence=[EvidenceHint(block_id=block.id, quote=excerpt)],
                )
                grounded = _ground(proposal, document, blocks, "set", None)
                if grounded is not None:
                    findings.append(grounded)
    return findings


def _checks_valid(checks: Iterable[Check], expected: tuple[Criterion, ...]) -> bool:
    keys = [check.criterion for check in checks]
    return len(keys) == len(expected) and set(keys) == set(expected)


def _local_valid(result: LocalResponse, ids: tuple[str, ...]) -> bool:
    returned_ids = [item.requirement_id for item in result.items]
    return (
        len(returned_ids) == len(ids) and set(returned_ids) == set(ids)
        and all(_checks_valid(item.checks, LOCAL_CRITERIA) for item in result.items)
    )


def _set_valid(result: SetResponse) -> bool:
    relations = [link.relation for link in result.links]
    return (
        _checks_valid(result.checks, SET_CRITERIA)
        and len(relations) == len(RELATIONS) and set(relations) == set(RELATIONS)
    )


def _unknown_checks(criteria: tuple[Criterion, ...]) -> tuple[Check, ...]:
    return tuple(Check(criterion=criterion, status="uncertain") for criterion in criteria)


def _aligned_checks(
    checks: Iterable[Check], expected: tuple[Criterion, ...], findings: Iterable[Finding],
) -> tuple[Check, ...]:
    choices = {check.criterion: check.status for check in checks}
    issues = {finding.criterion for finding in findings}
    normalized: list[Check] = []
    for criterion in expected:
        status = choices[criterion]
        if criterion in issues:
            status = "issue"
        elif status == "issue":
            status = "uncertain"
        elif status == "not_applicable" and criterion in {"completeness", "ambiguity", "verifiability"}:
            status = "uncertain"
        normalized.append(Check(criterion=criterion, status=status))
    return tuple(normalized)


def _link_found(finding: Finding, relation: Relation, blocks: dict[str, Block]) -> bool:
    a, b = LINK_KINDS[relation]
    kinds = {blocks[anchor.block_id].kind for anchor in finding.evidence}
    return bool(kinds & a and kinds & b)


def _aligned_links(
    links: Iterable[LinkCheck], findings: Iterable[Finding], blocks: dict[str, Block],
) -> tuple[LinkCheck, ...]:
    choices = {link.relation: link.status for link in links}
    items = tuple(findings)
    kinds = {block.kind for block in blocks.values()}
    normalized: list[LinkCheck] = []
    for relation in RELATIONS:
        status = choices[relation]
        if any(_link_found(f, relation, blocks) for f in items):
            status = "issue"
        elif status == "issue":
            status = "uncertain"
        elif status == "not_applicable":
            left, right = LINK_KINDS[relation]
            if kinds & left and kinds & right:
                status = "uncertain"
        normalized.append(LinkCheck(relation=relation, status=status))
    return tuple(normalized)


def call_with_validation(
    reviewer: Reviewer, system: str, message: str, schema: type[T], validate: Callable[[T], bool],
    *, max_input_chars: int, retry_note: str, retry_error: str, retry_kind: RetryKind = "validation",
    timings: RunTimings | None = None, reasoning_effort: ReasoningEffort | None = None,
) -> T:
    """Check input budget and retry one provider-validated answer that failed a local check."""
    if len(system) + len(message) + len(retry_note) > max_input_chars:
        raise ReviewLimitError(
            "Полный контекст превышает REQVALIDATOR_MAX_INPUT_CHARS. "
            "Увеличьте лимит в пределах окна выбранной модели. Текст не обрезался."
        )
    provider_timed = isinstance(reviewer, OpenAIReviewer) and reviewer.timings is timings
    for attempt, prompt in enumerate((message, message + retry_note)):
        retry = timings.retry(retry_kind) if timings is not None and attempt else nullcontext()
        model_call = timings.model_call(measured_transport=False) if timings is not None and not provider_timed else nullcontext()
        with retry:
            with model_call:
                if reasoning_effort is not None and isinstance(reviewer, OpenAIReviewer):
                    result = reviewer.ask(system, prompt, schema, reasoning_effort=reasoning_effort)
                else:
                    result = reviewer.ask(system, prompt, schema)
            if validate(result):
                return result
    raise ReviewError(retry_error)


class ReviewEngine:
    def __init__(
        self, reviewer: Reviewer, *, max_input_chars: int = 100_000, local_batch_size: int = 6,
        timings: RunTimings | None = None, merge_reasoning_effort: ReasoningEffort | None = None,
        reviewers: tuple[ReviewRole, ...] = (), reviewer_max_findings: int = 5,
        external_docs: bool = False, context7_api_key: str | None = None,
    ) -> None:
        if max_input_chars < 1 or local_batch_size < 1:
            raise ValueError("Лимит входа и размер пакета должны быть положительными.")
        if not 1 <= reviewer_max_findings <= 30 or any(role not in SPECIALISTS for role in reviewers):
            raise ValueError("Укажите известные роли и лимит от 1 до 30 находок за проход.")
        self.reviewer = reviewer
        self.max_input_chars = max_input_chars
        self.local_batch_size = local_batch_size
        self.timings = timings
        self.merge_reasoning_effort = merge_reasoning_effort
        self.reviewers = tuple(dict.fromkeys(reviewers))
        self.reviewer_max_findings = reviewer_max_findings
        self.external_docs = external_docs
        self.context7_api_key = context7_api_key

    def _stage(self, stage: str) -> AbstractContextManager[float | None]:
        return self.timings.stage(stage) if self.timings is not None else nullcontext()

    def _call(
        self, system: str, message: str, schema: type[T], validate: Callable[[T], bool],
        *,
        retry_note: str = (
            "\n\nПовтор: предыдущий ответ нарушил покрытие. Верни ровно указанные во входе "
            "ID, критерии и связки. Не пропускай записи, у которых нет замечаний."
        ),
        retry_error: str = "Ответ не покрыл все обязательные ID и критерии.",
        retry_kind: RetryKind = "coverage",
        reasoning_effort: ReasoningEffort | None = None,
    ) -> T:
        return call_with_validation(
            self.reviewer, system, message, schema, validate, max_input_chars=self.max_input_chars,
            retry_note=retry_note, retry_error=retry_error, retry_kind=retry_kind, timings=self.timings,
            reasoning_effort=reasoning_effort,
        )

    def _collect(
        self,
        proposals: Iterable[Proposal],
        document: Document,
        blocks: dict[str, Block],
        scope: Scope,
        owner_id: str | None = None,
    ) -> tuple[list[Finding], int]:
        verified: list[Finding] = []
        discarded = 0
        criteria = LOCAL_CRITERIA if scope == "local" else SET_CRITERIA
        for proposal in proposals:
            finding = (
                _ground(proposal, document, blocks, scope, owner_id)
                if proposal.criterion in criteria else None
            )
            if finding is None:
                discarded += 1
            else:
                verified.append(finding)
        return verified, discarded

    def _semantic_pairs(
        self, findings: list[Finding], warnings: list[str], *, consolidated: bool = False,
    ) -> tuple[tuple[int, int], ...]:
        candidates = merge_candidates(findings)
        if consolidated:
            blocks = [{anchor.block_id for anchor in f.evidence} for f in findings]
            candidates = tuple(
                (i, j) for i, j in candidates
                if len(blocks[i - 1] & blocks[j - 1]) >= 2
                or findings[i - 1].severity == findings[j - 1].severity == "critical"
            )
        if self.timings is not None:
            stage = "merge_consolidated" if consolidated else "merge"
            self.timings.merge_pairs[stage] += len(candidates)
        if not candidates:
            return ()
        merged: list[tuple[int, int]] = []
        for start in range(0, len(candidates), 30):
            batch = candidates[start:start + 30]
            ids = sorted({i for pair in batch for i in pair})
            query = _dumps({
                "pairs": batch,
                "findings": [
                    {
                        "id": i, "criterion": findings[i - 1].criterion,
                        "title": findings[i - 1].title, "description": findings[i - 1].description,
                        "recommendation": findings[i - 1].recommendation,
                        "scenario": findings[i - 1].scenario,
                        "evidence": [[a.block_id, a.quote] for a in findings[i - 1].evidence],
                    }
                    for i in ids
                ],
            })
            try:
                system = prompts.MERGE_CONSOLIDATED if consolidated else prompts.MERGE
                answer = self._call(
                    system, query, MergeResponse, lambda _: True,
                    reasoning_effort=self.merge_reasoning_effort,
                )
            except (ReviewError, ReviewLimitError):
                warnings.append("Семантическое слияние выполнено частично. Точные дубли удалены кодом.")
                break
            allowed = set(batch)
            for pair in answer.pairs:
                key = tuple(sorted((pair.left, pair.right)))
                if key in allowed:
                    merged.append((key[0], key[1]))
        return tuple(merged)

    def consolidate(self, findings: list[Finding], warnings: list[str]) -> tuple[Finding, ...]:
        with self._stage("merge"):
            semantic = self._semantic_pairs(findings, warnings)
            unique = deduplicate(findings, semantic)
        return self.consolidate_shared_findings(unique, warnings)

    def consolidate_shared_findings(
        self, findings: tuple[Finding, ...], warnings: list[str],
    ) -> tuple[Finding, ...]:
        with self._stage("merge_consolidated"):
            pairs = self._semantic_pairs(list(findings), warnings, consolidated=True)
            return deduplicate(findings, pairs) if pairs else findings

    def _clarify(self, source: str, targets: dict[str, str]) -> dict[str, str | None]:
        if not targets:
            return {}
        try:
            answer = self._call(
                prompts.CLARIFY, _dumps(targets) + "\n\nДокумент JSON:\n" + source,
                ClarificationResponse,
                lambda result: sorted(item.target for item in result.items) == sorted(targets),
            )
        except (ReviewError, ReviewLimitError):
            return {}
        return {
            item.target: None if item.resolved else (item.question or "").strip() or targets[item.target]
            for item in answer.items
        }

    def _specialist_batches(
        self, role: ReviewRole, document: Document, inventory: tuple[Block, ...], source: str,
    ) -> Iterator[tuple[list[Proposal], tuple[Block, ...]]]:
        specialist = SPECIALISTS[role]
        limit = self.reviewer_max_findings
        system = f"{specialist.task}\nЛимит находок за проход — {limit}. Верни самые существенные."

        def ask(payload: str, *, partial: bool = False) -> SpecialistResponse:
            message = "Передан фрагмент документа.\n" if partial else ""
            return self._call(
                system, message + "Документ JSON:\n" + payload, specialist.schema,
                lambda result: len(result.findings) <= limit,
                retry_note=f"\n\nПовтор: верни не более {limit} находок.",
                retry_error=f"Ответ ревьюера {role} превысил лимит находок.",
                retry_kind="validation",
            )

        try:
            whole = ask(source)
        except ReviewLimitError:
            pass
        else:
            yield whole.findings, inventory
            return

        offset = 0
        while offset < len(inventory):
            size = min(self.local_batch_size, len(inventory) - offset)
            while True:
                batch = inventory[offset:offset + size]
                try:
                    response = ask(_payload(document, batch, include_context_only=False), partial=True)
                except ReviewLimitError:
                    if size == 1:
                        raise
                    size //= 2
                else:
                    break
            yield response.findings, batch
            offset += size

    def run(
        self, document: Document, *, model: str, provider: str, version: str,
    ) -> ReviewReport:
        with self._stage("segment"):
            inventory = segment_document(document)
            blocks = {block.id: block for block in inventory}
            requirements = tuple(block for block in inventory if block.is_requirement)
            if not requirements:
                raise ReviewLimitError("В документе не выделено ни одного требования.")
            source = _payload(document, inventory)
        available = self.max_input_chars - 256
        main_fits = len(prompts.SET) + len(prompts.set_input(source)) <= available
        if not main_fits and not self.reviewers:
            raise ReviewLimitError(
                "Полный документ не помещается в REQVALIDATOR_MAX_INPUT_CHARS. "
                "Увеличьте лимит в пределах окна выбранной модели. Текст не обрезался."
            )
        warnings = list(document.warnings)
        if not main_fits:
            warnings.append(
                "Основное ревью пропущено. Полный документ не помещается в "
                "REQVALIDATOR_MAX_INPUT_CHARS. Доступны результаты дополнительных ролей."
            )
        findings: list[Finding] = []
        results: list[RequirementResult] = []
        discarded = 0
        call_blocked = False
        failed_stages = 0
        failed_reviewers: list[ReviewRole] = []
        batch_starts = range(0, len(requirements), self.local_batch_size)
        with self._stage("local") as local_started:
            for number, start in enumerate(batch_starts, 1):
                batch = requirements[start:start + self.local_batch_size]
                ids = tuple(block.id for block in batch)
                local: LocalResponse | None = None
                if main_fits and not call_blocked:
                    try:
                        local = self._call(
                            prompts.LOCAL, prompts.local_input(_dumps(ids), source),
                            LocalResponse, lambda result: _local_valid(result, ids),
                        )
                    except ReviewError as error:
                        failed_stages += 1
                        call_blocked = error.fatal
                        warnings.append(f"Локальный проход для {', '.join(ids)} не завершён. {error}")
                if local is None:
                    results.extend(RequirementResult(block, _unknown_checks(LOCAL_CRITERIA)) for block in batch)
                else:
                    by_id = {item.requirement_id: item for item in local.items}
                    with self._stage("evidence"):
                        for block in batch:
                            item = by_id[block.id]
                            grounded, rejected = self._collect(
                                item.findings, document, blocks, "local", block.id,
                            )
                            findings.extend(grounded)
                            discarded += rejected
                            checks = _aligned_checks(item.checks, LOCAL_CRITERIA, grounded)
                            results.append(RequirementResult(block, checks))
                if self.timings is not None and local_started is not None:
                    self.timings.local_batch(number, len(batch_starts), local_started)

        with self._stage("set"):
            with self._stage("evidence"):
                set_findings = _governance(document, blocks)
            pending: list[tuple[ReviewRole | None, list[Proposal], dict[str, Block]]] = []
            whole: SetResponse | None = None
            set_checks = _unknown_checks(SET_CRITERIA)
            link_checks = tuple(LinkCheck(relation=relation, status="uncertain") for relation in RELATIONS)
            if main_fits and not call_blocked:
                try:
                    whole = self._call(prompts.SET, prompts.set_input(source), SetResponse, _set_valid)
                except ReviewError as error:
                    failed_stages += 1
                    call_blocked = error.fatal
                    warnings.append(f"Проверка набора не завершена. {error}")
                else:
                    pending.append((None, whole.findings, blocks))
            elif call_blocked:
                warnings.append("Проверка набора пропущена после ошибки обращения к модели.")

        if self.reviewers and not call_blocked:
            with self._stage("reviewers"):
                for role in self.reviewers:
                    if not SPECIALISTS[role].applies(inventory):
                        continue
                    try:
                        for proposals, batch in self._specialist_batches(role, document, inventory, source):
                            pending.append((role, proposals, {block.id: block for block in batch}))
                    except (ReviewError, ReviewLimitError) as error:
                        failed_reviewers.append(role)
                        warnings.append(f"Проверка ревьюером {role} не завершена. {error}")
                        if isinstance(error, ReviewError) and error.fatal:
                            call_blocked = True
                            break

        if pending:
            with self._stage("evidence"):
                for role, proposals, cited_blocks in pending:
                    grounded, rejected = self._collect(proposals, document, cited_blocks, "set")
                    if role is not None:
                        grounded = [replace(finding, origins=(role,)) for finding in grounded]
                    set_findings.extend(grounded)
                    discarded += rejected
        if whole is not None or any(finding.origins for finding in set_findings):
            set_checks = _aligned_checks(
                whole.checks if whole is not None else set_checks, SET_CRITERIA, set_findings,
            )
            link_checks = _aligned_links(
                whole.links if whole is not None else link_checks, set_findings, blocks,
            )

        findings.extend(set_findings)
        if self.reviewers:
            findings = [
                finding if finding.origins else replace(finding, origins=("main",))
                for finding in findings
            ]
        if call_blocked:
            with self._stage("merge"):
                unique = deduplicate(findings)
        else:
            unique = self.consolidate(findings, warnings)
        complete = main_fits and not document.warnings and failed_stages == 0
        targets = {
            f"{row.block.id}/{check.criterion}": (
                f"Что уточнить для проверки «{prompts.CRITERIA[check.criterion]}» в блоке {row.block.id}?"
            )
            for row in results for check in row.checks if check.status == "uncertain"
        }
        targets.update({
            f"набор/{check.criterion}": f"Как подтвердить {prompts.CRITERIA[check.criterion]}?"
            for check in set_checks if check.status == "uncertain"
        })
        targets.update({
            f"связь/{check.relation}": f"Как согласовать {prompts.RELATION_LABELS[check.relation]}?"
            for check in link_checks if check.status == "uncertain"
        })
        with self._stage("clarify"):
            resolved = self._clarify(source, targets) if complete and not call_blocked else {}

        def clarify_status(item: T, key: str) -> T:
            if key in resolved and resolved[key] is None:
                return item.model_copy(update={"status": "pass"})
            return item

        results = [
            RequirementResult(row.block, tuple(
                clarify_status(check, f"{row.block.id}/{check.criterion}") for check in row.checks
            )) for row in results
        ]
        set_checks = tuple(clarify_status(check, f"набор/{check.criterion}") for check in set_checks)
        link_checks = tuple(clarify_status(check, f"связь/{check.relation}") for check in link_checks)
        questions = tuple(
            (key, question) for key, fallback in targets.items()
            if complete and (question := resolved.get(key, fallback)) is not None
        )
        external_references = None
        if self.external_docs:
            with self._stage("external_docs"):
                unique, external_references = check_external_docs(inventory, unique, self.context7_api_key)
        referenced = {anchor.block_id for finding in unique for anchor in finding.evidence}
        return ReviewReport(
            document=DocumentInfo(
                document.name, document.format, document.sha256, len(document.lines),
                document.page_count,
            ),
            reviewed_at=datetime.now(UTC_PLUS_FIVE).isoformat(timespec="seconds"),
            model=model, provider=provider, version=version,
            requirements=tuple(results), set_checks=set_checks, link_checks=link_checks,
            findings=unique, readiness=assess_readiness(unique, complete and not questions),
            coverage_complete=complete, discarded_proposals=discarded,
            merged_proposals=len(findings) - len(unique), failed_stages=failed_stages,
            warnings=tuple(warnings),
            context_blocks=tuple(block for block in inventory if block.id in referenced and not block.is_requirement),
            clarifications=questions, failed_reviewers=tuple(failed_reviewers),
            external_references=external_references,
        )
