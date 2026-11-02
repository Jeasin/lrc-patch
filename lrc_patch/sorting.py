"""Finder-like name ordering for folders, albums and track files.

macOS Finder does not sort by code point: in a Chinese system it groups
punctuation, digits, Han characters (in pinyin order), Latin letters and
finally kana.  Plain ``sorted()`` produced the terminal order the user saw,
where every CJK name fell after every Latin one and the Han block was in
code-point order (久石譲 before 五月天 before 周杰伦 before 宇多田ヒカル), which
looks random when you know the folder.

The standard library has no pinyin table, but the C library does:
``locale.strxfrm`` under ``zh_CN`` returns pinyin collation keys for Han
characters, so 宮脇 < 接吻 < 結束 < 久石 < 林 < 鈴木 < 米津 < 逃跑 < 五月天 <
许嵩 < 宇多田 < 张杰 < 周杰伦 exactly like Finder.  We combine that key with an
explicit script rank, so the group order matches Finder even when the
user's locale is not Chinese, and fall back to code points when the
zh_CN locale is unavailable.
"""
from __future__ import annotations

import functools
import locale
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

_ZH_LOCALES = ("zh_CN.UTF-8", "zh_CN.utf8", "zh_CN")
_COLLATION = "code-point"


def _init_collation() -> None:
    global _COLLATION
    for name in _ZH_LOCALES:
        try:
            locale.setlocale(locale.LC_COLLATE, name)
        except locale.Error:
            continue
        _COLLATION = name
        return


_init_collation()


def collation_name() -> str:
    """Which collation was actually installed (exposed for --debug/help)."""
    return _COLLATION


@functools.lru_cache(maxsize=8192)
def _collation_key(text: str) -> bytes:
    if _COLLATION == "code-point":
        return text.encode("utf-8", "surrogatepass")
    try:
        return locale.strxfrm(text)
    except (locale.Error, ValueError):
        return text.encode("utf-8", "surrogatepass")


def _is_han(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
    )


def _is_kana(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0xFF66 <= code <= 0xFF9F
    )


def _is_hangul(char: str) -> bool:
    code = ord(char)
    return 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF


def rank(name: str) -> int:
    """Script group, in the order Finder shows them on a Chinese system.

    0 punctuation/symbol, 1 digits, 2 Han, 3 Latin (and other alphabets),
    4 kana/Hangul, 9 empty/other.  ``『ユイカ』`` therefore leads the list and
    ``ヨルシカ`` closes it, exactly like the folders in Finder.
    """
    for char in name:
        if char.isspace():
            continue
        if char.isdigit():
            return 1
        if _is_han(char):
            return 2
        if _is_kana(char) or _is_hangul(char):
            return 4
        if char.isalpha():
            return 3
        category = unicodedata.category(char)
        if category[0] in "PS":
            return 0
        return 9
    return 9


_NUM_RE = re.compile(r"(\d+)")


def _natural(name: str) -> tuple:
    """Digits compare numerically, text compares by the collation table."""
    parts: list[tuple[int, int, bytes]] = []
    for chunk in _NUM_RE.split(name):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk), b""))
        else:
            parts.append((1, 0, _collation_key(chunk)))
    return tuple(parts)


@functools.lru_cache(maxsize=8192)
def name_key(name: str) -> tuple:
    """Sort key matching Finder's name order (numeric-aware, pinyin-aware)."""
    return (rank(name), _natural(name))


def sort_names(names: Iterable[str]) -> list[str]:
    return sorted(names, key=name_key)


def relative_parts(path: Path, root: Path | None = None) -> tuple[str, ...]:
    if root is not None:
        try:
            return tuple(path.resolve().relative_to(root.resolve()).parts)
        except (ValueError, OSError):
            pass
    return tuple(path.parts)


def path_key(path: Path, root: Path | None = None) -> tuple:
    """Compare every path component with the Finder rule, not the whole string."""
    return tuple(name_key(part) for part in relative_parts(path, root))


def sort_paths(paths: Iterable[Path], root: Path | None = None) -> list[Path]:
    return sorted(paths, key=lambda item: path_key(item, root))


def sort_tracks(tracks: Sequence, root: Path | None = None) -> list:
    """Sort tracks by folder, then by file name (both Finder-style)."""
    return sorted(
        tracks,
        key=lambda track: (path_key(track.path.parent, root), name_key(track.path.name)),
    )
