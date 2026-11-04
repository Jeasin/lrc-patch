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

from .config import SCORING_VERSION
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
                    f"状态目录被占用，等待另一个 lrcpatch 结束（最多 {timeout:.0f} 秒）…",
                    flush=True,
                )
                waiting = True
            if time.monotonic() >= deadline:
                handle.close()
                raise ToolError(
                    f"状态目录正被另一个 lrcpatch 占用：{state_dir}\n"
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

def row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that may not exist yet (old database, plain dict row)."""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def read_plan_cache(path: Path) -> dict[str, sqlite3.Row]:
    """Read plan rows without taking the run lock or creating the database.

    ``scan`` is advertised as safe to run while a plan/apply is writing, so it
    opens the file read-only and silently gives up on any lock contention: a
    missing annotation is harmless, a blocked scan is not.
    """
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM plans").fetchall()
        return {row["path"]: row for row in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

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
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Columns added later; ALTER keeps an existing database usable
            # without a migration step the user has to run.
            #   native_lyric/native_kind — the provider's own text and format,
            #     so apply can convert instead of re-fetching.
            #   src_set — which --src list produced this plan, so `--src qq`
            #     does not silently reuse a netease verdict.
            self._add_column(conn, "plans", "native_lyric", "TEXT")
            self._add_column(conn, "plans", "native_kind", "TEXT")
            self._add_column(conn, "plans", "src_set", "TEXT")
            # 1.6 and earlier stored provider failures as no_match. Reclassify
            # them once so the 2.x retry policy actually picks them up instead
            # of reusing a stale "no match" forever.
            conn.execute(
                """
                UPDATE plans SET status='error'
                WHERE status='no_match' AND (
                    reason LIKE '%连续请求失败%'
                    OR reason LIKE '%ToolError%'
                    OR reason LIKE '%HTTP 503%'
                    OR reason LIKE '%HTTP 406%'
                    OR reason LIKE '%code 406%'
                    OR reason LIKE '%Connection reset%'
                )
                """
            )
            # A new scoring rule invalidates previous "needs review" verdicts:
            # retry them once so the user does not have to know about
            # --retry-failed after an upgrade.
            row = conn.execute(
                "SELECT value FROM meta WHERE key='scoring_version'"
            ).fetchone()
            if not row or row["value"] != SCORING_VERSION:
                conn.execute(
                    "UPDATE plans SET status='error', "
                    "reason='匹配规则已更新，等待重新匹配' "
                    "WHERE status IN ('ambiguous','suspect')"
                )
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('scoring_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (SCORING_VERSION,),
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

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def get_plan(self, path: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM plans WHERE path=?", (path,)
            ).fetchone()

    def upsert_plan(
        self, result: PlanResult, fingerprint: str, src_set: str = ""
    ) -> None:
        c = result.candidate
        lyric = result.lyrics or None
        native = result.native_lyrics or None
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO plans(
                    path,fingerprint,planned_at,status,reason,source,source_id,
                    match_title,match_artist,match_album,match_duration,
                    duration_diff,score,lyric,lyric_sha,lyric_kind,lyric_lines,
                    native_lyric,native_kind,src_set
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    lyric_lines=excluded.lyric_lines,
                    native_lyric=excluded.native_lyric,
                    native_kind=excluded.native_kind,
                    src_set=excluded.src_set
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
                    native,
                    result.native_kind or (lyric_kind(lyric) if lyric else None),
                    src_set,
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

    # -- cleanup --------------------------------------------------------
    def purge_http_cache(
        self, expired_only: bool = True, dry_run: bool = False
    ) -> tuple[int, int]:
        """Delete cached responses; returns (rows, payload bytes)."""
        where = "WHERE fetched_at + ttl < ?" if expired_only else ""
        args: tuple[Any, ...] = (time.time(),) if expired_only else ()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0) "
                f"FROM http_cache {where}",
                args,
            ).fetchone()
            if not dry_run:
                conn.execute(f"DELETE FROM http_cache {where}", args)
        return int(row[0]), int(row[1])

    def purge_plans(
        self, before: str | None, dry_run: bool = False
    ) -> tuple[int, int]:
        """Drop plan rows older than an ISO timestamp (all when None)."""
        where = "WHERE planned_at < ?" if before else ""
        args: tuple[Any, ...] = (before,) if before else ()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM("
                "LENGTH(COALESCE(lyric,'')) + LENGTH(COALESCE(native_lyric,''))"
                "),0) "
                f"FROM plans {where}",
                args,
            ).fetchone()
            if not dry_run:
                conn.execute(f"DELETE FROM plans {where}", args)
        return int(row[0]), int(row[1])

    def purge_journal(
        self, before: str | None, dry_run: bool = False
    ) -> tuple[int, int]:
        """Drop journal rows older than an ISO timestamp (all when None)."""
        where = "WHERE applied_at < ?" if before else ""
        args: tuple[Any, ...] = (before,) if before else ()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(pre_lyrics,''))),0) "
                f"FROM journal {where}",
                args,
            ).fetchone()
            if not dry_run:
                conn.execute(f"DELETE FROM journal {where}", args)
        return int(row[0]), int(row[1])

    def count_http_cache(self, expired_only: bool = True) -> int:
        where = "WHERE fetched_at + ttl < ?" if expired_only else ""
        args: tuple[Any, ...] = (time.time(),) if expired_only else ()
        with self.connection() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) FROM http_cache {where}", args).fetchone()[0]
            )

    def vacuum(self) -> None:
        # VACUUM cannot run inside a transaction, so use autocommit mode.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    def size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            item = Path(str(self.path) + suffix)
            if item.exists():
                total += item.stat().st_size
        return total
