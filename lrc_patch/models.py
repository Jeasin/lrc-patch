"""Plain data objects passed between planning, state and rendering."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .text import lyric_hash, lyric_kind, sha256_text

# What each provider is known to serve; a candidate that has not been fetched
# yet can still say which native format it would arrive in.
_NATIVE_FORMATS = {"qq": "qrc", "netease": "yrc", "lrclib": "lrc", "local": "lrc"}


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
    # What the provider actually serves, when that is known before fetching:
    # QQ is always QRC, netease usually YRC plus a line-timed LRC, LRCLIB is
    # LRC or plain text.  Shown in the candidate table so the format is a
    # choice made at apply time, not at fetch time.
    native_kind: str = ""

    def __post_init__(self) -> None:
        if self.native_kind:
            return
        if self.source in _NATIVE_FORMATS:
            self.native_kind = _NATIVE_FORMATS[self.source]
        elif self.lyrics:
            self.native_kind = lyric_kind(self.lyrics)

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists)

@dataclass
class FetchResult:
    lyrics: str = ""
    instrumental: bool = False
    note: str = ""
    # The untouched provider text and its format (qrc / yrc / lrc / plain).
    # ``lyrics`` above is the *requested* target, converted from this.
    native_lyrics: str = ""
    native_kind: str = ""

@dataclass
class PlanResult:
    track: Track
    status: str
    reason: str = ""
    candidate: Candidate | None = None
    lyrics: str = ""
    score: int = 0
    duration_diff: float | None = None
    native_lyrics: str = ""
    native_kind: str = ""

@dataclass
class TreeEntry:
    path: Path
    status: str
    note: str = ""
    # Optional fixed columns rendered before ``note``: source / score / diff /
    # lyric / artist for plan rows.  ``print_tree`` aligns each column per
    # album group, so ``qq`` and ``netease`` share one score column.
    cells: tuple[str, ...] = ()
