"""Paths, endpoints and tunables shared by the package."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolError


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "lrc-patch"

def default_lyr_dir() -> Path:
    """Where ``output`` drops exported lyric files.

    Named ``lyr`` rather than ``lrc``: the directory holds ``.alrc`` / ``.yrc``
    / ``.qrc`` / ``.ttml`` as often as plain ``.lrc``, and the old name read
    like a promise about the format.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "lrc-patch" / "lyr"


def legacy_lyr_dir() -> Path:
    """The pre-2.15 export mirror (``lrc-patch/lrc``), kept only for cleanup."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "lrc-patch" / "lrc"

APP_VERSION = "2.17"

# Bump when the scoring rules change: old ambiguous/suspect plans are then
# retried once instead of being reused with the previous rules.
SCORING_VERSION = "6"

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
    "qq": ("https://u.y.qq.com/cgi-bin/musicu.fcg",),
}

TIMESTAMP_RE = re.compile(r"^\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", re.MULTILINE)

LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")

PLACEHOLDERS = {"纯音乐", "純音樂", "instrumental", "暂无歌词", "無歌詞"}

SOURCE_NAMES = {"netease", "lrclib", "qq"}

# Display names.  The long name stays canonical in the state database, in
# reports and on the wire; only the table output is shortened, so `ne` / `qq`
# are the same width and the score column gets one straight edge.  Every place
# a source is *typed* accepts the short form as well.
SOURCE_LABELS = {"netease": "ne", "lrclib": "lrclib", "qq": "qq"}

SOURCE_ALIASES = {
    "ne": "netease",
    "netease": "netease",
    "lrclib": "lrclib",
    "qq": "qq",
}


def source_label(name: str) -> str:
    """Short form for tables; unknown names pass through unchanged."""
    return SOURCE_LABELS.get(name, name)


def canonical_source(name: str) -> str:
    """Map a typed source (``ne``) to the canonical one (``netease``)."""
    key = (name or "").strip().lower()
    return SOURCE_ALIASES.get(key, key)

# netease is the primary source: it covers the user's Japanese catalogue best
# and its line-timed LRC is the most stable.  qq is an automatic fallback for
# the tracks netease cannot match (its Chinese catalogue and verbatim QRC are
# strong); lrclib is opt-in via ``--src lrclib`` only, as it has been answering
# 503/429 continuously.
DEFAULT_SOURCES = ("netease", "qq")

DEFAULT_DURATION_TOLERANCE = 2.0

MIN_LRC_COVERAGE = 0.55

MAX_LRC_OVERRUN = 3.0


# -- user configuration --------------------------------------------------
# A tiny TOML subset: plain ``key = value`` lines with strings, arrays and
# integers.  Python 3.9 is still supported, so ``tomllib`` is not available;
# the surface here is deliberately small enough not to need it.

CONFIG_ENV = "LRCPATCH_CONFIG"

CONFIG_KEYS = ("library", "src", "workers")

CONFIG_TEMPLATE = """\
# lrcpatch 配置。命令行参数优先于这里的值。
# 查看当前生效值：lrcpatch config

# 音乐库根目录；相对路径按家目录展开。也可以每次用 --library DIR 临时覆盖。
library = "~/Music/JMusic"

# 歌词源顺序：先问 ne（网易云），缺的再问 qq；lrclib 需要显式写进来。
src = ["ne", "qq"]

# 并发查询 / 写入数（1..10）。
workers = 4
"""


@dataclass(frozen=True)
class Config:
    path: Path
    exists: bool = False
    library: Path | None = None
    src: tuple[str, ...] = ()
    workers: int | None = None


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "lrc-patch" / "config.toml"


def _strip_comment(text: str) -> str:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return text[:index].rstrip()
    return text.rstrip()


def _unquote(raw: str, where: str) -> str:
    if not raw or raw[0] not in "\"'":
        raise ToolError(f"{where}：字符串需要加引号")
    if len(raw) < 2 or raw[-1] != raw[0]:
        raise ToolError(f"{where}：字符串的引号没有闭合")
    return raw[1:-1]


def _split_array(body: str, where: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote = ""
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == ",":
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote:
        raise ToolError(f"{where}：数组里的字符串没有闭合")
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return [item for item in items if item]


def _parse_value(raw: str, where: str) -> object:
    raw = raw.strip()
    if not raw:
        raise ToolError(f"{where}：缺少值")
    if raw[0] in "\"'":
        return _unquote(raw, where)
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise ToolError(f"{where}：数组缺少结尾的 ]")
        return [
            _unquote(item, where) for item in _split_array(raw[1:-1], where)
        ]
    if raw in {"true", "false"}:
        return raw == "true"
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    raise ToolError(f"{where}：无法识别的值 {raw}（字符串需要加引号）")


def load_config(path: Path | None = None) -> Config:
    """Read the config file; a missing file simply means "all defaults"."""
    target = path or config_path()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return Config(target)
    except OSError as exc:
        raise ToolError(f"读取配置失败：{target}（{exc}）") from None

    values: dict[str, object] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        where = f"{target}:{lineno}"
        key, sep, value = line.partition("=")
        if not sep:
            raise ToolError(f"{where}：应为「键 = 值」")
        key = key.strip()
        if key not in CONFIG_KEYS:
            raise ToolError(
                f"{where}：未知配置项 {key}；可用：{' / '.join(CONFIG_KEYS)}"
            )
        values[key] = _parse_value(value, where)

    library: Path | None = None
    if "library" in values:
        raw_library = values["library"]
        if not isinstance(raw_library, str) or not raw_library.strip():
            raise ToolError(f"{target}：library 必须是非空字符串")
        library = Path(raw_library).expanduser()
        if not library.is_absolute():
            library = Path.home() / library

    src: tuple[str, ...] = ()
    if "src" in values:
        raw_src = values["src"]
        if isinstance(raw_src, str):
            items = [part.strip() for part in raw_src.split(",") if part.strip()]
        elif isinstance(raw_src, list):
            items = [str(part).strip() for part in raw_src if str(part).strip()]
        else:
            raise ToolError(f"{target}：src 应是字符串数组，例如 [\"ne\", \"qq\"]")
        items = [canonical_source(item) for item in items]
        unknown = [item for item in items if item not in SOURCE_NAMES]
        if unknown:
            raise ToolError(
                f"{target}：未知歌词源 {', '.join(unknown)}；"
                f"可用：ne（netease） / qq / lrclib"
            )
        if not items:
            raise ToolError(f"{target}：src 不能为空")
        src = tuple(items)

    workers: int | None = None
    if "workers" in values:
        raw_workers = values["workers"]
        if isinstance(raw_workers, bool) or not isinstance(raw_workers, int):
            raise ToolError(f"{target}：workers 必须是整数")
        if not 1 <= raw_workers <= 10:
            raise ToolError(f"{target}：workers 必须在 1..10 之间")
        workers = raw_workers

    return Config(target, True, library, src, workers)


def write_config_template(path: Path) -> bool:
    """Create the commented template; never overwrite an existing file."""
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"无法写入配置文件：{path}（{exc}）") from None
    return True
