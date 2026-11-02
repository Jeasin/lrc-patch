"""SQLite plan/journal store and the cross-process run lock."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .errors import ToolError
from .models import PlanResult
from .text import lyric_hash, lyric_kind, lyrics_line_count, utc_now


@contextmanager
def state_lock(state_dir: Path, timeout: float = 3.0) -> Iterable[None]:
    """Hold an exclusive lock so two runs cannot interleave state updates."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".lock"
    handle = lock_path.open("w", encoding="utf-8")
    deadline = time.monotonic() + timeout
    waiting = False
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if not waiting:
                print(
                    f"状态目录被占用，等待另一个 lyrics.py 结束（最多 {timeout:.0f} 秒）…",
                    flush=True,
                )
                waiting = True
            if time.monotonic() >= deadline:
                handle.close()
                raise ToolError(
                    f"状态目录正被另一个 lyrics.py 占用：{state_dir}\n"
                    "等它结束再运行；如果是残留进程，可用 --state-dir 指定别的目录。"
                ) from None
            time.sleep(0.2)
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

class State:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self) -> Iterable[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    path TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    planned_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT,
                    source_id TEXT,
                    match_title TEXT,
                    match_artist TEXT,
                    match_album TEXT,
                    match_duration REAL,
                    duration_diff REAL,
                    score INTEGER NOT NULL DEFAULT 0,
                    lyric TEXT,
                    lyric_sha TEXT,
                    lyric_kind TEXT,
                    lyric_lines INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS journal (
                    path TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    source TEXT,
                    source_id TEXT,
                    written_lyrics_sha TEXT NOT NULL,
                    pre_lyrics_present INTEGER NOT NULL,
                    pre_lyrics TEXT,
                    pre_tags_sha TEXT NOT NULL,
                    pre_cover_sha TEXT NOT NULL,
                    pre_audio_sha TEXT NOT NULL,
                    post_tags_sha TEXT NOT NULL,
                    post_cover_sha TEXT NOT NULL,
                    post_audio_sha TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS http_cache (
                    cache_key TEXT PRIMARY KEY,
                    fetched_at REAL NOT NULL,
                    ttl REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def cached_http(self, key: str) -> Any | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT fetched_at, ttl, payload FROM http_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
        if not row or time.time() > row["fetched_at"] + row["ttl"]:
            return None
        return json.loads(row["payload"])

    def cache_http(self, key: str, payload: Any, ttl: float) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO http_cache(cache_key,fetched_at,ttl,payload) "
                "VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "fetched_at=excluded.fetched_at, ttl=excluded.ttl, "
                "payload=excluded.payload",
                (key, time.time(), ttl, json.dumps(payload, ensure_ascii=False)),
            )

    def get_plan(self, path: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM plans WHERE path=?", (path,)
            ).fetchone()

    def upsert_plan(self, result: PlanResult, fingerprint: str) -> None:
        c = result.candidate
        lyric = result.lyrics or None
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO plans(
                    path,fingerprint,planned_at,status,reason,source,source_id,
                    match_title,match_artist,match_album,match_duration,
                    duration_diff,score,lyric,lyric_sha,lyric_kind,lyric_lines
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    fingerprint=excluded.fingerprint,
                    planned_at=excluded.planned_at,
                    status=excluded.status,
                    reason=excluded.reason,
                    source=excluded.source,
                    source_id=excluded.source_id,
                    match_title=excluded.match_title,
                    match_artist=excluded.match_artist,
                    match_album=excluded.match_album,
                    match_duration=excluded.match_duration,
                    duration_diff=excluded.duration_diff,
                    score=excluded.score,
                    lyric=excluded.lyric,
                    lyric_sha=excluded.lyric_sha,
                    lyric_kind=excluded.lyric_kind,
                    lyric_lines=excluded.lyric_lines
                """,
                (
                    str(result.track.path),
                    fingerprint,
                    utc_now(),
                    result.status,
                    result.reason,
                    c.source if c else None,
                    str(c.source_id) if c else None,
                    c.title if c else None,
                    c.artist_text if c else None,
                    c.album if c else None,
                    c.duration if c else None,
                    result.duration_diff,
                    result.score,
                    lyric,
                    lyric_hash(lyric) if lyric else None,
                    lyric_kind(lyric) if lyric else None,
                    lyrics_line_count(lyric),
                ),
            )

    def set_plan_status(self, path: str, status: str, reason: str = "") -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE plans SET status=?, reason=? WHERE path=?",
                (status, reason, path),
            )

    def list_plans(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM plans"
        args: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY path"
        with self.connection() as conn:
            return list(conn.execute(sql, args).fetchall())

    def add_journal(self, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self.connection() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO journal({cols}) VALUES({marks})",
                tuple(row.values()),
            )

    def list_journal(self, status: str = "applied") -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM journal WHERE status=? ORDER BY path", (status,)
                ).fetchall()
            )

    def set_journal_status(self, path: str, status: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE journal SET status=? WHERE path=?", (status, path))
