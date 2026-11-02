"""Paths, endpoints and tunables shared by the package."""
from __future__ import annotations

import os
import re
from pathlib import Path


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "lrc-patch"

def default_lrc_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "lrc-patch" / "lrc"

APP_VERSION = "2.1"

AUDIO_EXTS = {".m4a": "m4a", ".mp3": "mp3", ".dsf": "dsf"}

LYRICS_LANG = "jpn"

DEFAULT_LIBRARY = Path.home() / "Music" / "JMusic"

# Providers sit behind WAFs that reject obvious tool user-agents; a normal
# browser UA is the baseline, and curl_cffi adds a matching TLS fingerprint.
HTTP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SOURCE_ENDPOINTS = {
    "netease": (
        "https://music.163.com/api/search/get",
        "https://music.163.com/api/song/lyric",
    ),
    "lrclib": ("https://lrclib.net/api/search",),
}

TIMESTAMP_RE = re.compile(r"^\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", re.MULTILINE)

LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")

PLACEHOLDERS = {"纯音乐", "純音樂", "instrumental", "暂无歌词", "無歌詞"}

SOURCE_NAMES = {"netease", "lrclib"}

DEFAULT_DURATION_TOLERANCE = 2.0

MIN_LRC_COVERAGE = 0.55

MAX_LRC_OVERRUN = 3.0
