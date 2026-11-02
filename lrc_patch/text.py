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

# Lyric markup beyond plain LRC.  Detection is deliberately conservative: a
# wrong "advanced" label only changes the colour, but a wrong "ttml" label
# would skip the timeline check for an ordinary LRC.
_TTML_RE = re.compile(
    r"<\s*(?:tt|tt:tt|p)\b[^>]*\b(?:begin|xmlns|dur|end)=", re.IGNORECASE
)
_WORD_TIME_RE = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")
_YRC_LINE_RE = re.compile(r"^\[\d{1,4},\d{1,5}\]", re.MULTILINE)

LYRIC_TAGS = {
    "plain": "TXT",
    "lrc": "LRC",
    "enhanced": "LRC+",
    "yrc": "YRC",
    "ttml": "TTML",
}


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


# -- title / album comparison -------------------------------------------
#
# Providers decorate titles in ways that do not change the lyrics:
# "Beautiful World (Da Capo Version)" is the same song as "Beautiful World"
# on that album, while "(feat. X)" and language variants are different
# recordings.  The helpers below separate the base title from its trailing
# decorations and score the difference, so a version suffix no longer wipes
# out an otherwise perfect match.

_TITLE_BRACKET_RE = re.compile(r"[\(\[（【]([^\)\]）】]*)[\)\]）】]")
_TITLE_DASH_RE = re.compile(r"\s+[-–—]\s+(.+)$")
_ALBUM_BRACKET_RE = re.compile(r"\s*[\(\[（【]([^\)\]）】]*)[\)\]）】]\s*$")
_ALBUM_DASH_RE = re.compile(r"\s+[-–—]\s+(.+)$")
_ALBUM_YEAR_RE = re.compile(
    r"^\s*(?:[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*|(?:19|20)\d{2}\s*[-–—.:]\s*)"
)

_VERSION_TOKENS = {
    "version", "ver", "remaster", "remastered", "remix", "live", "acoustic",
    "extended", "demo", "mono", "stereo", "instrumental", "inst", "karaoke",
    "dacapo", "reprise", "orchestra", "orchestral", "piano", "anniversary",
    "deluxe", "edition", "soundtrack", "rerecorded", "tv",
}
_INSTRUMENTAL_TOKENS = {"instrumental", "inst", "karaoke"}
_FEAT_TOKENS = {"feat", "ft", "featuring", "duet"}
_LANGUAGE_TOKENS = {
    "english", "japanese", "chinese", "korean", "jpn", "eng", "chi", "kor",
    "mandarin", "cantonese",
}
_EDITION_TOKENS = {
    "edition", "deluxe", "remaster", "remastered", "special", "expanded",
    "tour", "bonus", "anniversary", "explicit", "digital", "international",
    "japan", "japanese", "christmas", "holiday", "mono", "stereo", "reissue",
    "collector", "limited", "complete",
}
_CJK_VERSION = (
    "バージョン", "リマスター", "ライブ", "アコースティック", "インスト",
    "カラオケ", "オフボーカル", "ピアノ", "オーケストラ", "版本", "重制",
)
_CJK_INSTRUMENTAL = ("インスト", "オフボーカル", "カラオケ", "伴奏", "纯音乐", "純音樂")
_CJK_LANGUAGE = ("日本語", "英語", "中文", "國語", "粤语", "韓国語", "한국어")
_CJK_EDITION = ("版", "盤", "盘")


def _latin_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", unicodedata.normalize("NFKC", text or "").casefold()))


def _has_version(text: str) -> bool:
    tokens = _latin_tokens(text)
    if tokens & _VERSION_TOKENS:
        return True
    compact = re.sub(r"[^a-z]+", "", unicodedata.normalize("NFKC", text or "").casefold())
    if "offvocal" in compact or "tvsize" in compact:
        return True
    return any(word in (text or "") for word in _CJK_VERSION)


def _has_instrumental(text: str) -> bool:
    if _latin_tokens(text) & _INSTRUMENTAL_TOKENS:
        return True
    compact = re.sub(r"[^a-z]+", "", text or "")
    if "offvocal" in compact:
        return True
    return any(word in (text or "") for word in _CJK_INSTRUMENTAL)


def _has_feat(text: str) -> bool:
    return bool(_latin_tokens(text) & _FEAT_TOKENS)


def _languages(text: str) -> set[str]:
    found = set(_latin_tokens(text) & _LANGUAGE_TOKENS)
    found.update(word for word in _CJK_LANGUAGE if word in (text or ""))
    return found


def split_title(text: str) -> tuple[str, str]:
    """Return (base, decorations) for ``Beautiful World (Da Capo Version)``."""
    raw = unicodedata.normalize("NFKC", text or "")
    decorations = _TITLE_BRACKET_RE.findall(raw)
    base = _TITLE_BRACKET_RE.sub(" ", raw)
    match = _TITLE_DASH_RE.search(base)
    if match and (_has_version(match.group(1)) or _has_feat(match.group(1))):
        decorations.append(match.group(1))
        base = base[: match.start()]
    return norm_loose(base), " ".join(part.strip() for part in decorations if part.strip())


def title_points(file_title: str, candidate_title: str) -> tuple[int, str]:
    """Points for the title alone, plus a human note when it is not exact.

    55 exact / 42 version suffix / 35 other suffix / 30 featuring differs /
    0 incompatible (different base, instrumental or language variant).
    """
    file_base, file_dec = split_title(file_title)
    cand_base, cand_dec = split_title(candidate_title)
    if not file_base or not cand_base:
        return 0, ""
    if norm_loose(file_title) == norm_loose(candidate_title):
        return 55, ""
    if file_base != cand_base:
        return 0, ""

    if _has_instrumental(file_dec) != _has_instrumental(cand_dec):
        return 0, "纯音乐版本不同"
    file_lang, cand_lang = _languages(file_dec), _languages(cand_dec)
    if file_lang and cand_lang and file_lang != cand_lang:
        return 0, "语言版本不同"
    if _has_feat(file_dec) != _has_feat(cand_dec):
        return 30, "合作者不同"

    detail = cand_dec or file_dec
    if _has_version(file_dec) or _has_version(cand_dec):
        return 42, f"标题版本变体（{detail}）" if detail else "标题版本变体"
    return 35, f"标题附加信息（{detail}）" if detail else "标题附加信息"


def _is_edition(text: str) -> bool:
    if _latin_tokens(text) & _EDITION_TOKENS:
        return True
    return any(word in (text or "") for word in _CJK_EDITION)


def strip_edition(text: str) -> str:
    """Drop ``(2021)`` prefixes and ``(Deluxe Edition)`` style suffixes."""
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = _ALBUM_YEAR_RE.sub("", value)
    while True:
        match = _ALBUM_BRACKET_RE.search(value)
        if not match or not _is_edition(match.group(1)):
            break
        value = value[: match.start()].strip()
    match = _ALBUM_DASH_RE.search(value)
    if match and _is_edition(match.group(1)):
        value = value[: match.start()].strip()
    return value


def album_points(file_album: str, candidate_album: str) -> tuple[int, str]:
    """15 exact / 12 same album in another edition / 8 containment / 0 none."""
    file_norm = norm_loose(file_album)
    cand_norm = norm_loose(candidate_album)
    if not file_norm or not cand_norm:
        return 0, ""
    if file_norm == cand_norm:
        return 15, ""
    file_base = strip_edition(file_album)
    cand_base = strip_edition(candidate_album)
    if file_base and cand_base and norm_text(file_base) == norm_text(cand_base):
        return 12, "专辑版本不同"
    if file_norm in cand_norm or cand_norm in file_norm:
        return 8, "专辑名相近"
    return 0, ""


def match_note(track: "Any", candidate: "Any") -> str:
    """Short explanation of every non-exact field, for review output."""
    _, title_note = title_points(track.title, candidate.title)
    _, album_note = album_points(track.album, candidate.album)
    return " · ".join(note for note in (title_note, album_note) if note)

def sanitize_lyrics(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return text.strip() + "\n" if text.strip() else ""

def lyric_kind(text: str) -> str:
    """Classify lyric markup: plain / lrc / enhanced / yrc / ttml.

    Providers and local files use more than one lyric format, and they look
    identical in a green column.  ``enhanced`` is LRC with per-word
    timestamps (``[00:12.34]<00:12.34>word``), ``yrc``/``qrc`` are the
    ``[start,duration](start,duration,0)`` line formats used by QQ Music and
    karaoke players, ``ttml`` is Apple Music's XML.  Only the first two share
    the line-timestamp checks; the rest are still accepted as lyrics.
    """
    body = (text or "").lstrip("\ufeff")
    if not body.strip():
        return "plain"
    head = body[:4000]
    if head.startswith("<?xml") or _TTML_RE.search(head):
        return "ttml"
    if _YRC_LINE_RE.search(body):
        return "yrc"
    if TIMESTAMP_RE.search(body):
        return "enhanced" if _WORD_TIME_RE.search(body) else "lrc"
    return "plain"


def lyric_tag(kind: str) -> str:
    """Short uppercase label used by the terminal and the scan list."""
    return LYRIC_TAGS.get(kind, kind.upper())


def lyric_line_count(text: str, kind: str | None = None) -> int:
    """Meaningful line count; TTML counts <p> blocks instead of XML lines."""
    body = text or ""
    if (kind or lyric_kind(body)) == "ttml":
        return len(re.findall(r"<p\b", body))
    return len([line for line in body.splitlines() if line.strip()])

def is_placeholder(text: str) -> bool:
    plain = re.sub(r"\[[^\]]*\]", "", text or "").strip().casefold()
    return not plain or plain in PLACEHOLDERS

def lyrics_line_count(text: str) -> int:
    return lyric_line_count(text)

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
    """Describe a suspicious LRC timeline, or return "" when it looks sane.

    Only line-timestamped formats can be checked this way; word-level
    (enhanced), YRC and TTML carry their own timing model and are left alone
    rather than guessed at.
    """
    if duration <= 0 or lyric_kind(text) not in {"lrc", "enhanced"}:
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
