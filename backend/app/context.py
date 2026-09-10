"""Application-wide service container.

One instance is created at startup and attached to ``app.state``. Routes resolve it
through :func:`get_ctx` rather than importing services directly, which keeps the
route layer free of construction concerns and makes the whole container replaceable
in tests.

This is the *only* place where the object graph is wired; adding a service means
adding it here and nowhere else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings
from app.infra.db import Database
from app.services.events import CancelRegistry, RunEventBus
from app.services.git_service import GitService
from app.services.kb_service import KnowledgeBase
from app.services.run_service import RunService

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    bus: RunEventBus
    git: GitService
    kb: KnowledgeBase
    cancels: CancelRegistry
    runs: RunService

    @classmethod
    def create(cls, settings: Settings) -> AppContext:
        settings.ensure_dirs()
        db = Database(settings.db_path)
        bus = RunEventBus(settings.runs_dir)
        git = GitService(settings)
        kb = KnowledgeBase(db)
        cancels = CancelRegistry()

        # Runs left mid-flight by a crash can never be resumed with an in-process
        # worker, so mark them failed now instead of leaving them "running" forever.
        stale = db.reset_stale_runs()
        if stale:
            logger.warning("marked %d interrupted run(s) as failed on startup", stale)

        runs = RunService(settings=settings, db=db, bus=bus, git=git, kb=kb, cancels=cancels)
        logger.info(
            "context ready: data_root=%s repos=%d model=%s",
            settings.data_root,
            len(git.list_repos()),
            settings.llm_model,
        )
        return cls(settings=settings, db=db, bus=bus, git=git, kb=kb, cancels=cancels, runs=runs)

    async def shutdown(self) -> None:
        await self.runs.shutdown()


def get_ctx(request: Request) -> AppContext:
    """Dependency: the process-wide context.

    The ``Request`` annotation is load-bearing: without it FastAPI treats the
    parameter as a query field and every request fails validation.
    """
    return request.app.state.ctx
