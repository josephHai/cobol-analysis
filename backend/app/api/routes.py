"""HTTP routes.

Routes stay thin by design (CODESTYLE.md §2.1): validate input, apply the actor
dependency, delegate to :class:`~app.services.run_service.RunService`, return a DTO.
No business logic, no direct database or filesystem access.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from app.context import AppContext, get_ctx
from app.core.auth import Actor, require
from app.domain.models import (
    AuditEntry,
    KbQueryRequest,
    KbQueryResponse,
    RepoInfo,
    Run,
    RunCreateRequest,
    RunLogEntry,
)
from app.services.events import EVENT_DONE, EVENT_ERROR
from app.services.git_service import GitError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

# One alias per permission level. Every route uses one of these rather than spelling out
# Depends(require(...)) inline: a uniform shape is what makes "who can call this?" answerable
# by reading the signature, and it is what a security review greps for.
Ctx = Annotated[AppContext, Depends(get_ctx)]
Reader = Annotated[Actor, Depends(require("read"))]
Creator = Annotated[Actor, Depends(require("create_run"))]
Querier = Annotated[Actor, Depends(require("query_kb"))]
Admin = Annotated[Actor, Depends(require("admin"))]


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def _bad_request(exc: GitError) -> HTTPException:
    """Turn a git-layer refusal into a 400 that names what to fix.

    Only ``GitError`` is translated: anything else reaching a route is a defect in this service,
    and reporting it as "400 Bad Request" would blame the caller and hide the trace id.
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ------------------------------------------------------------------- meta
@router.get("/health", tags=["meta"])
def health(ctx: Ctx, actor: Reader) -> dict[str, Any]:
    """Liveness plus the facts an operator needs to interpret the UI.

    Authenticated on purpose: the payload names the data root and the model gateway, which is
    an internal-layout disclosure. An unauthenticated liveness probe can use ``GET /`` instead,
    which returns only the service name.
    """
    return {
        "status": "ok",
        "data_root": str(ctx.settings.data_root),
        "repos": len(ctx.runs.list_repos()),
        "model": ctx.settings.llm_model,
        "llm_base_url": ctx.settings.llm_base_url,
        "disk_usage_mb": ctx.git.disk_usage_mb(),
        "kb": ctx.runs.kb_stats(),
    }


@router.get("/whoami", tags=["meta"])
def whoami(actor: Reader) -> dict[str, Any]:
    return {"actor": actor.id, "roles": sorted(actor.roles)}


@router.get("/audit", tags=["meta"], response_model=list[AuditEntry])
def audit(
    ctx: Ctx, actor: Admin, limit: int = Query(default=100, ge=1, le=1000)
) -> list[AuditEntry]:
    return ctx.runs.recent_audit(limit)


# ------------------------------------------------------------------ repos
@router.get("/repos", response_model=list[RepoInfo], tags=["repos"])
def list_repos(ctx: Ctx, actor: Reader) -> list[RepoInfo]:
    return ctx.runs.list_repos()


@router.post("/repos/{repo_key}/fetch", tags=["repos"])
def fetch_remote(repo_key: str, ctx: Ctx, actor: Creator) -> dict[str, Any]:
    """Pull the latest refs for a repository from its remote.

    Always contacts the remote — the operator-facing counterpart to the implicit fetch a run
    performs — and reports what it found, so the console can show a concrete result rather than
    a spinner that may have done nothing.

    Requires ``create_run`` rather than ``read``: it is a network operation against somebody
    else's server, and a viewer should not be able to trigger one.
    """
    try:
        return ctx.git.fetch_remote(repo_key)
    except GitError as exc:
        raise _bad_request(exc) from exc


@router.post("/repos/{repo_key}/resolve", tags=["repos"])
def resolve_ref(
    repo_key: str, ctx: Ctx, actor: Reader, ref: str | None = Query(default=None)
) -> dict[str, Any]:
    """Resolve a branch, tag or commit to an immutable SHA, and list available branches.

    The composer calls this as soon as a repository is chosen, so the operator sees the exact
    revision the analysis will be pinned to before committing to it.
    """
    try:
        return ctx.runs.resolve_ref(repo_key, ref)
    except GitError as exc:
        raise _bad_request(exc) from exc


# -------------------------------------------------------------------- runs
@router.post("/runs", response_model=Run, status_code=status.HTTP_201_CREATED, tags=["runs"])
async def create_run(request: RunCreateRequest, ctx: Ctx, actor: Creator) -> Run:
    """Create a run and start it.

    Async on purpose: the run is scheduled as an asyncio task, which requires a
    running event loop (see ``RunService.start``).
    """
    try:
        run = ctx.runs.create(request, actor.id)
    except HTTPException:
        raise
    except GitError as exc:
        raise _bad_request(exc) from exc
    ctx.runs.start(run)
    return run


@router.get("/runs", response_model=list[Run], tags=["runs"])
def list_runs(
    ctx: Ctx,
    actor: Reader,
    status_filter: str | None = Query(default=None, alias="status"),
    repo_key: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Run]:
    return ctx.runs.list(status=status_filter, repo_key=repo_key, limit=limit)


@router.get("/runs/{run_id}", response_model=Run, tags=["runs"])
def get_run(run_id: str, ctx: Ctx, actor: Reader) -> Run:
    run = ctx.runs.get(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} does not exist. It may have been deleted by the retention policy.",
        )
    return run


@router.post("/runs/{run_id}/cancel", tags=["runs"])
def cancel_run(run_id: str, ctx: Ctx, actor: Creator) -> dict[str, Any]:
    run = ctx.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    accepted = ctx.runs.cancel(run_id, actor.id)
    return {
        "accepted": accepted,
        "detail": (
            "Cancellation requested; the run stops at the next checkpoint."
            if accepted
            else "The run is not currently executing."
        ),
    }


@router.delete("/runs/{run_id}", tags=["runs"])
def delete_run(
    run_id: str,
    ctx: Ctx,
    actor: Admin,
    purge_files: bool = False,
) -> dict[str, Any]:
    if not ctx.runs.delete(run_id, actor.id, purge_files=purge_files):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    return {"deleted": run_id, "files_purged": purge_files}


# ------------------------------------------------------------------ events
@router.get("/runs/{run_id}/events", tags=["runs"])
async def run_events(
    run_id: str,
    request: Request,
    ctx: Ctx,
    actor: Reader,
) -> StreamingResponse:
    """Server-sent event stream for one run.

    Replays the persisted journal from ``Last-Event-ID`` before following live, so a
    browser refresh or a dropped connection resumes without losing the sequence. The header is
    the only cursor: ``EventSource`` re-sends it by itself on reconnect, and a second, query-string
    cursor would be a credential-shaped surface with no client using it.

    The browser sends no credential here. ``EventSource`` cannot set headers, which used to
    force this endpoint to accept a credential in the query string; the console now relies on
    the proxy to attach the identity header instead, so the stream inherits the same
    authentication as every other request and no secret appears in a URL.
    """
    if ctx.runs.get(run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")

    after = _parse_last_event_header(request.headers.get("last-event-id"))

    async def generator():
        yield ": connected\n\n"
        async for envelope in ctx.bus.stream(
            run_id, after_seq=after, is_finished=lambda: ctx.runs.is_finished(run_id)
        ):
            if await request.is_disconnected():
                break
            yield _sse(envelope["event"], envelope["data"], envelope.get("seq"))
            if envelope["event"] in (EVENT_DONE, EVENT_ERROR) and envelope.get("data", {}).get(
                "terminal"
            ):
                break

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # do not let a reverse proxy buffer the stream
        },
    )


def _parse_last_event_header(value: str | None) -> int:
    """The replay cursor, defaulting to 0 when absent or unparseable."""
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


@router.get("/runs/{run_id}/logs", tags=["runs"], response_model=list[RunLogEntry])
def run_logs(
    run_id: str,
    ctx: Ctx,
    actor: Reader,
    level: str | None = None,
    q: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[RunLogEntry]:
    """Flatten the event journal into a log view for the console."""
    if ctx.runs.get(run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    return ctx.runs.log_entries(run_id, level=level, q=q, limit=limit)


# --------------------------------------------------------------- artifacts
@router.get("/runs/{run_id}/artifacts", tags=["artifacts"])
def list_artifacts(run_id: str, ctx: Ctx, actor: Reader) -> dict[str, Any]:
    run = ctx.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    store_files = ctx.git.artifacts_dir(run_id)
    files = (
        [p.name for p in sorted(store_files.iterdir()) if p.is_file()]
        if store_files.exists()
        else []
    )
    return {
        "run_id": run_id,
        "artifacts": [a.model_dump(mode="json") for a in run.artifacts],
        "files": files,
    }


@router.get("/runs/{run_id}/artifacts/{kind}", tags=["artifacts"])
def preview_artifact(run_id: str, kind: str, ctx: Ctx, actor: Reader) -> dict[str, Any]:
    """Preview an artifact as JSON-ready data.

    Workbooks arrive as rows rather than as a binary, so the browser never has to
    parse an untrusted XLSX (DESIGN.md §4.7).
    """
    payload = ctx.runs.preview(run_id, kind)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Artifact '{kind}' is not available for run {run_id}. "
                "It may still be generating, or generation may have failed."
            ),
        )
    return payload


@router.get("/runs/{run_id}/artifacts/{kind}/download", tags=["artifacts"])
def download_artifact(run_id: str, kind: str, ctx: Ctx, actor: Reader) -> FileResponse:
    path = ctx.runs.artifact_path(run_id, kind)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Artifact '{kind}' is not available"
        )
    ctx.runs.audit(actor.id, "artifact.download", target=f"{run_id}:{kind}")
    return FileResponse(
        path,
        filename=f"{run_id}-{path.name}",
        media_type="application/octet-stream",
    )


# -------------------------------------------------------------- knowledge base
@router.post("/kb/query", response_model=KbQueryResponse, tags=["kb"])
def kb_query(request: KbQueryRequest, ctx: Ctx, actor: Querier) -> KbQueryResponse:
    return ctx.runs.kb_query(request)


@router.get("/kb/stats", tags=["kb"])
def kb_stats(ctx: Ctx, actor: Reader) -> dict[str, Any]:
    return ctx.runs.kb_stats()


@router.post("/kb/reindex/{run_id}", tags=["kb"])
def kb_reindex(run_id: str, ctx: Ctx, actor: Creator) -> dict[str, Any]:
    count = ctx.runs.kb_reindex(run_id, actor.id)
    return {"run_id": run_id, "chunks": count}


# ------------------------------------------------------------------- admin
@router.post("/admin/gc", tags=["admin"])
def gc(ctx: Ctx, actor: Admin, days: int | None = None) -> dict[str, Any]:
    result = ctx.git.gc(days)
    ctx.runs.audit(actor.id, "admin.gc", detail=json.dumps(result, ensure_ascii=False))
    return result


@router.post("/admin/reload-repos", tags=["admin"])
def reload_repos(ctx: Ctx, actor: Admin) -> dict[str, Any]:
    count = ctx.git.reload_specs()
    ctx.runs.audit(actor.id, "admin.reload_repos", detail=f"repos={count}")
    return {"repos": count}
