"""Read tags, write only the lyrics field, and verify every write."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import uuid
from pathlib import Path
from typing import Any, Iterable

from mutagen.dsf import DSF
from mutagen.id3 import ID3, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from .config import AUDIO_EXTS, LYRICS_LANG
from .errors import ToolError, VerificationError
from .models import Track
from .paths import iter_selected_files, resolve_selection, selection_matches
from .text import (
    first_text,
    lyric_hash,
    sha256_bytes,
    sha256_text,
    utc_now,
    write_text_atomic,
)


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

def lrc_dest(path: Path, library: Path, lrc_dir: Path) -> Path:
    """Mirror the library layout under the cache, keeping the track name."""
    try:
        rel = path.resolve().relative_to(library)
    except ValueError:
        rel = Path("_external") / f"{sha256_text(str(path))[:8]}-{path.name}"
    return (lrc_dir / rel).with_suffix(".lrc")

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
