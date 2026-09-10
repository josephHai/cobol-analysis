"""Run lifecycle tests: how a run ends, and what the caller is told.

No model and no network: the pipeline is replaced by a stub, because what is under test is the
*classification* of an ending — wall-clock timeout, cancellation, failure, success — and the fact
that exactly one terminal event is published for each.

These are the properties the console depends on. Two of them were wrong before this file existed:
a wall-clock timeout was recorded as "cancelled" (the pipeline swallowed the cancellation
``wait_for`` delivered), and the caller's timeout branch published a second ``done`` event on top
of the one the pipeline had already emitted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.models import (
    TERMINAL_STATUSES,
    Run,
    RunStatus,
    default_artifacts,
    default_phases,
)
from app.infra.db import Database
from app.services import run_service as run_service_module
from app.services.events import CancelRegistry, RunEventBus
from app.services.kb_service import KnowledgeBase
from app.services.run_service import RunService


def _stub_pipeline(mode: str) -> type:
    """A pipeline stand-in whose behaviour is the named ending."""

    class _StubPipeline:
        def __init__(self, **kwargs: Any) -> None:
            self.run: Run = kwargs["run"]

        async def execute(self) -> None:
            if mode == "hang":
                await asyncio.sleep(30)
            elif mode == "cancel":
                self.run.status = RunStatus.CANCELLED
                self.run.error = "Run cancelled"
                raise asyncio.CancelledError("run cancelled by user")
            elif mode == "fail":
                self.run.status = RunStatus.FAILED
                self.run.error = "the analysis skill produced nothing"
                raise RuntimeError("the analysis skill produced nothing")
            self.run.status = RunStatus.COMPLETED

    return _StubPipeline


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch):
    """Install a stub pipeline; call with the ending the test wants."""

    def _install(mode: str) -> None:
        monkeypatch.setattr(run_service_module, "Pipeline", _stub_pipeline(mode))

    return _install


@pytest.fixture
def make_service(settings, git_service):
    """Build a ``RunService``, optionally with settings overridden."""

    def _make(**overrides) -> RunService:
        cfg = settings.model_copy(update=overrides) if overrides else settings
        db = Database(cfg.db_path)
        return RunService(
            settings=cfg,
            db=db,
            bus=RunEventBus(cfg.runs_dir),
            git=git_service,
            kb=KnowledgeBase(db),
            cancels=CancelRegistry(),
        )

    return _make


def _pending_run(service: RunService) -> Run:
    run = Run(
        id="lifecycle",
        repo_key="demo",
        message="Analyse PGM001 for the lifecycle test",
        phases=default_phases(),
        artifacts=default_artifacts(),
    )
    service.db.save_run(run)
    return run


def _done_events(service: RunService, run_id: str) -> list[dict]:
    return [e for e in service.bus.read_since(run_id, 0) if e["event"] == "done"]


def _stored_run(service: RunService, run_id: str) -> Run:
    stored = service.db.get_run(run_id)
    assert stored is not None, f"run {run_id} was not persisted"
    return stored


def test_success_publishes_one_terminal_event(make_service, stub_pipeline) -> None:
    stub_pipeline("complete")
    service = make_service()
    run = _pending_run(service)
    asyncio.run(service._execute(run, CancelRegistry().create(run.id)))

    done = _done_events(service, run.id)
    assert len(done) == 1, f"expected exactly one done event, got {len(done)}"
    assert done[0]["data"]["status"] == "completed"
    assert _stored_run(service, run.id).status is RunStatus.COMPLETED


def test_wall_clock_timeout_is_a_failure_not_a_cancellation(make_service, stub_pipeline) -> None:
    """The limit that stopped the run must be the one reported.

    ``asyncio.wait_for`` raises ``TimeoutError`` only when the cancellation it sends actually
    propagates, so a pipeline that caught ``CancelledError`` and returned normally would turn
    every timeout into "cancelled" and never reach the timeout message at all.
    """
    stub_pipeline("hang")
    service = make_service(max_wall_seconds=1)
    run = _pending_run(service)
    asyncio.run(service._execute(run, CancelRegistry().create(run.id)))

    stored = _stored_run(service, run.id)
    assert stored.status is RunStatus.FAILED
    assert "wall-clock limit" in (stored.error or "")
    done = _done_events(service, run.id)
    assert len(done) == 1, f"expected exactly one done event, got {len(done)}"
    assert done[0]["data"]["status"] == "failed"


def test_cancellation_publishes_one_terminal_event(make_service, stub_pipeline) -> None:
    stub_pipeline("cancel")
    service = make_service()
    run = _pending_run(service)
    asyncio.run(service._execute(run, CancelRegistry().create(run.id)))

    assert _stored_run(service, run.id).status is RunStatus.CANCELLED
    done = _done_events(service, run.id)
    assert len(done) == 1, f"expected exactly one done event, got {len(done)}"
    assert done[0]["data"]["status"] == "cancelled"


def test_pipeline_failure_keeps_the_pipeline_message(make_service, stub_pipeline) -> None:
    stub_pipeline("fail")
    service = make_service()
    run = _pending_run(service)
    asyncio.run(service._execute(run, CancelRegistry().create(run.id)))

    stored = _stored_run(service, run.id)
    assert stored.status is RunStatus.FAILED
    assert "produced nothing" in (stored.error or "")
    assert len(_done_events(service, run.id)) == 1


def test_every_ending_leaves_a_terminal_status(make_service, stub_pipeline) -> None:
    stub_pipeline("complete")
    service = make_service()
    run = _pending_run(service)
    asyncio.run(service._execute(run, CancelRegistry().create(run.id)))
    assert _stored_run(service, run.id).status in TERMINAL_STATUSES
