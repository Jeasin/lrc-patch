"""The native-lyric store: one file per song, on disk, where a human can read it.

The plan database records *which* file a track uses; the provider's own text
lives here as ``歌手 - 标题 [ne:123].yrc`` under ``$XDG_CACHE_HOME/lrc-patch/lyr``.
A file can be opened, diffed, grep-ed, copied to a phone or edited by hand; a
BLOB in a cache database cannot.

Everything here is regenerable: the same song always lands on the same name,
a re-plan overwrites it, and a format the source stopped serving is dropped —
so an ID maps to the formats it actually has, and no stale text lingers.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import source_label
from .text import write_text_atomic

# Same taxonomy as the embedded lyric: the extension is how the next tool
# knows a word-timed file from a line-timed one.
SUFFIX = {
    "lrc": ".lrc",
    "a2": ".alrc",
    "eslrc": ".eslrc",
    "yrc": ".yrc",
    "qrc": ".qrc",
    "ttml": ".ttml",
    "plain": ".txt",
}

KIND_BY_SUFFIX = {value: key for key, value in SUFFIX.items()}

# Which file to prefer when one song has several: the most detailed format,
# never the most recent one (an ``output --lrc`` would otherwise hide the YRC).
RANK = {"yrc": 6, "qrc": 5, "ttml": 4, "eslrc": 3, "a2": 2, "lrc": 1, "plain": 0}

# ":" is legal on POSIX and is how an ID is written everywhere else in the
# tool, so it stays in the filename; Windows has to lose it.
_BAD_NAME_CHARS = '<>":/\\|?*' if os.name == "nt" else '<>/\\|?*'

# Formats that carry a real word timeline.  Only these make a lyric "逐字";
# a ``.qrc`` whose every line has one ``[0,0]`` block is not one of them.
VERBATIM_KINDS = frozenset({"yrc", "qrc"})


def _field(row: Any, key: str, default: Any = "") -> Any:
    """Read a column that may not exist (old database, plain dict row)."""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def stem_for(artist: str, title: str, ident: str) -> str:
    """``歌手 - 标题 [ne:123]``, safe on every filesystem.

    The ID stays in the name: two uploads of the same song (a single and an
    album cut) would otherwise land on the same file.
    """
    head = " - ".join(part for part in (artist.strip(), title.strip()) if part)
    stem = f"{head} [{ident}]" if head else f"[{ident}]"
    cleaned = "".join(
        "_" if char in _BAD_NAME_CHARS or ord(char) < 0x20 else char for char in stem
    )
    return " ".join(cleaned.split())


def file_name(stem: str, kind: str) -> str:
    return f"{stem}{SUFFIX.get(kind or 'plain', '.txt')}"


def save(
    root: Path, artist: str, title: str, ident: str, texts: dict[str, str]
) -> dict[str, str]:
    """Write every served format under its canonical name; return kind -> name.

    The name depends only on artist / title / ID, so re-planning the same song
    overwrites in place instead of accumulating copies.  Files with a format the
    source did not serve this time are dropped: after a song switches from
    ``.lrc`` to ``.yrc``, keeping both would make :func:`locate` ambiguous and
    leave a stale lyric behind.
    """
    stem = stem_for(artist, title, ident)
    root.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for kind, text in texts.items():
        if not text:
            continue
        name = file_name(stem, kind)
        write_text_atomic(root / name, text)
        written[kind] = name
    if written:
        _drop_stale(root, stem, set(written.values()))
    return written


def drop_stem(root: Path, stem: str, keep: set[str] | None = None) -> None:
    """Delete every stored format of one song, except the names in ``keep``.

    Used when a track switches to another source ID: the old ``[qq:…]`` files
    are another song as far as the store is concerned, so nothing else would
    ever overwrite them.  The ID is part of the name, which is what makes this
    exact — no guessing from artist/title, no chance of hitting a neighbour.
    """
    if not stem:
        return
    _drop_stale(Path(root), stem, set(keep or ()))


def _drop_stale(root: Path, stem: str, keep: set[str]) -> None:
    prefix = f"{stem}."
    try:
        siblings = list(root.iterdir())
    except OSError:
        return
    for sibling in siblings:
        if sibling.name in keep or not sibling.name.startswith(prefix):
            continue
        if sibling.is_file():
            try:
                sibling.unlink()
            except OSError:
                pass


def load(root: Path, name: str) -> str:
    """The text of ``name`` under ``root``; empty when it is gone."""
    if not name:
        return ""
    path = Path(root) / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def kind_of(name: str) -> str:
    """The format a stored file holds, from its extension."""
    suffix = Path(name).suffix.lower()
    return KIND_BY_SUFFIX.get(suffix, "")


def locate(root: Path, ident: str) -> tuple[str, str]:
    """``(filename, kind)`` of the file stored for ``SOURCE:ID``, or ``("", "")``.

    Searches the directory instead of the database on purpose: a file the user
    dropped there by hand counts too, and the name always ends with the ID.
    When a song has several stored formats the most detailed one wins, so
    ``output --pick ne:1`` re-exports the YRC rather than flattening it again.
    """
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return "", ""
    tail = f"[{ident}]"
    found: list[tuple[int, float, str]] = []
    for path in entries:
        if not path.is_file() or not path.stem.endswith(tail):
            continue
        kind = kind_of(path.name)
        if not kind:
            continue
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        found.append((RANK.get(kind, 0), stamp, path.name))
    if not found:
        return "", ""
    kind_rank, _stamp, name = max(found)
    return name, kind_of(name)


def text_of(row: Any, root: Path, kind: str = "") -> str:
    """The text stored for one plan row, in ``kind`` (native when empty).

    Plans written by 2.16 and earlier kept the provider text in ``native_lyric``
    and the requested one in ``lyric``; both are migrated to files on start-up,
    and these column reads keep a database that has not been migrated yet
    readable.
    """
    stem = stem_of(row)
    if stem:
        name = file_name(stem, kind) if kind else str(_field(row, "native_path", ""))
        if name:
            text = load(root, name)
            if text:
                return text
    if kind:
        stored_kind = str(_field(row, "lyric_kind", "") or "")
        if kind == stored_kind:
            return str(_field(row, "lyric", "") or "")
        return ""
    return str(_field(row, "native_lyric", "") or "")


def stem_of(row: Any) -> str:
    """The file stem for a row: from its stored file, else from its metadata."""
    name = str(_field(row, "native_path", "") or "")
    if name:
        return Path(name).stem
    source = str(_field(row, "source", "") or "")
    source_id = str(_field(row, "source_id", "") or "")
    if not source or not source_id:
        return ""
    ident = f"{source_label(source)}:{source_id}"
    return stem_for(
        str(_field(row, "match_artist", "") or ""),
        str(_field(row, "match_title", "") or ""),
        ident,
    )


def verbatim(kinds: Any) -> bool:
    """True when a served set contains a real word timeline (YRC / QRC)."""
    if isinstance(kinds, str):
        kinds = [part for part in kinds.replace("/", ",").split(",") if part]
    return any(str(item).lower() in VERBATIM_KINDS for item in kinds)
