"""Domain models for the analysis platform.

Kept deliberately small for the demo: a *Run* is one end-to-end analysis of one
interface inside one repository revision, and it owns a set of *Artifacts*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunStatus(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    VALIDATING = "validating"
    ARCHIVING = "archiving"
    INDEXING_KB = "indexing_kb"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class PhaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ArtifactKind(StrEnum):
    PROGRESS = "progress"
    CALL_FLOW = "call_flow"
    CODE_SLICE = "code_slice"
    BUSINESS_RULES = "business_rules"
    FDD = "fdd"
    TEST_CASE = "test_case"
    DATA_MAPPING = "data_mapping"


class ArtifactState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class Phase(BaseModel):
    name: str
    label: str
    status: PhaseStatus = PhaseStatus.PENDING
    progress: float = 0.0
    detail: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None


class Artifact(BaseModel):
    kind: ArtifactKind
    label: str
    state: ArtifactState = ArtifactState.PENDING
    filename: str | None = None
    size_bytes: int = 0
    version: int = 1
    summary: str = ""
    error: str | None = None
    updated_at: str = Field(default_factory=now_iso)


class RunCreateRequest(BaseModel):
    """What a caller supplies to start an analysis.

    Deliberately minimal: a repository, a revision, and a request in the operator's own words.
    Everything else — which program or transaction to analyse, what the interface is called —
    is **inferred from the message** by the analysis skill, because a human describing the task
    is more accurate than a form that forces them to name a PROGRAM-ID up front. An operator who
    knows the entry point can still say so in the message, and the skill will use it.
    """

    repo_key: str
    ref: str | None = None
    """Branch, tag, or commit. Empty means the repository's default branch."""

    message: str = Field(min_length=8, max_length=4000)
    """The analysis request, in natural language.

    This is the primary input. It reaches the agent verbatim and is also recorded on the run so
    a reviewer can see what was actually asked for.
    """

    skills: list[Literal["fdd", "test_case", "data_mapping"]] = Field(
        default_factory=lambda: ["fdd", "test_case", "data_mapping"]
    )
    locale: Literal["en", "zh"] = "en"
    """Output language for the generated deliverables.

    The project baseline is English; locale stays a request parameter because the delivery
    language is a customer requirement, not an implementation detail.
    """


class Run(BaseModel):
    id: str
    repo_key: str
    repo_url: str = ""
    ref: str | None = None
    commit_sha: str | None = None

    message: str = ""
    """The operator's request, verbatim. The pipeline's primary input and the audit record of
    what was asked for."""

    entrypoint: str = ""
    """The analysed entry point. **Derived**, not supplied: the analysis skill reports which
    program it settled on, and the pipeline writes it back here so the console can show it."""

    interface: str = ""
    """A human-readable label for the analysis, derived from the message when not stated."""

    skills: list[str] = Field(default_factory=list)
    locale: str = "en"
    status: RunStatus = RunStatus.QUEUED
    phases: list[Phase] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    actor: str = "anonymous"
    error: str | None = None
    created_at: str = Field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class RepoInfo(BaseModel):
    repo_key: str
    name: str
    url: str
    default_branch: str = "main"
    description: str = ""
    has_credential: bool = False
    cached: bool = False
    last_fetched_at: str | None = None


class KbChunk(BaseModel):
    """A retrievable unit of knowledge produced by a run."""

    id: str
    run_id: str
    repo_key: str
    commit_sha: str | None = None
    kind: ArtifactKind
    title: str
    text: str
    locator: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class KbQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    repo_key: str | None = None
    run_id: str | None = None


class KbCitation(BaseModel):
    run_id: str
    repo_key: str
    commit_sha: str | None = None
    kind: ArtifactKind
    title: str
    locator: str = ""
    snippet: str = ""


class KbQueryResponse(BaseModel):
    answer: str
    citations: list[KbCitation] = Field(default_factory=list)
    provider: str = "local"


class RunLogEntry(BaseModel):
    """One line of the log view the console shows for a run.

    A flattened projection of the event journal, not an event: the journal is the record, and
    this is what a reader can scan. Keeping it a model rather than a dict is what lets the route
    declare it in the contract instead of describing the shape in prose.
    """

    seq: int
    ts: str
    level: str
    message: str


class AuditEntry(BaseModel):
    """One audited action: who did what, to what, and how it ended."""

    id: int
    ts: str
    actor: str
    action: str
    target: str = ""
    result: str = "ok"
    detail: str = ""


# ------------------------------------------------------- run initial state
# Pure data constructors, so they belong to the domain layer rather than to a
# storage adapter. `services/` can then build a Run without importing `infra/`.
def default_phases() -> list[Phase]:
    return [
        Phase(name="fetch", label="Fetch source"),
        Phase(name="analyze", label="COBOL analysis"),
        Phase(name="generate", label="Generate artifacts"),
        Phase(name="validate", label="Validate artifacts"),
        Phase(name="archive", label="Archive"),
        Phase(name="kb", label="Index knowledge base"),
    ]


def default_artifacts() -> list[Artifact]:
    return [
        Artifact(kind=ArtifactKind.PROGRESS, label="Analysis progress (progress.json)"),
        Artifact(kind=ArtifactKind.CALL_FLOW, label="Call flow (call_flow.json)"),
        Artifact(kind=ArtifactKind.CODE_SLICE, label="Code slices (code_slice.json)"),
        Artifact(kind=ArtifactKind.BUSINESS_RULES, label="Business rules (business_rules.json)"),
        Artifact(kind=ArtifactKind.FDD, label="Functional Design Document"),
        Artifact(kind=ArtifactKind.TEST_CASE, label="Test case workbook (xlsx)"),
        Artifact(kind=ArtifactKind.DATA_MAPPING, label="Data mapping workbook (xlsx)"),
    ]
