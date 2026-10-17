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
    status      TEXT NOT NULL,          -- pending | running | done | failed | skipped | attention
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    cost_usd    REAL,
    duration_ms INTEGER,
    num_turns   INTEGER,
    output_path TEXT,
    updated_at  REAL NOT NULL,
    started_at  REAL,                   -- when the latest attempt started
    phase       TEXT,                   -- checkout | review: how far the latest attempt got
    done_count  INTEGER NOT NULL DEFAULT 0,  -- successful reviews of this commit (re-requests add more)
    PRIMARY KEY (repo, pr, head_sha)
);
CREATE TABLE IF NOT EXISTS comment_watch (
    repo        TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,          -- commands posted before this are ignored
    last_poll   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handled_comments (
    repo        TEXT NOT NULL,
    comment_id  INTEGER NOT NULL,
    PRIMARY KEY (repo, comment_id)
);
"""

# Columns added after the first release, with their SQL types.
_MIGRATIONS = {"started_at": "REAL", "phase": "TEXT", "done_count": "INTEGER NOT NULL DEFAULT 0"}

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
ATTENTION = "attention"

PHASE_CHECKOUT = "checkout"
PHASE_REVIEW = "review"
PHASE_POST = "post"  # rocket-critic posting the review (posting = "rocket-critic")


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
    started_at: float | None = None
    phase: str | None = None
    done_count: int = 0


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {row["name"] for row in self.db.execute("PRAGMA table_info(reviews)")}
        with self.db:
            for column, sql_type in _MIGRATIONS.items():
                if column not in have:
                    self.db.execute(f"ALTER TABLE reviews ADD COLUMN {column} {sql_type}")
            if "done_count" not in have:
                self.db.execute("UPDATE reviews SET done_count = 1 WHERE status = ?", (DONE,))

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

    def count_done(self, repo: str, pr: int) -> int:
        """Successful reviews of ``pr``, counting repeat reviews of the same commit."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(done_count), 0) FROM reviews WHERE repo = ? AND pr = ?", (repo, pr)
        ).fetchone()
        return row[0]

    def last_done(self, repo: str, pr: int) -> Record | None:
        """The most recent successful review of ``pr``."""
        row = self.db.execute(
            "SELECT * FROM reviews WHERE repo = ? AND pr = ? AND status = ? ORDER BY updated_at DESC LIMIT 1",
            (repo, pr, DONE),
        ).fetchone()
        return Record(**row) if row else None

    def mark_pending(self, repo: str, pr: int, head_sha: str) -> None:
        """Record when a new head was first seen (its ``updated_at``), for the settle delay.

        Pending rows for heads this one superseded are dropped.
        """
        with self.db:
            self.db.execute(
                "DELETE FROM reviews WHERE repo = ? AND pr = ? AND status = ? AND head_sha != ?",
                (repo, pr, PENDING, head_sha),
            )
            self.db.execute(
                """INSERT INTO reviews (repo, pr, head_sha, status, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (repo, pr, head_sha) DO NOTHING""",
                (repo, pr, head_sha, PENDING, time.time()),
            )

    def start(self, repo: str, pr: int, head_sha: str) -> None:
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT INTO reviews (repo, pr, head_sha, status, attempts, updated_at, started_at, phase)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT (repo, pr, head_sha)
                   DO UPDATE SET status = excluded.status, error = NULL,
                                 -- a finished commit being reviewed again starts a fresh round of attempts
                                 attempts = CASE WHEN status IN (?, ?, ?) THEN 1 ELSE attempts + 1 END,
                                 updated_at = excluded.updated_at, started_at = excluded.started_at,
                                 phase = excluded.phase""",
                (repo, pr, head_sha, RUNNING, now, now, PHASE_CHECKOUT, DONE, SKIPPED, ATTENTION),
            )

    def set_phase(self, repo: str, pr: int, head_sha: str, phase: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE reviews SET phase = ? WHERE repo = ? AND pr = ? AND head_sha = ?",
                (phase, repo, pr, head_sha),
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
                                        num_turns, output_path, updated_at, done_count)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (repo, pr, head_sha)
                   DO UPDATE SET status = excluded.status, error = excluded.error,
                                 done_count = done_count + excluded.done_count,
                                 cost_usd = coalesce(excluded.cost_usd, cost_usd),
                                 duration_ms = coalesce(excluded.duration_ms, duration_ms),
                                 num_turns = coalesce(excluded.num_turns, num_turns),
                                 output_path = coalesce(excluded.output_path, output_path),
                                 updated_at = excluded.updated_at""",
                (repo, pr, head_sha, status, error, cost_usd, duration_ms, num_turns, output_path, time.time(),
                 1 if status == DONE else 0),
            )

    def recent(self, repo: str | None = None, limit: int = 50) -> list[Record]:
        if repo:
            rows = self.db.execute(
                "SELECT * FROM reviews WHERE repo = ? ORDER BY updated_at DESC LIMIT ?", (repo, limit)
            )
        else:
            rows = self.db.execute("SELECT * FROM reviews ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [Record(**row) for row in rows]

    def comment_watch(self, repo: str) -> tuple[float, float]:
        """``(started_at, last_poll)`` for comment commands in ``repo``, starting the watch now if new."""
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO comment_watch (repo, started_at, last_poll) VALUES (?, ?, ?) ON CONFLICT (repo) DO NOTHING",
                (repo, now, now),
            )
        row = self.db.execute("SELECT started_at, last_poll FROM comment_watch WHERE repo = ?", (repo,)).fetchone()
        return row["started_at"], row["last_poll"]

    def set_comment_poll(self, repo: str, when: float) -> None:
        with self.db:
            self.db.execute("UPDATE comment_watch SET last_poll = ? WHERE repo = ?", (when, repo))

    def comment_handled(self, repo: str, comment_id: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM handled_comments WHERE repo = ? AND comment_id = ?", (repo, comment_id)
        ).fetchone()
        return row is not None

    def mark_comment_handled(self, repo: str, comment_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO handled_comments (repo, comment_id) VALUES (?, ?)",
                            (repo, comment_id))

    def reset_running(self) -> int:
        """Mark runs left 'running' by a crashed process as failed.

        ``phase`` and ``started_at`` are kept, so the retry logic can check
        whether the interrupted run already posted anything.
        """
        with self.db:
            cur = self.db.execute(
                "UPDATE reviews SET status = ?, error = ? WHERE status = ?",
                (FAILED, "interrupted", RUNNING),
            )
        return cur.rowcount
