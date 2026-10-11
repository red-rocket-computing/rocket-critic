"""Review history kept in a small SQLite database inside the workdir."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    head_sha    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- running | done | failed | skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    cost_usd    REAL,
    duration_ms INTEGER,
    num_turns   INTEGER,
    output_path TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (repo, pr, head_sha)
);
"""

DONE = "done"
FAILED = "failed"
RUNNING = "running"
SKIPPED = "skipped"


@dataclass
class Record:
    repo: str
    pr: int
    head_sha: str
    status: str
    attempts: int
    error: str | None
    cost_usd: float | None
    duration_ms: int | None
    num_turns: int | None
    output_path: str | None
    updated_at: float


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def get(self, repo: str, pr: int, head_sha: str) -> Record | None:
        row = self.db.execute(
            "SELECT * FROM reviews WHERE repo = ? AND pr = ? AND head_sha = ?", (repo, pr, head_sha)
        ).fetchone()
        return Record(**row) if row else None

    def any_done(self, repo: str, pr: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM reviews WHERE repo = ? AND pr = ? AND status = ? LIMIT 1", (repo, pr, DONE)
        ).fetchone()
        return row is not None

    def start(self, repo: str, pr: int, head_sha: str) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO reviews (repo, pr, head_sha, status, attempts, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT (repo, pr, head_sha)
                   DO UPDATE SET status = excluded.status, attempts = attempts + 1, error = NULL,
                                 updated_at = excluded.updated_at""",
                (repo, pr, head_sha, RUNNING, time.time()),
            )

    def finish(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        status: str,
        *,
        error: str | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        num_turns: int | None = None,
        output_path: str | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO reviews (repo, pr, head_sha, status, attempts, error, cost_usd, duration_ms,
                                        num_turns, output_path, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (repo, pr, head_sha)
                   DO UPDATE SET status = excluded.status, error = excluded.error, cost_usd = excluded.cost_usd,
                                 duration_ms = excluded.duration_ms, num_turns = excluded.num_turns,
                                 output_path = excluded.output_path, updated_at = excluded.updated_at""",
                (repo, pr, head_sha, status, error, cost_usd, duration_ms, num_turns, output_path, time.time()),
            )

    def recent(self, repo: str | None = None, limit: int = 50) -> list[Record]:
        if repo:
            rows = self.db.execute(
                "SELECT * FROM reviews WHERE repo = ? ORDER BY updated_at DESC LIMIT ?", (repo, limit)
            )
        else:
            rows = self.db.execute("SELECT * FROM reviews ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [Record(**row) for row in rows]

    def reset_running(self) -> int:
        """Mark runs left 'running' by a crashed process as failed so they are retried."""
        with self.db:
            cur = self.db.execute(
                "UPDATE reviews SET status = ?, error = ? WHERE status = ?",
                (FAILED, "interrupted", RUNNING),
            )
        return cur.rowcount
