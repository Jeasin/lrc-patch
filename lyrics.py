#!/usr/bin/env -S uv run --script --no-project --quiet
# /// script
# requires-python = ">=3.9"
# dependencies = ["mutagen>=1.47"]
# ///
"""Batch lyrics planner/embedder for a local music library.

Dependencies
  * Python 3.9+ and mutagen; mutagen is declared inline (PEP 723), so the
    shebang above lets uv install it into its cache on first run.
  * Everything else is the standard library, including sqlite3; the sqlite3
    command-line tool is NOT required.
  * `python3 lyrics.py` still works: it re-execs into an interpreter that has
    mutagen (uv tool env, or $LYRICS_MUTAGEN_PYTHON).

Safety properties:
  * `plan` is read-only; it never modifies an audio file;
  * only the lyrics field is changed, and existing lyrics are skipped;
  * every write goes to a temporary same-directory copy, is re-read and checked
    for tag/cover/audio changes, then atomically replaces the original;
  * a SQLite journal supports verify and targeted rollback;
  * state, HTTP cache and CSV/HTML reports default to $XDG_STATE_HOME/lrc-patch;
  * optional --lrc copies are written under $XDG_CACHE_HOME/lrc-patch/lrc.

Scope a run with repeatable --path; one flag may take several paths at once:
  lyrics.py plan  --path "Artist A" "Artist B/Album" "track.m4a"
  lyrics.py apply --path "Artist A"
  lyrics.py verify --path "Artist A"
Bare paths are accepted too, and the read-only plan is assumed when the
subcommand is omitted:
  lyrics.py "/Music/Artist A" "/Music/Artist B/Album"
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
from html import escape as html_escape
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


# The user's mutagen is installed in an isolated ``uv tool`` environment.
# Re-exec into that interpreter so `./lyrics.py` also works from stock python3.
def _ensure_mutagen() -> None:
    try:
        import mutagen  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    candidates: list[Path] = []
    env_python = os.environ.get("LYRICS_MUTAGEN_PYTHON")
    if env_python:
        candidates.append(Path(env_python))
    mid3v2 = shutil.which("mid3v2")
    if mid3v2:
        try:
            first_line = Path(mid3v2).read_text(encoding="utf-8").splitlines()[0]
            if first_line.startswith("#!"):
                candidates.append(Path(first_line[2:].strip()))
        except (OSError, IndexError):
            pass
    candidates.extend(
        Path(p).expanduser()
        for p in (
            "~/.local/share/uv/tools/mutagen/bin/python",
            "~/.local/share/uv/tools/mutagen/bin/python3",
        )
    )
    me = str(Path(__file__).resolve())
    for candidate in candidates:
        if candidate.is_file():
            os.execv(str(candidate), [str(candidate), me, *sys.argv[1:]])
    sys.exit(
        "未找到 mutagen。推荐直接运行 lyrics.py（shebang 会让 uv 自动安装依赖）；"
        "也可以先运行 uv run lyrics.py，或 uv tool install mutagen。"
    )


_ensure_mutagen()

from mutagen.dsf import DSF  # noqa: E402
from mutagen.id3 import ID3, USLT  # noqa: E402
from mutagen.mp3 import MP3  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "lrc-patch"


def default_lrc_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "lrc-patch" / "lrc"


APP_VERSION = "1.6"
AUDIO_EXTS = {".m4a": "m4a", ".mp3": "mp3", ".dsf": "dsf"}
LYRICS_LANG = "jpn"
DEFAULT_LIBRARY = Path.home() / "Music" / "JMusic"
HTTP_UA = f"JMusic-Lyrics/{APP_VERSION} (personal library batch tool)"
SOURCE_ENDPOINTS = {
    "netease": (
        "https://music.163.com/api/search/get/web",
        "https://music.163.com/api/song/lyric",
    ),
    "lrclib": ("https://lrclib.net/api/search",),
}
TIMESTAMP_RE = re.compile(r"^\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", re.MULTILINE)
LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
PLACEHOLDERS = {"纯音乐", "純音樂", "instrumental", "暂无歌词", "無歌詞"}
SOURCE_NAMES = {"netease", "lrclib"}
DEFAULT_DURATION_TOLERANCE = 2.0
# Last LRC timestamp normally lands near the end of the song; a much earlier
# ending suggests a TV-size / short edit paired with the full track.
MIN_LRC_COVERAGE = 0.55
MAX_LRC_OVERRUN = 3.0


class ToolError(RuntimeError):
    pass


class VerificationError(ToolError):
    pass


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


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def lyric_hash(text: str) -> str:
    """Hash the semantic lyric body, ignoring an incidental trailing newline."""
    return sha256_text((text or "").strip())


def first_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " / ".join(str(x) for x in value if x is not None).strip()
    return str(value).strip()


def norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = text.replace("〜", "~").replace("～", "~")
    return re.sub(r"[\s\u3000]+", "", text)


def norm_loose(text: str) -> str:
    return re.sub(r"[\W_]+", "", norm_text(text), flags=re.UNICODE)


def artist_parts(text: str) -> list[str]:
    raw = norm_text(text)
    parts = re.split(r"[,/&、;]|\bfeat\.?\b|\bft\.?\b|\bx\b", raw)
    return [p for p in (norm_loose(x) for x in parts) if p]


def sanitize_lyrics(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return text.strip() + "\n" if text.strip() else ""


def lyric_kind(text: str) -> str:
    return "lrc" if TIMESTAMP_RE.search(text or "") else "plain"


def is_placeholder(text: str) -> bool:
    plain = re.sub(r"\[[^\]]*\]", "", text or "").strip().casefold()
    return not plain or plain in PLACEHOLDERS


def lyrics_line_count(text: str) -> int:
    return len([line for line in (text or "").splitlines() if line.strip()])


def lrc_time_values(text: str) -> list[float]:
    """Return every timestamp in seconds; metadata tags like [ar:] are ignored."""
    values: list[float] = []
    for match in LRC_TIME_RE.finditer(text or ""):
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        fraction = match.group(3) or "0"
        values.append(minutes * 60 + seconds + int(fraction) / (10 ** len(fraction)))
    return values


def lrc_timestamp_issue(text: str, duration: float) -> str:
    """Describe a suspicious LRC timeline, or return "" when it looks sane."""
    if duration <= 0 or lyric_kind(text) != "lrc":
        return ""
    values = lrc_time_values(text)
    if not values:
        return ""
    last = max(values)
    if last > duration + MAX_LRC_OVERRUN:
        return f"时间轴末尾 {last:.0f}s 超过音频时长 {duration:.0f}s"
    if duration >= 90 and last < duration * MIN_LRC_COVERAGE:
        return (
            f"时间轴只到 {last:.0f}s，约为时长的 {last / duration * 100:.0f}%"
            f"（可能是 TV size 或剪辑版）"
        )
    return ""


def parse_pick(value: str) -> tuple[str, str]:
    """Parse SOURCE:ID, accepting a bare numeric ID as netease."""
    raw = (value or "").strip()
    if not raw:
        raise ToolError("--pick 不能为空，格式为 SOURCE:ID，例如 netease:2003496467")
    if ":" in raw:
        source, source_id = raw.split(":", 1)
        source = source.strip().lower()
    else:
        source, source_id = "netease", raw
    source_id = source_id.strip()
    if source not in SOURCE_NAMES:
        raise ToolError(f"未知歌词源：{source}（可用：netease,lrclib）")
    if not source_id.isdigit():
        raise ToolError(f"候选 ID 应为数字：{value}")
    return source, source_id


def read_pick_file(path: Path, library: Path, tracks: list[Track]) -> dict[str, str]:
    """Read TAB-separated 'track path <TAB> SOURCE:ID' lines."""
    picks: dict[str, str] = {}
    by_name: dict[str, list[Track]] = {}
    for track in tracks:
        by_name.setdefault(track.path.name, []).append(track)
    try:
        lines = path.expanduser().read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ToolError(f"无法读取 --pick-file：{path}：{exc}") from exc

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" not in line:
            raise ToolError(
                f"{path}:{number} 缺少制表符；格式为：曲目路径<TAB>SOURCE:ID"
            )
        raw_path, raw_pick = line.split("\t", 1)
        raw_path = raw_path.strip()
        try:
            parse_pick(raw_pick)
        except ToolError as exc:
            raise ToolError(f"{path}:{number} {exc}") from exc
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = library / candidate
        candidate = canonical_path(candidate.resolve())
        if not candidate.is_file():
            matches = by_name.get(Path(raw_path).name, [])
            if len(matches) == 1:
                candidate = matches[0].path
            else:
                raise ToolError(
                    f"{path}:{number} 找不到曲目：{raw_path}"
                    + ("（同名文件不止一个，请写完整路径）" if matches else "")
                )
        picks[str(candidate)] = raw_pick.strip()
    if not picks:
        raise ToolError(f"--pick-file 没有有效条目：{path}")
    return picks


def plan_html_path(state_dir: Path, stamp: str) -> Path:
    return state_dir / "reports" / f"plan-{stamp}.html"


@dataclass(frozen=True)
class Track:
    path: Path
    relpath: str
    fmt: str
    title: str
    artist: str
    album: str
    albumartist: str
    duration: float
    existing_lyrics: str
    size: int
    mtime_ns: int

    @property
    def has_lyrics(self) -> bool:
        return bool(self.existing_lyrics.strip())

    def fingerprint(self) -> str:
        payload = {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "fmt": self.fmt,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "albumartist": self.albumartist,
            "duration": round(self.duration, 3),
            "lyrics_sha": lyric_hash(self.existing_lyrics),
        }
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@dataclass
class Candidate:
    source: str
    source_id: str
    title: str
    artists: list[str]
    album: str
    duration: float
    lyrics: str = ""
    synced: bool = False
    instrumental: bool = False

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists)


@dataclass
class FetchResult:
    lyrics: str = ""
    instrumental: bool = False
    note: str = ""


@dataclass
class PlanResult:
    track: Track
    status: str
    reason: str = ""
    candidate: Candidate | None = None
    lyrics: str = ""
    score: int = 0
    duration_diff: float | None = None


def canonical_path(path: Path) -> Path:
    """Use one Unicode form for database keys; macOS may return NFD names."""
    return Path(unicodedata.normalize("NFC", str(path)))


def iter_audio_files(root: Path) -> Iterable[Path]:
    root = canonical_path(root.resolve())
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in AUDIO_EXTS:
            yield canonical_path(path)


def album_matches(path: Path, library: Path, album: str | None) -> bool:
    if not album:
        return True
    wanted = canonical_path(Path(album).expanduser())
    if not wanted.is_absolute():
        wanted = library / wanted
    return canonical_path(path.parent.resolve()) == canonical_path(wanted.resolve())


def resolve_selection(
    library: Path, paths: list[str] | None, strict: bool = True
) -> list[Path]:
    """Resolve --path/--dir values; relative paths are anchored at the library."""
    selected: list[Path] = []
    library_key = canonical_path(library.resolve())
    for raw in paths or []:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = library / candidate
        candidate = canonical_path(candidate.resolve())
        if strict and not candidate.exists():
            raise ToolError(f"路径不存在：{candidate}")
        if candidate != library_key and library_key not in candidate.parents:
            raise ToolError(
                f"路径不在音乐库内：{candidate}\n"
                f"当前音乐库：{library_key}\n"
                "用 --library 指向它的根目录，或只传库内路径。"
            )
        selected.append(candidate)
    return selected


def selection_matches(
    path: Path, library: Path, album: str | None, selected: list[Path]
) -> bool:
    if not album_matches(path, library, album):
        return False
    if not selected:
        return True
    resolved = canonical_path(path.resolve())
    return any(resolved == item or item in resolved.parents for item in selected)


def iter_selected_files(library: Path, selected: list[Path]) -> Iterable[Path]:
    if not selected:
        yield from iter_audio_files(library)
        return
    seen: set[Path] = set()
    for item in selected:
        if item.is_file():
            if item.suffix.lower() in AUDIO_EXTS and item not in seen:
                seen.add(item)
                yield canonical_path(item)
            continue
        for path in iter_audio_files(item):
            if path not in seen:
                seen.add(path)
                yield path


def read_track(path: Path, root: Path) -> Track:
    fmt = AUDIO_EXTS[path.suffix.lower()]
    stat = path.stat()
    if fmt == "m4a":
        audio = MP4(str(path))
        tags = audio.tags or {}
        existing = first_text(tags.get("\xa9lyr"))
        title = first_text(tags.get("\xa9nam"))
        artist = first_text(tags.get("\xa9ART"))
        album = first_text(tags.get("\xa9alb"))
        albumartist = first_text(tags.get("aART"))
    else:
        audio = MP3(str(path)) if fmt == "mp3" else DSF(str(path))
        tags = audio.tags
        existing = ""
        if tags:
            existing = "\n".join(
                str(frame.text).strip()
                for frame in tags.getall("USLT")
                if str(frame.text).strip()
            )
        title = first_text(tags.get("TIT2")) if tags else ""
        artist = first_text(tags.get("TPE1")) if tags else ""
        album = first_text(tags.get("TALB")) if tags else ""
        albumartist = first_text(tags.get("TPE2")) if tags else ""
    return Track(
        path=path,
        relpath=str(path.relative_to(root)),
        fmt=fmt,
        title=title,
        artist=artist,
        album=album,
        albumartist=albumartist,
        duration=float(audio.info.length),
        existing_lyrics=existing,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def load_tracks(
    library: Path,
    album: str | None = None,
    limit: int | None = None,
    paths: list[str] | None = None,
) -> tuple[list[Track], list[tuple[Path, str]]]:
    tracks: list[Track] = []
    errors: list[tuple[Path, str]] = []
    selected = resolve_selection(library, paths)
    for path in sorted(iter_selected_files(library, selected)):
        if not selection_matches(path, library, album, selected):
            continue
        try:
            tracks.append(read_track(path, library))
        except Exception as exc:  # corrupt/unsupported tag should not stop scan
            errors.append((path, f"{type(exc).__name__}: {exc}"))
        if limit and len(tracks) >= limit:
            break
    return tracks, errors


def canonical_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return ["bytes", len(value), sha256_bytes(bytes(value))]
    if isinstance(value, (list, tuple)):
        return ["seq", [canonical_value(x) for x in value]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return [type(value).__name__, value]
    return ["repr", repr(value)]


def non_lyrics_tags_hash(path: Path) -> str:
    fmt = AUDIO_EXTS[path.suffix.lower()]
    if fmt == "m4a":
        tags = MP4(str(path)).tags or {}
        values = {
            key: canonical_value(value)
            for key, value in tags.items()
            if key != "\xa9lyr"
        }
        payload = json.dumps(values, ensure_ascii=False, sort_keys=True)
    else:
        audio = MP3(str(path)) if fmt == "mp3" else DSF(str(path))
        tags = audio.tags
        frames: list[list[Any]] = []
        if tags:
            for key, frame in tags.items():
                if key.upper().startswith("USLT"):
                    continue
                try:
                    raw = bytes(frame._writeData())
                except Exception:
                    raw = repr(frame).encode("utf-8", "replace")
                frames.append([key, sha256_bytes(raw)])
        payload = json.dumps(sorted(frames), ensure_ascii=False)
    return sha256_text(payload)


def cover_hash(path: Path) -> str:
    fmt = AUDIO_EXTS[path.suffix.lower()]
    images: list[bytes] = []
    if fmt == "m4a":
        tags = MP4(str(path)).tags or {}
        images = [bytes(x) for x in tags.get("covr", [])]
    else:
        audio = MP3(str(path)) if fmt == "mp3" else DSF(str(path))
        if audio.tags:
            images = [bytes(frame.data) for frame in audio.tags.getall("APIC")]
    return sha256_text("\0".join(sha256_bytes(x) for x in images))


def _hash_range(path: Path, start: int, end: int) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        fh.seek(start)
        remaining = max(0, end - start)
        while remaining:
            chunk = fh.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    if remaining:
        raise ToolError(f"音频范围读取不完整: {path}")
    return h.hexdigest()


def _mp4_mdat_hash(path: Path) -> str:
    h = hashlib.sha256()
    count = 0
    size = path.stat().st_size
    with path.open("rb") as fh:
        pos = 0
        while pos + 8 <= size:
            fh.seek(pos)
            header = fh.read(16)
            if len(header) < 8:
                break
            atom_size = struct.unpack(">I", header[:4])[0]
            atom_type = header[4:8]
            header_size = 8
            if atom_size == 1:
                if len(header) < 16:
                    raise ToolError(f"MP4 atom 头不完整: {path}")
                atom_size = struct.unpack(">Q", header[8:16])[0]
                header_size = 16
            elif atom_size == 0:
                atom_size = size - pos
            if atom_size < header_size or pos + atom_size > size:
                raise ToolError(f"MP4 atom 结构异常: {path}")
            if atom_type == b"mdat":
                h.update(_hash_range(path, pos + header_size, pos + atom_size).encode())
                count += 1
            pos += atom_size
    if not count:
        raise ToolError(f"未找到 mdat: {path}")
    return h.hexdigest()


def _mp3_audio_hash(path: Path) -> str:
    size = path.stat().st_size
    start = 0
    with path.open("rb") as fh:
        header = fh.read(10)
        if header[:3] == b"ID3" and len(header) == 10:
            tag_size = (
                (header[6] << 21)
                | (header[7] << 14)
                | (header[8] << 7)
                | header[9]
            )
            start = 10 + tag_size
        fh.seek(max(0, size - 128))
        end = size - 128 if fh.read(3) == b"TAG" else size
    if start >= end:
        raise ToolError(f"MP3 音频范围异常: {path}")
    return _hash_range(path, start, end)


def _dsf_audio_hash(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        header = fh.read(28)
        if len(header) != 28 or header[:4] != b"DSD ":
            raise ToolError(f"DSF 头异常: {path}")
        metadata_offset = struct.unpack("<Q", header[20:28])[0]
    end = metadata_offset if 0 < metadata_offset <= size else size
    if 28 >= end:
        raise ToolError(f"DSF 音频范围异常: {path}")
    return _hash_range(path, 28, end)


def audio_hash(path: Path) -> str:
    fmt = AUDIO_EXTS[path.suffix.lower()]
    if fmt == "m4a":
        return _mp4_mdat_hash(path)
    if fmt == "mp3":
        return _mp3_audio_hash(path)
    return _dsf_audio_hash(path)


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


class JsonClient:
    def __init__(self, state: State, refresh: bool = False):
        self.state = state
        self.refresh = refresh
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._lock_guard = threading.Lock()

    def _source_lock(self, source: str) -> threading.Lock:
        with self._lock_guard:
            return self._locks.setdefault(source, threading.Lock())

    def get(
        self,
        source: str,
        url: str,
        min_interval: float,
        cache_predicate: Callable[[Any], bool] | None = None,
    ) -> tuple[int, Any | None]:
        cache_key = sha256_text(url)
        if not self.refresh:
            cached = self.state.cached_http(cache_key)
            if cached is not None:
                return int(cached["status"]), cached.get("data")

        lock = self._source_lock(source)
        with lock:
            wait = min_interval - (time.monotonic() - self._last.get(source, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[source] = time.monotonic()

        last_error = ""
        for attempt in range(4):
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": HTTP_UA,
                    "Accept": "application/json",
                    "Referer": "https://music.163.com/"
                    if source == "netease"
                    else "https://lrclib.net/",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.load(response)
                code = data.get("code") if isinstance(data, dict) else None
                if code is not None and str(code) != "200":
                    last_error = f"{source} 返回 code {code}"
                    if attempt < 3:
                        delay = 2.0 if str(code) == "406" else 1.5 ** attempt
                        time.sleep(min(delay, 8.0))
                    continue
                if cache_predicate is None or cache_predicate(data):
                    self.state.cache_http(
                        cache_key, {"status": 200, "data": data}, 30 * 86400
                    )
                return 200, data
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 400):
                    self.state.cache_http(
                        cache_key, {"status": exc.code, "data": None}, 86400
                    )
                    return exc.code, None
                last_error = f"HTTP {exc.code}"
                if exc.code not in (429, 500, 502, 503, 504):
                    raise ToolError(f"{source} 请求失败: {last_error}") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 ** attempt
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                delay = 1.5 ** attempt
            if attempt < 3:
                time.sleep(min(delay, 8.0))
        raise ToolError(f"{source} 连续请求失败: {last_error}")


def netease_has_songs(payload: Any) -> bool:
    """Do not freeze a transient empty search response in the 30-day cache."""
    return bool(((payload or {}).get("result") or {}).get("songs"))


class NeteaseSource:
    name = "netease"
    min_interval = 0.30

    def __init__(self, client: JsonClient):
        self.client = client

    def search(self, track: Track) -> list[Candidate]:
        base = "https://music.163.com/api/search/get/web?"
        queries = [f"{track.title} {track.artist}".strip()]
        if track.artist:
            queries.append(track.title)
        seen: set[str] = set()
        candidates: list[Candidate] = []
        for query in queries:
            url = base + urllib.parse.urlencode(
                {"s": query, "type": 1, "limit": 15, "offset": 0}
            )
            code, data = self.client.get(
                self.name, url, self.min_interval, netease_has_songs
            )
            if code != 200 or not data:
                continue
            songs = (data.get("result") or {}).get("songs") or []
            for song in songs:
                sid = str(song.get("id") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                candidates.append(
                    Candidate(
                        source=self.name,
                        source_id=sid,
                        title=str(song.get("name") or ""),
                        artists=[
                            str(a.get("name") or "")
                            for a in (song.get("artists") or [])
                            if a.get("name")
                        ],
                        album=str((song.get("album") or {}).get("name") or ""),
                        duration=float(song.get("duration") or 0) / 1000.0,
                    )
                )
            if candidates:
                break
        return candidates

    def fetch(self, candidate: Candidate) -> FetchResult:
        url = (
            "https://music.163.com/api/song/lyric?"
            + urllib.parse.urlencode(
                {"id": candidate.source_id, "lv": 1, "kv": 1, "tv": -1}
            )
        )
        code, data = self.client.get(self.name, url, self.min_interval)
        if code != 200 or not data:
            return FetchResult(note=f"歌词接口 HTTP {code}")
        raw = (data.get("lrc") or {}).get("lyric") or ""
        lyrics = sanitize_lyrics(raw)
        if not lyrics or is_placeholder(lyrics):
            instrumental = bool(data.get("nolyric") or data.get("pureMusic"))
            return FetchResult(instrumental=instrumental, note="无可用歌词")
        return FetchResult(lyrics=lyrics)


class LrclibSource:
    name = "lrclib"
    min_interval = 0.55

    def __init__(self, client: JsonClient):
        self.client = client

    def search(self, track: Track) -> list[Candidate]:
        params = {"track_name": track.title}
        if track.artist:
            params["artist_name"] = track.artist
        if track.album:
            params["album_name"] = track.album
        url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
        code, data = self.client.get(self.name, url, self.min_interval)
        if code != 200 or not isinstance(data, list):
            return []
        out: list[Candidate] = []
        for item in data:
            lyrics = sanitize_lyrics(
                item.get("syncedLyrics") or item.get("plainLyrics") or ""
            )
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(item.get("id") or ""),
                    title=str(item.get("trackName") or item.get("name") or ""),
                    artists=[str(item.get("artistName") or "")],
                    album=str(item.get("albumName") or ""),
                    duration=float(item.get("duration") or 0),
                    lyrics=lyrics,
                    synced=bool(item.get("syncedLyrics")),
                    instrumental=bool(item.get("instrumental")),
                )
            )
        return out

    def fetch(self, candidate: Candidate) -> FetchResult:
        if candidate.instrumental:
            return FetchResult(instrumental=True, note="纯音乐")
        if candidate.lyrics and not is_placeholder(candidate.lyrics):
            return FetchResult(lyrics=candidate.lyrics)
        return FetchResult(note="无可用歌词")


def score_candidate(
    track: Track, candidate: Candidate, tolerance: float = DEFAULT_DURATION_TOLERANCE
) -> tuple[int, float]:
    if not candidate.title or not candidate.duration:
        return 0, 999.0
    diff = abs(track.duration - candidate.duration)
    if diff > tolerance:
        return 0, diff

    title_file = norm_loose(track.title)
    title_cand = norm_loose(candidate.title)
    if not title_file or title_file != title_cand:
        return 0, diff
    score = 55

    file_artists = artist_parts(track.artist)
    cand_artists = [p for a in candidate.artists for p in artist_parts(a)]
    if file_artists and cand_artists:
        if any(a == b for a in file_artists for b in cand_artists):
            score += 30
        elif any(a in b or b in a for a in file_artists for b in cand_artists):
            score += 20
        else:
            return 0, diff
    else:
        return 0, diff

    file_album = norm_loose(track.album)
    cand_album = norm_loose(candidate.album)
    if file_album and cand_album:
        if file_album == cand_album:
            score += 15
        elif file_album in cand_album or cand_album in file_album:
            score += 8
    return score, diff


def choose_candidate(
    track: Track,
    candidates: list[Candidate],
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> tuple[Candidate | None, int, float, str]:
    scored: list[tuple[int, float, Candidate]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (
            norm_loose(candidate.title),
            norm_loose(candidate.artist_text),
            round(candidate.duration, 1),
        )
        if key in seen:
            continue
        seen.add(key)
        score, diff = score_candidate(track, candidate, tolerance)
        if score:
            scored.append((score, diff, candidate))
    if not scored:
        return None, 0, 999.0, "无标题/艺术家/时长同时匹配的候选"
    scored.sort(key=lambda x: (-x[0], x[1], -x[2].synced))
    score, diff, candidate = scored[0]
    if score < 85:
        return None, score, diff, f"最佳候选仅 {score} 分，未达自动写入阈值"
    if diff > tolerance:
        return None, score, diff, f"时长差 {diff:.1f}s，超过容差 {tolerance:.1f}s"
    return candidate, score, diff, ""


def best_partial_candidate(
    track: Track, candidates: list[Candidate], tolerance: float
) -> tuple[tuple[int, float], Candidate, int, float, str] | None:
    """Pick the most plausible rejected candidate, with its real duration gap."""
    best: tuple[int, float, Candidate] | None = None
    for candidate in candidates:
        if not candidate.duration:
            continue
        metadata_score, diff = score_candidate(track, candidate, 999.0)
        key = (metadata_score, -diff)
        if best is None or key > (best[0], -best[1]):
            best = (metadata_score, diff, candidate)
    if best is None:
        return None
    metadata_score, diff, candidate = best
    score, _ = score_candidate(track, candidate, tolerance)
    if metadata_score < 85:
        reason = f"候选元数据仅 {metadata_score} 分（标题或艺术家不一致）"
    elif diff > tolerance:
        reason = f"时长差 {diff:.1f}s，超过容差 {tolerance:.1f}s"
    else:
        reason = "未达到自动写入条件"
    return (metadata_score, -diff), candidate, score, diff, reason


def local_sidecar(track: Track) -> FetchResult | None:
    sidecar = track.path.with_suffix(".lrc")
    if not sidecar.is_file():
        return None
    try:
        text = sanitize_lyrics(sidecar.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        text = sanitize_lyrics(sidecar.read_text(encoding="gb18030", errors="replace"))
    if not text or is_placeholder(text):
        return FetchResult(note="同名 LRC 为空")
    return FetchResult(lyrics=text)


def plan_from_lyrics(
    track: Track,
    candidate: Candidate,
    lyrics: str,
    score: int,
    diff: float | None,
    reason: str,
    check_timestamps: bool,
) -> PlanResult:
    """Common tail: timestamp sanity check, then ready or suspect."""
    if check_timestamps:
        issue = lrc_timestamp_issue(lyrics, track.duration)
        if issue:
            return PlanResult(
                track, "suspect", f"{reason}；{issue}", candidate, lyrics, score, diff
            )
    return PlanResult(track, "ready", reason, candidate, lyrics, score, diff)


def plan_with_pick(
    track: Track,
    sources: list[Any],
    pick: str,
    allow_plain: bool,
    check_timestamps: bool,
) -> PlanResult:
    """Force a user-chosen candidate; metadata is verified when search finds it."""
    try:
        source_name, source_id = parse_pick(pick)
    except ToolError as exc:
        return PlanResult(track, "error", str(exc))
    source = next((item for item in sources if item.name == source_name), None)
    if source is None:
        return PlanResult(track, "error", f"--pick 指定的源未启用：{source_name}")

    candidate: Candidate | None = None
    search_note = ""
    try:
        results = source.search(track)
    except Exception as exc:
        results = []
        search_note = f"搜索失败 {type(exc).__name__}: {exc}"
    for item in results:
        if str(item.source_id) == source_id:
            candidate = item
            break

    verified = candidate is not None
    if candidate is None:
        candidate = Candidate(
            source=source_name,
            source_id=source_id,
            title=track.title,
            artists=[track.artist],
            album=track.album,
            duration=track.duration,
        )
    diff = abs(track.duration - candidate.duration) if verified else None
    label = f"人工指定 {source_name}:{source_id}"
    if verified:
        label += f"（时长已核验 Δ{diff:.1f}s）"
    else:
        label += "（未出现在搜索结果中，标题/时长未核验）"
        if search_note:
            label += f"；{search_note}"

    try:
        fetched = source.fetch(candidate)
    except Exception as exc:
        return PlanResult(
            track, "error", f"{label}；取词失败 {type(exc).__name__}: {exc}", candidate
        )
    if fetched.instrumental:
        return PlanResult(track, "instrumental", f"{label}；标记为纯音乐", candidate)
    if not fetched.lyrics:
        return PlanResult(
            track, "error", f"{label}；{fetched.note or '没有可用歌词'}", candidate
        )
    if lyric_kind(fetched.lyrics) == "plain" and not allow_plain:
        return PlanResult(track, "error", f"{label}；只有纯文本，已按 --no-plain 拒绝", candidate)
    return plan_from_lyrics(
        track, candidate, fetched.lyrics, 100, diff, label, check_timestamps
    )


def plan_one(
    track: Track,
    sources: list[Any],
    allow_plain: bool,
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
    check_timestamps: bool = True,
    pick: str | None = None,
) -> PlanResult:
    if track.has_lyrics:
        return PlanResult(track, "skip_existing", "已有歌词，默认不覆盖")
    if pick:
        return plan_with_pick(track, sources, pick, allow_plain, check_timestamps)

    sidecar = local_sidecar(track)
    if sidecar and sidecar.lyrics:
        candidate = Candidate(
            source="local",
            source_id=str(track.path.with_suffix(".lrc")),
            title=track.title,
            artists=[track.artist],
            album=track.album,
            duration=track.duration,
            lyrics=sidecar.lyrics,
            synced=lyric_kind(sidecar.lyrics) == "lrc",
        )
        return plan_from_lyrics(
            track, candidate, sidecar.lyrics, 100, 0.0, "同名 LRC", check_timestamps
        )

    notes: list[str] = []
    best_partial: tuple[Candidate, int, float, str] | None = None
    best_partial_key: tuple[int, float] | None = None
    for source in sources:
        try:
            candidates = source.search(track)
        except Exception as exc:
            notes.append(f"{source.name}: {type(exc).__name__}: {exc}")
            continue
        if not candidates:
            notes.append(f"{source.name}: 无搜索结果")
            continue
        candidate, score, diff, reason = choose_candidate(track, candidates, tolerance)
        if not candidate:
            partial = best_partial_candidate(track, candidates, tolerance)
            if partial and (
                best_partial_key is None or partial[0] > best_partial_key
            ):
                best_partial_key = partial[0]
                best_partial = partial[1:]
            notes.append(f"{source.name}: {reason}")
            continue
        try:
            fetched = source.fetch(candidate)
        except Exception as exc:
            notes.append(f"{source.name}: 取词失败 {type(exc).__name__}: {exc}")
            continue
        if fetched.instrumental:
            return PlanResult(track, "instrumental", "匹配为纯音乐")
        if not fetched.lyrics:
            notes.append(f"{source.name}: {fetched.note or '无可用歌词'}")
            continue
        kind = lyric_kind(fetched.lyrics)
        if kind == "plain" and not allow_plain:
            notes.append(f"{source.name}: 仅纯文本，已按 --no-plain 跳过")
            continue
        return plan_from_lyrics(
            track,
            candidate,
            fetched.lyrics,
            score,
            diff,
            f"{kind.upper()} / {candidate.artist_text}",
            check_timestamps,
        )

    if best_partial:
        candidate, score, diff, reason = best_partial
        return PlanResult(
            track,
            "ambiguous",
            f"{reason}; 最佳：{candidate.title} / {candidate.artist_text} / "
            f"{candidate.duration:.0f}s",
            candidate,
            "",
            score,
            diff,
        )
    return PlanResult(track, "no_match", "; ".join(notes) or "无结果")


def id3_major(tags: ID3) -> int:
    version = getattr(tags, "version", 4)
    if isinstance(version, tuple):
        return int(version[1])
    return int(version)


def write_lyrics_only(path: Path, lyrics: str) -> None:
    fmt = AUDIO_EXTS[path.suffix.lower()]
    if fmt == "m4a":
        audio = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        audio.tags["\xa9lyr"] = [lyrics]
        audio.save()
        return

    audio = MP3(str(path)) if fmt == "mp3" else DSF(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    version = id3_major(tags)
    tags.add(USLT(encoding=3, lang=LYRICS_LANG, desc="", text=lyrics))
    if fmt == "mp3":
        trailing_v1 = b""
        had_v1 = False
        if path.stat().st_size >= 128:
            with path.open("rb") as fh:
                fh.seek(-128, os.SEEK_END)
                tail = fh.read(128)
            if tail[:3] == b"TAG":
                trailing_v1, had_v1 = tail, True
        audio.save(v1=1, v2_version=4 if version == 4 else 3)
        if had_v1:
            with path.open("r+b") as fh:
                fh.seek(-128, os.SEEK_END)
                fh.write(trailing_v1)
    else:
        audio.save(v2_version=4 if version == 4 else 3)


def clear_lyrics_only(path: Path, written_sha: str) -> None:
    fmt = AUDIO_EXTS[path.suffix.lower()]
    if fmt == "m4a":
        audio = MP4(str(path))
        if audio.tags and "\xa9lyr" in audio.tags:
            del audio.tags["\xa9lyr"]
            audio.save()
        return

    audio = MP3(str(path)) if fmt == "mp3" else DSF(str(path))
    tags = audio.tags
    if not tags:
        return
    changed = False
    for key, frame in list(tags.items()):
        if (
            key.upper().startswith("USLT")
            and getattr(frame, "lang", "") == LYRICS_LANG
            and getattr(frame, "desc", "") == ""
            and lyric_hash(str(frame.text)) == written_sha
        ):
            del tags[key]
            changed = True
    if not changed:
        return

    version = id3_major(tags)
    if fmt == "mp3":
        trailing_v1 = b""
        had_v1 = False
        if path.stat().st_size >= 128:
            with path.open("rb") as fh:
                fh.seek(-128, os.SEEK_END)
                tail = fh.read(128)
            if tail[:3] == b"TAG":
                trailing_v1, had_v1 = tail, True
        audio.save(v1=1, v2_version=4 if version == 4 else 3)
        if had_v1:
            with path.open("r+b") as fh:
                fh.seek(-128, os.SEEK_END)
                fh.write(trailing_v1)
    else:
        audio.save(v2_version=4 if version == 4 else 3)


def stage_copy(path: Path) -> Path:
    """Create a cheap same-directory copy for verify-then-atomic-replace."""
    staged = path.with_name(f".lyrics-txn-{uuid.uuid4().hex}{path.suffix.lower()}")
    try:
        result = subprocess.run(
            ["/bin/cp", "-c", str(path), str(staged)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip())
    except (OSError, subprocess.SubprocessError):
        free = shutil.disk_usage(path.parent).free
        if free < path.stat().st_size * 1.2:
            raise ToolError(f"临时副本空间不足: {path}")
        shutil.copy2(path, staged)
    if not staged.is_file() or staged.stat().st_size != path.stat().st_size:
        staged.unlink(missing_ok=True)
        raise ToolError(f"临时副本创建失败: {path}")
    return staged


def apply_one(
    row: sqlite3.Row,
    library: Path,
) -> dict[str, Any]:
    path = Path(row["path"])
    before = read_track(path, library)
    if before.fingerprint() != row["fingerprint"]:
        raise ToolError("计划生成后文件已变化，请重新 plan")
    if before.has_lyrics:
        raise ToolError("文件已有歌词，拒绝覆盖")

    pre_tags = non_lyrics_tags_hash(path)
    pre_cover = cover_hash(path)
    pre_audio = audio_hash(path)
    staged = stage_copy(path)
    try:
        write_lyrics_only(staged, row["lyric"])
        after = read_track(staged, library)
        if after.existing_lyrics.strip() != row["lyric"].strip():
            raise VerificationError("歌词回读不一致")
        post_tags = non_lyrics_tags_hash(staged)
        post_cover = cover_hash(staged)
        post_audio = audio_hash(staged)
        if post_tags != pre_tags:
            raise VerificationError("非歌词标签发生变化")
        if post_cover != pre_cover:
            raise VerificationError("封面发生变化")
        if post_audio != pre_audio:
            raise VerificationError("音频数据发生变化")
        os.replace(staged, path)
    except Exception:
        staged.unlink(missing_ok=True)
        raise

    return {
        "path": str(path),
        "applied_at": utc_now(),
        "source": row["source"],
        "source_id": row["source_id"],
        "written_lyrics_sha": lyric_hash(row["lyric"]),
        "pre_lyrics_present": int(before.has_lyrics),
        "pre_lyrics": before.existing_lyrics or None,
        "pre_tags_sha": pre_tags,
        "pre_cover_sha": pre_cover,
        "pre_audio_sha": pre_audio,
        "post_tags_sha": post_tags,
        "post_cover_sha": post_cover,
        "post_audio_sha": post_audio,
        "status": "applied",
    }


def default_report_path(state_dir: Path, prefix: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return state_dir / "reports" / f"{prefix}-{stamp}.csv"


def lrc_dest(path: Path, library: Path, lrc_dir: Path) -> Path:
    """Mirror the library layout under the cache, keeping the track name."""
    try:
        rel = path.resolve().relative_to(library)
    except ValueError:
        rel = Path("_external") / f"{sha256_text(str(path))[:8]}-{path.name}"
    return (lrc_dir / rel).with_suffix(".lrc")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def export_lrc(
    library: Path,
    rows: Iterable[sqlite3.Row],
    tracks_by_path: dict[str, Track],
    lrc_dir: Path,
) -> tuple[int, int]:
    """Write .lrc copies; falls back to existing lyrics when plan has none."""
    written = 0
    failed = 0
    for row in rows:
        lyric = row["lyric"]
        if not lyric:
            track = tracks_by_path.get(row["path"])
            lyric = track.existing_lyrics if track else ""
        if not lyric or not lyric.strip():
            continue
        try:
            write_text_atomic(lrc_dest(Path(row["path"]), library, lrc_dir), lyric)
            written += 1
        except OSError as exc:
            failed += 1
            print(f"WARN LRC 写入失败 {Path(row['path']).name}: {exc}")
    return written, failed


def write_plan_report(rows: list[sqlite3.Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "path",
        "status",
        "reason",
        "source",
        "source_id",
        "match_title",
        "match_artist",
        "match_album",
        "match_duration",
        "duration_diff",
        "score",
        "lyric_kind",
        "lyric_lines",
        "lyric_sha",
        "planned_at",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))


HTML_STATUS = {
    "ready": ("可写入", "ok"),
    "applied": ("已写入", "ok"),
    "skip_existing": ("已有歌词", "skip"),
    "ambiguous": ("需确认", "warn"),
    "suspect": ("时间轴可疑", "suspect"),
    "no_match": ("无匹配", "miss"),
    "instrumental": ("纯音乐", "skip"),
    "error": ("错误", "bad"),
    "rolled_back": ("已回滚", "skip"),
}


def candidate_url(source: str | None, source_id: str | None) -> str:
    if not source or not source_id:
        return ""
    if source == "netease" and source_id.isdigit():
        return f"https://music.163.com/song?id={source_id}"
    return ""


def write_plan_html(rows: list[sqlite3.Row], path: Path, library: Path) -> None:
    """Album-grouped review page: scan, filter, then pick fixes for the rest."""
    counts = Counter(row["status"] for row in rows)
    albums: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        try:
            parent = str(Path(row["path"]).parent.relative_to(library))
        except ValueError:
            parent = "_external"
        albums.setdefault(parent, []).append(row)

    chips = []
    for status, (label, _) in HTML_STATUS.items():
        if counts.get(status):
            chips.append(
                f'<button data-filter="{status}">{html_escape(label)} '
                f'<b>{counts[status]}</b></button>'
            )
    chips.insert(0, '<button data-filter="all" class="active">全部 <b>%d</b></button>' % len(rows))
    chips.append('<button data-filter="review">待处理 <b>%d</b></button>' % (
        counts.get("ambiguous", 0)
        + counts.get("suspect", 0)
        + counts.get("no_match", 0)
        + counts.get("error", 0)
    ))

    sections = []
    for album in sorted(albums):
        items = sorted(albums[album], key=lambda row: (Path(row["path"]).name, row["path"]))
        album_counts = Counter(row["status"] for row in items)
        summary = " · ".join(
            f"{HTML_STATUS.get(status, (status, ''))[0]} {count}"
            for status, count in album_counts.items()
        )
        body = []
        for row in items:
            status = row["status"]
            label, tone = HTML_STATUS.get(status, (status, ""))
            title = row["match_title"] or ""
            artist = row["match_artist"] or ""
            match = html_escape(title)
            if artist:
                match += f'<span class="artist">{html_escape(artist)}</span>'
            if not match:
                match = '<span class="muted">—</span>'
            diff = row["duration_diff"]
            diff_text = f"{diff:.1f}s" if diff is not None else "—"
            score = row["score"] or 0
            source = row["source"] or ""
            source_id = row["source_id"] or ""
            link = candidate_url(source, source_id)
            source_cell = html_escape(f"{source}:{source_id}" if source_id else source)
            if link:
                source_cell = (
                    f'<a href="{html_escape(link)}" target="_blank" rel="noreferrer">'
                    f"{source_cell}</a>"
                )
            body.append(
                f'<tr data-status="{html_escape(status)}">'
                f'<td><span class="badge {tone}">{html_escape(label)}</span></td>'
                f'<td class="file">{html_escape(Path(row["path"]).name)}</td>'
                f'<td>{match}</td>'
                f'<td class="num">{html_escape(diff_text)}</td>'
                f'<td class="num">{score if score else "—"}</td>'
                f'<td class="src">{source_cell}</td>'
                f'<td class="reason">{html_escape(row["reason"] or "")}</td>'
                "</tr>"
            )
        sections.append(
            f'<section class="album" data-album="{html_escape(album)}">'
            f"<h2>{html_escape(album)}<span>{html_escape(summary)}</span></h2>"
            '<table><thead><tr><th>状态</th><th>文件</th><th>匹配</th>'
            "<th>Δ时长</th><th>分数</th><th>来源</th><th>说明</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></section>"
        )

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>歌词计划复核 · {html_escape(library.name)}</title>
<style>
  :root {{
    --bg: #f6f6f4; --panel: #fff; --text: #1c1c1a; --muted: #6b6b66;
    --line: #e4e3df; --ok: #1f7a4d; --okbg: #e3f3ea;
    --warn: #8a5a00; --warnbg: #fdf0d5; --suspect: #9a4b00; --suspectbg: #fde7d3;
    --miss: #6b6b66; --missbg: #ecece9; --bad: #a12828; --badbg: #fbe3e3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #171715; --panel: #1f1f1c; --text: #ecece7; --muted: #a3a39c;
      --line: #35352f; --ok: #7fd3a6; --okbg: #1e3a2c;
      --warn: #f0c26a; --warnbg: #3d3118; --suspect: #f2a86b; --suspectbg: #3d2718;
      --miss: #a3a39c; --missbg: #2b2b27; --bad: #f19a9a; --badbg: #3d1f1f;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 28px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "SF Pro Text", "Helvetica Neue", "Hiragino Sans", sans-serif; }}
  header {{ max-width: 1180px; margin: 0 auto 18px; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  .meta {{ color: var(--muted); font-size: 12px; }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 20px; }}
  .toolbar button, .toolbar input {{ font: inherit; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); border-radius: 999px; padding: 6px 12px;
    cursor: pointer; }}
  .toolbar button.active {{ border-color: var(--text); }}
  .toolbar input {{ border-radius: 8px; min-width: 240px; cursor: text; }}
  section.album {{ max-width: 1180px; margin: 0 auto 18px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px; overflow: auto; }}
  section.album h2 {{ font-size: 14px; margin: 0; padding: 12px 16px;
    border-bottom: 1px solid var(--line); display: flex; gap: 12px;
    justify-content: space-between; flex-wrap: wrap; }}
  section.album h2 span {{ color: var(--muted); font-weight: 400; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line);
    vertical-align: top; font-size: 13px; }}
  th {{ color: var(--muted); font-weight: 500; font-size: 11px; letter-spacing: .04em;
    text-transform: uppercase; }}
  tr:last-child td {{ border-bottom: 0; }}
  .file {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }}
  .artist {{ display: block; color: var(--muted); font-size: 12px; }}
  .num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .src {{ font-size: 12px; overflow-wrap: anywhere; }}
  .reason {{ color: var(--muted); font-size: 12px; max-width: 360px; }}
  .muted {{ color: var(--muted); }}
  a {{ color: inherit; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; white-space: nowrap; }}
  .badge.ok {{ color: var(--ok); background: var(--okbg); }}
  .badge.warn {{ color: var(--warn); background: var(--warnbg); }}
  .badge.suspect {{ color: var(--suspect); background: var(--suspectbg); }}
  .badge.miss, .badge.skip {{ color: var(--miss); background: var(--missbg); }}
  .badge.bad {{ color: var(--bad); background: var(--badbg); }}
  tr.hidden, section.hidden {{ display: none; }}
</style>
</head>
<body>
<header>
  <h1>歌词计划复核</h1>
  <div class="meta">{html_escape(str(library))} · 生成于 {html_escape(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))} · 共 {len(rows)} 首</div>
  <div class="toolbar">
    {''.join(chips)}
    <input id="q" type="search" placeholder="筛选文件名 / 曲名 / 艺术家 / 说明">
  </div>
</header>
{''.join(sections)}
<script>
const buttons = Array.from(document.querySelectorAll('[data-filter]'));
const rows = Array.from(document.querySelectorAll('tr[data-status]'));
const albums = Array.from(document.querySelectorAll('section.album'));
const query = document.getElementById('q');
let filter = 'all';
function apply() {{
  const q = query.value.trim().toLowerCase();
  rows.forEach(row => {{
    const status = row.dataset.status;
    const matchFilter = filter === 'all'
      || (filter === 'review' ? ['ambiguous','suspect','no_match','error'].includes(status) : status === filter);
    const matchText = !q || row.textContent.toLowerCase().includes(q);
    row.classList.toggle('hidden', !(matchFilter && matchText));
  }});
  albums.forEach(album => {{
    const visible = album.querySelectorAll('tr[data-status]:not(.hidden)').length;
    album.classList.toggle('hidden', visible === 0);
  }});
}}
buttons.forEach(button => button.addEventListener('click', () => {{
  filter = button.dataset.filter;
  buttons.forEach(item => item.classList.toggle('active', item === button));
  apply();
}}));
query.addEventListener('input', apply);
</script>
</body>
</html>
"""
    write_text_atomic(path, document)


# ---------------------------------------------------------------------------
# Terminal presentation: colors, badges and album trees
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_COLOR_ENABLED = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and os.environ.get("TERM", "") != "dumb"
)


def set_color(enabled: bool) -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = (
        bool(enabled)
        and sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )


def style(text: str, code: str) -> str:
    if not _COLOR_ENABLED or not text:
        return text
    return f"\033[{code}m{text}\033[0m"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def disp_width(text: str) -> int:
    """Terminal columns, ignoring ANSI escapes and counting CJK as two."""
    width = 0
    for char in strip_ansi(text):
        width += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
    return width


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


def truncate_display(text: str, limit: int) -> str:
    if limit <= 1 or disp_width(text) <= limit:
        return text
    out: list[str] = []
    width = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        if width + step > limit - 1:
            break
        out.append(char)
        width += step
    return "".join(out) + "…"


def wrap_display(text: str, limit: int) -> list[str]:
    """Wrap plain text by terminal columns, preferring spaces and slashes."""
    if limit <= 2:
        return [text]
    lines: list[str] = []
    remaining = text
    while remaining:
        if disp_width(remaining) <= limit:
            lines.append(remaining)
            break
        width = 0
        cut = 0
        for index, char in enumerate(remaining):
            step = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
            if width + step > limit:
                break
            width += step
            cut = index + 1
        window = remaining[:cut]
        for separator in (" ", "-", "/", "\\"):
            position = window.rfind(separator)
            if position >= limit // 2:
                cut = position + 1
                break
        if cut <= 0:
            cut = 1
        lines.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return lines or [""]


def screen_width() -> int:
    try:
        return max(64, shutil.get_terminal_size((100, 24)).columns)
    except OSError:
        return 100


def rule() -> str:
    return style("─" * min(screen_width() - 4, 76), "90")


def print_header(title: str, subtitle: str = "") -> None:
    print()
    head = style(title, "1;36")
    if subtitle:
        head += style("  " + subtitle, "90")
    print("  " + head)
    print("  " + rule())


def print_meta(label: str, value: str) -> None:
    print("  " + style(label, "90") + "  " + value)


def selection_label(args: argparse.Namespace) -> str:
    if getattr(args, "path", None):
        return " · ".join(args.path)
    album = getattr(args, "album", None)
    return f"专辑 {album}" if album else "整个音乐库"


def short_path(path: Path, library: Path | None = None) -> str:
    try:
        path = path.resolve()
    except OSError:
        pass
    if library is not None:
        try:
            rel = path.relative_to(library.resolve())
            return str(rel) if str(rel) != "." else "（音乐库根目录）"
        except (ValueError, OSError):
            pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


STATUS_STYLE: dict[str, tuple[str, str, str]] = {
    "ready": ("✓", "1;32", "可写入"),
    "applied": ("✓", "1;32", "已写入"),
    "ok": ("✓", "1;32", "通过"),
    "skip_existing": ("·", "90", "已有歌词"),
    "instrumental": ("♪", "36", "纯音乐"),
    "ambiguous": ("!", "1;33", "需确认"),
    "suspect": ("!", "1;35", "时间轴可疑"),
    "no_match": ("✗", "1;31", "无匹配"),
    "error": ("✗", "1;31", "错误"),
    "missing": ("✗", "1;31", "文件丢失"),
    "changed": ("!", "1;33", "已变化"),
    "conflict": ("!", "1;33", "拒绝回滚"),
    "rolled_back": ("↩", "36", "已回滚"),
    "pending": ("↩", "36", "待移除"),
}


def status_style(status: str) -> tuple[str, str, str]:
    return STATUS_STYLE.get(status, ("•", "0", status))


CHIP_GROUPS = [
    (("ready", "applied", "ok"), "✓", "1;32"),
    (("ambiguous", "suspect", "changed", "conflict"), "!", "1;33"),
    (("no_match", "error", "missing"), "✗", "1;31"),
    (("rolled_back", "pending"), "↩", "36"),
    (("skip_existing", "instrumental"), "·", "90"),
]


NOTE_STYLE = {
    "ready": "90",
    "applied": "90",
    "ok": "90",
    "skip_existing": "90",
    "instrumental": "36",
    "ambiguous": "33",
    "suspect": "35",
    "no_match": "31",
    "error": "31",
    "missing": "31",
    "changed": "33",
    "conflict": "33",
    "rolled_back": "36",
    "pending": "36",
}


@dataclass
class TreeEntry:
    path: Path
    status: str
    note: str = ""


def print_tree(
    entries: list[TreeEntry],
    library: Path,
    limit: int = 200,
    totals: Counter | None = None,
) -> None:
    """Album-grouped tree with one annotated line per track."""
    if limit == 0:
        print("  " + style("明细已按 --tree-limit 0 关闭。", "90"))
        print()
        return
    if not entries:
        print("  " + style("没有可显示的明细。", "90"))
        return
    groups: dict[Path, list[TreeEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.path.parent, []).append(entry)
    plan = [
        (
            folder,
            sorted(groups[folder], key=lambda item: (item.path.name, str(item.path))),
        )
        for folder in sorted(groups, key=lambda item: str(item))
    ]
    name_budget = max(24, min(48, screen_width() - 44))
    shown = 0
    for folder, items in plan:
        room = len(items) if not limit else limit - shown
        if room <= 0:
            break
        visible = items[:room]
        counts = Counter(item.status for item in items)
        chips: list[str] = []
        for statuses, symbol, code in CHIP_GROUPS:
            total = sum(counts.get(status, 0) for status in statuses)
            if total:
                chips.append(style(f"{symbol}{total}", code))
        header = "  " + style("▸ ", "36") + style(short_path(folder, library), "1;36")
        total_tracks = totals.get(folder, len(items)) if totals else len(items)
        count_text = (
            f"{len(items)}/{total_tracks} 首"
            if total_tracks > len(items)
            else f"{len(items)} 首"
        )
        header += style(f"   {count_text}", "90")
        if chips:
            header += "  " + "  ".join(chips)
        print(header)
        names = [truncate_display(item.path.name, name_budget) for item in visible]
        name_width = max((disp_width(name) for name in names), default=0)
        for index, item in enumerate(visible):
            branch = "     └─ " if index == len(visible) - 1 else "     ├─ "
            symbol, code, _ = status_style(item.status)
            name_cell = (
                pad_display(names[index], name_width) if item.note else names[index]
            )
            line = (
                style(branch, "90")
                + style(symbol, code)
                + " "
                + name_cell
            )
            note_budget = screen_width() - disp_width(line) - 3
            detail = ""
            if item.note:
                if disp_width(item.note) <= note_budget:
                    line += "  " + style(
                        item.note, NOTE_STYLE.get(item.status, "90")
                    )
                elif item.status in {
                    "ambiguous",
                    "suspect",
                    "no_match",
                    "error",
                    "missing",
                    "changed",
                    "conflict",
                }:
                    detail = item.note
                else:
                    line += "  " + style(
                        truncate_display(item.note, max(16, note_budget)),
                        NOTE_STYLE.get(item.status, "90"),
                    )
            print(line.rstrip())
            if detail:
                note_code = NOTE_STYLE.get(item.status, "90")
                chunks = wrap_display(detail, max(24, screen_width() - 12))
                clipped = len(chunks) > 3
                for chunk_index, chunk in enumerate(chunks[:3]):
                    prefix = "        └ " if chunk_index == 0 else "          "
                    print(style(prefix, "90") + style(chunk, note_code))
                if clipped:
                    print(style("          …（完整信息见报告）", "90"))
            shown += 1
        print()
    omitted = len(entries) - shown
    if omitted > 0:
        print(
            "  "
            + style(
                f"… 另有 {omitted} 行未展开；--tree-limit 可调，或查看 HTML 复核页。",
                "33",
            )
        )
        print()


class ProgressLine:
    """Animated status line; silent when stdout is not a TTY."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self) -> None:
        self.enabled = (
            sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"
        )
        self._visible = False
        self._text = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = time.monotonic()
        self._frame = 0

    def start(self, text: str, started: float | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._text = text
            self._started = started if started is not None else time.monotonic()
            self._render()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._render()

    def update(self, text: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._text = text
            self._render()

    def _render(self) -> None:
        if self._stop.is_set():
            return
        frame = self.FRAMES[self._frame % len(self.FRAMES)]
        self._frame += 1
        elapsed = time.monotonic() - self._started
        line = (
            "\r  "
            + style(frame, "36")
            + " "
            + self._text
            + style(f"  ·  {elapsed:.0f}s", "90")
        )
        sys.stdout.write(line + "\033[K")
        sys.stdout.flush()
        self._visible = True

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=1)
            self._thread = None
        if self.enabled and self._visible:
            with self._lock:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                self._visible = False


def plan_note(row: sqlite3.Row) -> str:
    status = row["status"]
    if status in {"ready", "applied"}:
        bits: list[str] = []
        if row["source"]:
            bits.append(str(row["source"]))
        if row["score"]:
            bits.append(f"{row['score']}分")
        diff = row["duration_diff"]
        if diff is not None and diff < 900:
            bits.append(f"Δ{diff:.1f}s")
        kind = str(row["lyric_kind"] or "").upper()
        if kind:
            bits.append(f"{kind} {row['lyric_lines']}行" if row["lyric_lines"] else kind)
        return " · ".join(bits) or "匹配通过"
    if status == "skip_existing":
        return "已有歌词，未覆盖"
    if status == "instrumental":
        return "匹配为纯音乐"
    return str(row["reason"] or status_style(status)[2])


def print_summary(
    counter: Counter, label: str = "汇总", order: list[str] | None = None
) -> None:
    order = order or [
        "ready",
        "applied",
        "ambiguous",
        "suspect",
        "no_match",
        "error",
        "ok",
        "changed",
        "missing",
        "conflict",
        "rolled_back",
        "pending",
        "skip_existing",
        "instrumental",
    ]
    parts: list[str] = []
    for key in order:
        value = counter.get(key, 0)
        if value:
            symbol, code, name = status_style(key)
            parts.append(style(f"{symbol} {name}", code) + " " + str(value))
    for key, value in counter.items():
        if key not in order and value:
            parts.append(f"{key} {value}")
    if not parts:
        print("  " + style(label, "90") + "  " + style("无", "90"))
        return
    print("  " + style(label, "90") + "  " + "   ".join(parts))


def print_intro() -> None:
    print()
    print(
        "  "
        + style("lyrics", "1;36")
        + style(f"  {APP_VERSION}", "90")
        + "   批量歌词：匹配 → 写入 → 校验 → 回滚"
    )
    print("  " + rule())
    print("  只改歌词字段；其他标签、封面与音频数据不动。默认先 plan，不写文件。")
    print("  默认音乐库 ~/Music/JMusic；路径相对它解析，也可以直接拖入目录。")
    print()
    print("  " + style("常用", "1"))
    rows = [
        ('lyrics.py "結束バンド"', "直接拖入目录 = 只读 plan"),
        ('lyrics.py plan --path "Ado" --html', "生成计划与 HTML 复核页"),
        ('lyrics.py candidates --path "Ado"', "查看候选 ID（配合 --pick）"),
        ('lyrics.py apply --path "Ado"', "写入 ready 的曲目"),
        ('lyrics.py verify --path "Ado"', "校验歌词与其他标签"),
        ('lyrics.py rollback --path "Ado" --yes', "回滚本工具写入的歌词"),
    ]
    width = max(disp_width(command) for command, _ in rows)
    for command, note in rows:
        gap = " " * (width - disp_width(command) + 2)
        print("    " + style(command, "36") + gap + style(note, "90"))
    print()
    print(
        "  "
        + style("命令", "1")
        + "  "
        + "  ".join(
            style(name, "36")
            for name in ("scan", "plan", "candidates", "apply", "verify", "rollback")
        )
    )
    print(
        "  "
        + style("详细参数", "90")
        + "  lyrics.py -h"
        + style("      单个命令", "90")
        + "  lyrics.py plan -h"
    )
    print()


def summarize_plans(rows: list[sqlite3.Row]) -> Counter:
    return Counter(row["status"] for row in rows)


def build_sources(
    names: list[str], client: JsonClient
) -> list[Any]:
    registry = {"netease": NeteaseSource, "lrclib": LrclibSource}
    return [registry[name](client) for name in names]


def cmd_scan(args: argparse.Namespace, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    formats = Counter(track.fmt for track in tracks)
    existing = Counter(track.fmt for track in tracks if track.has_lyrics)
    print_header("scan", short_path(library))
    print("  " + style("▸ ", "36") + style("格式", "1;36"))
    items = sorted(formats)
    for index, fmt in enumerate(items):
        branch = "     └─ " if index == len(items) - 1 else "     ├─ "
        total = formats[fmt]
        have = existing.get(fmt, 0)
        line = (
            style(branch, "90")
            + style(fmt.upper().ljust(4), "1")
            + f"{total:>4} 首"
            + style(f"   已有歌词 {have:>4}   待补 {total - have:>4}", "90")
        )
        print(line)
    print()
    have_total = sum(existing.values())
    print_meta(
        "曲目",
        f"{len(tracks)} 首"
        + style(
            f"   已有歌词 {have_total}   待补 {len(tracks) - have_total}",
            "90",
        ),
    )
    if errors:
        print("  " + style("▸ ", "36") + style("标签读取失败", "1;31"))
        for index, (path, error) in enumerate(errors[:20]):
            branch = "     └─ " if index == len(errors[:20]) - 1 else "     ├─ "
            print(
                style(branch, "90")
                + style("✗ ", "1;31")
                + short_path(path, library)
                + style(f"  {error}", "90")
            )
        if len(errors) > 20:
            print(style(f"      … 另有 {len(errors) - 20} 个", "90"))
        print()
    else:
        print_meta("标签", style("全部读取成功", "32"))
    return 1 if errors else 0


def collect_picks(
    args: argparse.Namespace, tracks: list[Track], library: Path
) -> dict[str, str]:
    """Merge --pick and --pick-file into path -> SOURCE:ID."""
    picks: dict[str, str] = {}
    pick_file = getattr(args, "pick_file", None)
    if pick_file:
        picks.update(read_pick_file(Path(pick_file), library, tracks))

    values = getattr(args, "pick", None) or []
    if values:
        eligible = [track for track in tracks if not track.has_lyrics]
        if len(eligible) != 1:
            raise ToolError(
                f"--pick 要求当前范围恰好一首缺歌词的曲目，实际 {len(eligible)} 首。"
                "请把 --path 缩小到单首，或改用 --pick-file（每行：路径<TAB>源:ID）。"
            )
        if len(values) != 1:
            raise ToolError("单首曲目只能配一个 --pick；批量指定请用 --pick-file。")
        picks[str(eligible[0].path)] = values[0]

    selected = {str(track.path) for track in tracks}
    for path, value in picks.items():
        if path not in selected:
            raise ToolError(f"人工指定的曲目不在当前 --path 范围内：{path}")
        parse_pick(value)
    return picks


def cmd_plan(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    if not tracks:
        print_header("plan")
        print("  " + style("没有找到可处理的音频文件。", "33"))
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    client = JsonClient(state, refresh=args.refresh)
    sources = build_sources(args.sources, client)
    picks = collect_picks(args, tracks, library)
    report = args.report or (state_dir / "reports" / f"plan-{stamp}.csv")

    pending: list[Track] = []
    reused = 0
    for track in tracks:
        old = state.get_plan(str(track.path))
        if str(track.path) in picks:
            pending.append(track)
            continue
        if old and old["status"] == "suspect" and args.no_timestamp_check:
            pending.append(track)
            continue
        if (
            old
            and old["fingerprint"] == track.fingerprint()
            and not args.refresh
            and (
                old["status"] in {"skip_existing", "ready", "instrumental"}
                or (
                    old["status"] in {"no_match", "ambiguous", "suspect", "error"}
                    and not args.retry_failed
                )
            )
        ):
            reused += 1
            continue
        pending.append(track)

    scope = selection_label(args)
    print_header(
        "plan",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(tracks)} 首",
    )
    print_meta(
        "源",
        f"{','.join(args.sources)}"
        + style(f"   复用 {reused}   查询 {len(pending)}   并发 {args.workers}", "90"),
    )
    if picks:
        ignored = sum(1 for track in tracks if str(track.path) in picks and track.has_lyrics)
        print_meta(
            "指定",
            f"{len(picks)} 首"
            + (style(f"   其中 {ignored} 首已有歌词会被跳过", "33") if ignored else ""),
        )

    if pending:
        live = ProgressLine()
        if not live.enabled:
            print("  " + style(f"查询 {len(pending)} 首…（Ctrl+C 可中断）", "90"), flush=True)
        done = 0
        started = time.monotonic()
        live.start(f"查询 0/{len(pending)}", started)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    plan_one,
                    track,
                    sources,
                    not args.no_plain,
                    args.duration_tolerance,
                    not args.no_timestamp_check,
                    picks.get(str(track.path)),
                ): track
                for track in pending
            }
            for future in as_completed(futures):
                track = futures[future]
                try:
                    result = future.result()
                    fingerprint = track.fingerprint()
                except Exception as exc:
                    result = PlanResult(track, "error", f"{type(exc).__name__}: {exc}")
                    fingerprint = track.fingerprint()
                state.upsert_plan(result, fingerprint)
                done += 1
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed else 0
                if live.enabled:
                    live.update(f"查询 {done}/{len(pending)}  ·  {rate:.1f} 首/秒")
                elif done % args.progress_every == 0 or done == len(pending):
                    print(f"  查询 {done}/{len(pending)} · {rate:.1f} 首/秒")
        live.close()

    selected_paths = {str(track.path) for track in tracks}
    rows = [row for row in state.list_plans() if row["path"] in selected_paths]
    summary = summarize_plans(rows)
    actionable = {
        "ready",
        "applied",
        "ambiguous",
        "suspect",
        "no_match",
        "error",
        "rolled_back",
    }
    entries = [
        TreeEntry(Path(row["path"]), row["status"], plan_note(row))
        for row in rows
        if row["status"] in actionable
    ]
    print()
    if entries:
        totals = Counter(Path(row["path"]).parent for row in rows)
        print_tree(entries, library, args.tree_limit, totals)
    else:
        print("  " + style("所有曲目都已有歌词或无需处理。", "32"))
        print()

    print("  " + rule())
    print_summary(summary)
    for path in picks:
        row = state.get_plan(path)
        if row:
            symbol, code, label = status_style(row["status"])
            print(
                "  "
                + style("指定", "90")
                + "  "
                + style(symbol, code)
                + " "
                + Path(path).name
                + style(f"  {label} · {row['reason']}", "90")
            )
    idle = summary.get("skip_existing", 0) + summary.get("instrumental", 0)
    if idle and entries:
        print_meta("略过", f"{idle} 首已有歌词或纯音乐（未在树中展开）")
    if errors:
        print_meta("标签", style(f"读取失败 {len(errors)} 首（详见 scan）", "33"))

    write_plan_report(rows, report)
    writable = {"ready", "applied", "skip_existing"}
    if getattr(args, "lrc", False):
        lrc_dir = (args.lrc_dir or default_lrc_dir()).expanduser()
        written, failed = export_lrc(
            library,
            [row for row in rows if row["status"] in writable],
            {str(track.path): track for track in tracks},
            lrc_dir,
        )
        suffix = style(f"（失败 {failed}）", "31") if failed else ""
        print_meta("LRC", f"{written} 个 -> {short_path(lrc_dir)}{suffix}")
    if args.html is not None:
        html_path = (
            plan_html_path(state_dir, stamp)
            if args.html is True
            else Path(str(args.html)).expanduser()
        )
        write_plan_html(rows, html_path, library)
        print_meta("复核", short_path(html_path))
    print_meta("报告", short_path(report))
    print_meta("状态", short_path(state.path))
    if pending:
        for name in args.sources:
            print_meta(
                "请求",
                f"{name}  " + style("  ".join(SOURCE_ENDPOINTS[name]), "90"),
            )
    elif not pending and reused:
        print_meta("网络", "未发起请求，全部复用已有计划")
    scope_args = " ".join(f'"{item}"' for item in args.path) if args.path else ""
    hint = "lyrics.py apply" + (f" --path {scope_args}" if scope_args else "")
    print()
    print(
        "  "
        + style("未修改任何音频文件。", "32")
        + "确认后运行 "
        + style(hint, "36")
    )
    print()
    return 0


def cmd_candidates(args: argparse.Namespace, state: State) -> int:
    """Read-only candidate listing: the IDs needed by --pick / --pick-file."""
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    if not tracks:
        print_header("candidates")
        print("  " + style("没有找到可处理的音频文件。", "33"))
        return 2
    client = JsonClient(state, refresh=args.refresh)
    sources = build_sources(args.sources, client)
    wanted = [track for track in tracks if args.all or not track.has_lyrics]

    if args.tsv:
        for track in wanted:
            for source in sources:
                try:
                    candidates = source.search(track)
                except Exception:
                    continue
                ranked = []
                for candidate in candidates:
                    score, diff = score_candidate(track, candidate, args.duration_tolerance)
                    metadata_score, _ = score_candidate(track, candidate, 999.0)
                    ranked.append((metadata_score, score, diff, candidate))
                ranked.sort(
                    key=lambda item: (-item[0], -item[1], item[2], -item[3].synced)
                )
                for _, score, diff, candidate in ranked[: args.top]:
                    print(
                        f"{track.relpath}\t{candidate.source}:{candidate.source_id}"
                        f"\t{score}\t{diff:.1f}\t{candidate.title}"
                    )
        return 0

    print_header(
        "candidates",
        f"{len(wanted)} 首  ·  每源最多 {args.top} 条  ·  源 {','.join(args.sources)}",
    )
    if not wanted:
        print("  " + style("范围内所有曲目都已有歌词；加 --all 可强制列出。", "32"))
        print()
        return 0

    groups: dict[Path, list[Track]] = {}
    for track in wanted:
        groups.setdefault(track.path.parent, []).append(track)
    for folder in sorted(groups, key=lambda item: str(item)):
        print("  " + style("▸ ", "36") + style(short_path(folder, library), "1;36"))
        items = sorted(groups[folder], key=lambda track: (track.path.name, str(track.path)))
        for index, track in enumerate(items):
            last_track = index == len(items) - 1
            print(
                style("     └─ " if last_track else "     ├─ ", "90")
                + style(track.path.name, "1")
                + style(f"  {track.duration:.0f}s", "90")
            )
            prefix = "        " if last_track else "     │  "
            rows: list[tuple[str, str, str, str]] = []
            for source in sources:
                try:
                    candidates = source.search(track)
                except Exception as exc:
                    rows.append(
                        ("✗", "1;31", f"{source.name}", f"搜索失败 {type(exc).__name__}: {exc}")
                    )
                    continue
                ranked = []
                for candidate in candidates:
                    score, diff = score_candidate(track, candidate, args.duration_tolerance)
                    # Rank by metadata first: a correct title/artist with a long
                    # duration gap should outrank a cover that happens to be closer.
                    metadata_score, _ = score_candidate(track, candidate, 999.0)
                    ranked.append((metadata_score, score, diff, candidate))
                ranked.sort(
                    key=lambda item: (-item[0], -item[1], item[2], -item[3].synced)
                )
                for metadata_score, score, diff, candidate in ranked[: args.top]:
                    if score >= 85:
                        symbol, code = "✓", "1;32"
                    elif metadata_score >= 85:
                        symbol, code = "!", "1;33"
                    else:
                        symbol, code = "·", "90"
                    if candidate.synced or lyric_kind(candidate.lyrics) == "lrc":
                        kind = "LRC"
                    elif candidate.lyrics:
                        kind = "TXT"
                    else:
                        kind = "待取词"
                    score_text = (
                        f"{score:>3}分"
                        if score
                        else ("超容差" if metadata_score else "不匹配")
                    )
                    diff_text = f"Δ{diff:.1f}s" if diff < 900 else "Δ?"
                    rest = (
                        f"{score_text} {diff_text} {pad_display(kind, 6)}"
                        f"  {candidate.title} — {candidate.artist_text} / {candidate.album}"
                    )
                    rows.append(
                        (
                            symbol,
                            code,
                            f"{candidate.source}:{candidate.source_id}",
                            rest,
                        )
                    )
            if not rows:
                print(style(prefix, "90") + style("└─ ", "90") + style("无候选", "90"))
            id_width = max((disp_width(ident) for _, _, ident, _ in rows), default=0)
            for row_index, (symbol, code, ident, rest) in enumerate(rows):
                branch = "└─ " if row_index == len(rows) - 1 else "├─ "
                line = (
                    style(prefix, "90")
                    + style(branch, "90")
                    + style(symbol, code)
                    + " "
                    + style(pad_display(ident, id_width), "36")
                )
                budget = screen_width() - disp_width(line) - 3
                line += "  " + style(
                    truncate_display(rest, max(16, budget)), "90"
                )
                print(line)
        print()

    print(
        "  "
        + style(f"共 {len(wanted)} 首。", "90")
        + "把选中的 SOURCE:ID 交给 "
        + style("plan --pick", "36")
        + "，或整理成 "
        + style("--pick-file", "36")
        + "。"
    )
    if errors:
        print_meta("标签", style(f"读取失败 {len(errors)} 首（详见 scan）", "33"))
    print()
    return 0


def select_plan_rows(
    state: State,
    library: Path,
    album: str | None,
    limit: int | None,
    paths: list[str] | None = None,
) -> list[sqlite3.Row]:
    selected = resolve_selection(library, paths, strict=False)
    rows = [
        row
        for row in state.list_plans("ready")
        if selection_matches(Path(row["path"]), library, album, selected)
    ]
    rows.sort(key=lambda row: (Path(row["path"]).name, row["path"]))
    return rows[:limit] if limit else rows


def preflight_apply(rows: list[sqlite3.Row], workers: int) -> None:
    """Fail once, up front, instead of on the tenth file."""
    grouped: dict[Path, list[int]] = {}
    missing: list[str] = []
    for row in rows:
        path = Path(row["path"])
        try:
            size = path.stat().st_size
        except OSError:
            missing.append(str(path))
            continue
        grouped.setdefault(path.parent, []).append(size)

    problems: list[str] = []
    for directory, sizes in sorted(grouped.items()):
        probe = directory / f".lyrics-probe-{uuid.uuid4().hex[:8]}"
        try:
            probe.write_bytes(b"")
        except OSError as exc:
            problems.append(f"目录不可写：{directory}（{exc}）")
            continue
        finally:
            probe.unlink(missing_ok=True)
        free = shutil.disk_usage(directory).free
        need = sum(sorted(sizes, reverse=True)[: max(1, workers)]) * 1.2
        if free < need:
            problems.append(
                f"空间不足：{directory} 需要约 {need / 1e9:.2f} GB，可用 {free / 1e9:.2f} GB"
            )
    if missing:
        problems.append(f"{len(missing)} 个文件不存在，例如 {missing[0]}")
    if problems:
        hint = (
            "\n可在终端重新运行 apply；若确认目录可写，也可加 --no-preflight 跳过检查。"
        )
        raise ToolError("写入前检查未通过：\n  " + "\n  ".join(problems[:8]) + hint)


def cmd_apply(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    rows = select_plan_rows(state, library, args.album, args.limit, args.path)
    if not rows:
        print_header("apply")
        print(
            "  "
            + style("没有状态为 ready 的计划。先运行 plan，或检查筛选条件。", "33")
        )
        return 2
    scope = selection_label(args)
    print_header(
        "apply",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    print_meta(
        "方式",
        "临时副本验证后原子替换；失败则原文件不动"
        + style(f"   并发 {args.workers}", "90"),
    )
    if not args.no_preflight:
        live = ProgressLine()
        live.start("检查目录权限与空间…")
        try:
            preflight_apply(rows, args.workers)
        finally:
            live.close()
        print("  " + style("✓ 写入前检查通过", "32"))

    report = args.report or default_report_path(state_dir, "apply")
    counts: Counter = Counter()
    sources: Counter = Counter()
    applied_paths: set[str] = set()
    outcomes: list[tuple[Path, bool, str, str]] = []
    done = 0
    failed = False
    batch_size = 10
    live = ProgressLine()
    if not live.enabled:
        print("  " + style(f"写入 {len(rows)} 首…", "90"), flush=True)
    started = time.monotonic()
    live.start(f"写入 0/{len(rows)}", started)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            futures = {
                pool.submit(apply_one, row, library): row for row in batch
            }
            for future in as_completed(futures):
                row = futures[future]
                path = Path(row["path"])
                source = row["source"] or "-"
                try:
                    journal = future.result()
                    state.add_journal(journal)
                    state.set_plan_status(str(path), "applied", "写入并校验成功")
                    applied_paths.add(str(path))
                    counts["applied"] += 1
                    sources[source] += 1
                    outcomes.append((path, True, source, ""))
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    if "file policy" in str(exc):
                        message += "（当前会话无权覆盖音乐库，请在终端运行 apply）"
                    state.set_plan_status(str(path), "error", message)
                    counts["error"] += 1
                    outcomes.append((path, False, source, message))
                    failed = True
                done += 1
                if live.enabled:
                    elapsed = time.monotonic() - started
                    rate = done / elapsed if elapsed else 0
                    live.update(f"写入 {done}/{len(rows)}  ·  {rate:.1f} 首/秒")
                elif done % args.progress_every == 0 or done == len(rows):
                    print(f"  写入 {done}/{len(rows)}")
            if failed:
                break
    live.close()

    print()
    entries = [
        TreeEntry(
            path,
            "applied" if ok else "error",
            f"{source} · 写入并校验成功" if ok else message,
        )
        for path, ok, source, message in outcomes
    ]
    print_tree(entries, library, args.tree_limit)
    print("  " + rule())
    print_summary(counts)
    if sources:
        print_meta(
            "来源",
            "   ".join(f"{name} {count}" for name, count in sources.most_common()),
        )
    if failed and len(rows) > done:
        print_meta("未处理", f"{len(rows) - done} 首因失败停止而保持原样")
    print_meta("网络", "未发起请求，直接使用 plan 结果")
    if getattr(args, "lrc", False):
        lrc_dir = (args.lrc_dir or default_lrc_dir()).expanduser()
        written, failed_lrc = export_lrc(
            library,
            [row for row in rows if row["path"] in applied_paths],
            {},
            lrc_dir,
        )
        suffix = style(f"（失败 {failed_lrc}）", "31") if failed_lrc else ""
        print_meta("LRC", f"{written} 个 -> {short_path(lrc_dir)}{suffix}")
    attempted = {row["path"] for row in rows}
    rows_after = [row for row in state.list_plans() if row["path"] in attempted]
    write_plan_report(rows_after, report)
    print_meta("报告", short_path(report))
    print()
    if failed:
        print(
            "  "
            + style(
                "检测到失败，已停止后续批次。失败文件未替换，原文件保持原样。",
                "1;31",
            )
        )
        print()
        return 1
    print("  " + style("写入完成，可运行 lyrics.py verify 复核。", "32"))
    print()
    return 0


def verify_journal_row(row: sqlite3.Row, library: Path) -> tuple[str, str]:
    path = Path(row["path"])
    if not path.is_file():
        return "missing", "文件不存在"
    current = read_track(path, library)
    if lyric_hash(current.existing_lyrics) != row["written_lyrics_sha"]:
        return "changed", "歌词已被后续修改"
    if non_lyrics_tags_hash(path) != row["post_tags_sha"]:
        return "changed", "非歌词标签与写入后不一致"
    if cover_hash(path) != row["post_cover_sha"]:
        return "changed", "封面与写入后不一致"
    if audio_hash(path) != row["post_audio_sha"]:
        return "changed", "音频数据与写入后不一致"
    return "ok", ""


def cmd_verify(args: argparse.Namespace, state: State) -> int:
    library = args.library.expanduser().resolve()
    rows = state.list_journal("applied")
    selected = resolve_selection(library, args.path, strict=False)
    if args.album or selected:
        rows = [
            row
            for row in rows
            if selection_matches(Path(row["path"]), library, args.album, selected)
        ]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print_header("verify")
        print("  " + style("没有可校验的写入记录。", "33"))
        return 2
    scope = selection_label(args)
    print_header(
        "verify",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    print_meta("内容", "歌词、其他标签、封面、音频数据与写入时逐项比对")
    print()
    counts: Counter = Counter()
    entries: list[TreeEntry] = []
    live = ProgressLine()
    if not live.enabled:
        print("  " + style(f"校验 {len(rows)} 首…", "90"), flush=True)
    else:
        live.start(f"校验 0/{len(rows)}")
    for index, row in enumerate(rows, 1):
        status, reason = verify_journal_row(row, library)
        counts[status] += 1
        entries.append(TreeEntry(Path(row["path"]), status, reason))
        if live.enabled:
            live.update(f"校验 {index}/{len(rows)}")
        elif index % 25 == 0 and index < len(rows):
            print(f"  校验 {index}/{len(rows)}")
    live.close()
    print()
    print_tree(entries, library, args.tree_limit)
    print("  " + rule())
    print_summary(counts)
    print()
    if counts["ok"] == len(rows):
        print("  " + style("全部通过：文件与写入时一致。", "32"))
        print()
        return 0
    return 1


def cmd_rollback(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    rows = state.list_journal("applied")
    selected = resolve_selection(library, args.path, strict=False)
    if args.album or selected:
        rows = [
            row
            for row in rows
            if selection_matches(Path(row["path"]), library, args.album, selected)
        ]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print_header("rollback")
        print("  " + style("没有可回滚的记录。", "33"))
        return 2
    scope = selection_label(args)
    if not args.yes:
        print_header(
            "rollback",
            f"{truncate_display(scope, max(20, screen_width() - 30))}"
            f"  ·  {len(rows)} 首  ·  预览",
        )
        print_meta("范围", "只处理 journal 中由本工具写入、且歌词之后未被改动过的曲目")
        print()
        print_tree(
            [TreeEntry(Path(row["path"]), "pending") for row in rows],
            library,
            args.tree_limit,
        )
        print("  " + rule())
        print_summary(Counter({"pending": len(rows)}), label="合计")
        print()
        print(
            "  "
            + style("未修改任何文件。", "32")
            + "确认后追加 "
            + style("--yes", "1;36")
            + "。"
        )
        print()
        return 0

    print_header(
        "rollback",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    counts: Counter = Counter()
    entries: list[TreeEntry] = []
    live = ProgressLine()
    if not live.enabled:
        print("  " + style(f"回滚 {len(rows)} 首…", "90"), flush=True)
    else:
        live.start(f"回滚 0/{len(rows)}")
    for index, row in enumerate(rows, 1):
        path = Path(row["path"])
        if live.enabled:
            live.update(f"回滚 {index}/{len(rows)}")
        elif index % 10 == 0 and index < len(rows):
            print(f"  回滚 {index}/{len(rows)}")
        try:
            current = read_track(path, library)
            if lyric_hash(current.existing_lyrics) != row["written_lyrics_sha"]:
                counts["conflict"] += 1
                entries.append(TreeEntry(path, "conflict", "歌词已被修改，拒绝回滚"))
                continue
            pre_tags = non_lyrics_tags_hash(path)
            pre_cover = cover_hash(path)
            pre_audio = audio_hash(path)
            staged = stage_copy(path)
            try:
                clear_lyrics_only(staged, row["written_lyrics_sha"])
                if read_track(staged, library).existing_lyrics:
                    raise VerificationError("歌词未完全移除")
                if non_lyrics_tags_hash(staged) != pre_tags:
                    raise VerificationError("非歌词标签发生变化")
                if cover_hash(staged) != pre_cover:
                    raise VerificationError("封面发生变化")
                if audio_hash(staged) != pre_audio:
                    raise VerificationError("音频数据发生变化")
                os.replace(staged, path)
            except Exception:
                staged.unlink(missing_ok=True)
                raise
            state.set_journal_status(str(path), "rolled_back")
            state.set_plan_status(str(path), "rolled_back", "已回滚")
            counts["rolled_back"] += 1
            entries.append(TreeEntry(path, "rolled_back", "已移除并校验"))
        except Exception as exc:
            counts["error"] += 1
            entries.append(TreeEntry(path, "error", f"{type(exc).__name__}: {exc}"))
            break
    live.close()
    print()
    print_tree(entries, library, args.tree_limit)
    print("  " + rule())
    print_summary(counts)
    print()
    return 0 if not (counts["conflict"] or counts["error"]) else 1


MAIN_EPILOG = """\
依赖
  Python 3.9+，唯一第三方包是 mutagen，已用 PEP 723 写在文件头。
  直接运行 lyrics.py 时 uv 会自动把它装进自己的缓存，不需要手动 pip。
  sqlite3、urllib、csv 等全部是标准库，也不需要 sqlite 命令行工具。
  没有 uv 也能跑：python3 lyrics.py 会自动切换到装有 mutagen 的解释器。

直接运行 lyrics.py 显示简介；lyrics.py -h 显示本帮助。
省略命令时默认执行只读的 plan；也可以直接把文件或文件夹拖进来。

示例
  lyrics.py "/Users/jeasinl/Music/JMusic/結束バンド"     直接拖入目录＝只读 plan
  lyrics.py plan --path "Ado" "YOASOBI/(2023)THE BOOK 3"
  lyrics.py plan --path "Ado" --html                    生成 HTML 复核页
  lyrics.py candidates --path "Ado" --top 3             查看候选与 ID
  lyrics.py plan --path "Ado/某首.m4a" --pick netease:123
  lyrics.py apply --path "Ado"
  lyrics.py verify --path "Ado"
  lyrics.py rollback --path "Ado"          只预览
  lyrics.py rollback --path "Ado" --yes    执行
  lyrics.py plan --path "Ado" --lrc        额外在缓存目录留 .lrc

范围
  --path PATH [PATH ...]  选文件或目录；可重复；目录递归；相对路径按音乐库解析
  --library DIR           音乐库根目录，默认 ~/Music/JMusic
  --album DIR             只选单个专辑目录（旧参数，建议改用 --path）
  --limit N               最多处理 N 首，用于试跑
  --state-dir DIR         状态/缓存/报告目录，默认 ~/.local/state/lrc-patch

输出
  --tree-limit N          终端树状明细最多 N 行，默认 200；0 表示不展开
  --no-color              关闭颜色（也可设 NO_COLOR=1）
  --lrc                   同时把歌词保存为 .lrc（plan/apply）
  --lrc-dir DIR           .lrc 目录，默认 ~/.cache/lrc-patch/lrc

plan 专用
  --sources LIST          源顺序，默认 netease,lrclib；也可只写一个
  --workers N             查询并发 1..10，默认 4
  --no-plain              只接受带时间轴的 LRC
  --duration-tolerance S  时长容差，默认 2 秒
  --no-timestamp-check    不检查 LRC 末尾时间轴是否贴合音频时长
  --pick SOURCE:ID        人工指定候选；要求范围内恰好一首缺歌词的曲目
  --pick-file FILE        批量指定：每行 曲目路径<TAB>SOURCE:ID
  --html [FILE]           生成按专辑分组的复核页，可筛选状态
  --refresh               忽略 HTTP 缓存重查
  --retry-failed          重查 no_match/ambiguous/suspect/error
  --report FILE           CSV 报告路径
  --progress-every N      每 N 首打印一次进度

状态
  ✓ 可写入   ! 需确认   ! 时间轴可疑   ✗ 无匹配/错误   · 已有歌词   ♪ 纯音乐
  ambiguous / suspect 不会自动写入；用 candidates 找到 ID 后以 --pick 重新 plan。

apply / verify / rollback 请带上与 plan 相同的 --path，避免误处理之前规划过的其他曲目。
  apply 前会检查目录写权限和可用空间；--no-preflight 可跳过。
"""

PLAN_EPILOG = """\
示例
  lyrics.py plan --path "結束バンド"
  lyrics.py plan --path "Ado" "YOASOBI/(2023)THE BOOK 3" --workers 2
  lyrics.py plan --path "米津玄師" --lrc --lrc-dir ~/.cache/lrc-patch/lrc
  lyrics.py plan --path "結束バンド" --html
  lyrics.py plan --path "結束バンド/11 忘れてやらない.m4a" --pick netease:2003496467

plan 只读：不写音频，只更新状态数据库、CSV 和可选的 HTML 复核页。
ambiguous / suspect 不会自动写入；先看 HTML，再用 --pick 逐首确认。
"""

APPLY_EPILOG = """\
示例
  lyrics.py apply --path "結束バンド"
  lyrics.py apply --path "Ado" --workers 2

只写状态为 ready 的曲目；已有歌词的曲目不会被覆盖。
开始前会检查目标目录可写且空间足够；确实需要跳过时加 --no-preflight。
"""

VERIFY_EPILOG = """\
示例
  lyrics.py verify --path "結束バンド"

逐首比对歌词、其他标签、封面和音频数据与写入时的记录。
"""

ROLLBACK_EPILOG = """\
示例
  lyrics.py rollback --path "結束バンド"         只预览，不改文件
  lyrics.py rollback --path "結束バンド" --yes   执行移除

只处理 journal 中由本工具写入、且歌词之后没有被改动过的曲目。
"""


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--library",
        type=Path,
        default=DEFAULT_LIBRARY,
        metavar="DIR",
        help=f"音乐库根目录（默认 {DEFAULT_LIBRARY}）",
    )
    parser.add_argument(
        "--album",
        metavar="DIR",
        help="只处理某个专辑目录（绝对路径或相对音乐库）；建议改用 --path",
    )
    parser.add_argument(
        "--path",
        "--dir",
        action="append",
        nargs="+",
        metavar="PATH",
        help="只处理指定文件或目录；可重复；一次可跟多个路径；目录递归",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="最多处理 N 首，用于试跑")
    parser.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        help="状态、缓存与报告目录（默认 $XDG_STATE_HOME/lrc-patch）",
    )
    parser.add_argument(
        "--tree-limit",
        type=int,
        default=200,
        metavar="N",
        help="终端树状明细最多展开 N 行；0 表示不展开（默认 200）",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出（也可设 NO_COLOR=1）",
    )


def add_lrc_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lrc",
        action="store_true",
        help="同时把歌词保存为 .lrc 文件",
    )
    parser.add_argument(
        "--lrc-dir",
        type=Path,
        metavar="DIR",
        help=".lrc 输出目录（默认 $XDG_CACHE_HOME/lrc-patch/lrc）",
    )


def add_help_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyrics.py",
        usage="lyrics.py [命令] [参数 ...]",
        description="批量歌词规划、嵌入、校验与回滚工具（只改歌词字段）",
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(parser)
    parser.add_argument(
        "--version", action="version", version=APP_VERSION, help="显示版本并退出"
    )
    sub = parser.add_subparsers(
        dest="command", required=True, title="命令", metavar="<命令>"
    )

    scan = sub.add_parser(
        "scan",
        prog="lyrics.py scan",
        help="只读扫描格式和已有歌词",
        description="只读扫描：统计格式、数量与已有歌词，不修改任何文件。",
        add_help=False,
    )
    add_help_option(scan)
    add_common(scan)

    plan = sub.add_parser(
        "plan",
        prog="lyrics.py plan",
        help="生成计划，不修改音频",
        description="匹配歌词并写入状态数据库和 CSV；不修改音频。",
        epilog=PLAN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(plan)
    add_common(plan)
    add_lrc_options(plan)
    plan.add_argument(
        "--sources",
        metavar="LIST",
        default="netease,lrclib",
        help="歌词源顺序，逗号分隔：netease,lrclib（默认）",
    )
    plan.add_argument("--workers", type=int, metavar="N", default=4, help="查询并发数，默认 4")
    plan.add_argument("--no-plain", action="store_true", help="只接受带时间轴的 LRC")
    plan.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    plan.add_argument(
        "--retry-failed",
        action="store_true",
        help="重查 no_match/ambiguous/suspect/error；默认复用结果",
    )
    plan.add_argument("--report", type=Path, metavar="FILE", help="CSV 报告路径")
    plan.add_argument(
        "--pick",
        action="append",
        metavar="SOURCE:ID",
        help="人工指定候选，要求范围内恰好一首缺歌词的曲目；可写 netease:123",
    )
    plan.add_argument(
        "--pick-file",
        type=Path,
        metavar="FILE",
        help="批量人工指定：每行 曲目路径<TAB>SOURCE:ID，# 开头为注释",
    )
    plan.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"时长容差秒数，默认 {DEFAULT_DURATION_TOLERANCE:g}",
    )
    plan.add_argument(
        "--no-timestamp-check",
        action="store_true",
        help="不检查 LRC 末尾时间轴与音频时长是否相符",
    )
    plan.add_argument(
        "--html",
        nargs="?",
        const=True,
        metavar="FILE",
        help="生成按专辑分组的 HTML 复核页；省略文件名时写入 reports/",
    )
    plan.add_argument(
        "--progress-every", type=int, metavar="N", default=25, help="每 N 首打印进度"
    )

    candidates = sub.add_parser(
        "candidates",
        prog="lyrics.py candidates",
        help="列出候选 ID，供 --pick 使用（只读）",
        description="逐首查询并列出候选（含 ID、分数、时长差），不修改音频和计划。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        epilog=(
            "示例\n"
            '  lyrics.py candidates --path "結束バンド" --top 3\n'
            '  lyrics.py candidates --path "Ado" --tsv > picks.tsv\n\n'
            "输出中的 SOURCE:ID 可直接给 plan --pick，或整理成 --pick-file。"
        ),
    )
    add_help_option(candidates)
    add_common(candidates)
    candidates.add_argument(
        "--sources",
        metavar="LIST",
        default="netease,lrclib",
        help="歌词源顺序，逗号分隔（默认 netease,lrclib）",
    )
    candidates.add_argument("--top", type=int, metavar="N", default=3, help="每源最多显示 N 条")
    candidates.add_argument("--all", action="store_true", help="连已有歌词的曲目一起列出")
    candidates.add_argument("--tsv", action="store_true", help="输出制表符分隔，便于做 pick-file")
    candidates.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    candidates.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"排序用时长容差，默认 {DEFAULT_DURATION_TOLERANCE:g}",
    )

    apply_cmd = sub.add_parser(
        "apply",
        prog="lyrics.py apply",
        help="执行 ready 计划并逐文件校验",
        description="把 ready 的歌词写入音频；先写临时副本，校验通过后替换原文件。",
        epilog=APPLY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(apply_cmd)
    add_common(apply_cmd)
    add_lrc_options(apply_cmd)
    apply_cmd.add_argument(
        "--workers", type=int, metavar="N", default=4, help="写入并发数，默认 4"
    )
    apply_cmd.add_argument("--report", type=Path, metavar="FILE", help="CSV 报告路径")
    apply_cmd.add_argument(
        "--no-preflight",
        action="store_true",
        help="跳过写入前的目录权限与空间检查",
    )
    apply_cmd.add_argument(
        "--progress-every", type=int, metavar="N", default=10, help="每 N 首打印进度"
    )

    verify = sub.add_parser(
        "verify",
        prog="lyrics.py verify",
        help="校验已写入文件的歌词、标签、封面和音频",
        description="逐首比对写入时的歌词、其他标签、封面和音频数据。",
        epilog=VERIFY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(verify)
    add_common(verify)

    rollback = sub.add_parser(
        "rollback",
        prog="lyrics.py rollback",
        help="移除本工具写入且未改动的歌词",
        description="移除 journal 中由本工具写入、且之后未被改动过的歌词。",
        epilog=ROLLBACK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(rollback)
    add_common(rollback)
    rollback.add_argument(
        "--yes", action="store_true", help="确认执行移除（不加只预览）"
    )

    # Keep the generated help consistent with the hand-written sections.
    for item in (parser, scan, plan, candidates, apply_cmd, verify, rollback):
        item._optionals.title = "选项"  # noqa: SLF001

    return parser


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    argv = sys.argv[1:]
    if not argv:
        print_intro()
        return 0
    if argv[0] in {"help", "?"}:
        parser.print_help()
        return 0

    commands = {"scan", "plan", "apply", "verify", "rollback", "candidates"}
    if argv[0] in {"-h", "--help", "--version"}:
        pass
    elif argv[0].startswith("-"):
        print("未指定子命令，默认执行只读的 plan。", file=sys.stderr)
        argv = ["plan", *argv]
    elif argv[0] not in commands:
        print("未指定子命令，按只读 plan 处理这些路径。", file=sys.stderr)
        argv = ["plan", "--path", *argv]

    args = parser.parse_args(argv)
    if getattr(args, "no_color", False):
        set_color(False)
    raw_paths = getattr(args, "path", None) or []
    args.path = [item for group in raw_paths for item in group]
    state_dir = (args.state_dir or default_state_dir()).resolve()
    args.sources = [
        item.strip() for item in getattr(args, "sources", "").split(",") if item.strip()
    ]
    unknown = set(args.sources) - SOURCE_NAMES
    if unknown:
        parser.error(f"未知歌词源: {', '.join(sorted(unknown))}")
    if getattr(args, "workers", 1) < 1 or getattr(args, "workers", 1) > 10:
        parser.error("workers 必须在 1..10 之间")
    tolerance = getattr(args, "duration_tolerance", None)
    if tolerance is not None and not 0.5 <= tolerance <= 15:
        parser.error("--duration-tolerance 必须在 0.5..15 秒之间")
    if getattr(args, "top", 1) < 1:
        parser.error("--top 必须大于 0")
    if getattr(args, "tree_limit", 200) < 0:
        parser.error("--tree-limit 不能小于 0")

    # scan is read-only and does not touch the state database, so it may run
    # while another plan/apply holds the lock.
    if args.command == "scan":
        return cmd_scan(args, state_dir)

    with state_lock(state_dir):
        state = State(state_dir / "lyrics.sqlite3")
        if args.command == "plan":
            return cmd_plan(args, state, state_dir)
        if args.command == "candidates":
            return cmd_candidates(args, state)
        if args.command == "apply":
            return cmd_apply(args, state, state_dir)
        if args.command == "verify":
            return cmd_verify(args, state)
        if args.command == "rollback":
            return cmd_rollback(args, state, state_dir)

    parser.print_help()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n已中断；已完成的写入均已校验，未替换的文件保持原样。", file=sys.stderr)
        raise SystemExit(130) from None
