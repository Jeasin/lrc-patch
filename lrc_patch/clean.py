"""``lrcpatch clean``: reclaim space in the state database and the caches.

Every artefact this tool creates lives in one of two places:

* ``$XDG_STATE_HOME/lrc-patch`` — ``lyrics.sqlite3`` (plans, journal, HTTP
  cache), ``reports/`` (plan CSVs and HTML review pages) and ``picks/``
  (generated pick templates);
* ``$XDG_CACHE_HOME/lrc-patch/lyr`` — the ``lrcpatch output`` export mirror
  (the pre-2.15 name ``lrc`` is still cleaned up if it is lying around).

Like ``brew cleanup`` this is a *safe* cleanup: by default it only drops what
is clearly stale (expired HTTP cache, reports/templates/exports older than
``--days``).  Plans and the rollback journal are the resume state, so they are
only touched when named explicitly (``--plans``/``--journal``) or with
``--all``.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from .config import default_lyr_dir, legacy_lyr_dir
from .errors import ToolError
from .render import format_size, pad_display, print_header, print_meta, rule, short_path, style
from .state import State

TARGETS: dict[str, str] = {
    "http": "HTTP 缓存",
    "reports": "报告",
    "lyr": "导出歌词",
    "picks": "pick 模板",
    "plans": "计划状态",
    "journal": "回滚记录",
}

# What a bare ``lrcpatch clean`` may delete: regenerable, never the resume
# state.  Reports and templates are the user's own review material, so they
# only go once they are older than --days.
DEFAULT_TARGETS = ("http", "reports", "lyr", "picks")

_UNITS = {"http": "条", "plans": "条", "journal": "条"}


def _selected(args: argparse.Namespace) -> list[str]:
    if getattr(args, "all", False):
        return list(TARGETS)
    chosen = [name for name in TARGETS if getattr(args, name, False)]
    return chosen or list(DEFAULT_TARGETS)


def _cutoff(days: int) -> float:
    return (dt.datetime.now() - dt.timedelta(days=days)).timestamp()


def _iso_cutoff(days: int) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return moment.isoformat(timespec="seconds")


def _roots(value: Path | list[Path]) -> list[Path]:
    """One target may live in two places (the current and the legacy dir)."""
    return list(value) if isinstance(value, list) else [value]


def _clean_files(
    root: Path, cutoff: float | None, dry_run: bool
) -> tuple[int, int]:
    """Delete files under ``root`` older than ``cutoff`` (all when None)."""
    if not root.is_dir():
        return 0, 0
    count = size = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if cutoff is not None and stat.st_mtime >= cutoff:
            continue
        count += 1
        size += stat.st_size
        if not dry_run:
            path.unlink(missing_ok=True)
    if not dry_run:
        # Remove the directories the deletion just emptied, so the mirror of a
        # deleted album does not linger as an empty tree.
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                try:
                    next(path.iterdir())
                except StopIteration:
                    path.rmdir()
                except OSError:
                    pass
    return count, size


def _left_http(state: State, expired_only: bool) -> int:
    return state.count_http_cache(expired_only=expired_only)


def cmd_clean(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    dry_run = bool(getattr(args, "dry_run", False))
    days = int(getattr(args, "days", 30))
    if days < 1:
        raise ToolError("--days 必须大于 0")
    # --all means "everything, whatever its age"; --days stays the default for
    # every other mode.  Mixing the two made `clean --all` leave today's
    # reports and plans behind, which is exactly what it promised to remove.
    everything = bool(getattr(args, "all", False))
    wanted = _selected(args)
    cutoff = None if everything else _cutoff(days)
    before_size = state.size()

    lyr_dir = (getattr(args, "out", None) or default_lyr_dir()).expanduser()
    paths = {
        "reports": state_dir / "reports",
        "lyr": lyr_dir,
        "picks": state_dir / "picks",
    }
    # 2.14 and earlier exported into ``lrc-patch/lrc``; the folder may still
    # hold files nobody will ever look at again.
    legacy = legacy_lyr_dir()
    if legacy != lyr_dir:
        paths["lyr"] = [lyr_dir, legacy]

    print_header(
        "clean",
        f"{short_path(state_dir)}  ·  {format_size(before_size)}"
        + ("  ·  预览" if dry_run else "")
        + ("  ·  全部（不限天数）" if everything else ""),
    )

    freed = 0
    freed_rows = 0
    db_rows = 0
    skipped = 0
    for name in wanted:
        label = TARGETS[name]
        unit = _UNITS.get(name, "个")
        if name == "http":
            expired_only = not everything
            count, size = state.purge_http_cache(
                expired_only=expired_only, dry_run=dry_run
            )
            tail = ""
            if count and not dry_run:
                left = _left_http(state, expired_only)
                tail = style(f"   保留 {left} 条", "90")
        elif name in paths:
            count = size = 0
            for root in _roots(paths[name]):
                got, freed_bytes = _clean_files(root, cutoff, dry_run)
                count += got
                size += freed_bytes
            tail = ""
            if count:
                tail = style(
                    "   全部" if everything else f"   {days} 天前", "90"
                )
            elif not everything:
                skipped += 1
        else:  # plans / journal
            before = None if everything else _iso_cutoff(days)
            purge = state.purge_plans if name == "plans" else state.purge_journal
            count, size = purge(before, dry_run=dry_run)
            tail = (
                style("   全部" if everything else f"   {days} 天前", "90")
                if count
                else ""
            )
            if not count and not everything:
                skipped += 1

        if name in {"http", "plans", "journal"}:
            db_rows += count
        freed += size
        freed_rows += count
        verb = "可释放" if dry_run else "释放"
        if count:
            body = f"{verb} {count} {unit} · {format_size(size)}{tail}"
        elif everything:
            body = style("目录为空", "90")
        else:
            body = style(f"无需清理（没有 {days} 天前的）", "90")
        print("  " + style(pad_display(label, 10), "90") + "  " + body)

    print("  " + rule())
    if dry_run:
        summary = f"{freed_rows} 项 · {format_size(freed)}" if freed_rows else "无需清理"
        print(
            "  "
            + style("预览", "33")
            + "  未改动任何内容；去掉 --dry-run 才会删除。"
            + style(f"   {summary}", "90")
        )
        print()
        return 0

    if freed_rows:
        extra = ""
        if db_rows:
            state.vacuum()
            extra = style(
                f"   数据库 {format_size(before_size)} → {format_size(state.size())}",
                "90",
            )
        print(
            "  "
            + style("已清理", "32")
            + f"  {freed_rows} 项  ·  {format_size(freed)}"
            + extra
        )
    if not freed_rows and skipped and not everything:
        print(
            "  "
            + style("没有需要清理的内容。", "32")
            + style(f"   忽略的 {skipped} 类都未满 {days} 天（--all 不限天数）", "90")
        )
    elif not freed_rows:
        print("  " + style("没有需要清理的内容。", "32"))
    if (freed_rows or dry_run) and ("plans" in wanted or "journal" in wanted):
        print_meta(
            "注意",
            style(
                "计划状态删除后需要重新查询；回滚记录删除后无法再 rollback 这些曲目。",
                "33",
            ),
        )
    print()
    return 0
