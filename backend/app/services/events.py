"""Run event log + live subscribers, powering the SSE stream.

Events are appended to ``<run_dir>/events.ndjson`` (source of truth, survives
restart, supports replay) and fanned out to in-process subscribers for live
delivery. On reconnect the client sends ``Last-Event-ID`` and the reader replays
from that sequence number.

For a single-host deployment this removes the need for Redis/Kafka entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from app.domain.models import now_iso

# Event names shared with the frontend (see docs/DESIGN.md §4.4)
EVENT_STATUS = "run.status"
EVENT_PHASE = "phase"
EVENT_TOOL = "tool"
EVENT_LOG = "log"
EVENT_ARTIFACT = "artifact"
EVENT_ERROR = "error"
EVENT_DONE = "done"


class RunEventBus:
    """Append-only event journal per run, with in-process live fan-out."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._seq: dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths
    def log_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "events.ndjson"

    def _ensure(self, run_id: str) -> Path:
        path = self.log_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ write
    def emit(self, run_id: str, event: str, data: dict[str, Any]) -> dict[str, Any]:
        """Append one event and push it to live subscribers. Returns the envelope."""
        with self._lock:
            # The journal is the source of truth for the sequence number, not this dictionary:
            # after a restart the in-memory counter is empty, and numbering from 1 again would
            # hand out sequence numbers a client has already processed — its Last-Event-ID
            # cursor would then skip every replayed event for the rest of the run.
            if run_id not in self._seq:
                self._seq[run_id] = self.last_seq(run_id)
            self._seq[run_id] += 1
            envelope = {
                "seq": self._seq[run_id],
                "ts": now_iso(),
                "event": event,
                "data": data,
            }
            path = self._ensure(run_id)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            subs = list(self._subs.get(run_id, ()))
        for queue in subs:
            # A slow consumer must never block or fail the producer; dropping the frame
            # is safe because the journal remains the source of truth for replay.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(envelope)
        return envelope

    def status(self, run_id: str, status: str, **extra: Any) -> None:
        self.emit(run_id, EVENT_STATUS, {"run_id": run_id, "status": status, **extra})

    def log(self, run_id: str, message: str, level: str = "info", **extra: Any) -> None:
        self.emit(run_id, EVENT_LOG, {"level": level, "message": message, **extra})

    # ------------------------------------------------------------------- read
    def read_since(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        path = self.log_path(run_id)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("seq", 0) > after_seq:
                    out.append(item)
        return out

    def last_seq(self, run_id: str) -> int:
        items = self.read_since(run_id, 0)
        return items[-1]["seq"] if items else 0

    # --------------------------------------------------------------- subscribe
    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        with self._lock:
            self._subs[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subs.get(run_id, set()).discard(queue)

    async def stream(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        is_finished: Callable[[], bool] | None = None,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield envelopes: replay the journal, then follow live until finished.

        ``is_finished`` is polled so the stream terminates even if the ``done``
        event was missed (e.g. the run failed between journal writes).
        """
        cursor = after_seq
        for item in self.read_since(run_id, after_seq):
            cursor = item["seq"]
            yield item

        queue = self.subscribe(run_id)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=poll_interval)
                except TimeoutError:
                    # catch anything written by another process/worker
                    for extra in self.read_since(run_id, cursor):
                        cursor = extra["seq"]
                        yield extra
                    if is_finished and is_finished():
                        break
                    continue
                if item["seq"] <= cursor:
                    continue
                cursor = item["seq"]
                yield item
                if item["event"] in (EVENT_DONE, EVENT_ERROR) and item.get("data", {}).get(
                    "terminal"
                ):
                    break
        finally:
            self.unsubscribe(run_id, queue)


class CancelRegistry:
    """Cancellation flags for running jobs (single process, in-memory)."""

    def __init__(self) -> None:
        self._flags: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def create(self, run_id: str) -> threading.Event:
        with self._lock:
            flag = threading.Event()
            self._flags[run_id] = flag
            return flag

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            flag = self._flags.get(run_id)
        if flag:
            flag.set()
            return True
        return False

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            flag = self._flags.get(run_id)
        return bool(flag and flag.is_set())

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._flags.pop(run_id, None)
