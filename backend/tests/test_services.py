"""Service-level unit tests for the small invariants the pipeline relies on.

Each of these pins a bug that was invisible in review and would have surfaced as wrong data
rather than as a crash:

* the knowledge base gave a chunk a *randomised* id, because ``hash()`` is per-process;
* the console was told a repository had just been fetched whenever a mirror existed;
* the event journal restarted its sequence numbers after a process restart, which makes a
  client's ``Last-Event-ID`` cursor skip every subsequent event.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.domain.models import ArtifactKind
from app.services.events import RunEventBus
from app.services.kb_service import _chunk


def test_chunk_ids_are_derived_from_the_chunk_not_the_process(tmp_path: Path) -> None:
    """A stable digest, pinned so a future change has to be deliberate.

    ``hash()`` over a tuple of strings is salted per interpreter, so the previous implementation
    produced a different id for identical input after a restart.
    """
    chunk = _chunk("run1", "core-banking", "abc123", ArtifactKind.FDD, "Title", "body", "#anchor")
    assert chunk.id == "run1:fdd:bf257562ff40"
    assert (
        chunk.id
        == _chunk("run1", "core-banking", "abc123", ArtifactKind.FDD, "Title", "body", "#anchor").id
    )
    assert (
        chunk.id
        != _chunk("run2", "core-banking", "abc123", ArtifactKind.FDD, "Title", "body", "#anchor").id
    )


def test_event_sequence_continues_after_a_restart(tmp_path: Path) -> None:
    """The journal, not memory, decides the next sequence number."""
    bus = RunEventBus(tmp_path)
    bus.emit("run1", "log", {"message": "first"})
    bus.emit("run1", "log", {"message": "second"})

    # A new bus instance is what a restarted service looks like.
    reopened = RunEventBus(tmp_path)
    assert reopened.emit("run1", "log", {"message": "third"})["seq"] == 3


def test_replay_cursor_still_works_across_a_restart(tmp_path: Path) -> None:
    """A client that already processed seq 2 must receive only what came after it."""
    bus = RunEventBus(tmp_path)
    bus.emit("run1", "log", {"message": "first"})
    bus.emit("run1", "log", {"message": "second"})

    reopened = RunEventBus(tmp_path)
    reopened.emit("run1", "log", {"message": "third"})
    replayed = [e["data"]["message"] for e in reopened.read_since("run1", 2)]
    assert replayed == ["third"]


def test_last_fetched_at_reports_the_recorded_time(settings, git_service) -> None:
    """The console shows when the remote was last contacted, not when it was asked."""
    assert git_service.last_fetched_at("demo") is None

    stamp = git_service._fetch_stamp("demo")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    recorded = int(time.time()) - 3600
    stamp.write_text(str(recorded), encoding="utf-8")

    value = git_service.last_fetched_at("demo")
    assert value is not None
    assert value.startswith(time.strftime("%Y-%m-%dT%H", time.gmtime(recorded)))
    assert 3500 < git_service.mirror_age_seconds("demo") < 3700
