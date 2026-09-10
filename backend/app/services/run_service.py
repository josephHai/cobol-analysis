"""Run service: creates runs, drives them in the background, exposes their state.

The service owns the run lifecycle and is the only place that mutates
``Run.status``. Routes stay thin; the pipeline stays unaware of HTTP.

Concurrency model (single host): runs execute as asyncio tasks inside the API
process, with an in-memory cancellation registry. A restart therefore fails
in-flight runs rather than resuming them — recorded as a known gap in
``docs/DEMO.md`` and the trigger for moving to a real queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from app.agent.pipeline import Pipeline
from app.core.config import Settings
from app.domain.models import (
    TERMINAL_STATUSES,
    AuditEntry,
    KbQueryRequest,
    KbQueryResponse,
    RepoInfo,
    Run,
    RunCreateRequest,
    RunLogEntry,
    RunStatus,
    default_artifacts,
    default_phases,
    now_iso,
)
from app.infra.db import Database
from app.services.events import EVENT_DONE, CancelRegistry, RunEventBus
from app.services.git_service import GitService
from app.services.kb_service import KnowledgeBase

logger = logging.getLogger(__name__)


class RunService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        bus: RunEventBus,
        git: GitService,
        kb: KnowledgeBase,
        cancels: CancelRegistry,
    ) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self.git = git
        self.kb = kb
        self.cancels = cancels
        # TODO(demo): runs execute in this process; a restart loses in-flight runs.
        # Registered in docs/DEMO.md §7 gap 3 — move to a Redis queue past ~5 concurrent runs.
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ repos
    def list_repos(self) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        for spec in self.git.list_repos():
            out.append(
                RepoInfo(
                    repo_key=spec.repo_key,
                    name=spec.display_name,
                    url=spec.url,
                    default_branch=spec.default_branch,
                    description=spec.description,
                    has_credential=bool(spec.credential_env),
                    cached=(self.git.mirror_path(spec.repo_key) / "HEAD").exists(),
                    last_fetched_at=self.git.last_fetched_at(spec.repo_key),
                )
            )
        return out

    def resolve_ref(self, repo_key: str, ref: str | None) -> dict[str, Any]:
        commit = self.git.resolve_ref(repo_key, ref)
        branches = self.git.list_branches(repo_key)
        return {"repo_key": repo_key, "ref": ref, "commit_sha": commit, "branches": branches}

    # -------------------------------------------------------------------- runs
    def create(self, request: RunCreateRequest, actor: str) -> Run:
        """Persist a run and return it. Does **not** start it.

        Persisting and scheduling are split because a synchronous FastAPI route runs
        in a worker thread with no event loop, so ``asyncio.create_task`` would fail
        there. The async route calls :meth:`start` from inside the loop instead.
        """
        # Validate the repository before creating a record, so a typo produces a
        # clear 4xx instead of a run that fails thirty seconds later.
        spec = self.git.spec(request.repo_key)

        run = Run(
            id=uuid.uuid4().hex[:12],
            repo_key=request.repo_key,
            repo_url=spec.url,
            ref=request.ref,
            message=request.message.strip(),
            skills=list(request.skills),
            locale=request.locale,
            actor=actor,
            phases=default_phases(),
            artifacts=default_artifacts(),
        )
        self.db.save_run(run)
        self.db.audit(actor, "run.create", target=run.id, repo_key=run.repo_key)
        self.bus.log(run.id, f"Run queued for {run.repo_key}@{request.ref or spec.default_branch}")
        return run

    def start(self, run: Run) -> None:
        """Schedule a persisted run. Must be called from a running event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - programming error, fail loudly
            raise RuntimeError(
                "RunService.start() requires a running event loop; call it from an "
                "async route handler, not from a thread pool worker."
            ) from exc
        flag = self.cancels.create(run.id)
        task = asyncio.create_task(self._execute(run, flag), name=f"run-{run.id}")
        self._tasks[run.id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run.id, None))

    async def _execute(self, run: Run, flag: threading.Event) -> None:
        """Drive one run and publish exactly one terminal event.

        This method, not the pipeline, decides what the caller sees when the run ends. The
        pipeline records the outcome and re-raises; the ``finally`` below turns whatever it left
        behind into one ``done`` event. Emitting from both places is what previously produced two
        terminal events — and, because the pipeline swallowed the cancellation a wall-clock
        timeout is delivered as, made ``MAX_WALL_SECONDS`` report the run as *cancelled* while the
        timeout message below never ran.
        """
        pipeline = Pipeline(
            settings=self.settings,
            bus=self.bus,
            git=self.git,
            kb=self.kb,
            run=run,
            cancel_flag=flag,
        )
        try:
            await asyncio.wait_for(pipeline.execute(), timeout=self.settings.max_wall_seconds)
        except TimeoutError:
            # ``wait_for`` converts its own cancellation into TimeoutError, so reaching here means
            # the wall-clock limit is what stopped the run.
            run.status = RunStatus.FAILED
            run.error = (
                f"Run exceeded the wall-clock limit of {self.settings.max_wall_seconds}s "
                "and was stopped."
            )
            self.bus.log(run.id, run.error, level="error")
        except asyncio.CancelledError:
            # The pipeline has already logged "Run cancelled"; this branch only classifies it.
            run.status = RunStatus.CANCELLED
            run.error = run.error or "Run cancelled"
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = run.error or str(exc)[:600]
        finally:
            if run.status not in TERMINAL_STATUSES:
                # Only reachable if the task was stopped before the pipeline could classify the
                # outcome (service shutdown). Recording it here keeps a restart from leaving a
                # half-written status behind.
                run.status = RunStatus.CANCELLED
                run.error = run.error or "Run stopped before it completed."
            run.finished_at = run.finished_at or now_iso()
            self.bus.emit(
                run.id,
                EVENT_DONE,
                {
                    "run_id": run.id,
                    "status": str(run.status),
                    "terminal": True,
                    "error": run.error,
                    "usage": dict(run.usage),
                },
            )
            self.cancels.clear(run.id)
            self.db.save_run(run)

    def get(self, run_id: str) -> Run | None:
        return self.db.get_run(run_id)

    def list(
        self, *, status: str | None = None, repo_key: str | None = None, limit: int = 50
    ) -> list[Run]:
        return self.db.list_runs(status=status, repo_key=repo_key, limit=limit)

    def cancel(self, run_id: str, actor: str) -> bool:
        accepted = self.cancels.cancel(run_id)
        self.db.audit(actor, "run.cancel", target=run_id, result="accepted" if accepted else "noop")
        if accepted:
            self.bus.log(run_id, "Cancellation requested", level="warning")
        return accepted

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())

    def is_finished(self, run_id: str) -> bool:
        run = self.db.get_run(run_id)
        return bool(run and run.status in TERMINAL_STATUSES)

    async def shutdown(self) -> None:
        """Cancel in-flight tasks so the process exits cleanly."""
        for run_id, task in list(self._tasks.items()):
            self.cancels.cancel(run_id)
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # ------------------------------------------------------------- artifacts
    def artifact_path(self, run_id: str, kind: str) -> Path | None:
        """Filesystem path of a produced artifact, or ``None`` when it is not there yet."""
        run = self.db.get_run(run_id)
        if not run:
            return None
        entry = next((a for a in run.artifacts if a.kind == kind), None)
        if not entry or not entry.filename:
            return None
        target = self.git.artifacts_dir(run_id) / entry.filename
        return target if target.exists() and target.is_file() else None

    def preview(self, run_id: str, kind: str, *, limit: int = 500) -> dict[str, Any] | None:
        """Render an artifact for the UI.

        Workbooks are converted to rows server-side rather than shipping the XLSX
        to the browser: it keeps the client simple and avoids parsing an untrusted
        binary in the page. ``limit`` bounds the row count per sheet, and each sheet reports
        whether it was cut so the console can say so instead of guessing.
        """
        path = self.artifact_path(run_id, kind)
        if path is None:
            return None

        if path.suffix == ".md":
            return {"kind": kind, "type": "markdown", "content": path.read_text("utf-8")}

        if path.suffix == ".xlsx":
            wb = load_workbook(path, read_only=True, data_only=True)
            sheets: list[dict[str, Any]] = []
            for ws in wb.worksheets:
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    sheets.append({"name": ws.title, "columns": [], "rows": [], "truncated": False})
                    continue
                header = [str(c) if c is not None else "" for c in rows[0]]
                body = [
                    ["" if cell is None else cell for cell in row] for row in rows[1 : limit + 1]
                ]
                sheets.append(
                    {
                        "name": ws.title,
                        "columns": header,
                        "rows": body,
                        "truncated": len(rows) - 1 > limit,
                    }
                )
            return {
                "kind": kind,
                "type": "workbook",
                "sheets": sheets,
                "filename": path.name,
            }

        if path.suffix == ".json":
            return {
                "kind": kind,
                "type": "json",
                "content": json.loads(path.read_text("utf-8")),
            }

        return {"kind": kind, "type": "text", "content": path.read_text("utf-8", errors="replace")}

    # -------------------------------------------------------------------- kb
    def kb_query(self, request: KbQueryRequest) -> KbQueryResponse:
        return self.kb.query(request)

    def kb_reindex(self, run_id: str, actor: str) -> int:
        """Re-index one run's artifacts. Returns the number of chunks written."""
        run = self.db.get_run(run_id)
        if not run:
            return 0
        artifacts_dir = self.git.artifacts_dir(run_id)
        analysis: dict[str, Any] | None = None
        analysis_file = artifacts_dir / "analysis.json"
        if analysis_file.exists():
            analysis = json.loads(analysis_file.read_text("utf-8"))
        count = self.kb.index_run(run, analysis, artifacts_dir)
        self.db.audit(actor, "kb.reindex", target=run_id, chunks=count)
        return count

    def kb_stats(self) -> dict[str, Any]:
        chunks = self.db.all_chunks()
        by_kind: dict[str, int] = {}
        for chunk in chunks:
            by_kind[str(chunk.kind)] = by_kind.get(str(chunk.kind), 0) + 1
        return {
            "chunks": len(chunks),
            "runs_indexed": len({c.run_id for c in chunks}),
            "by_kind": by_kind,
            "retriever": self.kb.retriever.name,
        }

    def delete(self, run_id: str, actor: str, *, purge_files: bool = False) -> bool:
        run = self.db.get_run(run_id)
        if not run:
            return False
        self.db.delete_run(run_id)
        if purge_files:
            self.git.unseal_worktree(self.git.worktree_dir(run_id))
            shutil.rmtree(self.git.run_dir(run_id), ignore_errors=True)
        self.db.audit(actor, "run.delete", target=run_id, purge_files=purge_files)
        return True

    # ----------------------------------------------------------------- audit
    def audit(
        self, actor: str, action: str, target: str = "", result: str = "ok", **detail: Any
    ) -> None:
        """Record an audited action.

        Routes call this instead of reaching into ``db`` themselves: the audit sink is a service
        concern, and keeping it here is what leaves the route layer able to talk to services only
        (CODESTYLE.md §2.1).
        """
        self.db.audit(actor, action, target, result, **detail)

    def recent_audit(self, limit: int = 100) -> list[AuditEntry]:
        return [AuditEntry.model_validate(row) for row in self.db.recent_audit(limit=limit)]

    # ------------------------------------------------------------------ logs
    def log_entries(
        self,
        run_id: str,
        *,
        level: str | None = None,
        q: str | None = None,
        limit: int = 500,
    ) -> list[RunLogEntry]:
        """Flatten a run's event journal into the log view the console shows.

        A projection, not a second journal: lifecycle events become one line each so a reader can
        scan them beside the messages the pipeline logged itself.
        """
        entries: list[RunLogEntry] = []
        for envelope in self.bus.read_since(run_id, 0):
            data = envelope.get("data", {})
            if envelope["event"] == "log":
                entry = RunLogEntry(
                    seq=envelope["seq"],
                    ts=envelope["ts"],
                    level=data.get("level", "info"),
                    message=data.get("message", ""),
                )
            elif envelope["event"] in ("phase", "artifact", "tool"):
                entry = RunLogEntry(
                    seq=envelope["seq"],
                    ts=envelope["ts"],
                    level="info",
                    message=f"[{envelope['event']}] "
                    + " ".join(f"{k}={v}" for k, v in data.items() if k != "usage"),
                )
            else:
                continue
            if level and entry.level != level:
                continue
            if q and q.lower() not in entry.message.lower():
                continue
            entries.append(entry)
        return entries[-limit:]
