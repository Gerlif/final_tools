"""Persistent bookkeeping so a restart never re-uploads what is already there."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"
STATUS_NO_MATCH = "no_match"
STATUS_BASELINE = "baseline"
STATUS_GIVEN_UP = "given_up"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path              TEXT PRIMARY KEY,
    size              INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL,
    status            TEXT    NOT NULL,
    frameio_file_id   TEXT,
    frameio_folder_id TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    first_seen        REAL    NOT NULL,
    updated_at        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS files_status_idx ON files (status);
"""


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    mtime_ns: int
    status: str
    frameio_file_id: str | None
    frameio_folder_id: str | None
    attempts: int
    last_error: str | None
    first_seen: float
    updated_at: float

    def matches(self, size: int, mtime_ns: int) -> bool:
        """True when the file on disk is byte-for-byte the one we recorded."""
        return self.size == size and self.mtime_ns == mtime_ns


class StateStore:
    """SQLite-backed state, safe to use from the scanner and upload threads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, path: str) -> FileRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE path = ?", (path,)
            ).fetchone()
        return _to_record(row) if row else None

    def record(
        self,
        path: str,
        *,
        size: int,
        mtime_ns: int,
        status: str,
        file_id: str | None = None,
        folder_id: str | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        """Insert or update the row for ``path``."""
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT attempts, first_seen, size, mtime_ns FROM files WHERE path = ?",
                (path,),
            ).fetchone()
            if existing is None:
                next_attempts = attempts if attempts is not None else 0
                first_seen = now
            else:
                changed = existing["size"] != size or existing["mtime_ns"] != mtime_ns
                if attempts is not None:
                    next_attempts = attempts
                elif changed:
                    # A re-export of the same name starts its own attempt count.
                    next_attempts = 0
                else:
                    next_attempts = existing["attempts"]
                first_seen = existing["first_seen"]
            self._conn.execute(
                """
                INSERT INTO files (path, size, mtime_ns, status, frameio_file_id,
                                   frameio_folder_id, attempts, last_error,
                                   first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    status = excluded.status,
                    frameio_file_id = COALESCE(excluded.frameio_file_id, files.frameio_file_id),
                    frameio_folder_id = COALESCE(excluded.frameio_folder_id, files.frameio_folder_id),
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    path,
                    size,
                    mtime_ns,
                    status,
                    file_id,
                    folder_id,
                    next_attempts,
                    error,
                    first_seen,
                    now,
                ),
            )
            self._conn.commit()

    def bump_failure(
        self, path: str, *, size: int, mtime_ns: int, error: str, max_attempts: int
    ) -> int:
        """Record a failed attempt and return the new attempt count."""
        record = self.get(path)
        attempts = (record.attempts if record and record.matches(size, mtime_ns) else 0) + 1
        status = STATUS_GIVEN_UP if attempts >= max_attempts else STATUS_FAILED
        self.record(
            path,
            size=size,
            mtime_ns=mtime_ns,
            status=status,
            error=error[:2000],
            attempts=attempts,
        )
        return attempts

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def recent(self, limit: int = 20, status: str | None = None) -> list[FileRecord]:
        query = "SELECT * FROM files"
        params: list[object] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_to_record(row) for row in rows]

    def forget(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
            self._conn.commit()


def _to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        path=row["path"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        status=row["status"],
        frameio_file_id=row["frameio_file_id"],
        frameio_folder_id=row["frameio_folder_id"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        first_seen=row["first_seen"],
        updated_at=row["updated_at"],
    )
