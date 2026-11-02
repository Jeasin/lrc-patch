"""Resolve user-supplied files and folders inside the music library."""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Iterable

from .config import AUDIO_EXTS
from .errors import ToolError


def canonical_path(path: Path) -> Path:
    """Use one Unicode form for database keys; macOS may return NFD names."""
    return Path(unicodedata.normalize("NFC", str(path)))

def iter_audio_files(root: Path) -> Iterable[Path]:
    root = canonical_path(root.resolve())
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in AUDIO_EXTS:
            yield canonical_path(path)

def album_matches(path: Path, library: Path, album: str | None) -> bool:
    if not album:
        return True
    wanted = canonical_path(Path(album).expanduser())
    if not wanted.is_absolute():
        wanted = library / wanted
    return canonical_path(path.parent.resolve()) == canonical_path(wanted.resolve())

def resolve_selection(
    library: Path, paths: list[str] | None, strict: bool = True
) -> list[Path]:
    """Resolve --path/--dir values; relative paths are anchored at the library."""
    selected: list[Path] = []
    library_key = canonical_path(library.resolve())
    for raw in paths or []:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = library / candidate
        candidate = canonical_path(candidate.resolve())
        if strict and not candidate.exists():
            raise ToolError(f"路径不存在：{candidate}")
        if candidate != library_key and library_key not in candidate.parents:
            raise ToolError(
                f"路径不在音乐库内：{candidate}\n"
                f"当前音乐库：{library_key}\n"
                "用 --library 指向它的根目录，或只传库内路径。"
            )
        selected.append(candidate)
    return selected

def selection_matches(
    path: Path, library: Path, album: str | None, selected: list[Path]
) -> bool:
    if not album_matches(path, library, album):
        return False
    if not selected:
        return True
    resolved = canonical_path(path.resolve())
    return any(resolved == item or item in resolved.parents for item in selected)

def iter_selected_files(library: Path, selected: list[Path]) -> Iterable[Path]:
    if not selected:
        yield from iter_audio_files(library)
        return
    seen: set[Path] = set()
    for item in selected:
        if item.is_file():
            if item.suffix.lower() in AUDIO_EXTS and item not in seen:
                seen.add(item)
                yield canonical_path(item)
            continue
        for path in iter_audio_files(item):
            if path not in seen:
                seen.add(path)
                yield path
