#!/usr/bin/env -S uv run --script --no-project --quiet
# /// script
# requires-python = ">=3.9"
# dependencies = ["mutagen>=1.47"]
# ///
"""Batch lyrics planner/embedder for a local music library.

Entry point only. The implementation lives in the ``lrc_patch`` package next to
this file; ``lyrics.py -h`` documents usage, and a bare ``lyrics.py`` prints a
short intro.

Dependencies
  * Python 3.9+ and mutagen; mutagen is declared inline (PEP 723), so the
    shebang above lets uv install it into its cache on first run.
  * Everything else is the standard library, including sqlite3; the sqlite3
    command-line tool is NOT required.
  * ``python3 lyrics.py`` still works: it re-execs into an interpreter that has
    mutagen (uv tool env, or $LYRICS_MUTAGEN_PYTHON).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path


# The user's mutagen is installed in an isolated ``uv tool`` environment.
# Re-exec into that interpreter so `./lyrics.py` also works from stock python3.
def _ensure_mutagen() -> None:
    if importlib.util.find_spec("mutagen") is not None:
        return

    candidates: list[Path] = []
    env_python = os.environ.get("LYRICS_MUTAGEN_PYTHON")
    if env_python:
        candidates.append(Path(env_python))
    mid3v2 = shutil.which("mid3v2")
    if mid3v2:
        try:
            first_line = Path(mid3v2).read_text(encoding="utf-8").splitlines()[0]
            if first_line.startswith("#!"):
                candidates.append(Path(first_line[2:].strip()))
        except (OSError, IndexError):
            pass
    candidates.extend(
        Path(p).expanduser()
        for p in (
            "~/.local/share/uv/tools/mutagen/bin/python",
            "~/.local/share/uv/tools/mutagen/bin/python3",
        )
    )
    me = str(Path(__file__).resolve())
    for candidate in candidates:
        if candidate.is_file():
            os.execv(str(candidate), [str(candidate), me, *sys.argv[1:]])
    sys.exit(
        "未找到 mutagen。推荐直接运行 lyrics.py（shebang 会让 uv 自动安装依赖）；"
        "也可以先运行 uv run lyrics.py，或 uv tool install mutagen。"
    )


_ensure_mutagen()

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lrc_patch.cli import run  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run())
