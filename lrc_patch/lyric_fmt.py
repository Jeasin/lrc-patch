"""Lyric-format conversion: verbatim (word-timed) formats and their targets.

The providers serve two native verbatim formats:

* netease serves **YRC**  — ``[lineStart,lineDur](sylStart,sylDur,0)text``
* QQ serves **QRC**      — ``[lineStart,lineDur]text(sylStart,sylDur)``

The user picks a *target* with a CLI flag.  Private formats (``--yrc``,
``--qrc``) are only ever served straight from their native source and error
out on the other source; public formats (``--a2``, ``--eslrc``, ``--ttml``)
are produced by converting either native format, and plain line-timed LRC is
the default (no flag) — any verbatim timeline is flattened to it.

Line model: a ``Line`` is ``(start_ms, dur_ms, [(syl_start_ms, syl_dur_ms,
word), ...])``.  YRC and QRC share this shape, so every public target can be
rendered from it regardless of which source produced the lyric.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ToolError

# Target formats the CLI accepts.
TARGET_FORMATS = ("a2", "eslrc", "yrc", "qrc", "ttml")

# Which native source each target requires; None means "convert from either".
_TARGET_NATIVE = {
    "a2": None,
    "eslrc": None,
    "ttml": None,
    "yrc": "netease",
    "qrc": "qq",
}

_YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_YRC_WORD_RE = re.compile(r"\((\d+),(\d+),0\)([^(]*)")
# QRC word: text immediately followed by (start,dur).  The text may itself
# contain no parentheses (QQ leaves literal parens in text, but they are rare
# and the marker always follows the word).
_QRC_PAIR_RE = re.compile(r"([^(]+?)\((\d+),(\d+)\)")


@dataclass
class Word:
    start: int
    dur: int
    text: str


@dataclass
class Line:
    start: int
    dur: int
    words: list[Word]
    # Raw text when the line carries no per-word markers.
    text: str = ""


def target_native_source(target: str) -> str | None:
    """The source a private target must come from, or None if convertible."""
    return _TARGET_NATIVE.get(target)


def check_target_source(target: str, source_name: str) -> None:
    """Raise when a private format is asked of the wrong source."""
    native = target_native_source(target)
    if native and native != source_name:
        raise ToolError(
            f"{target.upper()} 是 {native} 的私有逐字格式，不能从 {source_name} 源获取；"
            f"请改用 --a2 / --eslrc / --ttml 转换，或去掉该逐字参数"
        )


def parse_yrc(text: str) -> list[Line]:
    """Parse YRC into lines; tags and unknown lines become plain text lines."""
    return _parse_bracketed(text, word_first=True)


def parse_qrc(text: str) -> list[Line]:
    """Parse QRC into lines (word markers follow the text)."""
    return _parse_bracketed(text, word_first=False)


def _parse_bracketed(text: str, *, word_first: bool) -> list[Line]:
    lines: list[Line] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _YRC_LINE_RE.match(line)
        if not m:
            lines.append(Line(0, 0, [], text=line))
            continue
        start, dur = int(m.group(1)), int(m.group(2))
        content = m.group(3)
        words: list[Word] = []
        if word_first:
            for ws, wd, wtext in _YRC_WORD_RE.findall(content):
                if wtext:
                    words.append(Word(int(ws), int(wd), wtext))
        else:
            # QRC: text(start,dur)text(start,dur)…  Match each word with its
            # trailing marker; any trailing text without a marker is appended
            # to the last word (or kept as the line's raw text).
            pos = 0
            for m in _QRC_PAIR_RE.finditer(content):
                ws, wd, wtext = int(m.group(2)), int(m.group(3)), m.group(1)
                if wtext.strip():
                    words.append(Word(ws, wd, wtext.strip()))
                pos = m.end()
            trailing = content[pos:].strip()
            if trailing and words:
                # Text after the last marker: fold it into the last word.
                words[-1] = Word(words[-1].start, words[-1].dur, words[-1].text + trailing)
            elif trailing:
                words = [Word(0, 0, trailing)]
        if not words:
            lines.append(Line(start, dur, [], text=content))
        else:
            lines.append(Line(start, dur, words))
    return lines


def _fmt_ms(ms: int) -> str:
    ms = max(0, int(ms))
    minutes, remainder = divmod(ms, 60000)
    seconds, hundredths = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{hundredths // 10:02d}"


def lines_to_a2(lines: list[Line]) -> str:
    """Render lines as LRC A2 (angle-bracket word stamps)."""
    out: list[str] = []
    for line in lines:
        if not line.words:
            if line.text:
                out.append(line.text)
            else:
                out.append(f"[{_fmt_ms(line.start)}]")
            continue
        parts = [f"[{_fmt_ms(line.start)}]"]
        for w in line.words:
            parts.append(f"<{_fmt_ms(w.start)}>{w.text}")
        out.append("".join(parts))
    return "\n".join(out) + ("\n" if out else "")


def lines_to_eslrc(lines: list[Line]) -> str:
    """Render lines as ESLyric (square-bracket word stamps)."""
    out: list[str] = []
    for line in lines:
        if not line.words:
            out.append(line.text if line.text else f"[{_fmt_ms(line.start)}]")
            continue
        parts = []
        for w in line.words:
            parts.append(f"[{_fmt_ms(w.start)}]{w.text}")
        out.append("".join(parts))
    return "\n".join(out) + ("\n" if out else "")


def lines_to_lrc(lines: list[Line]) -> str:
    """Render lines as plain line-timed LRC, dropping per-word timing."""
    out: list[str] = []
    for line in lines:
        if line.words:
            text = "".join(w.text for w in line.words)
            out.append(f"[{_fmt_ms(line.start)}]{text}")
        elif line.text:
            out.append(line.text)
        else:
            out.append(f"[{_fmt_ms(line.start)}]")
    return "\n".join(out) + ("\n" if out else "")


def lines_to_ttml(lines: list[Line]) -> str:
    """Render lines as a minimal TTML document with word-level timing."""
    begin = 0.0
    body: list[str] = []
    for line in lines:
        text = "".join(w.text for w in line.words) if line.words else line.text
        if not text:
            continue
        start_s = line.start / 1000.0 if (line.words or line.start) else begin
        end_s = (line.start + line.dur) / 1000.0 if line.dur else start_s + 3.0
        body.append(
            f'    <p begin="{start_s:.3f}s" end="{end_s:.3f}s">{_xml_escape(text)}</p>'
        )
        begin = end_s
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tt xmlns="http://www.w3.org/ns/ttml">\n'
        "  <body>\n"
    )
    return header + "\n".join(body) + "\n  </body>\n</tt>\n"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def verbatim_to_target(native_text: str, source_name: str, target: str) -> str:
    """Convert a native verbatim lyric to the requested target format.

    ``target`` is one of ``a2``/``eslrc``/``ttml`` (public) or the native
    private format itself (``yrc`` for netease, ``qrc`` for QQ).  For a
    private target the native text is returned unchanged (already validated by
    :func:`check_target_source`).
    """
    if target in ("yrc", "qrc"):
        return native_text
    parse = parse_yrc if source_name == "netease" else parse_qrc
    lines = parse(native_text)
    if target == "a2":
        return lines_to_a2(lines)
    if target == "eslrc":
        return lines_to_eslrc(lines)
    if target == "ttml":
        return lines_to_ttml(lines)
    # Default (line LRC) is handled by the caller via lines_to_lrc.
    return lines_to_lrc(lines)
