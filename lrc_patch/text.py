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
#
# Format taxonomy (see https://amll.dev/guides/lyric/formats):
#   * lrc      — plain line-timed ``[mm:ss.xx]``
#   * a2       — LRC A2: word timestamps in angle brackets, ``[mm:ss.xx]<mm:ss.xx>词``
#   * eslrc    — ESLyric / SPL: word timestamps in square brackets, ``[mm:ss.xx]词[mm:ss.xx]语``
#   * yrc      — netease verbatim: ``[lineStart,lineDur](sylStart,sylDur,0)text``
#   * qrc      — QQ verbatim:     ``[lineStart,lineDur]text(sylStart,sylDur)``
#   * ttml     — Apple Music XML
_TTML_RE = re.compile(
    r"<\s*(?:tt|tt:tt|p)\b[^>]*\b(?:begin|xmlns|dur|end)=", re.IGNORECASE
)
# Angle-bracket word timestamps (LRC A2): <mm:ss.xx> or <mm:ss.xxx>.
_WORD_TIME_RE = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")
# ESLyric/SPL: a line whose body still carries square-bracket timestamps.
_SQ_WORD_TIME_RE = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
# One or more line timestamps *at the head* of a line.  A plain LRC may write
# a repeated chorus as ``[00:26.22][01:10.69][02:18.93]Rose``: several stamps,
# still one line-timed lyric, not word timing.
_LEAD_STAMPS_RE = re.compile(r"^(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+")
# YRC/QRC line head: [lineStart,lineDur] where both are integer milliseconds.
_YRC_LINE_RE = re.compile(r"^\[\d{1,7},\d{1,7}\]", re.MULTILINE)
# YRC word marker: (start,dur,0)text — timestamp first, three fields.
_YRC_WORD_RE = re.compile(r"\(\d+,\d+,0\)")
# QRC word marker: text(start,dur) — timestamp after text, two fields.
_QRC_WORD_RE = re.compile(r"\(\d+,\d+\)")

LYRIC_TAGS = {
    "plain": "TXT",
    "lrc": "LRC",
    "a2": "A2",
    "eslrc": "ESLRC",
    "yrc": "YRC",
    "qrc": "QRC",
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

_ARTIST_SEP_RE = re.compile(r"[;；,，、&]")
_ARTIST_FEAT_RE = re.compile(r"\bfeat\.?|\bft\.?|\bfeaturing\b", re.IGNORECASE)
_ARTIST_SLASH_RE = re.compile(r"[/／]")
_CJK_CHARS = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def _split_slash(chunk: str) -> list[str]:
    """Split ``A / B`` and ``甲/乙`` but keep ``AC/DC`` in one piece."""
    if not _ARTIST_SLASH_RE.search(chunk):
        return [chunk]
    if re.search(r"\s[/／]\s", chunk):
        return _ARTIST_SLASH_RE.split(chunk)
    if re.search(rf"[{_CJK_CHARS}][/／]|[/／][{_CJK_CHARS}]", chunk):
        return _ARTIST_SLASH_RE.split(chunk)
    return [chunk]


def artist_parts(text: str) -> list[str]:
    """Split an artist tag into loose names.

    Netease lists collaborators separately, hand-edited tags usually pack them
    into one frame (``Aimer; 阿部真央``, ``Aimer / chelly``, ``A & B``,
    ``A feat. B``).  Comparing the parts instead of the raw string keeps those
    spellings equivalent.
    """
    raw = unicodedata.normalize("NFKC", text or "")
    raw = _ARTIST_FEAT_RE.sub(";", raw)
    parts: list[str] = []
    for chunk in _ARTIST_SEP_RE.split(raw):
        for piece in _split_slash(chunk):
            loose = norm_loose(piece)
            if loose:
                parts.append(loose)
    return parts


def artist_matches(file_artists: list[str], cand_artists: list[str]) -> tuple[int, str]:
    """30 every file artist is listed / 26 some / 18 prefix match / 0 none."""
    if not file_artists or not cand_artists:
        return 0, ""
    missing = [name for name in file_artists if name not in cand_artists]
    if not missing:
        return 30, ""
    if len(missing) < len(file_artists):
        return 26, "歌手列表少了一部分合作者"
    if any(a in b or b in a for a in file_artists for b in cand_artists):
        return 18, "歌手名只是部分相同"
    return 0, ""


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
_FEAT_TOKENS = {"feat", "ft", "featuring", "duet", "with"}
_LANGUAGE_TOKENS = {
    "english", "japanese", "chinese", "korean", "jpn", "eng", "chi", "kor",
    "mandarin", "cantonese",
}
_EDITION_TOKENS = {
    "edition", "deluxe", "remaster", "remastered", "special", "expanded",
    "tour", "bonus", "anniversary", "explicit", "digital", "international",
    "japan", "japanese", "christmas", "holiday", "mono", "stereo", "reissue",
    "collector", "limited", "complete", "single", "ep",
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


_SCRIPT_RES = {
    "han": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "kana": re.compile(r"[\u3040-\u30ff]"),
    "latin": re.compile(r"[A-Za-z]"),
}


def scripts(text: str) -> set[str]:
    """Which writing systems a string uses (han / kana / latin)."""
    return {name for name, pattern in _SCRIPT_RES.items() if pattern.search(text or "")}


def looks_translated(base: str, decoration: str) -> str:
    """Return 读音 / 译名 when the decoration is a second script, else "".

    Netease ships ``Hz(ヘルツ)`` (kana reading of a Latin title) and
    ``太陽が昇らない世界 - A World Where the Sun Never Rises`` (a translated
    title after a dash).  Both are the same recording, so they must not read
    as a different song or a version variant.
    """
    base_scripts, deco_scripts = scripts(base), scripts(decoration)
    if not base_scripts or not deco_scripts:
        return ""
    if "kana" in deco_scripts and "kana" not in base_scripts:
        return "读音"
    # A Japanese title annotated with pure Chinese characters is a translation
    # ("歩く (漫步)"), even though both sides use Han script; treating it as a
    # generic decoration cost the candidate 15 points for no reason.
    if "kana" in base_scripts and deco_scripts == {"han"}:
        return "译名"
    if deco_scripts & base_scripts:
        return ""
    if "latin" in deco_scripts:
        words = re.findall(r"[A-Za-z][A-Za-z'’-]*", decoration)
        return "译名" if len(words) >= 2 else ""
    return "译名"


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
    decorations = [
        part.strip() for part in _TITLE_BRACKET_RE.findall(raw) if part.strip()
    ]
    base = _TITLE_BRACKET_RE.sub(" ", raw).strip()
    match = _TITLE_DASH_RE.search(base)
    if match:
        head, tail = base[: match.start()].strip(), match.group(1).strip()
        if _has_version(tail) or _has_feat(tail) or looks_translated(head, tail):
            decorations.append(tail)
            base = head
    return norm_loose(base), " ".join(decorations)


def feat_names(text: str) -> list[str]:
    """Names after a feat/with marker inside a title decoration."""
    raw = unicodedata.normalize("NFKC", text or "")
    raw = re.sub(r"[\(\)\[\]（）【】]", " ", raw)
    match = re.search(
        r"(?:feat\.?|ft\.?|featuring|with|duet)\b(.*)$", raw, re.IGNORECASE
    )
    if not match:
        return []
    tail = re.split(r"[;,、&]", match.group(1))
    return [part for part in (norm_loose(item) for item in tail) if part]


def _name_known(name: str, known: set[str]) -> bool:
    if not known:
        return False
    if name in known:
        return True
    return any(name in item or item in name for item in known if len(item) >= 3)


def title_points(
    file_title: str, candidate_title: str, artists: set[str] | None = None
) -> tuple[int, str]:
    """Points for the title alone, plus a human note when it is not exact.

    55 exact, or a collaborator the artist tag already lists / 50 translated
    title or reading / 42 version suffix / 35 other decoration / 30 featuring
    differs / 0 incompatible (different base, instrumental or language).
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
    if _has_version(file_dec) or _has_version(cand_dec):
        detail = cand_dec or file_dec
        return 42, f"标题版本变体「{detail}」" if detail else "标题版本变体"

    known = artists or set()
    file_feat, cand_feat = feat_names(file_dec), feat_names(cand_dec)
    if file_feat or cand_feat:
        names = file_feat + cand_feat
        if all(_name_known(name, known) for name in names):
            # The artist tag already names the collaborator: same recording,
            # only the title spelling differs.
            return 55, ""
        if set(file_feat) != set(cand_feat):
            return 30, f"合作者不同（{cand_dec or file_dec}）"
        return 55, ""

    detail = cand_dec or file_dec
    if detail:
        translated = looks_translated(cand_base, detail) or looks_translated(
            file_base, detail
        )
        if translated:
            return 50, f"标题附{translated}「{detail}」"
        return 35, f"标题附注「{detail}」"
    return 35, "标题附加信息"


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


def _album_parts(album: str) -> list[str]:
    """Netease names two-sided singles ``insane dream / us``."""
    raw = unicodedata.normalize("NFKC", album or "")
    if not re.search(r"\s[/／]\s", raw):
        return []
    return [part.strip() for part in re.split(r"\s[/／]\s", raw) if part.strip()]


def album_points(file_album: str, candidate_album: str) -> tuple[int, str]:
    """15 exact / 12 another edition or one side of a split single / 8 similar."""
    file_norm = norm_loose(file_album)
    cand_norm = norm_loose(candidate_album)
    if not file_norm or not cand_norm:
        return 0, ""
    if file_norm == cand_norm:
        return 15, ""
    file_base = strip_edition(file_album)
    cand_base = strip_edition(candidate_album)
    if file_base and cand_base and norm_text(file_base) == norm_text(cand_base):
        return 12, f"专辑版本不同「{candidate_album}」"
    for part in _album_parts(candidate_album):
        if norm_text(strip_edition(part)) == norm_text(file_base):
            return 12, f"专辑是合集「{candidate_album}」的一面"
    for part in _album_parts(file_album):
        if norm_text(strip_edition(part)) == norm_text(cand_base):
            return 12, f"专辑是合集「{file_album}」的一面"
    if min(len(file_norm), len(cand_norm)) >= 4 and (
        file_norm in cand_norm or cand_norm in file_norm
    ):
        return 8, "专辑名相近"
    return 0, ""


def match_notes(track: "Any", candidate: "Any", tolerance: float = 0.0) -> list[str]:
    """Every non-exact field, each as a short standalone sentence."""
    known: set[str] = set()
    for name in getattr(candidate, "artists", []) or []:
        known.update(artist_parts(name))
    _, title_note = title_points(track.title, candidate.title, known)
    _, album_note = album_points(track.album, candidate.album)
    notes = [note for note in (title_note, album_note) if note]
    if tolerance and candidate.duration:
        diff = abs(track.duration - candidate.duration)
        if diff > tolerance:
            notes.append(f"时长差 {diff:.1f}s，超过容差 {tolerance:.1f}s")
    return notes


def match_note(track: "Any", candidate: "Any", tolerance: float = 0.0) -> str:
    """One line of every non-exact field, for review output."""
    return "；".join(match_notes(track, candidate, tolerance))

def sanitize_lyrics(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return text.strip() + "\n" if text.strip() else ""

def lyric_kind(text: str) -> str:
    """Classify lyric markup: plain / lrc / a2 / eslrc / yrc / qrc / ttml.

    Providers and local files use several verbatim formats that look alike in
    a green column, so they must be told apart:
      * ``lrc``   — ``[00:12.34]`` line only
      * ``a2``    — LRC A2, word stamps in angle brackets ``<00:12.34>``
      * ``eslrc`` — ESLyric/SPL, word stamps in square brackets ``[00:12.34]``
      * ``yrc``   — netease ``[start,dur](start,dur,0)text``
      * ``qrc``   — QQ ``[start,dur](start,dur)text``
      * ``ttml``  — Apple Music XML

    YRC and QRC share the ``[lineStart,lineDur]`` head and both put the word
    marker before its text; the marker itself differs (three numbers for YRC,
    two for QRC).  Only ``lrc`` and ``a2`` share the line-timestamp checks.
    """
    body = (text or "").lstrip("\ufeff")
    if not body.strip():
        return "plain"
    head = body[:4000]
    if head.startswith("<?xml") or _TTML_RE.search(head):
        return "ttml"
    if _YRC_LINE_RE.search(body):
        # Both YRC and QRC open with [start,dur]; distinguish on the body.
        if _YRC_WORD_RE.search(body):
            return "yrc"
        return "qrc"
    if TIMESTAMP_RE.search(body):
        # Line-timed.  Angle brackets → LRC A2; square stamps *inside* the
        # text → ESLyric; otherwise plain LRC.  The leading run of stamps is
        # stripped first: ``[00:26.22][01:10.69]Rose`` is a repeated line, and
        # counting its stamps as word markers used to mislabel plain LRC as
        # ESLRC (which also changed the export suffix).
        if _WORD_TIME_RE.search(body):
            return "a2"
        for line in body.splitlines():
            if not _SQ_WORD_TIME_RE.search(line):
                continue
            if _SQ_WORD_TIME_RE.search(_LEAD_STAMPS_RE.sub("", line)):
                return "eslrc"
        return "lrc"
    return "plain"


def lyric_tag(kind: str) -> str:
    """Short uppercase label used by the terminal and the scan list."""
    return LYRIC_TAGS.get(kind, kind.upper())


_WORD_TIME_STRIP_RE = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")
_SQ_WORD_STRIP_RE = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")


def verbatim_to_line_lrc(text: str) -> str:
    """Flatten a verbatim (word-timed) lyric back to line-timed LRC.

    Handles LRC A2 (``[00:12.34]<00:12.34>一<00:12.78>起``) and ESLyric
    (``[00:12.34]一[00:12.78]起``), dropping the per-word stamps so the result
    is ``[00:12.34]一起``.  This is the safer form for players that choke on
    word markers, at the cost of losing per-word sync.  Plain LRC and other
    formats pass through unchanged.
    """
    kind = lyric_kind(text)
    out: list[str] = []
    if kind == "a2":
        strip = _WORD_TIME_STRIP_RE
        for line in (text or "").splitlines():
            if not strip.search(line):
                out.append(line)
                continue
            out.append(strip.sub("", line))
        return "\n".join(out) + ("\n" if out else "")
    if kind == "eslrc":
        # ESLyric puts a square timestamp before every word; the first one is
        # the line start, the rest are word stamps.  Keep the first, drop the
        # rest so the line stays line-timed LRC.
        for line in (text or "").splitlines():
            if not _SQ_WORD_STRIP_RE.search(line):
                out.append(line)
                continue
            # Remove all but the leading timestamp.
            parts = _SQ_WORD_STRIP_RE.split(line)
            # parts[0] is "" (line begins with a stamp), parts[1..] are words.
            stamps = _SQ_WORD_STRIP_RE.findall(line)
            if stamps:
                out.append(stamps[0] + "".join(parts[1:]))
            else:
                out.append(line)
        return "\n".join(out) + ("\n" if out else "")
    return text


def enhanced_to_line_lrc(text: str) -> str:
    """Back-compat alias for :func:`verbatim_to_line_lrc` (LRC A2 flavour)."""
    return verbatim_to_line_lrc(text)


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

    Only line-timestamped formats (LRC, LRC A2, ESLyric) can be checked this
    way; YRC, QRC and TTML carry their own timing model and are left alone
    rather than guessed at.
    """
    if duration <= 0 or lyric_kind(text) not in {"lrc", "a2", "eslrc"}:
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
