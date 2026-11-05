"""Lyric-format conversion: verbatim (word-timed) formats and their targets.

The providers serve two native verbatim formats:

* netease serves **YRC**  — ``[lineStart,lineDur](sylStart,sylDur,0)text``
* QQ serves **QRC**      — ``[lineStart,lineDur](sylStart,sylDur)text``

The user picks a *target* with a CLI flag.  Private formats (``--yrc``,
``--qrc``) are only ever served straight from their native source and error
out on the other source; public formats (``--a2``, ``--eslrc``, ``--ttml``)
are produced by converting either native format, and plain line-timed LRC is
the default (no flag) — any verbatim timeline is flattened to it.

What each format can carry
--------------------------

* ``lrc`` / ``plain`` — line start only, no word timing.
* ``a2`` — line start plus a word *start* per syllable.  A word's end is the
  next word's start, so durations are not stored.  Timestamps are
  ``mm:ss.cc``: **centiseconds**.
* ``eslrc`` — same data as A2, different markup (``[mm:ss.cc]`` inside the
  line).  Also centiseconds.
* ``ttml`` — line and word start **and** end, milliseconds, nested markup.
* ``yrc`` / ``qrc`` — the native formats; start and duration per syllable.

Two consequences are stated here because they used to be silent:

**Centisecond formats quantise.**  A millisecond timeline lands on the
nearest 10 ms when written as A2/ESLRC, so re-importing those files is
accurate to 10 ms rather than exact.  The untouched provider text stays in
the lyric store (``lrc-patch/lyr``, one file per format), so switching back to
``--qrc``/``--yrc`` loses nothing.

**Ruby (注音) has nowhere to live.**  QQ ships it inside the native QRC as a
``[kana:…]`` tag — a second, syllable-timed annotation for the whole song.
A2 and ESLRC are single-timeline formats, and decoding ``[kana:…]`` into
TTML's ruby structure has no published reference, so guessing would be worse
than dropping.  The tag is therefore never written into a converted file; it
survives in the native ``.qrc`` file of the lyric store, which is what
``apply --qrc`` reads, and the
plan note says how many lines were dropped.  (Before this rule the tag was
emitted verbatim: a 2 KB line of kana and millisecond numbers appeared at
``00:00`` in every converted file.)

Metadata tags follow the same logic: standard LRC tags (``[ti:]``, ``[ar:]``,
``[offset:]``…) travel with the lyric, provider-private ones (``[kana:]``,
``[klyric:]``…) do not.

Line model: a ``Line`` is ``(start_ms, dur_ms, [(syl_start_ms, syl_dur_ms,
word), ...])``.  YRC and QRC share this shape, so every public target can be
rendered from it regardless of which source produced the lyric — and every
public target can be parsed back into it, which is what makes the round-trip
tests in ``tests/test_core.py`` possible.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .errors import ToolError
from .text import lyric_tag, scripts

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

# A clock stamp: ``mm:ss``, ``mm:ss.cc``, ``mm:ss.ccc`` (hours are accepted on
# read for TTML, never written by us).
_CLOCK = r"(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?"

_YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_YRC_WORD_RE = re.compile(r"\((\d+),(\d+),0\)([^(]*)")
# QRC word: ``(start,dur)text`` — the marker *precedes* the word, exactly as
# ``qrc.py`` decodes and re-renders it (``[0,5000](0,200)我(200,300)爱``).
# Reading the text first turned the marker digits into part of the lyric and
# glued "0,200)" in front of every word.
_QRC_WORD_RE = re.compile(r"\((\d+),(\d+)\)([^(]*)")
# Any word marker, whichever provider wrote it (YRC has a third field).
_WORD_MARK_RE = re.compile(r"\(\d+,\d+(?:,0)?\)")

# Line-timed formats: ``[mm:ss.cc]rest`` and, inside ``rest``, the per-word
# variants ``<mm:ss.cc>`` (A2) and ``[mm:ss.cc]`` (ESLRC).
_LRC_LINE_RE = re.compile(r"^\[" + _CLOCK + r"\](.*)$")
_A2_WORD_RE = re.compile(r"<" + _CLOCK + r">([^<]*)")
_ES_WORD_RE = re.compile(r"\[" + _CLOCK + r"\]([^\[]*)")

# ``[ti:…]`` / ``[ar:…]`` … — a metadata line, not a lyric line.  The key must
# start with a letter so ``[00:12.34]`` never matches.
_TAG_LINE_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9_]*):(.*)\]\s*$")
# Netease's newer YRC starts with JSON credit lines
# (``{"t":0,"c":[{"tx":"作词 : Ayase"},…]}``) mixed into the same text.  They are
# not lyrics and must not become a lyric line either.
_JSON_META_RE = re.compile(r'^\{"t":\d+,"c":\[')
# Tags every player understands: they may travel into a converted file.
_STANDARD_TAGS = frozenset(
    {"ti", "ar", "al", "by", "offset", "length", "tool", "re", "ve", "au"}
)
# Provider-private payloads: a second timeline (``klyric``), ruby (``kana``),
# language hints.  Meaningful only next to the native text.
_RUBY_TAGS = frozenset({"kana", "roma", "ruby"})

TTML_NS = "http://www.w3.org/ns/ttml"


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
    # Set when the line is an LRC metadata tag: the lower-cased key (``ti``,
    # ``kana``).  ``text`` then holds the whole ``[key:value]`` line.
    tag: str = ""


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


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def parse_yrc(text: str) -> list[Line]:
    """Parse YRC into lines; tags and unknown lines become plain text lines."""
    return _parse_bracketed(text, word_first=True)


def parse_qrc(text: str) -> list[Line]:
    """Parse QRC into lines (the word marker precedes its text)."""
    return _parse_bracketed(text, word_first=False)


def _parse_bracketed(text: str, *, word_first: bool) -> list[Line]:
    lines: list[Line] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _YRC_LINE_RE.match(line)
        if not m:
            lines.append(_loose_line(line))
            continue
        start, dur = int(m.group(1)), int(m.group(2))
        content = m.group(3)
        words: list[Word] = []
        if word_first:
            words = [
                Word(int(ws), int(wd), wtext)
                for ws, wd, wtext in _YRC_WORD_RE.findall(content)
                if wtext
            ]
        else:
            # QRC: (start,dur)text(start,dur)text…  Each marker owns the text
            # up to the next marker; any text before the first marker is folded
            # into the first word so nothing is dropped.
            matches = list(_QRC_WORD_RE.finditer(content))
            if matches:
                lead = content[: matches[0].start()].strip()
                words = [
                    Word(int(match.group(1)), int(match.group(2)), match.group(3))
                    for match in matches
                ]
                if lead and words:
                    words[0] = Word(words[0].start, words[0].dur, lead + words[0].text)
                words = [word for word in words if word.text]
        if words:
            lines.append(Line(start, dur, words))
        elif _WORD_MARK_RE.search(content):
            # Timed but wordless: an instrumental/pause line.  Keep the stamp
            # and drop the marker, instead of printing "(0,200)" as lyrics.
            lines.append(Line(start, dur, [], text=""))
        else:
            lines.append(Line(start, dur, [], text=content))
    return lines


def parse_lrc(text: str) -> list[Line]:
    """Parse line-timed LRC: stamps only, no word timing to recover."""
    lines: list[Line] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LRC_LINE_RE.match(line)
        if not m:
            lines.append(_loose_line(line))
            continue
        # A repeated stamp (``[00:26.22][01:10.69]Rose``) means "show this line
        # again"; every stamp becomes its own line, as players do.
        rest = m.group(4)
        starts = [_stamp_ms(m)]
        while True:
            again = _LRC_LINE_RE.match(rest)
            if not again:
                break
            starts.append(_stamp_ms(again))
            rest = again.group(4)
        for start in starts:
            lines.append(Line(start, 0, [], text=rest))
    return lines


def parse_a2(text: str) -> list[Line]:
    """Parse LRC A2 (``[mm:ss.cc]<mm:ss.cc>word…``) back into lines.

    A2 stores no word duration: a word ends where the next one starts.  The
    parser reconstructs that, so a round trip through A2 keeps every word
    start and loses only the (inaudible) tail of the last word.
    """
    lines: list[Line] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LRC_LINE_RE.match(line)
        if not m:
            lines.append(_loose_line(line))
            continue
        start = _stamp_ms(m)
        rest = m.group(4)
        marks = list(_A2_WORD_RE.finditer(rest))
        if not marks:
            lines.append(Line(start, 0, [], text=rest))
            continue
        words = _words_from_marks(marks)
        lines.append(_line_from_words(start, words))
    return lines


def parse_eslrc(text: str) -> list[Line]:
    """Parse ESLyric (``[mm:ss.cc]word[mm:ss.cc]word``) back into lines.

    Every word carries its own stamp and the first stamp doubles as the line
    start, which is exactly how :func:`lines_to_eslrc` writes it.
    """
    lines: list[Line] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        marks = list(_ES_WORD_RE.finditer(line))
        if not marks:
            lines.append(_loose_line(line))
            continue
        start = _to_ms(*marks[0].groups()[:3])
        words = _words_from_marks(marks)
        lines.append(_line_from_words(start, words))
    return lines


def parse_ttml(text: str) -> list[Line]:
    """Parse TTML into lines, including word-level ``<span>`` timing.

    Only what lyric files actually use is read: ``<p>`` blocks with
    ``begin``/``end`` and their ``<span>`` children.  Both TTML time syntaxes
    are accepted — clock (``00:00:12.340``) and offset (``12.34s``,
    ``12340ms``) — because Apple writes the first one and older versions of
    this tool wrote the second.
    """
    try:
        root = ET.fromstring((text or "").lstrip("\ufeff"))
    except ET.ParseError as exc:
        raise ValueError(f"TTML 不是合法 XML：{exc}") from exc
    lines: list[Line] = []
    for para in root.iter(f"{{{TTML_NS}}}p"):
        begin = _ttml_ms(para.get("begin", ""))
        end = _ttml_ms(para.get("end", ""))
        spans = [child for child in para if child.tag == f"{{{TTML_NS}}}span"]
        words: list[Word] = []
        for index, span in enumerate(spans):
            start = _ttml_ms(span.get("begin", "")) or begin
            stop = _ttml_ms(span.get("end", ""))
            if not stop:
                following = spans[index + 1] if index + 1 < len(spans) else None
                stop = _ttml_ms(following.get("begin", "")) if following is not None else end
            words.append(Word(start, max(0, stop - start), span.text or ""))
        words = [word for word in words if word.text]
        if words:
            lines.append(Line(begin, max(0, end - begin), words))
            continue
        plain = "".join(para.itertext()).strip()
        if plain:
            lines.append(Line(begin, max(0, end - begin), [], text=plain))
    return lines


_PARSERS = {
    "lrc": parse_lrc,
    "a2": parse_a2,
    "eslrc": parse_eslrc,
    "ttml": parse_ttml,
    "yrc": parse_yrc,
    "qrc": parse_qrc,
}


def parse_native(text: str, kind: str) -> list[Line]:
    """Parse a lyric by the kind that produced it."""
    parser = _PARSERS.get((kind or "").lower())
    return parser(text) if parser else parse_lrc(text)


def count_ruby(text: str, kind: str) -> int:
    """How many 注音 tag lines the native lyric carries (0 for most songs)."""
    try:
        parsed = parse_native(text, kind)
    except (ValueError, ToolError):
        return 0
    return sum(1 for line in parsed if line.tag in _RUBY_TAGS)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _stamp_ms(match: re.Match) -> int:
    return _to_ms(*match.groups()[:3])


def _to_ms(minutes: object, seconds: object, fraction: object = None) -> int:
    total = int(str(minutes)) * 60_000 + int(str(seconds)) * 1000
    if fraction:
        total += int(str(fraction).ljust(3, "0")[:3])
    return total


def _words_from_marks(marks: list[re.Match]) -> list[Word]:
    """Turn ``<t>text`` / ``[t]text`` matches into words with inferred ends."""
    words: list[Word] = []
    for index, mark in enumerate(marks):
        start = _to_ms(*mark.groups()[:3])
        following = marks[index + 1] if index + 1 < len(marks) else None
        dur = max(0, _to_ms(*following.groups()[:3]) - start) if following else 0
        words.append(Word(start, dur, mark.group(4)))
    return [word for word in words if word.text]


def _line_from_words(start: int, words: list[Word]) -> Line:
    if not words:
        return Line(start, 0, [], text="")
    end = words[-1].start + words[-1].dur if words[-1].dur else words[-1].start
    return Line(start, max(0, end - start), words)


def _loose_line(raw: str) -> Line:
    """A line that is not timed: a metadata tag, a JSON credit, or text."""
    m = _TAG_LINE_RE.match(raw)
    if m:
        return Line(0, 0, [], text=raw, tag=m.group(1).lower())
    if _JSON_META_RE.match(raw):
        return Line(0, 0, [], text=raw, tag="jsonmeta")
    return Line(0, 0, [], text=raw)


def _fmt_ms(ms: int) -> str:
    """``mm:ss.cc`` — rounded, not truncated, with the carry handled.

    Truncating turned ``59_999`` into ``00:59.99`` and rounding it naively
    would have produced ``00:60.00``; doing the arithmetic in centiseconds
    keeps both cases honest.
    """
    total = max(0, int(round(max(0, int(ms)) / 10.0)))
    minutes, remainder = divmod(total, 6000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def _ttml_clock(ms: int) -> str:
    total = max(0, int(round(ms)))
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _ttml_ms(value: str) -> int:
    """Read a TTML time value: clock time or an offset with a metric."""
    value = (value or "").strip()
    if not value:
        return 0
    for metric, scale in (("ms", 1.0), ("s", 1000.0), ("m", 60_000.0), ("h", 3_600_000.0)):
        if value.endswith(metric):
            try:
                return int(round(float(value[: -len(metric)]) * scale))
            except ValueError:
                return 0
    parts = value.split(":")
    if len(parts) < 2:
        try:
            return int(round(float(value) * 1000))
        except ValueError:
            return 0
    try:
        total = 0
        for part in parts[:-1]:
            total = total * 60 + int(part)
        return int(round((total * 60 + float(parts[-1])) * 1000))
    except ValueError:
        return 0


def _join(out: list[str]) -> str:
    return "\n".join(out) + ("\n" if out else "")


def _text_line(line: Line) -> str:
    """One line with no per-word marks, in line-timed form.

    A stamp is kept whenever the line has one: ``[1000,2000]plain text`` from
    QRC and ``[00:01.00]plain text`` from LRC both used to fall through to the
    bare text, so a timed line silently lost its timing on the way to A2/ESLRC.
    """
    if line.text:
        if line.start or line.dur:
            return f"[{_fmt_ms(line.start)}]{line.text}"
        return line.text
    return f"[{_fmt_ms(line.start)}]"


def lines_to_a2(lines: list[Line]) -> str:
    """Render lines as LRC A2 (angle-bracket word stamps)."""
    out: list[str] = []
    for line in lines:
        if line.tag:
            if line.tag in _STANDARD_TAGS:
                out.append(line.text)
            continue
        if not line.words:
            out.append(_text_line(line))
            continue
        parts = [f"[{_fmt_ms(line.start)}]"]
        for w in line.words:
            parts.append(f"<{_fmt_ms(w.start)}>{w.text}")
        out.append("".join(parts))
    return _join(out)


def lines_to_eslrc(lines: list[Line]) -> str:
    """Render lines as ESLyric (square-bracket word stamps)."""
    out: list[str] = []
    for line in lines:
        if line.tag:
            if line.tag in _STANDARD_TAGS:
                out.append(line.text)
            continue
        if not line.words:
            out.append(_text_line(line))
            continue
        parts = []
        for w in line.words:
            parts.append(f"[{_fmt_ms(w.start)}]{w.text}")
        out.append("".join(parts))
    return _join(out)


def lines_to_lrc(lines: list[Line]) -> str:
    """Render lines as plain line-timed LRC, dropping per-word timing."""
    out: list[str] = []
    for line in lines:
        if line.tag:
            # Metadata rides along: ``[ti:…]`` is what players show as the
            # title, and dropping it would make the flattening lossy for no
            # reason.  Ruby/provider blobs are tags too, but they are not
            # standard, so _STANDARD_TAGS filters them out.
            if line.tag in _STANDARD_TAGS:
                out.append(line.text)
            continue
        if line.words:
            text = "".join(w.text for w in line.words)
            out.append(f"[{_fmt_ms(line.start)}]{text}")
        else:
            out.append(_text_line(line))
    return _join(out)


def _ttml_lang(text: str) -> str:
    used = scripts(text)
    if "kana" in used:
        return "ja"
    if "han" in used:
        return "zh"
    return "en"


def lines_to_ttml(lines: list[Line]) -> str:
    """Render lines as a TTML document with word-level timing.

    Word timing goes into ``<span>`` children of each ``<p>``, which is the
    shape Apple Music and every TTML lyric player reads; timestamps are clock
    time (``00:00:12.340``) rather than offset time (``12.340s``).  Metadata
    tags are not lyrics and are not written.
    """
    blocks = [
        line for line in lines if not line.tag and (line.words or line.text.strip())
    ]
    if not blocks:
        return ""
    lang = _ttml_lang(
        "".join(
            ("".join(w.text for w in line.words) if line.words else line.text)
            for line in blocks
        )
    )
    body: list[str] = []
    cursor = 0
    for line in blocks:
        if line.words:
            words = line.words
            line_end = line.start + line.dur if line.dur else 0
            if not line_end:
                line_end = words[-1].start + words[-1].dur
            if line_end <= line.start:
                # A line whose words all carry zero duration: give the block a
                # visible length rather than an end at or before its begin.
                line_end = line.start + max(1, len(words)) * 200
            spans = []
            for index, word in enumerate(words):
                stop = word.start + word.dur if word.dur else 0
                if not stop:
                    following = words[index + 1] if index + 1 < len(words) else None
                    stop = following.start if following else line_end
                if stop <= word.start:
                    stop = min(line_end, word.start + 200)
                spans.append(
                    f'<span begin="{_ttml_clock(word.start)}"'
                    f' end="{_ttml_clock(stop)}">{_xml_escape(word.text)}</span>'
                )
            body.append(
                f'      <p begin="{_ttml_clock(line.start)}"'
                f' end="{_ttml_clock(line_end)}">{"".join(spans)}</p>'
            )
            cursor = line_end
            continue
        # Text without word markers.  A QRC line can carry a line stamp and no
        # syllables; a stamp-less line (rare) is placed after the previous
        # block so its words are not lost.
        begin = line.start if (line.start or line.dur) else cursor
        stop = begin + line.dur if line.dur else begin + 2000
        body.append(
            f'      <p begin="{_ttml_clock(begin)}"'
            f' end="{_ttml_clock(stop)}">{_xml_escape(line.text)}</p>'
        )
        cursor = stop
    header = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<tt xmlns="{TTML_NS}" xml:lang="{lang}">\n'
        "  <body>\n"
        "    <div>\n"
    )
    return header + "\n".join(body) + "\n    </div>\n  </body>\n</tt>\n"


def lines_to_plain(lines: list[Line]) -> str:
    """Render lines as plain text, dropping whatever timing they carry."""
    out: list[str] = []
    for line in lines:
        if line.tag:
            continue
        text = "".join(w.text for w in line.words) if line.words else line.text
        if text.strip():
            out.append(text.rstrip())
    return _join(out)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


_RENDERERS = {
    "a2": lines_to_a2,
    "eslrc": lines_to_eslrc,
    "lrc": lines_to_lrc,
    "ttml": lines_to_ttml,
    "plain": lines_to_plain,
    "txt": lines_to_plain,
}


def render_lines(lines: list[Line], target: str) -> str:
    """Render parsed lines in ``target``; unknown targets flatten to LRC."""
    return _RENDERERS.get((target or "lrc").lower(), lines_to_lrc)(lines)


def verbatim_to_target(native_text: str, source_name: str, target: str) -> str:
    """Convert a native verbatim lyric to the requested target format.

    ``target`` is one of ``a2``/``eslrc``/``ttml`` (public) or the native
    private format itself (``yrc`` for netease, ``qrc`` for QQ).  For a
    private target the native text is returned unchanged (already validated by
    :func:`check_target_source`).
    """
    if target in ("yrc", "qrc"):
        return native_text
    kind = "yrc" if source_name == "netease" else "qrc"
    return render_lines(parse_native(native_text, kind), target)


def strip_provider_tags(text: str, kind: str) -> str:
    """Drop provider-private tags (注音 ``[kana:…]``, ``[klyric:…]``) in place.

    Plans written before this rule kept the whole kana blob inside the lyric,
    and apply writes the stored lyric as-is.  Re-rendering from that same text
    — no network, no re-plan — is how those rows reach a file without it.  A
    native ``yrc``/``qrc`` text is returned untouched: there the tag is part of
    the format, which is exactly what ``--qrc`` is for.
    """
    if (kind or "").lower() in ("yrc", "qrc"):
        return text
    try:
        parsed = parse_native(text, kind)
    except ValueError:
        return text
    if not any(line.tag and line.tag not in _STANDARD_TAGS for line in parsed):
        return text
    return render_lines(parsed, kind)


def has_timing(lines: list[Line]) -> bool:
    """True when the parsed lyric carries any real time information.

    QQ sometimes answers with a single ``[0,0]`` block holding the whole lyric
    and no per-line timing.  Flattening that to LRC used to emit one
    ``[00:00.00]`` line containing the entire song, which then looked like a
    suspicious timeline instead of a plain-text lyric.
    """
    return any(not line.tag and (line.words or line.start or line.dur) for line in lines)


def render_native(
    native_text: str, native_kind: str, target: str | None
) -> tuple[str, str]:
    """Produce the requested target from the provider's native lyric.

    Returns ``(lyrics, note)``.  A request the native format cannot satisfy
    falls back to line-timed LRC and explains itself in ``note`` instead of
    failing the whole track: the format is now stored per plan (and shown in
    plan/candidates), so apply can still convert later when that is possible.
    """
    want = (target or "lrc").lower()
    kind = (native_kind or "plain").lower()
    if want == kind:
        return native_text, ""
    if kind == "plain":
        if want in ("plain", "txt"):
            return native_text, ""
        return native_text, f"原生只有纯文本，无法生成 {want.upper()}，已按纯文本取词"
    if kind not in _PARSERS:
        return native_text, f"原生是 {lyric_tag(kind)}，无法生成 {want.upper()}，已按原样取词"
    parsed = parse_native(native_text, kind)
    if want in ("yrc", "qrc"):
        # The private formats are only ever served by their own source; a
        # conversion would have to re-encode the provider's own container.
        return lines_to_lrc(parsed), (
            f"原生是 {lyric_tag(kind)}，无法生成 {want.upper()}"
            f"（{want.upper()} 只能从源原样获取），已按逐行 LRC 取词"
        )
    if want in ("plain", "txt"):
        return lines_to_plain(parsed), ""
    if want == "lrc":
        return lines_to_lrc(parsed), ""
    # Word-level targets need syllables to place: a line-timed native has none.
    if not any(line.words for line in parsed):
        return lines_to_lrc(parsed), (
            f"原生没有逐字时间轴，无法生成 {want.upper()}，已按逐行 LRC 取词"
        )
    if not has_timing(parsed):
        return lines_to_lrc(parsed), (
            f"原生歌词没有时间轴，无法生成 {want.upper()}，已按逐行 LRC 取词"
        )
    return render_lines(parsed, want), ""
