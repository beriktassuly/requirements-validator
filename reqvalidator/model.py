"""Validated model responses and grounded application results."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .segment import Block


Criterion = Literal[
    "contradiction", "completeness", "ambiguity", "verifiability", "atomicity",
    "duplication", "traceability",
]
Status = Literal["pass", "issue", "uncertain", "not_applicable"]
Severity = Literal["critical", "major", "minor"]
Scope = Literal["local", "set"]
ReviewRole = Literal["qa", "api"]
ReviewOrigin = Literal["main", ReviewRole]
ExternalStatus = Literal["confirmed", "contradicts", "version_mismatch", "not_found", "unavailable"]
Relation = Literal["functional_acceptance", "business_api", "data_api", "status_examples"]
ReadinessStatus = Literal["ready", "conditional", "not_ready", "not_confirmed"]

LOCAL_CRITERIA: tuple[Criterion, ...] = (
    "contradiction", "completeness", "ambiguity", "verifiability", "atomicity",
)
SET_CRITERIA: tuple[Criterion, ...] = (
    "contradiction", "completeness", "ambiguity", "verifiability", "duplication", "traceability",
)
RELATIONS: tuple[Relation, ...] = (
    "functional_acceptance", "business_api", "data_api", "status_examples",
)
SEVERITY_RANK: dict[Severity, int] = {"critical": 0, "major": 1, "minor": 2}


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceHint(StrictSchema):
    block_id: str
    quote: str


class Check(StrictSchema):
    criterion: Criterion
    status: Status


class LinkCheck(StrictSchema):
    relation: Relation
    status: Status


class Proposal(StrictSchema):
    criterion: Criterion
    severity: Severity
    title: str
    description: str
    recommendation: str
    scenario: str | None
    evidence: list[EvidenceHint]


class LocalItem(StrictSchema):
    requirement_id: str
    checks: list[Check]
    findings: list[Proposal]


class LocalResponse(StrictSchema):
    items: list[LocalItem]


class SetResponse(StrictSchema):
    checks: list[Check]
    links: list[LinkCheck]
    findings: list[Proposal]


class Clarification(StrictSchema):
    target: str
    resolved: bool
    question: str | None


class ClarificationResponse(StrictSchema):
    items: list[Clarification]


class MergePair(StrictSchema):
    left: int
    right: int


class MergeResponse(StrictSchema):
    pairs: list[MergePair]


@dataclass(frozen=True, slots=True)
class Anchor:
    block_id: str
    quote: str
    start_line: int
    end_line: int
    start_col: int
    end_col: int
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True, slots=True)
class ExternalReference:
    url: str | None
    requested_version: str
    served_version: str | None
    retrieved_at: str
    status: ExternalStatus
    technology: str
    question: str
    source: Literal["context7", "official"]


@dataclass(frozen=True, slots=True)
class Finding:
    criterion: Criterion
    severity: Severity
    title: str
    description: str
    recommendation: str
    scenario: str | None
    requirement_ids: tuple[str, ...]
    evidence: tuple[Anchor, ...]
    scopes: tuple[Scope, ...]
    id: str = ""
    related_criteria: tuple[Criterion, ...] = ()
    additional_recommendations: tuple[str, ...] = ()
    origins: tuple[ReviewOrigin, ...] = ()
    external_references: tuple[ExternalReference, ...] = ()
    document_gaps: bool = False


@dataclass(frozen=True, slots=True)
class RequirementResult:
    block: Block
    checks: tuple[Check, ...]


@dataclass(frozen=True, slots=True)
class Readiness:
    development: ReadinessStatus
    testing: ReadinessStatus


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    name: str
    format: str
    sha256: str
    line_count: int
    page_count: int | None


@dataclass(frozen=True, slots=True)
class ReviewReport:
    document: DocumentInfo
    reviewed_at: str
    model: str
    provider: str
    version: str
    requirements: tuple[RequirementResult, ...]
    set_checks: tuple[Check, ...]
    link_checks: tuple[LinkCheck, ...]
    findings: tuple[Finding, ...]
    readiness: Readiness
    coverage_complete: bool
    discarded_proposals: int
    merged_proposals: int
    failed_stages: int
    warnings: tuple[str, ...]
    context_blocks: tuple[Block, ...] = ()
    clarifications: tuple[tuple[str, str], ...] = ()
    failed_reviewers: tuple[ReviewRole, ...] = ()
    external_references: tuple[ExternalReference, ...] | None = None
