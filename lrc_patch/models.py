"""Plain data objects passed between planning, state and rendering."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .text import lyric_hash, sha256_text


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

@dataclass
class TreeEntry:
    path: Path
    status: str
    note: str = ""
