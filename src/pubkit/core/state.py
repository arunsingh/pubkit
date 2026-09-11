# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Idempotency and resumption.

A publishing run is a distributed transaction across platforms you do not
control, over links that drop. During the run that motivated this tool the
automation bridge disconnected eight times and the browser extension died
mid-publish. The only sane response is to make every step resumable and every
step safe to re-enter.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    plan_hash   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    status      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    run_id      TEXT NOT NULL,
    document_id TEXT NOT NULL,
    platform    TEXT NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,
    content_id  TEXT,
    fingerprint TEXT,
    remote_ref  TEXT,
    error       TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (run_id, document_id, platform, step)
);
CREATE TABLE IF NOT EXISTS remotes (
    document_id TEXT NOT NULL,
    platform    TEXT NOT NULL,
    content_id  TEXT NOT NULL,
    remote_ref  TEXT NOT NULL,
    url         TEXT,
    published   INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (document_id, platform)
);
CREATE TABLE IF NOT EXISTS channel_tuning (
    channel     TEXT PRIMARY KEY,
    chunk_size  INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class Step(str, Enum):
    AUTH = "auth"
    DRAFT = "draft"
    CONTENT = "content"
    MEDIA = "media"
    VERIFY = "verify"
    PUBLISH = "publish"

    @classmethod
    def order(cls) -> list[Step]:
        return [cls.AUTH, cls.DRAFT, cls.CONTENT, cls.MEDIA, cls.VERIFY, cls.PUBLISH]


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class RemoteRecord:
    document_id: str
    platform: str
    content_id: str
    remote_ref: str
    url: str | None
    published: bool


class StateStore:
    def __init__(self, path: Path | str = ".pubkit/state.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ runs
    def start_run(self, run_id: str, plan_hash: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
            (run_id, plan_hash, time.time(), Status.RUNNING.value),
        )
        self._conn.commit()

    def finish_run(self, run_id: str, status: Status) -> None:
        self._conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status.value, run_id))
        self._conn.commit()

    # ----------------------------------------------------------------- steps
    def step_status(self, run_id: str, doc: str, platform: str, step: Step) -> Status:
        row = self._conn.execute(
            "SELECT status FROM steps WHERE run_id=? AND document_id=? AND platform=? AND step=?",
            (run_id, doc, platform, step.value),
        ).fetchone()
        return Status(row["status"]) if row else Status.PENDING

    def record_step(
        self,
        run_id: str,
        doc: str,
        platform: str,
        step: Step,
        status: Status,
        *,
        content_id: str | None = None,
        fingerprint: dict | None = None,
        remote_ref: str | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                doc,
                platform,
                step.value,
                status.value,
                content_id,
                json.dumps(fingerprint) if fingerprint else None,
                remote_ref,
                error,
                time.time(),
            ),
        )
        self._conn.commit()

    def resume_point(self, run_id: str, doc: str, platform: str) -> Step:
        """First step that is not `done`. Re-running picks up exactly here."""
        for step in Step.order():
            if self.step_status(run_id, doc, platform, step) is not Status.DONE:
                return step
        return Step.PUBLISH

    # --------------------------------------------------------------- remotes
    def remember_remote(
        self, doc: str, platform: str, content_id: str, ref: str, url: str | None = None, published: bool = False
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO remotes VALUES (?,?,?,?,?,?,?)",
            (doc, platform, content_id, ref, url, int(published), time.time()),
        )
        self._conn.commit()

    def remote(self, doc: str, platform: str) -> RemoteRecord | None:
        row = self._conn.execute(
            "SELECT * FROM remotes WHERE document_id=? AND platform=?", (doc, platform)
        ).fetchone()
        if not row:
            return None
        return RemoteRecord(
            document_id=row["document_id"],
            platform=row["platform"],
            content_id=row["content_id"],
            remote_ref=row["remote_ref"],
            url=row["url"],
            published=bool(row["published"]),
        )

    def needs_update(self, doc: str, platform: str, content_id: str) -> bool:
        """False when the remote already holds exactly this content.

        This is what stops a re-run creating a second draft — the mistake that
        turns a retry into a mess someone has to clean up by hand (failure D2).
        """
        rec = self.remote(doc, platform)
        return rec is None or rec.content_id != content_id

    # -------------------------------------------------------- channel tuning
    def tuned_chunk_size(self, channel: str) -> int | None:
        row = self._conn.execute(
            "SELECT chunk_size FROM channel_tuning WHERE channel=?", (channel,)
        ).fetchone()
        return row["chunk_size"] if row else None

    def save_chunk_size(self, channel: str, size: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO channel_tuning VALUES (?,?,?)", (channel, size, time.time())
        )
        self._conn.commit()

    @contextmanager
    def transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()
