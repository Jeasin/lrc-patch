"""Text, hashing and LRC helpers (no audio or network dependencies)."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .config import (
    LRC_TIME_RE,
    MAX_LRC_OVERRUN,
    MIN_LRC_COVERAGE,
    PLACEHOLDERS,
    TIMESTAMP_RE,
)


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

def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
