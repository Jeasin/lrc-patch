"""The user-facing commands: scan, plan, candidates, apply, verify, rollback.

``clean`` lives in :mod:`lrc_patch.clean`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audio import (
    apply_one,
    audio_hash,
    clear_lyrics_only,
    cover_hash,
    load_tracks,
    non_lyrics_tags_hash,
    preflight_apply,
    read_track,
    stage_copy,
    write_lyrics_only,
)
from .config import (
    DEFAULT_DURATION_TOLERANCE,
    DEFAULT_LIBRARY,
    DEFAULT_SOURCES,
    canonical_source,
    default_lyr_dir,
    load_config,
    source_label,
    write_config_template,
)
from .errors import ToolError, VerificationError
from .lyric_fmt import render_native, strip_provider_tags
from .matching import (
    assign_picks,
    keep_previous_plan,
    parse_pick,
    plan_one,
    read_pick_file,
    score_candidate,
)
from .models import Candidate, PlanResult, Track, TreeEntry
from .paths import resolve_selection, selection_matches
from .render import (
    LYRIC_COLORS,
    ProgressLine,
    default_report_path,
    disp_width,
    format_diff,
    format_duration,
    format_size,
    pad_display,
    plan_fields,
    plan_html_path,
    print_header,
    print_meta,
    print_meta_table,
    print_scan_list,
    print_summary,
    print_tree,
    rule,
    screen_width,
    selection_label,
    short_path,
    status_style,
    style,
    truncate_display,
    write_plan_html,
    write_plan_report,
)
from .sorting import name_key, path_key, sort_paths
from .sources import JsonClient, build_sources
from . import store
from .state import State, read_plan_cache, read_state_summary, row_get
from .text import (
    kind_set,
    kind_set_label,
    lyric_hash,
    lyric_kind,
    lyric_tag,
    lyrics_line_count,
    match_notes,
    sanitize_lyrics,
    write_text_atomic,
)


def summarize_plans(rows: list[sqlite3.Row]) -> Counter:
    return Counter(row["status"] for row in rows)


KIND_LABELS = {"search": "搜索", "lyric": "取词", "request": "请求"}


def network_line(client) -> str:
    """One line: per-source request counts split into search and lyric fetch."""
    kinds = client.breakdown()
    network = [item for item in client.health() if item[1]]
    if not network:
        return ""
    bits: list[str] = []
    for name, requests, failed_requests, cooldown in network:
        text = f"{source_label(name)} {requests} 次"
        detail = kinds.get(name, {})
        if detail:
            order = [key for key in ("search", "lyric") if key in detail]
            order += [key for key in detail if key not in order]
            text += (
                "（"
                + " · ".join(
                    f"{KIND_LABELS.get(kind, kind)} {detail[kind]}" for kind in order
                )
                + "）"
            )
        if failed_requests:
            text += f" 失败 {failed_requests}"
        if cooldown:
            text += f" 冷却 {cooldown}s"
        bits.append(text)
    hits = client.cache_hits()
    if hits:
        bits.append(f"缓存命中 {hits}")
    transport = getattr(getattr(client, "transport", None), "name", "")
    if transport:
        bits.append(f"传输 {transport}")
    return "  ·  ".join(bits)

def scan_scope(args: argparse.Namespace, library: Path) -> str:
    """Readable description of what was scanned: relative paths, not $PWD joins."""
    paths = getattr(args, "path", None) or []
    if not paths:
        return short_path(library)
    parts: list[str] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = library / candidate
        parts.append(short_path(candidate, library))
    return " · ".join(parts)


def cmd_scan(args: argparse.Namespace, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    formats = Counter(track.fmt for track in tracks)
    existing = Counter(track.fmt for track in tracks if track.has_lyrics)

    # Only plans that still describe this exact file count; a changed file
    # means the cached verdict is stale even if the row is still there.
    cached: dict[str, tuple[str, int, str, str]] = {}
    ready_cached = 0
    review_cached = 0
    overwrite_cached = 0
    plan_rows = read_plan_cache(state_dir / "lyrics.sqlite3")
    for track in tracks:
        row = plan_rows.get(str(track.path))
        if not row or row["fingerprint"] != track.fingerprint():
            continue
        cached[str(track.path)] = (
            source_label(str(row["source"] or "")),
            int(row["score"] or 0),
            str(row["status"]),
            str(row_get(row, "native_kinds", "") or row_get(row, "native_kind", "") or ""),
        )
        if row["status"] == "ready":
            if track.has_lyrics:
                # `plan --force` on a file that already has lyrics: the row is
                # real, it just will not be written without --force.
                overwrite_cached += 1
            else:
                ready_cached += 1
        if row["status"] in {"ambiguous", "suspect"} and not track.has_lyrics:
            review_cached += 1

    if getattr(args, "plans", False):
        # "Which 14 tracks does apply want to write?" is a question the summary
        # line raised but could not answer.
        tracks = [track for track in tracks if str(track.path) in cached]
        formats = Counter(track.fmt for track in tracks)
        existing = Counter(track.fmt for track in tracks if track.has_lyrics)
        ready_cached = sum(
            1
            for track in tracks
            if cached[str(track.path)][2] == "ready" and not track.has_lyrics
        )
        overwrite_cached = sum(
            1
            for track in tracks
            if cached[str(track.path)][2] == "ready" and track.has_lyrics
        )
        review_cached = sum(
            1
            for track in tracks
            if cached[str(track.path)][2] in {"ambiguous", "suspect"}
            and not track.has_lyrics
        )

    print_header(
        "scan",
        f"{truncate_display(scan_scope(args, library), max(20, screen_width() - 30))}"
        f"  ·  {len(tracks)} 首"
        + ("  ·  只看有计划" if getattr(args, "plans", False) else ""),
    )
    print("  " + style("▸ ", "36") + style("格式", "1;36"))
    items = sorted(formats)
    for index, fmt in enumerate(items):
        branch = "     └─ " if index == len(items) - 1 else "     ├─ "
        total = formats[fmt]
        have = existing.get(fmt, 0)
        line = (
            style(branch, "90")
            + style(fmt.upper().ljust(4), "1")
            + f"{total:>4} 首"
            + style(f"   已有歌词 {have:>4}   待补 {total - have:>4}", "90")
        )
        print(line)
    print()

    if not getattr(args, "summary", False):
        print_scan_list(
            tracks, library, getattr(args, "tree_limit", 0) or 0, cached
        )

    have_total = sum(existing.values())
    print_meta(
        "曲目",
        f"{len(tracks)} 首"
        + style(
            f"   已有歌词 {have_total}   待补 {len(tracks) - have_total}",
            "90",
        ),
    )
    if ready_cached or review_cached or overwrite_cached:
        parts = []
        if ready_cached:
            parts.append(f"{ready_cached} 首已匹配" + style("，可直接 apply", "90"))
        if overwrite_cached:
            parts.append(
                f"{overwrite_cached} 首已有歌词但有新结论"
                + style("（写入要 apply --force）", "90")
            )
        if review_cached:
            parts.append(f"{review_cached} 首需确认" + style("（带 ?）", "90"))
        if not getattr(args, "plans", False):
            parts.append(style("lrcpatch scan --plans 只看这些", "90"))
        print_meta("计划", "   ".join(parts))
    if errors:
        print("  " + style("▸ ", "36") + style("标签读取失败", "1;31"))
        for index, (path, error) in enumerate(errors[:20]):
            branch = "     └─ " if index == len(errors[:20]) - 1 else "     ├─ "
            print(
                style(branch, "90")
                + style("✗ ", "1;31")
                + short_path(path, library)
                + style(f"  {error}", "90")
            )
        if len(errors) > 20:
            print(style(f"      … 另有 {len(errors) - 20} 个", "90"))
        print()
    else:
        print_meta("标签", style("全部读取成功", "32"))
    return 1 if errors else 0

def cmd_config(args: argparse.Namespace) -> int:
    """Show the effective defaults and where they come from."""
    config = load_config()
    print_header("config", short_path(config.path))
    if getattr(args, "init", False):
        created = write_config_template(config.path)
        print_meta(
            "模板",
            ("已写入 " if created else "已存在，未覆盖 ")
            + short_path(config.path),
        )
        print()
        if not created:
            return 0
        config = load_config()
    library = config.library or DEFAULT_LIBRARY
    sources = config.src or DEFAULT_SOURCES
    workers = config.workers or 4
    rows = [
        ("音乐库", short_path(library), "配置文件" if config.library else "默认"),
        ("歌词源", ",".join(source_label(name) for name in sources), "配置文件" if config.src else "默认"),
        ("并发", str(workers), "配置文件" if config.workers else "默认"),
    ]
    value_width = max(disp_width(value) for _, value, _ in rows)
    label_width = max(
        disp_width(text) for text in ("文件", "音乐库", "歌词源", "并发", "说明")
    )
    print(
        "  "
        + style(pad_display("文件", label_width), "90")
        + "  "
        + short_path(config.path)
        + style(
            "   已读取" if config.exists else "   不存在，使用内置默认值",
            "90",
        )
    )
    print_meta_table(
        [
            (label, pad_display(value, value_width) + "  " + style(origin, "90"))
            for label, value, origin in rows
        ]
    )
    print()
    print(
        "  "
        + style(pad_display("说明", label_width), "90")
        + "  命令行参数（--library / --src / --workers）优先；"
        + style("lrcpatch config --init", "36")
        + " 生成配置文件。"
    )
    print()
    return 0


STATUS_ORDER = [
    ("applied", "✓ 已写入"),
    ("ready", "✓ 可写入"),
    ("ambiguous", "! 需确认"),
    ("suspect", "! 时间轴可疑"),
    ("no_match", "✗ 无匹配"),
    ("error", "✗ 失败"),
    ("instrumental", "♪ 纯音乐"),
    ("skip_existing", "· 已有歌词"),
    ("rolled_back", "↩ 已回滚"),
]


def _human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} GB"


def _human_age(seconds: float) -> str:
    if seconds <= 0:
        return "刚刚"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟前"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} 小时前"
    return f"{hours / 24:.0f} 天前"


def _local_stamp(value: str) -> str:
    """Stored timestamps are UTC; show them in the user's own time zone."""
    if not value:
        return ""
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return value[:16].replace("T", " ")
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def cmd_status(args: argparse.Namespace, state_dir: Path) -> int:
    """Read-only overview of the state database, caches and reports."""
    db = state_dir / "lyrics.sqlite3"
    config = load_config()
    library = getattr(args, "library", None) or config.library or DEFAULT_LIBRARY
    summary = read_state_summary(db)
    reports = state_dir / "reports"
    report_files = sorted(reports.glob("*")) if reports.is_dir() else []
    report_bytes = sum(item.stat().st_size for item in report_files if item.is_file())

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "state_dir": str(state_dir),
                    "database": str(db),
                    "size": db.stat().st_size if db.exists() else 0,
                    "library": str(library),
                    "reports": {"files": len(report_files), "bytes": report_bytes},
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print_header("status", short_path(state_dir))
    if summary is None:
        if db.exists():
            print(
                "  "
                + style("状态库读不出来：可能正在另一次写入中被锁住，或文件已损坏。", "33")
            )
            print_meta("文件", f"{short_path(db)}   {_human_bytes(db.stat().st_size)}")
        else:
            print("  " + style("状态库还没有建立：跑一次 plan 就有了。", "33"))
        print()
        return 1

    wal = db.with_name(db.name + "-wal")
    db_size = _human_bytes(db.stat().st_size if db.exists() else 0)
    wal_size = _human_bytes(wal.stat().st_size) if wal.exists() else "0 B"
    print_meta("状态库", f"{db_size}" + style(f"   WAL {wal_size}", "90"))
    print_meta(
        "配置",
        short_path(config.path)
        + style("   已读取" if config.exists else "   不存在，使用默认值", "90"),
    )
    print_meta("音乐库", short_path(library))
    print()

    plans = summary["plans"]
    by_status = plans["by_status"]
    print(
        "  "
        + style("▸ 计划", "36")
        + f"   {plans['total']} 条"
        + style(
            f"   最近 {_local_stamp(plans['latest'])}" if plans["latest"] else "", "90"
        )
    )
    chips = [
        style(label, status_style(status)[1]) + f" {by_status[status]}"
        for status, label in STATUS_ORDER
        if by_status.get(status)
    ]
    if chips:
        print("    " + "   ".join(chips))
    if plans["by_source"]:
        print(
            "    "
            + style("来源", "90")
            + "  "
            + "   ".join(
                f"{source_label(name)} {count}"
                for name, count in sorted(
                    plans["by_source"].items(), key=lambda item: -item[1]
                )
            )
        )
    print()

    cache = summary["cache"]
    oldest_age = (
        _human_age(time.time() - cache["oldest"]) if cache["oldest"] else "—"
    )
    print(
        "  "
        + style("▸ 缓存", "36")
        + f"   {cache['rows']} 条   {_human_bytes(cache['bytes'])}"
        + style(
            f"   已过期 {cache['expired']}" if cache["expired"] else "   无过期",
            "33" if cache["expired"] else "90",
        )
        + style(f"   最旧 {oldest_age}", "90")
    )
    journal = summary["journal"]
    print(
        "  "
        + style("▸ 记录", "36")
        + f"   {journal['rows']} 条可回滚"
        + (
            style(f"   {journal['rolled_back']} 条已回滚", "90")
            if journal["rolled_back"]
            else ""
        )
        + (
            style(f"   最近写入 {_local_stamp(journal['latest'])}", "90")
            if journal["latest"]
            else ""
        )
    )
    print(
        "  "
        + style("▸ 报告", "36")
        + f"   {len(report_files)} 个   {_human_bytes(report_bytes)}"
        + style(f"   {short_path(reports)}", "90")
    )
    print()
    if by_status.get("ambiguous") or by_status.get("suspect"):
        print(
            "  "
            + style("提示", "90")
            + "  "
            + style("lrcpatch candidates", "36")
            + " 看这些 ! 需确认的曲目；"
            + style("lrcpatch clean --dry-run", "36")
            + " 看能回收多少空间。"
        )
        print()
    return 0


def collect_picks(
    args: argparse.Namespace,
    tracks: list[Track],
    library: Path,
    sources: list[Any] | None = None,
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
    force: bool = False,
) -> tuple[
    dict[str, str], list[str], list[tuple[Track, Candidate, int, float]]
]:
    """Merge --pick and --pick-file into path -> SOURCE:ID.

    One pick in a one-track scope is applied directly (the user is pointing at
    that file).  Anything else is paired by resolving each ID's metadata and
    scoring it against the tracks that still need lyrics.
    """
    picks: dict[str, str] = {}
    pick_file = getattr(args, "pick_file", None)
    if pick_file:
        picks.update(read_pick_file(Path(pick_file), library, tracks))

    values = [str(value).strip() for value in (getattr(args, "pick", None) or [])]
    values = [value for value in values if value]
    preview: list[tuple[Track, Candidate, int, float]] = []
    unresolved: list[str] = []
    if values:
        eligible = [track for track in tracks if force or not track.has_lyrics]
        if not eligible:
            raise ToolError(
                "--pick 指定了候选，但当前范围内没有缺歌词的曲目；"
                "要给已有歌词的文件换词，加 --force"
            )
        if len(values) == 1 and len(eligible) == 1:
            picks[str(eligible[0].path)] = values[0]
        else:
            if sources is None:
                raise ToolError("无法为多个 --pick 配对：缺少歌词源")
            assigned, unresolved, preview = assign_picks(
                tracks, values, sources, tolerance, force=force
            )
            picks.update(assigned)
            if unresolved:
                raise ToolError(
                    "这些 --pick 无法配对到范围内的曲目："
                    + "、".join(unresolved)
                    + "\n可能是 ID 查不到详情，或标题/歌手与范围内任何缺词曲目都不符。"
                    "\n把范围缩小到单个文件，或改用 --pick-file（每行：路径<TAB>源:ID）；"
                    "已有歌词的文件需要 --force。"
                )

    selected = {str(track.path) for track in tracks}
    for path, value in picks.items():
        if path not in selected:
            raise ToolError(f"人工指定的曲目不在当前 --path 范围内：{path}")
        parse_pick(value)
    return picks, unresolved, preview

def cmd_plan(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    if not tracks:
        print_header("plan")
        print("  " + style("没有找到可处理的音频文件。", "33"))
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    client = JsonClient(
        state,
        refresh=args.refresh,
        impersonate=not getattr(args, "no_impersonate", False),
    )
    source_objects = build_sources(args.src, client, getattr(args, "target_format", None))
    picks, _unresolved, pick_preview = collect_picks(
        args,
        tracks,
        library,
        source_objects,
        args.duration_tolerance,
        force=bool(getattr(args, "force", False)),
    )
    report = args.report or (state_dir / "reports" / f"plan-{stamp}.csv")

    pending: list[Track] = []
    reused = 0
    retrying = 0
    target_format = getattr(args, "target_format", None)
    force = bool(getattr(args, "force", False))
    src_set = ",".join(args.src)
    for track in tracks:
        old = state.get_plan(str(track.path))
        if force:
            # --force distrusts both the file's current lyrics and any stored
            # verdict: search, match and re-plan everything in scope.
            pending.append(track)
            continue
        if str(track.path) in picks:
            pending.append(track)
            continue
        if old and old["status"] == "suspect" and args.no_timestamp_check:
            pending.append(track)
            continue
        if (
            old
            and old["fingerprint"] == track.fingerprint()
            and not args.refresh
        ):
            if old["status"] in {"skip_existing", "instrumental"}:
                reused += 1
                continue
            if old["status"] == "ready":
                # A cached LRC is not a cached QRC: the format is decided when
                # the lyric is fetched, so asking for a different one has to
                # fetch again instead of reusing a plan that cannot satisfy it.
                if target_format and old["lyric_kind"] != target_format:
                    pending.append(track)
                    continue
                # A plan from a source that is no longer selected says nothing
                # about the sources that are: `--src qq` after `ne,qq` must
                # re-ask qq instead of showing netease's verdict, while the
                # qq plans it already has stay reused.
                if not plan_uses_source(old, args.src):
                    pending.append(track)
                    continue
                reused += 1
                continue
            if old["status"] == "error":
                # Transient provider failures are retried every run; they are
                # not a verdict and must never be reused as one.
                retrying += 1
                pending.append(track)
                continue
            if old["status"] in {"no_match", "ambiguous", "suspect"} and not args.retry_failed:
                # Every "not settled" verdict depends on which sources answered,
                # so a different --src list deserves a fresh look: after
                # `--src qq` the old netease ! / 无匹配 says nothing about qq.
                # Rows from before src_set existed are trusted once so an
                # upgrade does not re-query the whole library.
                stored_src = row_get(old, "src_set", "")
                if stored_src and stored_src != src_set:
                    pending.append(track)
                    continue
                reused += 1
                continue
        pending.append(track)

    scope = selection_label(args)
    print_header(
        "plan",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(tracks)} 首",
    )
    print_meta(
        "源",
        f"{','.join(source_label(name) for name in args.src)}"
        + style(f"   复用 {reused}   查询 {len(pending)}   并发 {args.workers}", "90"),
    )
    if force:
        overwrite = sum(1 for track in tracks if track.has_lyrics)
        print_meta(
            "强制",
            "忽略已有计划重新匹配"
            + (
                style(
                    f"   其中 {overwrite} 首已有歌词，写入要 apply --force"
                    "（一步到位：plan --force --apply）",
                    "33",
                )
                if overwrite
                else ""
            ),
        )
    if retrying:
        print_meta(
            "重试",
            f"{retrying} 首上次未完成"
            + style("（失败或匹配规则更新，本次自动重查）", "90"),
        )
    if picks:
        ignored = sum(
            1
            for track in tracks
            if str(track.path) in picks and track.has_lyrics and not force
        )
        print_meta(
            "指定",
            f"{len(picks)} 首"
            + (style(f"   其中 {ignored} 首已有歌词会被跳过", "33") if ignored else ""),
        )
        for track, candidate, _identity, diff in pick_preview:
            gate = f"  {format_diff(diff)}" if diff < 900 else ""
            print_meta(
                "配对",
                f"{track.path.name} ← {source_label(candidate.source)}:"
                f"{candidate.source_id} "
                f"{candidate.title}"
                + (f" / {candidate.artist_text}" if candidate.artist_text else "")
                + style(gate, "90"),
            )

    kept_good = 0
    switched = 0
    downgraded = 0
    if pending:
        live = ProgressLine()
        if not live.enabled:
            print("  " + style(f"查询 {len(pending)} 首…（Ctrl+C 可中断）", "90"), flush=True)
        done = 0
        started = time.monotonic()
        live.start(f"查询 0/{len(pending)}", started)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    plan_one,
                    track,
                    source_objects,
                    not args.no_plain,
                    args.duration_tolerance,
                    not args.no_timestamp_check,
                    picks.get(str(track.path)),
                    force,
                ): track
                for track in pending
            }
            for future in as_completed(futures):
                track = futures[future]
                try:
                    result = future.result()
                    fingerprint = track.fingerprint()
                except Exception as exc:
                    result = PlanResult(track, "error", f"{type(exc).__name__}: {exc}")
                    fingerprint = track.fingerprint()
                previous = state.get_plan(str(track.path))
                if keep_previous_plan(previous, fingerprint, result):
                    # A provider outage must not erase a plan that was already
                    # good; there is simply no new evidence to replace it.
                    kept_good += 1
                else:
                    # There is one plan per file, so a run with another --src
                    # replaces the previous verdict rather than keeping both.
                    # Say so: "my netease results vanished" is otherwise a
                    # silent surprise.
                    was = row_get(previous, "source", "") if previous else ""
                    now = result.candidate.source if result.candidate else ""
                    if was and was != now:
                        switched += 1
                        if previous["status"] == "ready" and result.status != "ready":
                            downgraded += 1
                    state.upsert_plan(result, fingerprint, src_set)
                done += 1
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed else 0
                if live.enabled:
                    live.update(f"查询 {done}/{len(pending)}  ·  {rate:.1f} 首/秒")
                elif done % args.progress_every == 0 or done == len(pending):
                    print(f"  查询 {done}/{len(pending)} · {rate:.1f} 首/秒")
        live.close()

    selected_paths = {str(track.path) for track in tracks}
    rows = [row for row in state.list_plans() if row["path"] in selected_paths]
    summary = summarize_plans(rows)
    actionable = {
        "ready",
        "applied",
        "ambiguous",
        "suspect",
        "no_match",
        "error",
        "rolled_back",
    }
    entries = []
    for row in rows:
        if row["status"] not in actionable:
            continue
        cells, note = plan_fields(row)
        entries.append(TreeEntry(Path(row["path"]), row["status"], note, cells))
    print()
    if entries:
        totals = Counter(Path(row["path"]).parent for row in rows)
        print_tree(entries, library, args.tree_limit, totals)
    else:
        print("  " + style("所有曲目都已有歌词或无需处理。", "32"))
        print()

    print("  " + rule())
    print_summary(summary)
    order = {str(track.path): index for index, track in enumerate(tracks)}
    for path in sorted(picks, key=lambda item: order.get(item, len(order))):
        row = state.get_plan(path)
        if row:
            symbol, code, label = status_style(row["status"])
            print(
                "  "
                + style("指定", "90")
                + "  "
                + style(symbol, code)
                + " "
                + Path(path).name
                + style(f"  {label}；{row['reason']}", "90")
            )
    if kept_good:
        print_meta(
            "保留",
            f"{kept_good} 条原 ready 计划"
            + style("（本次请求失败，未用失败结果覆盖）", "90"),
        )
    if switched:
        print_meta(
            "换源",
            f"{switched} 首改由 {','.join(source_label(name) for name in args.src)} 定论"
            + style("（一个文件只留一条计划，旧结论已被覆盖；切回原来的源会重查，取词已缓存）", "90"),
        )
        if downgraded:
            print_meta(
                "降级",
                style(
                    f"其中 {downgraded} 首原本 ✓ 可写入，新源只给到 ! / ✗"
                    "   想保留旧结论：candidates --src <原源> 比较后用 --pick 指定",
                    "33",
                ),
            )
    if errors:
        print_meta("标签", style(f"读取失败 {len(errors)} 首（详见 scan）", "33"))

    write_plan_report(rows, report)
    if getattr(args, "html", False):
        html_file = getattr(args, "html_file", None)
        html_arg = getattr(args, "html", False)
        if html_file:
            html_path = Path(str(html_file)).expanduser()
        elif isinstance(html_arg, str):
            html_path = Path(html_arg).expanduser()
        else:
            html_path = plan_html_path(state_dir, stamp)
        write_plan_html(rows, html_path, library)
        print_meta("复核", short_path(html_path))
    print_meta("报告", short_path(report))
    print_meta("状态", short_path(state.path))
    health = client.health()
    hits = client.cache_hits()
    if pending:
        line = network_line(client)
        if line:
            print_meta("请求", line)
        else:
            suffix_text = f"（命中 {hits} 次）" if hits else ""
            print_meta("网络", "全部命中本地缓存，未发起请求" + suffix_text)
    elif reused:
        print_meta("网络", "未发起请求，全部复用已有计划")

    scope_args = " ".join(shlex.quote(item) for item in (args.path or []))
    suffix = f" {scope_args}" if scope_args else ""
    review = summary.get("ambiguous", 0) + summary.get("suspect", 0)
    print()
    if review:
        print(
            "  "
            + style("待确认", "33")
            + f"  {review} 首不会写入。先看候选："
            + style(f"lrcpatch candidates{suffix}", "36")
        )
        print(
            "  "
            + style("指定", "90")
            + "  选中后："
            + style(
                "lrcpatch --pick SRC:ID"
                + (" --apply" if getattr(args, "apply", False) else "")
                + suffix
                + "（可选多个 --pick）",
                "36",
            )
        )
        print()
    if summary.get("error", 0):
        print(
            "  "
            + style("请求失败", "31")
            + f"  {summary['error']} 首未写入（不是“无匹配”）。稍后重跑同一命令会自动重试。"
        )
        print()
        failed_names = {name for name, _, failed, _ in health if failed}
        alive = [name for name in args.src if name not in failed_names]
        if len(args.src) > 1 and 0 < len(alive) < len(args.src):
            print(
                "  "
                + style("绕过", "90")
                + "  也可加 "
                + style(f"--src {','.join(alive)}", "36")
                + " 暂时只用正常的源，避免等待失败源。"
            )
            print()
    if getattr(args, "apply", False):
        print(
            "  "
            + style("计划已生成。", "32")
            + "接下来直接写入本次 "
            + style("✓ 可写入", "1;32")
            + " 的曲目；! 需确认和 ✗ 失败不会写入。"
        )
    else:
        print(
            "  "
            + style("未修改任何音频文件。", "32")
            + "确认后运行 "
            + style(f"lrcpatch apply{suffix}", "36")
        )
    print()
    return 0


TITLE_CAP = 40
ARTIST_CAP = 24
ALBUM_CAP = 22
SCORE_WIDTH = 4


@dataclass
class CandRow:
    """One candidate, already split into table columns."""

    symbol: str
    code: str
    ident: str
    score: str
    diff: str
    kind: str
    title: str
    artist: str
    album: str
    notes: list[str] = field(default_factory=list)
    metadata_score: int = 0
    over_tolerance: bool = False
    score_value: int = 0
    diff_value: float = 0.0
    error: bool = False
    # True when the format was verified by a fetch *and* carries a word
    # timeline; the table prints those bold, so "QRC" never claims more than
    # the text actually holds.
    verbatim: bool = False

    @property
    def pick(self) -> str:
        return self.ident


def probe_formats(
    tracks: list[Track],
    results: dict[str, list],
    sources: list[Any],
    args: argparse.Namespace,
    known: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    """Fetch the lyric for the shown candidates to learn what they really are.

    Off by default: ``plan`` fetches the one candidate it picked, but a table of
    three per source is three requests per track.  With ``--probe N`` the answer
    is checked *locally* — QQ's endpoint is a QRC container, yet a track without
    word timing decrypts to a plain ``[0,0]`` block, which is LRC or bare text,
    not 逐字.  Each probe lands in the same HTTP cache ``plan`` reads, so the
    following ``plan`` is free.
    """
    per_source = min(getattr(args, "top", 3), int(getattr(args, "probe", 0)))
    if per_source <= 0:
        return {}
    by_name = {item.name: item for item in sources}
    work: list[tuple[Any, Candidate]] = []
    seen: set[tuple[str, str]] = set()
    for track in tracks:
        for name, candidates, error, skipped in results[str(track.path)]:
            source = by_name.get(name)
            # LRCLIB answers in the search result itself (synced or not).
            if source is None or name not in {"netease", "qq"}:
                continue
            if skipped or error is not None or not candidates:
                continue
            ranked = ranked_candidates(track, candidates, args.duration_tolerance)
            for _within, _meta, _score, _diff, candidate in ranked[:per_source]:
                key = (candidate.source, candidate.source_id)
                if candidate.instrumental or key in known or key in seen:
                    continue
                seen.add(key)
                work.append((source, candidate))
    if not work:
        return {}
    found: dict[tuple[str, str], str] = {}
    guard = threading.Lock()

    def probe(job: tuple[Any, Candidate]) -> None:
        source, candidate = job
        try:
            fetched = source.fetch(candidate)
        except Exception:
            # A probe is a nicety: a failed one leaves the "?" in place.
            return
        kinds = fetched.native_kinds or ((fetched.native_kind,) if fetched.native_kind else ())
        if not kinds:
            return
        with guard:
            # Same shape the plan table stores (``"yrc,lrc"``), so the row
            # renders through one code path either way.
            found[(candidate.source, candidate.source_id)] = ",".join(kinds)

    live = ProgressLine()
    live.start(f"取词确认 0/{len(work)}")
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, getattr(args, "workers", 4))) as pool:
        futures = [pool.submit(probe, job) for job in work]
        for _future in as_completed(futures):
            done += 1
            live.update(f"取词确认 {done}/{len(work)}")
    live.close()
    return found


def known_native_sets(state_dir: Path) -> dict[tuple[str, str], str]:
    """``(source, source_id) -> served formats``, learned from earlier plans.

    netease only says what it has when the lyric is fetched — a word-timed YRC,
    a line-timed LRC, or both in the same answer — so the candidate table
    cannot know before then.  A previous plan for the same song already
    recorded what came back, though, and repeating a guess after the answer is
    on disk is how "YRC/LRC" started reading like a promise.
    """
    out: dict[tuple[str, str], str] = {}
    for row in read_plan_cache(state_dir / "lyrics.sqlite3").values():
        source = str(row_get(row, "source", "") or "")
        source_id = str(row_get(row, "source_id", "") or "")
        kinds = str(
            row_get(row, "native_kinds", "") or row_get(row, "native_kind", "") or ""
        )
        if source and source_id and kinds:
            out.setdefault((source, source_id), kinds)
    return out


def ranked_candidates(
    track: Track, candidates: list[Candidate], tolerance: float
) -> list[tuple[bool, int, int, float, Candidate]]:
    """Every candidate with its scores, ordered the way the table shows it.

    Ranking is by metadata *first*: a correct title/artist with a long duration
    gap should outrank a cover that happens to be closer — but only among
    candidates that could actually be written.  A live version 13s off must not
    sit above the right recording.
    """
    ranked: list[tuple[bool, int, int, float, Candidate]] = []
    for candidate in candidates:
        score, diff = score_candidate(track, candidate, tolerance)
        metadata_score, _ = score_candidate(track, candidate, 999.0)
        ranked.append((diff <= tolerance, metadata_score, score, diff, candidate))
    ranked.sort(
        key=lambda item: (
            not item[0],
            -item[1],
            -item[2],
            item[3],
            -int(item[4].synced),
        )
    )
    return ranked


def candidate_rows(
    track: Track,
    results: list,
    args: argparse.Namespace,
    known: dict[tuple[str, str], str] | None = None,
) -> list[CandRow]:
    """Score and format every candidate for one track."""
    rows: list[CandRow] = []
    for name, candidates, error, skipped in results:
        if skipped:
            continue
        if error is not None:
            rows.append(
                CandRow(
                    "✗",
                    "1;31",
                    name,
                    "",
                    "",
                    "",
                    f"搜索失败 {type(error).__name__}: {error}",
                    "",
                    "",
                    error=True,
                )
            )
            continue
        ranked = ranked_candidates(track, candidates, args.duration_tolerance)
        for within, metadata_score, score, diff, candidate in ranked[: args.top]:
            known_kind = (known or {}).get(
                (candidate.source, candidate.source_id), ""
            )
            if candidate.instrumental:
                # Adopting it yields ♪ 纯音乐, not a lyric: do not mark ✓.
                symbol, code = "♪", "36"
            elif score >= 85:
                symbol, code = "✓", "1;32"
            elif metadata_score >= 85:
                symbol, code = "!", "1;33"
            else:
                symbol, code = "·", "90"
            if candidate.instrumental:
                kind = "纯音乐"
                verbatim = False
            elif candidate.lyrics:
                kind = lyric_tag(lyric_kind(candidate.lyrics))
                verbatim = store.verbatim(lyric_kind(candidate.lyrics))
            elif known_kind:
                # Asked for (--probe) or learned from an earlier plan.  Bold
                # means the answer was *checked*: a real word timeline, not a
                # QRC container whose only block is [0,0].
                kind = kind_set_label(known_kind)
                verbatim = store.verbatim(known_kind)
                if candidate.source == "qq" and "qrc" not in kind_set(known_kind):
                    # QQ has exactly one lyric container and it is QRC; when it
                    # decrypts without word timing the candidate is still the
                    # QRC answer, just not a 逐字 one.  Say QRC, drop the bold.
                    kind = "QRC"
                    verbatim = False
            elif candidate.synced:
                kind = "LRC"
                verbatim = False
            elif candidate.source == "netease":
                # netease serves YRC when the song has one and line-timed LRC
                # otherwise.  Before fetching that is unknowable, so show a
                # bare "?" instead of promising a format.
                kind = "?"
                verbatim = False
            else:
                kind = lyric_tag(candidate.native_kind or "lrc")
                verbatim = False
            notes = match_notes(track, candidate, args.duration_tolerance)
            rows.append(
                CandRow(
                    symbol=symbol,
                    code=code,
                    ident=f"{source_label(candidate.source)}:{candidate.source_id}",
                    # Show the metadata total, not the gated score: a cover that
                    # only matches the artist reads 30, not a bare "-".
                    score=str(metadata_score),
                    diff=format_diff(candidate.duration - track.duration, signed=True)
                    if diff < 900
                    else "Δ?",
                    kind=kind,
                    verbatim=verbatim,
                    title=candidate.title,
                    artist=candidate.artist_text,
                    album=candidate.album,
                    notes=notes,
                    metadata_score=metadata_score,
                    over_tolerance=bool(metadata_score) and not score,
                    score_value=metadata_score,
                    diff_value=candidate.duration - track.duration,
                )
            )
    return rows


def _column_widths(rows: list[CandRow]) -> tuple[int, int, int, int, int]:
    """ID, duration gap, format, title, artist — album-wide, so rows align."""
    return (
        max((disp_width(row.ident) for row in rows), default=0),
        max(6, max((disp_width(row.diff) for row in rows), default=0)),
        max(4, max((disp_width(row.kind) for row in rows), default=0)),
        min(TITLE_CAP, max((disp_width(row.title) for row in rows), default=0)),
        min(ARTIST_CAP, max((disp_width(row.artist) for row in rows), default=0)),
    )


def candidate_table_head(widths: tuple[int, int, int, int, int]) -> str:
    w_id, w_diff, w_kind, w_title, w_artist = widths
    cells = [
        pad_display("候选 ID", w_id),
        pad_display("分数", SCORE_WIDTH),
        pad_display("时长差", w_diff),
        pad_display("原生", w_kind),
        pad_display("标题", w_title),
        pad_display("歌手", w_artist),
        "专辑",
    ]
    return " " * 13 + style("  ".join(cells), "90")


def candidate_line(
    row: CandRow, widths: tuple[int, int, int, int, int], prefix: str, branch: str
) -> str:
    w_id, w_diff, w_kind, w_title, w_artist = widths
    title = truncate_display(row.title, w_title) if w_title else ""
    artist = truncate_display(row.artist, w_artist) if w_artist else ""
    diff_code = "31" if row.over_tolerance else "90"
    cells = [
        style(pad_display(row.ident, w_id), "36"),
        style(row.score.rjust(SCORE_WIDTH), "90"),
        style(pad_display(row.diff, w_diff), diff_code),
        style(pad_display(row.kind, w_kind), "1" if row.verbatim else "90"),
        pad_display(title, w_title),
        style(pad_display(artist, w_artist), "90"),
        style(truncate_display(row.album, ALBUM_CAP) if row.album else "", "90"),
    ]
    return (
        style(prefix, "90")
        + style(branch, "90")
        + style(row.symbol, row.code)
        + " "
        + "  ".join(cells).strip()
    )


def write_pick_template(
    path: Path, wanted: list[Track], rows_by_track: dict[str, list[CandRow]]
) -> Path:
    """Write an editable pick-file skeleton for a whole album.

    Auto-matched tracks are ready to use; anything that needs a human choice
    is commented out together with its alternatives, so uncommenting one line
    is the whole decision.
    """
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# lrcpatch 候选模板  {stamp}",
        "# 格式：曲目路径<TAB>SOURCE:ID。去掉行首的 # 即生效；可改 ID，也可整行删掉。",
        "# 用法：lrcpatch plan <同一范围> --pick-file 本文件",
        "",
    ]
    pending = 0
    for track in wanted:
        rows = rows_by_track.get(str(track.path), [])
        best = rows[0] if rows else None
        if best and best.symbol == "✓":
            lines.append(f"{track.relpath}\t{best.pick}")
            lines.append(f"#   ✓ 自动匹配 {best.metadata_score} 分 {best.diff}  {best.title}")
        else:
            pending += 1
            lines.append(f"# {track.relpath}")
            lines.append(f"#   未自动确定：{'；'.join(best.notes) if best else '无候选'}")
            for row in rows:
                lines.append(
                    f"#     {row.symbol} {row.ident}  {row.metadata_score} 分 {row.diff}  "
                    f"{row.title} — {row.artist} / {row.album}"
                )
            if best:
                lines.append(f"# {track.relpath}\t{best.pick}")
        lines.append("")
    lines.append(f"# 共 {len(wanted)} 首，其中 {pending} 首需要人工确认。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def template_target(args: argparse.Namespace, state_dir: Path) -> Path:
    """Where --template writes: the given FILE, or state_dir/picks/<scope>.tsv."""
    value = getattr(args, "template", None)
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str) and value:
        return Path(value).expanduser()
    scope = Path((args.path or [args.album or "library"])[0])
    if scope.suffix.lower() in {".m4a", ".mp3", ".dsf"}:
        scope = scope.parent
    name = scope.name or "library"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return state_dir / "picks" / f"{name}-{stamp}.tsv"


def plan_uses_source(row: Any, sources: list[str] | None) -> bool:
    """Did this plan come from one of the sources the user is asking for now?

    A ready plan from netease must not hide a track when the user runs
    ``candidates --src qq``: they are asking to compare the other provider, and
    skipping the row made that impossible (``--refresh`` included).
    """
    if not sources:
        return True
    source = row_get(row, "source", None)
    if not source:
        # skip_existing / instrumental rows carry no source; they are about the
        # file, not about a provider.
        return True
    return str(source) in sources


@dataclass
class CandidateScope:
    """What ``candidates`` is about to list, and what it is leaving out."""

    tracks: list[Track]
    mode: str  # questions | unplanned | named | all
    settled: int = 0
    unplanned: int = 0
    with_lyrics: int = 0
    other_source: int = 0


def open_tracks(
    tracks: list[Track],
    state_dir: Path,
    everything: bool = False,
    sources: list[str] | None = None,
    named: bool = False,
) -> CandidateScope:
    """Pick the tracks ``candidates`` should list.

    The point of ``candidates`` is to answer "what does the tool want me to
    decide?" — so the default scope is the open questions from the last plan
    (`!` 需确认 and `!` 时间轴可疑), not the whole library: a library of 2000
    mostly-unplanned files would otherwise bury the three tracks that matter.
    ``--all`` still lists everything, and a scope that has never been planned
    falls back to "everything without lyrics" so the first run is not empty.

    ``named`` is what a path or ``--album`` means: the user already narrowed the
    library, and typing a file name is a request to see *that file*.  Filtering
    it out because its plan already says ✓ turned the command into "0 首" with
    no way forward except ``--all`` — which then looked like the only way to
    make ``--refresh`` do anything.

    The database is opened read-only (and not at all with ``--all``/``named``),
    so a concurrent plan/apply is never disturbed.
    """
    with_lyrics = sum(1 for track in tracks if track.has_lyrics)
    if everything:
        return CandidateScope(list(tracks), "all", with_lyrics=with_lyrics)
    if named:
        return CandidateScope(list(tracks), "named", with_lyrics=with_lyrics)

    cached = read_plan_cache(state_dir / "lyrics.sqlite3")
    questions: list[Track] = []
    unplanned: list[Track] = []
    settled = 0
    other_source = 0
    for track in tracks:
        row = cached.get(str(track.path))
        fresh = row is not None and row["fingerprint"] == track.fingerprint()
        # An open question is about the plan, not about the file: after
        # `plan --force` a track that already carries lyrics can still be
        # ! 需确认, and that is exactly the case the user wants to look at.
        if fresh and row["status"] in {"ambiguous", "suspect"}:
            questions.append(track)
            continue
        if track.has_lyrics:
            continue
        if not fresh:
            unplanned.append(track)
            continue
        # A ✓ plan from a source that is no longer selected says nothing about
        # the sources being asked now: `candidates --src qq` must show qq.
        if row["status"] == "ready" and not plan_uses_source(row, sources):
            other_source += 1
            questions.append(track)
            continue
        settled += 1

    if questions:
        return CandidateScope(
            questions, "questions", settled, len(unplanned), with_lyrics, other_source
        )
    if unplanned:
        return CandidateScope(unplanned, "unplanned", settled, len(unplanned), with_lyrics)
    return CandidateScope([], "questions", settled, 0, with_lyrics)


def scope_line(scope: CandidateScope) -> str:
    """One line saying what is listed and what is being left out."""
    if scope.mode == "all":
        return f"全部 {len(scope.tracks)} 首（含 ✓ 与已有歌词）"
    if scope.mode == "named":
        # Named tracks are listed whole; the only thing worth adding is how many
        # of them already hold lyrics, and how to get back to the short list.
        head = f"你点名的 {len(scope.tracks)} 首"
        if scope.with_lyrics:
            head += style(f"   其中 {scope.with_lyrics} 首已有歌词", "90")
        return head + style("   去掉路径就只看上次 plan 的存疑项", "90")
    if scope.mode == "questions":
        head = f"上次 plan 的 ! 需确认 {len(scope.tracks)} 首"
        if scope.other_source:
            head += f"（含 {scope.other_source} 首来自别的源的 ✓）"
    else:
        head = f"还没有结论的 {len(scope.tracks)} 首"
    rest = []
    if scope.settled:
        rest.append(f"✓ {scope.settled}")
    if scope.mode == "questions" and scope.unplanned:
        rest.append(f"未 plan {scope.unplanned}")
    if scope.with_lyrics:
        rest.append(f"已有歌词 {scope.with_lyrics}")
    if rest:
        head += style(f"   另有 {' · '.join(rest)}", "90")
    if scope.mode == "questions":
        head += style("   看全部加 --all", "90")
    return head


def cmd_candidates(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    """Read-only candidate listing: the IDs needed by --pick / --pick-file."""
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    if not tracks:
        print_header("candidates")
        print("  " + style("没有找到可处理的音频文件。", "33"))
        return 2
    client = JsonClient(
        state,
        refresh=args.refresh,
        impersonate=not getattr(args, "no_impersonate", False),
    )
    sources = build_sources(args.src, client, getattr(args, "target_format", None))
    # Naming a path or an album is the narrowing: list what was named.  Without
    # it, "what is left to decide" is the question worth answering.
    named = bool(args.path or args.album)
    scope = open_tracks(tracks, state_dir, args.all, sources=args.src, named=named)
    wanted = scope.tracks

    if not args.tsv:
        print_header(
            "candidates",
            f"{len(wanted)} 首  ·  每源最多 {args.top} 条"
            f"  ·  源 {','.join(source_label(name) for name in args.src)}",
        )
        if not wanted:
            if scope.mode == "questions":
                # Nothing to decide: say what the library already has instead
                # of an empty table, and name the option that widens the scope.
                settled = f"{scope.settled} 首 ✓ 可写入" if scope.settled else ""
                written = f"{scope.with_lyrics} 首已有歌词" if scope.with_lyrics else ""
                facts = "，".join(part for part in (settled, written) if part)
                print(
                    "  "
                    + style(
                        "上一次 plan 没有 ! 需确认的曲目"
                        + (f"（{facts}）" if facts else "")
                        + "。",
                        "32",
                    )
                )
                print(
                    "  "
                    + style(
                        "要看库里其它曲目加 --all；只想看某首歌就点名："
                        "lrcpatch candidates <路径>",
                        "90",
                    )
                )
            else:
                print("  " + style("没有可查询的曲目。", "32"))
            print()
            return 0
        print_meta("范围", scope_line(scope))

    # Query in parallel and buffer the results, so output stays grouped while a
    # 20-track album does not look frozen for three minutes.
    paused: dict[str, str] = {}
    guard = threading.Lock()

    def search_all(track: Track) -> list[tuple[str, list[Candidate] | None, Exception | None, bool]]:
        rows: list[tuple[str, list[Candidate] | None, Exception | None, bool]] = []
        for source in sources:
            with guard:
                skip = source.name in paused
            if skip:
                rows.append((source.name, None, None, True))
                continue
            try:
                rows.append((source.name, source.search(track), None, False))
            except Exception as exc:
                if client.cooldown(source.name) > 0:
                    with guard:
                        paused.setdefault(source.name, str(exc))
                rows.append((source.name, None, exc, False))
        return rows

    results: dict[str, list] = {}
    live = ProgressLine(stream=sys.stderr if args.tsv else sys.stdout)
    if not live.enabled and not args.tsv:
        print("  " + style(f"查询 {len(wanted)} 首…（Ctrl+C 可中断）", "90"), flush=True)
    started = time.monotonic()
    live.start(f"查询 0/{len(wanted)}", started)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(search_all, track): track for track in wanted}
        done = 0
        for future in as_completed(futures):
            track = futures[future]
            results[str(track.path)] = future.result()
            done += 1
            if live.enabled:
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed else 0
                live.update(f"查询 {done}/{len(wanted)}  ·  {rate:.1f} 首/秒")
    live.close()

    known = known_native_sets(state_dir)
    # "原生" is only honest if the table actually knows the answer.  A previous
    # plan knows it for its own track; for everything else netease has to be
    # asked, so ask — one lyric request per shown candidate, cached for the
    # `plan` that follows.
    if not args.tsv and getattr(args, "probe", 0):
        known.update(probe_formats(wanted, results, sources, args, known))
    rows_by_track: dict[str, list[CandRow]] = {
        str(track.path): candidate_rows(track, results[str(track.path)], args, known)
        for track in wanted
    }

    if args.tsv:
        for track in wanted:
            for row in rows_by_track[str(track.path)]:
                if row.error:
                    continue
                print(
                    f"{track.relpath}\t{row.ident}\t{row.score_value}"
                    f"\t{abs(row.diff_value):.1f}\t{row.title}"
                )
        if getattr(args, "template", None):
            path = write_pick_template(
                template_target(args, state_dir), wanted, rows_by_track
            )
            print(f"# 模板：{path}", file=sys.stderr)
        if paused:
            print(
                "暂停的源："
                + ", ".join(f"{name}（{reason}）" for name, reason in paused.items()),
                file=sys.stderr,
            )
        return 0

    groups: dict[Path, list[Track]] = {}
    for track in wanted:
        groups.setdefault(track.path.parent, []).append(track)
    for folder in sort_paths(groups, library):
        print("  " + style("▸ ", "36") + style(short_path(folder, library), "1;36"))
        items = sorted(groups[folder], key=lambda track: name_key(track.path.name))
        album_rows = [row for track in items for row in rows_by_track[str(track.path)]]
        widths = _column_widths(album_rows)
        if album_rows:
            print(candidate_table_head(widths))
        for index, track in enumerate(items):
            last_track = index == len(items) - 1
            print(
                style("     └─ " if last_track else "     ├─ ", "90")
                + style(track.path.name, "1")
                + style(f"  {format_duration(track.duration)}", "90")
            )
            prefix = "        " if last_track else "     │  "
            rows = rows_by_track[str(track.path)]
            if not rows:
                print(style(prefix, "90") + style("└─ ", "90") + style("无候选", "90"))
                continue
            for row_index, row in enumerate(rows):
                last_row = row_index == len(rows) - 1
                branch = "└─ " if last_row else "├─ "
                print(candidate_line(row, widths, prefix, branch))
        print()

    # `settled` used to be a local of the empty-scope branch, so any listing
    # that actually had rows crashed here (feedback 20261105-1925).
    count_label = "首待确认" if scope.mode == "questions" and not args.all else "首"
    print(
        "  "
        + style(f"共 {len(wanted)} {count_label}。", "90")
        + "把选中的 SOURCE:ID 交给 "
        + style("plan --pick", "36")
        + "，或整理成 "
        + style("--pick-file", "36")
        + "。"
    )
    if getattr(args, "template", None):
        template_path = write_pick_template(
            template_target(args, state_dir), wanted, rows_by_track
        )
        print_meta("模板", str(template_path))
        scope_args = " ".join(shlex.quote(item) for item in (args.path or []))
        print_meta(
            "下一步",
            "编辑模板后运行 "
            + style(
                f"lrcpatch plan --pick-file {shlex.quote(str(template_path))}"
                f"{f' {scope_args}' if scope_args else ''}",
                "36",
            ),
        )
    if errors:
        print_meta("标签", style(f"读取失败 {len(errors)} 首（详见 scan）", "33"))
    if paused:
        print_meta(
            "暂停",
            "  ·  ".join(f"{name}: {reason}" for name, reason in paused.items()),
        )
    line = network_line(client)
    if line:
        print_meta("请求", line)
    elif client.cache_hits():
        print_meta(
            "网络", f"全部命中本地缓存（{client.cache_hits()} 次），未发起请求"
        )
    print()
    return 0

OUTPUT_FORMATS = ("lrc", "a2", "eslrc", "yrc", "qrc", "ttml")

# Same taxonomy as the embedded lyric: the extension is how the next tool
# knows a word-timed file from a line-timed one.
OUTPUT_SUFFIX = store.SUFFIX

_AUDIO_SUFFIXES = (".m4a", ".mp3", ".dsf", ".flac", ".ogg", ".wav", ".aiff")


def output_name(artist: str, title: str, ident: str) -> str:
    """``歌手 - 标题 [ne:123]``, safe on every filesystem.

    Same name the plan store uses, so ``output`` of a planned song lands on the
    file ``apply`` reads instead of next to it.
    """
    return store.stem_for(artist, title, ident)


@dataclass
class OutputJob:
    """One ``SOURCE:ID`` resolved to text, before any format is chosen."""

    source: str
    source_id: str
    ident: str
    title: str = ""
    artist: str = ""
    album: str = ""
    native: str = ""
    native_kind: str = ""
    native_kinds: tuple[str, ...] = ()
    # Text we already hold for a given format, other than the native one.  A
    # netease answer carries a YRC *and* a hand-written LRC; flattening the YRC
    # would produce a slightly different file from the one `apply` writes, so
    # the served LRC is exported as-is when it is the format asked for.
    texts: dict[str, str] = field(default_factory=dict)
    origin: str = ""  # 歌词文件 / 状态库 / 取词
    # Name the stored file already uses; exporting keeps the same stem instead
    # of creating a second file for the same ID.
    stem: str = ""
    error: str = ""


def resolve_output_id(
    state: State, sources: dict[str, Any], source: str, source_id: str
) -> OutputJob:
    """Prefer the lyric already in the store, then the one in the database."""
    job = OutputJob(source=source, source_id=source_id, ident=f"{source_label(source)}:{source_id}")
    row = state.find_native(source, source_id)
    if row is not None:
        job.native = state.native_text(row)
        job.native_kind = str(row_get(row, "native_kind", "") or "")
        job.native_kinds = kind_set(
            row_get(row, "native_kinds", "") or job.native_kind
        )
        job.title = str(row_get(row, "match_title", "") or "")
        job.artist = str(row_get(row, "match_artist", "") or "")
        job.album = str(row_get(row, "match_album", "") or "")
        job.origin = "状态库"
        if job.native:
            stored_kind = str(row_get(row, "lyric_kind", "") or "")
            stored = (
                store.text_of(row, state.lyr_root, stored_kind) if stored_kind else ""
            )
            if stored and stored_kind:
                job.texts.setdefault(stored_kind, stored)
            return job

    # No plan, or its file was cleaned: the store may still have the text —
    # a file dropped there by hand counts, and it knows its own name.
    name, kind = store.locate(state.lyr_root, job.ident)
    if name:
        job.native = store.load(state.lyr_root, name)
        if job.native:
            job.stem = Path(name).stem
            job.native_kind = kind
            job.native_kinds = job.native_kinds or kind_set(kind)
            job.origin = "歌词文件"
            return job

    provider = sources.get(source)
    if provider is None or source == "lrclib":
        # LRCLIB indexes songs by name and artist, not by ID: there is nothing
        # to ask it once all you have is an ID.
        job.error = f"{source_label(source)} 没有按 ID 取词的接口，请用 ne / qq 的 ID"
        return job
    candidate = None
    try:
        found = provider.lookup([source_id])
    except Exception as exc:  # a detail request must not kill the export
        found = []
        job.error = ""
        detail_error = exc
    else:
        detail_error = None
    candidate = next(
        (item for item in found if str(item.source_id) == str(source_id)), None
    )
    if candidate is None:
        if source != "netease":
            job.error = (
                f"查不到 {job.ident} 的标题/专辑"
                + (f"（{type(detail_error).__name__}: {detail_error}）" if detail_error else "")
                + "；这个源取词需要它们"
            )
            return job
        # netease takes the lyric request by ID alone; metadata is only a label.
        candidate = Candidate(
            source=source, source_id=source_id, title="", artists=[], album="", duration=0.0
        )
    try:
        fetched = provider.fetch(candidate)
    except Exception as exc:
        job.error = f"取词失败 {type(exc).__name__}: {exc}"
        return job
    if fetched.instrumental:
        job.error = "标记为纯音乐"
        return job
    if not fetched.native_lyrics:
        job.error = fetched.note or "无可用歌词"
        return job
    job.native = fetched.native_lyrics
    job.native_kind = fetched.native_kind
    rendered_kind = lyric_kind(fetched.lyrics)
    if fetched.lyrics and rendered_kind:
        job.texts.setdefault(rendered_kind, fetched.lyrics)
    job.native_kinds = kind_set(fetched.native_kinds or fetched.native_kind)
    job.title = job.title or candidate.title
    job.artist = job.artist or candidate.artist_text
    job.album = job.album or candidate.album
    job.origin = "取词"
    return job


def cmd_output(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    """Export lyric files for ``SOURCE:ID`` — several formats at once.

    ``apply`` writes *one* lyric into an audio file; this writes files and
    nothing else, so ``--a2 --lrc --ttml`` in one run is a reasonable request
    and the default is the provider's own text, unflattened.
    """
    idents: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # `--pick ne:1 --pick qq:2` is how plan and apply already name a song, so
    # `output` takes the same flag; a bare `ne:1` still works for old history.
    tokens = [*(getattr(args, "pick", None) or []), *(args.path or [])]
    for token in tokens:
        text = str(token).strip()
        if not text:
            continue
        looks_like_path = text.lower().endswith(_AUDIO_SUFFIXES) or os.sep in text
        if looks_like_path:
            raise ToolError(
                f"output 只接受 SOURCE:ID（例如 --pick ne:1815109506），不接文件路径：{text}\n"
                "  想导出某首歌的候选 ID，先跑 lrcpatch candidates <路径>"
            )
        item = parse_pick(text)
        if item in seen:
            continue
        seen.add(item)
        idents.append(item)
    if not idents:
        raise ToolError(
            "output 需要至少一个 SOURCE:ID，"
            "例如 lrcpatch output --pick ne:1815109506 --lrc --a2"
        )

    formats = [
        name for name in OUTPUT_FORMATS if getattr(args, name, False)
    ] or ["native"]
    # The default target *is* the plan store: `output ne:1` puts the provider's
    # text where `apply` reads it, and `--out` is for a copy elsewhere.
    out_dir = (getattr(args, "out", None) or state.lyr_root).expanduser()
    overwrite = bool(getattr(args, "force", False))

    client = JsonClient(
        state,
        refresh=getattr(args, "refresh", False),
        impersonate=not getattr(args, "no_impersonate", False),
    )
    wanted_sources = [name for name in dict.fromkeys(source for source, _ in idents)]
    sources = {
        source.name: source
        for source in build_sources(wanted_sources, client, None)
    }

    writing = "原生" if formats == ["native"] else " ".join(
        lyric_tag(name) for name in formats
    )
    print_header(
        "output",
        f"{len(idents)} 个 ID  ·  {writing}  ·  {short_path(out_dir)}",
    )

    written = skipped = failed = 0
    total_bytes = 0
    for source, source_id in idents:
        job = resolve_output_id(state, sources, source, source_id)
        print(
            "  "
            + style("▸ ", "36")
            + style(job.ident, "1;36")
            + (style(f"   {job.title}", "1") if job.title else "")
            + (style(f"   {job.artist}", "90") if job.artist else "")
            + (style(f"   {job.origin}", "90") if job.origin else "")
        )
        if job.error:
            failed += 1
            print(style("     └─ ", "90") + style(f"✗ {job.error}", "1;31"))
            print()
            continue
        served = kind_set_label(job.native_kinds) or lyric_tag(job.native_kind)
        asked = [job.native_kind] if formats == ["native"] else formats
        detail = f"格式  源提供 {served}"
        if formats != ["native"]:
            # When nothing was asked for, that line would just repeat itself.
            detail += f"  ·  导出 {writing}"
        print(style("     ├─ ", "90") + style(detail, "90"))
        stem = job.stem or output_name(job.artist, job.title, job.ident)
        for position, target in enumerate(asked):
            branch = "     └─ " if position == len(asked) - 1 else "     ├─ "
            text, kind, note = render_output(job, target)
            if text is None:
                print(
                    style(branch, "90")
                    + style(f"{lyric_tag(target):<5}", "90")
                    + style(note, "90")
                )
                skipped += 1
                continue
            path = out_dir / f"{stem}{OUTPUT_SUFFIX.get(kind, '.txt')}"
            existing = store.load(out_dir, path.name) if path.exists() else ""
            if existing == text:
                # The store already holds exactly this text — say so instead of
                # asking for --force on a file that needs no work.
                print(
                    style(branch, "90")
                    + style(f"{lyric_tag(kind):<5}", LYRIC_COLORS.get(kind, "32"))
                    + style(f"{short_path(path)}  已是最新", "90")
                )
                skipped += 1
                continue
            if path.exists() and not overwrite:
                print(
                    style(branch, "90")
                    + style(f"{lyric_tag(kind):<5}", LYRIC_COLORS.get(kind, "32"))
                    + style(f"{short_path(path)}  已存在（--force 覆盖）", "90")
                )
                skipped += 1
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(path, text)
            except OSError as exc:
                failed += 1
                print(
                    style(branch, "90")
                    + style(f"{lyric_tag(kind):<5}", "1;31")
                    + style(f"写入失败 {exc}", "90")
                )
                continue
            size = path.stat().st_size
            total_bytes += size
            written += 1
            lines = lyrics_line_count(text)
            print(
                style(branch, "90")
                + style(f"{lyric_tag(kind):<5}", LYRIC_COLORS.get(kind, "32"))
                + style(f"{path.name}", "1")
                + style(f"   {lines} 行 · {format_size(size)}", "90")
            )
        print()

    print("  " + rule())
    bits = [f"写入 {written} 个文件（{format_size(total_bytes)}）"]
    if skipped:
        bits.append(f"跳过 {skipped}")
    if failed:
        bits.append(f"失败 {failed}")
    print_meta("导出", "  ·  ".join(bits))
    print_meta("目录", short_path(out_dir))
    line = network_line(client)
    if line:
        print_meta("请求", line)
    elif client.cache_hits():
        print_meta("网络", f"全部命中本地缓存（{client.cache_hits()} 次），未发起请求")
    print()
    return 0 if not failed else 1


def render_output(
    job: OutputJob, target: str
) -> tuple[str | None, str, str]:
    """``(text, kind, note)`` for one target; ``text`` is None when impossible."""
    if target in ("native", job.native_kind):
        return job.native, job.native_kind, ""
    held = job.texts.get(target)
    if held:
        # Already the right shape: prefer it over re-deriving from the native.
        return held, target, ""
    if target in ("yrc", "qrc") and target not in job.native_kinds:
        # Private containers cannot be re-encoded from someone else's text.
        return (
            None,
            target,
            f"{lyric_tag(target)} 只能由源原样提供；{job.ident} 给的是"
            f" {kind_set_label(job.native_kinds) or lyric_tag(job.native_kind)}",
        )
    text, note = render_native(job.native, job.native_kind, target)
    if note:
        return None, target, note
    kind = lyric_kind(text)
    if kind != target:
        # render_native fell back silently; do not save it under the wrong name.
        return (
            None,
            target,
            f"原生是 {lyric_tag(job.native_kind)}，转不出 {lyric_tag(target)}",
        )
    return text, kind, ""


def select_plan_rows(
    state: State,
    library: Path,
    album: str | None,
    limit: int | None,
    paths: list[str] | None = None,
    statuses: tuple[str, ...] = ("ready",),
) -> list[sqlite3.Row]:
    selected = resolve_selection(library, paths, strict=False)
    rows = [
        row
        for status in statuses
        for row in state.list_plans(status)
        if selection_matches(Path(row["path"]), library, album, selected)
    ]
    rows.sort(key=lambda row: path_key(Path(row["path"]), library))
    return rows[:limit] if limit else rows


def _skip_text(counter: Counter) -> str:
    order = [
        "ambiguous",
        "suspect",
        "no_match",
        "error",
        "skip_existing",
        "instrumental",
        "applied",
        "rolled_back",
    ]
    parts = [
        f"{status_style(key)[0]} {status_style(key)[2]} {counter[key]}"
        for key in order
        if counter.get(key)
    ]
    parts.extend(
        f"{key} {value}"
        for key, value in sorted(counter.items())
        if key not in order and value
    )
    return " · ".join(parts)

FORMAT_TAGS = {"a2": "A2", "eslrc": "ESLRC", "yrc": "YRC", "qrc": "QRC", "ttml": "TTML"}


def lyric_for_target(
    row: Any, target: str | None, lyr_root: Path | None = None
) -> tuple[str, str]:
    """``(lyrics, kind)`` to write for one row, converting when asked.

    The text is no longer stored in the database: it is read from the lyric
    store (``lyr/歌手 - 标题 [ne:123].yrc``), so ``--qrc``/``--a2``/… at apply time
    is a local conversion instead of a re-fetch.  Raises :class:`ToolError` when
    the plan simply cannot produce that format.
    """
    root = lyr_root or default_lyr_dir()
    stored_kind = str(row_get(row, "lyric_kind", "") or "")
    if not target:
        target = stored_kind or str(row_get(row, "native_kind", "") or "")
    if not target:
        raise ToolError("计划里没有歌词（旧版本生成的计划），请先 plan 重新取词")
    stored = store.text_of(row, root, target)
    if stored:
        # Served by the source, or already converted and written to the store:
        # use it as-is, minus a provider-private tag an old plan may still have.
        kind = lyric_kind(stored) or target
        return strip_provider_tags(stored, kind), kind
    tag = FORMAT_TAGS.get(target, target.upper())
    native = store.text_of(row, root)
    native_kind = str(row_get(row, "native_kind", "") or "") or (
        lyric_kind(native) if native else ""
    )
    if not native:
        raise ToolError(
            f"歌词库里没有 {lyric_tag(native_kind) or '原生'} 文本"
            f"（文件可能被 clean 清掉了），无法转成 {tag}；请先 plan --{target} 重新取词"
        )
    converted, note = render_native(native, native_kind, target)
    if note:
        raise ToolError(note)
    text = sanitize_lyrics(converted)
    kind = lyric_kind(text)
    if kind != target:
        raise ToolError(
            f"转换结果是 {lyric_tag(kind)}，不是 {tag}"
        )
    return text, kind


def format_mismatch(rows: list, target: str, lyr_root: Path | None = None) -> list:
    """Rows that cannot produce the format ``--<target>`` asks for."""
    bad: list = []
    for row in rows:
        try:
            lyric_for_target(row, target, lyr_root)
        except ToolError:
            bad.append(row)
    return bad


def cmd_apply(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    force = bool(getattr(args, "force", False))
    writable = ("ready", "suspect") if force else ("ready",)
    selected = resolve_selection(library, args.path, strict=False)
    scoped = [
        row
        for row in state.list_plans()
        if selection_matches(Path(row["path"]), library, args.album, selected)
    ]
    rows = select_plan_rows(
        state, library, args.album, args.limit, args.path, writable
    )
    skipped = Counter(row["status"] for row in scoped if row["status"] not in writable)
    scope = selection_label(args)
    if not rows:
        print_header(
            "apply",
            f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  0 首",
        )
        print("  " + style("没有 ready 计划，未写入任何文件。", "33"))
        if skipped:
            print_meta(
                "跳过",
                f"{sum(skipped.values())} 首非 ready：" + _skip_text(skipped),
            )
            print_meta(
                "说明",
                "apply 只写 ✓ 可写入；! 需确认请先 candidates 查 ID，再 --pick 重新 plan",
            )
        print()
        return 2
    target = getattr(args, "target_format", None)
    # Convert every row before touching a file: failing on the third file after
    # two writes is worse than one clear message.  A row whose native lyric
    # cannot reach the requested format is skipped (it is written as LRC in a
    # normal run); only a batch with nothing convertible is refused outright.
    prepared: dict[str, tuple[str, str]] = {}
    unusable: list[tuple[str, str]] = []
    for row in rows:
        try:
            prepared[row["path"]] = lyric_for_target(row, target, state.lyr_root)
        except ToolError as exc:
            unusable.append((row["path"], str(exc)))
    if unusable and not prepared:
        names = "、".join(Path(path).name for path, _ in unusable[:3])
        more = f" 等 {len(unusable)} 首" if len(unusable) > 3 else ""
        raise ToolError(
            f"这些计划无法写成 {FORMAT_TAGS.get(target or '', target or '')}："
            f"{names}{more}\n  {unusable[0][1]}\n"
            "转不出来是因为源只给了逐行 LRC（没有逐字时间轴）；"
            "去掉该格式参数即可按 LRC 写入"
        )
    if unusable:
        rows = [row for row in rows if row["path"] in prepared]
    print_header(
        "apply",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    print_meta(
        "方式",
        "临时副本验证后原子替换；失败则原文件不动"
        + style(f"   并发 {args.workers}", "90"),
    )
    if force:
        print_meta(
            "强制",
            "连 ! 时间轴可疑的计划一起写；已有歌词会被覆盖"
            + style("（rollback 可还原原歌词）", "90"),
        )
    if skipped:
        print_meta(
            "跳过",
            f"{sum(skipped.values())} 首非 ready：" + _skip_text(skipped),
        )
        print_meta("说明", "只写 ✓ 可写入；! 需确认、✗ 失败/无匹配都不会写入")
    kinds = Counter(kind for _, kind in prepared.values())
    if target or len(kinds) > 1 or (kinds and next(iter(kinds)) not in ("lrc", "")):
        print_meta(
            "格式",
            "   ".join(
                f"{lyric_tag(kind) if kind else '—'} {count}"
                for kind, count in kinds.most_common()
            ),
        )
    if unusable:
        tag = FORMAT_TAGS.get(target or "", (target or "").upper())
        names = "、".join(Path(path).name for path, _ in unusable[:3])
        more = f" 等 {len(unusable)} 首" if len(unusable) > 3 else ""
        print_meta(
            "跳过格式",
            f"{names}{more} 无法写成 {tag}"
            + style("（源只给了逐行 LRC，没有逐字时间轴；去掉该参数可按 LRC 写）", "90"),
        )

    if not args.no_preflight:
        live = ProgressLine()
        live.start("检查目录权限与空间…")
        try:
            preflight_apply(rows, args.workers)
        finally:
            live.close()
        print("  " + style("✓ 写入前检查通过", "32"))

    report = args.report or default_report_path(state_dir, "apply")
    counts: Counter = Counter()
    sources: Counter = Counter()
    applied_paths: set[str] = set()
    outcomes: list[tuple[Path, bool, str, str]] = []
    done = 0
    overwritten = 0
    failed = False
    batch_size = 10
    live = ProgressLine()
    if not live.enabled:
        print("  " + style(f"写入 {len(rows)} 首…", "90"), flush=True)
    started = time.monotonic()
    live.start(f"写入 0/{len(rows)}", started)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            futures = {
                pool.submit(
                    apply_one,
                    row,
                    library,
                    prepared[row["path"]][0],
                    force,
                ): row
                for row in batch
            }
            for future in as_completed(futures):
                row = futures[future]
                path = Path(row["path"])
                source = source_label(row["source"] or "-")
                try:
                    journal = future.result()
                    state.add_journal(journal)
                    state.set_plan_status(str(path), "applied", "写入并校验成功")
                    applied_paths.add(str(path))
                    counts["applied"] += 1
                    if journal.get("pre_lyrics_present"):
                        overwritten += 1
                    sources[source] += 1
                    outcomes.append((path, True, source, ""))
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    if "file policy" in str(exc):
                        message += "（当前会话无权覆盖音乐库，请在终端运行 apply）"
                    elif "拒绝覆盖" in str(exc):
                        # The plan said ✓, yet the file grew lyrics since: say
                        # what unlocks it instead of leaving a dead end.
                        message += "（加 apply --force 可覆盖；原歌词会存入回滚记录）"
                        # Nothing is wrong with the lyric itself, so keep the
                        # plan usable: `apply --force` can retry right away
                        # instead of asking for another network round trip.
                        state.set_plan_status(str(path), "ready", message)
                    else:
                        state.set_plan_status(str(path), "error", message)
                    counts["error"] += 1
                    outcomes.append((path, False, source, message))
                    failed = True
                done += 1
                if live.enabled:
                    elapsed = time.monotonic() - started
                    rate = done / elapsed if elapsed else 0
                    live.update(f"写入 {done}/{len(rows)}  ·  {rate:.1f} 首/秒")
                elif done % args.progress_every == 0 or done == len(rows):
                    print(f"  写入 {done}/{len(rows)}")
            if failed:
                break
    live.close()

    print()
    entries = [
        TreeEntry(
            path,
            "applied" if ok else "error",
            f"{source} · 写入并校验成功" if ok else message,
        )
        for path, ok, source, message in outcomes
    ]
    print_tree(entries, library, args.tree_limit)
    print("  " + rule())
    print_summary(counts)
    if sources:
        print_meta(
            "来源",
            "   ".join(f"{name} {count}" for name, count in sources.most_common()),
        )
    if overwritten:
        print_meta(
            "覆盖",
            f"{overwritten} 首原本已有歌词"
            + style("，原歌词已存入回滚记录（rollback 可还原）", "90"),
        )
    if failed and len(rows) > done:
        print_meta("未处理", f"{len(rows) - done} 首因失败停止而保持原样")
    print_meta("网络", "未发起请求，直接使用 plan 结果")
    attempted = {row["path"] for row in rows}
    rows_after = [row for row in state.list_plans() if row["path"] in attempted]
    write_plan_report(rows_after, report)
    print_meta("报告", short_path(report))
    print()
    if failed:
        print(
            "  "
            + style(
                "检测到失败，已停止后续批次。失败文件未替换，原文件保持原样。",
                "1;31",
            )
        )
        print()
        return 1
    print("  " + style("写入完成，可运行 lrcpatch verify 复核。", "32"))
    print()
    return 0

def verify_journal_row(row: sqlite3.Row, library: Path) -> tuple[str, str]:
    path = Path(row["path"])
    if not path.is_file():
        return "missing", "文件不存在"
    current = read_track(path, library)
    if lyric_hash(current.existing_lyrics) != row["written_lyrics_sha"]:
        return "changed", "歌词已被后续修改"
    if non_lyrics_tags_hash(path) != row["post_tags_sha"]:
        return "changed", "非歌词标签与写入后不一致"
    if cover_hash(path) != row["post_cover_sha"]:
        return "changed", "封面与写入后不一致"
    if audio_hash(path) != row["post_audio_sha"]:
        return "changed", "音频数据与写入后不一致"
    return "ok", ""

def cmd_verify(args: argparse.Namespace, state: State) -> int:
    library = args.library.expanduser().resolve()
    rows = state.list_journal("applied")
    selected = resolve_selection(library, args.path, strict=False)
    if args.album or selected:
        rows = [
            row
            for row in rows
            if selection_matches(Path(row["path"]), library, args.album, selected)
        ]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print_header("verify")
        print("  " + style("没有可校验的写入记录。", "33"))
        return 2
    scope = selection_label(args)
    print_header(
        "verify",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    print_meta("内容", "歌词、其他标签、封面、音频数据与写入时逐项比对")
    print()
    counts: Counter = Counter()
    entries: list[TreeEntry] = []
    live = ProgressLine()
    if not live.enabled:
        print("  " + style(f"校验 {len(rows)} 首…", "90"), flush=True)
    else:
        live.start(f"校验 0/{len(rows)}")
    for index, row in enumerate(rows, 1):
        status, reason = verify_journal_row(row, library)
        counts[status] += 1
        entries.append(TreeEntry(Path(row["path"]), status, reason))
        if live.enabled:
            live.update(f"校验 {index}/{len(rows)}")
        elif index % 25 == 0 and index < len(rows):
            print(f"  校验 {index}/{len(rows)}")
    live.close()
    print()
    print_tree(entries, library, args.tree_limit)
    print("  " + rule())
    print_summary(counts)
    print()
    if counts["ok"] == len(rows):
        print("  " + style("全部通过：文件与写入时一致。", "32"))
        print()
        return 0
    return 1

def cmd_rollback(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    rows = state.list_journal("applied")
    selected = resolve_selection(library, args.path, strict=False)
    if args.album or selected:
        rows = [
            row
            for row in rows
            if selection_matches(Path(row["path"]), library, args.album, selected)
        ]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print_header("rollback")
        print("  " + style("没有可回滚的记录。", "33"))
        return 2
    scope = selection_label(args)
    if not args.yes:
        # A --force write may have replaced a lyric that was already there, so
        # rollback is "还原" for those rows and "移除" only for the rest.
        restores = sum(
            1
            for row in rows
            if row["pre_lyrics_present"] and (row["pre_lyrics"] or "").strip()
        )
        removals = len(rows) - restores
        print_header(
            "rollback",
            f"{truncate_display(scope, max(20, screen_width() - 30))}"
            f"  ·  {len(rows)} 首  ·  预览",
        )
        print_meta("范围", "只处理 journal 中由本工具写入、且歌词之后未被改动过的曲目")
        print()
        print_tree(
            [
                TreeEntry(
                    Path(row["path"]),
                    "pending",
                    "还原原歌词"
                    if row["pre_lyrics_present"] and (row["pre_lyrics"] or "").strip()
                    else "移除本工具写入的歌词",
                )
                for row in rows
            ],
            library,
            args.tree_limit,
        )
        print("  " + rule())
        parts = []
        if restores:
            parts.append(f"还原 {restores}")
        if removals:
            parts.append(f"移除 {removals}")
        print("  " + style("合计", "90") + "  " + style("↩", "36") + " " + " / ".join(parts))
        print()
        print(
            "  "
            + style("未修改任何文件。", "32")
            + "确认后追加 "
            + style("--yes", "1;36")
            + "。"
        )
        print()
        return 0

    print_header(
        "rollback",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    counts: Counter = Counter()
    entries: list[TreeEntry] = []
    live = ProgressLine()
    if not live.enabled:
        print("  " + style(f"回滚 {len(rows)} 首…", "90"), flush=True)
    else:
        live.start(f"回滚 0/{len(rows)}")
    for index, row in enumerate(rows, 1):
        path = Path(row["path"])
        if live.enabled:
            live.update(f"回滚 {index}/{len(rows)}")
        elif index % 10 == 0 and index < len(rows):
            print(f"  回滚 {index}/{len(rows)}")
        try:
            current = read_track(path, library)
            if lyric_hash(current.existing_lyrics) != row["written_lyrics_sha"]:
                counts["conflict"] += 1
                entries.append(TreeEntry(path, "conflict", "歌词已被修改，拒绝回滚"))
                continue
            pre_tags = non_lyrics_tags_hash(path)
            pre_cover = cover_hash(path)
            pre_audio = audio_hash(path)
            staged = stage_copy(path)
            previous = row["pre_lyrics"] or ""
            try:
                if row["pre_lyrics_present"] and previous.strip():
                    # A --force write replaced a lyric that was already there:
                    # rollback puts it back instead of emptying the field.
                    write_lyrics_only(staged, previous)
                    if read_track(staged, library).existing_lyrics.strip() != previous.strip():
                        raise VerificationError("原歌词回写不一致")
                else:
                    clear_lyrics_only(staged, row["written_lyrics_sha"])
                    if read_track(staged, library).existing_lyrics:
                        raise VerificationError("歌词未完全移除")
                if non_lyrics_tags_hash(staged) != pre_tags:
                    raise VerificationError("非歌词标签发生变化")
                if cover_hash(staged) != pre_cover:
                    raise VerificationError("封面发生变化")
                if audio_hash(staged) != pre_audio:
                    raise VerificationError("音频数据发生变化")
                os.replace(staged, path)
            except Exception:
                staged.unlink(missing_ok=True)
                raise
            state.set_journal_status(str(path), "rolled_back")
            state.set_plan_status(str(path), "rolled_back", "已回滚")
            counts["rolled_back"] += 1
            entries.append(
                TreeEntry(
                    path,
                    "rolled_back",
                    "已还原原歌词" if previous.strip() else "已移除并校验",
                )
            )
        except Exception as exc:
            counts["error"] += 1
            entries.append(TreeEntry(path, "error", f"{type(exc).__name__}: {exc}"))
            break
    live.close()
    print()
    print_tree(entries, library, args.tree_limit)
    print("  " + rule())
    print_summary(counts)
    print()
    return 0 if not (counts["conflict"] or counts["error"]) else 1
