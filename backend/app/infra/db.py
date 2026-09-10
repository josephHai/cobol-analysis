"""SQLite persistence for runs, artifacts, knowledge chunks and audit entries.

Intentionally dependency-free (stdlib ``sqlite3``): the demo host has no Postgres,
and the schema is small. Everything is accessed through :class:`Database`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.domain.models import (
    KbChunk,
    Run,
    RunStatus,
    now_iso,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    repo_key      TEXT NOT NULL,
    commit_sha    TEXT,
    entrypoint    TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'anonymous',
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_repo    ON runs(repo_key);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    repo_key   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_run  ON kb_chunks(run_id);
CREATE INDEX IF NOT EXISTS idx_chunks_repo ON kb_chunks(repo_key);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    result  TEXT NOT NULL DEFAULT 'ok',
    detail  TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                yield conn
            finally:
                conn.close()

    # ------------------------------------------------------------------ runs
    def save_run(self, run: Run) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, status, repo_key, commit_sha, entrypoint, actor,
                                  payload, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, commit_sha=excluded.commit_sha,
                    payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (
                    run.id,
                    str(run.status),
                    run.repo_key,
                    run.commit_sha,
                    run.entrypoint,
                    run.actor,
                    run.model_dump_json(),
                    run.created_at,
                    now_iso(),
                ),
            )

    def get_run(self, run_id: str) -> Run | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        return Run.model_validate_json(row["payload"]) if row else None

    def list_runs(
        self, *, status: str | None = None, repo_key: str | None = None, limit: int = 50
    ) -> list[Run]:
        sql = "SELECT payload FROM runs"
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status=?")
            args.append(status)
        if repo_key:
            where.append("repo_key=?")
            args.append(repo_key)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [Run.model_validate_json(r["payload"]) for r in rows]

    def delete_run(self, run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            conn.execute("DELETE FROM kb_chunks WHERE run_id=?", (run_id,))

    def reset_stale_runs(self) -> int:
        """Runs left 'running' by a crash are marked failed on startup."""
        stale = (
            "fetching",
            "analyzing",
            "generating",
            "validating",
            "archiving",
            "indexing_kb",
            "queued",
        )
        changed = 0
        with self.connect() as conn:
            for status in stale:
                rows = conn.execute("SELECT payload FROM runs WHERE status=?", (status,)).fetchall()
                for row in rows:
                    run = Run.model_validate_json(row["payload"])
                    run.status = RunStatus.FAILED
                    run.error = "Interrupted by a service restart"
                    run.finished_at = now_iso()
                    conn.execute(
                        "UPDATE runs SET status=?, payload=?, updated_at=? WHERE id=?",
                        (str(run.status), run.model_dump_json(), now_iso(), run.id),
                    )
                    changed += 1
        return changed

    # ----------------------------------------------------------------- chunks
    def replace_chunks(self, run_id: str, chunks: Iterable[KbChunk]) -> int:
        items = list(chunks)
        with self.connect() as conn:
            conn.execute("DELETE FROM kb_chunks WHERE run_id=?", (run_id,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO kb_chunks (id, run_id, repo_key, kind, title, text, payload)
                VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        c.id,
                        c.run_id,
                        c.repo_key,
                        str(c.kind),
                        c.title,
                        c.text,
                        c.model_dump_json(),
                    )
                    for c in items
                ],
            )
        return len(items)

    def all_chunks(self, *, repo_key: str | None = None) -> list[KbChunk]:
        sql = "SELECT payload FROM kb_chunks"
        args: list[Any] = []
        if repo_key:
            sql += " WHERE repo_key=?"
            args.append(repo_key)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [KbChunk.model_validate_json(r["payload"]) for r in rows]

    def chunks_for_run(self, run_id: str) -> list[KbChunk]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM kb_chunks WHERE run_id=?", (run_id,)
            ).fetchall()
        return [KbChunk.model_validate_json(r["payload"]) for r in rows]

    # ------------------------------------------------------------------ audit
    def audit(
        self, actor: str, action: str, target: str = "", result: str = "ok", **detail: Any
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, result, detail) VALUES (?,?,?,?,?,?)",
                (now_iso(), actor, action, target, result, json.dumps(detail, ensure_ascii=False)),
            )

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
