"""The six user-facing commands: scan, plan, candidates, apply, verify, rollback."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .audio import (
    apply_one,
    audio_hash,
    clear_lyrics_only,
    cover_hash,
    export_lrc,
    load_tracks,
    non_lyrics_tags_hash,
    preflight_apply,
    read_track,
    stage_copy,
)
from .config import default_lrc_dir
from .errors import ToolError, VerificationError
from .matching import (
    keep_previous_plan,
    parse_pick,
    plan_one,
    read_pick_file,
    score_candidate,
)
from .models import PlanResult, Track, TreeEntry
from .paths import resolve_selection, selection_matches
from .render import (
    ProgressLine,
    default_report_path,
    disp_width,
    pad_display,
    plan_html_path,
    plan_note,
    print_header,
    print_meta,
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
from .sources import JsonClient, build_sources
from .state import State
from .text import lyric_hash, lyric_kind


def summarize_plans(rows: list[sqlite3.Row]) -> Counter:
    return Counter(row["status"] for row in rows)

def cmd_scan(args: argparse.Namespace, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    live = ProgressLine()
    live.start("扫描音频与标签…")
    tracks, errors = load_tracks(library, args.album, args.limit, args.path)
    live.close()
    formats = Counter(track.fmt for track in tracks)
    existing = Counter(track.fmt for track in tracks if track.has_lyrics)
    print_header("scan", short_path(library))
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
    have_total = sum(existing.values())
    print_meta(
        "曲目",
        f"{len(tracks)} 首"
        + style(
            f"   已有歌词 {have_total}   待补 {len(tracks) - have_total}",
            "90",
        ),
    )
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

def collect_picks(
    args: argparse.Namespace, tracks: list[Track], library: Path
) -> dict[str, str]:
    """Merge --pick and --pick-file into path -> SOURCE:ID."""
    picks: dict[str, str] = {}
    pick_file = getattr(args, "pick_file", None)
    if pick_file:
        picks.update(read_pick_file(Path(pick_file), library, tracks))

    values = getattr(args, "pick", None) or []
    if values:
        eligible = [track for track in tracks if not track.has_lyrics]
        if len(eligible) != 1:
            raise ToolError(
                f"--pick 要求当前范围恰好一首缺歌词的曲目，实际 {len(eligible)} 首。"
                "请把拖入范围缩小到单首，或改用 --pick-file（每行：路径<TAB>源:ID）。"
            )
        if len(values) != 1:
            raise ToolError("单首曲目只能配一个 --pick；批量指定请用 --pick-file。")
        picks[str(eligible[0].path)] = values[0]

    selected = {str(track.path) for track in tracks}
    for path, value in picks.items():
        if path not in selected:
            raise ToolError(f"人工指定的曲目不在当前 --path 范围内：{path}")
        parse_pick(value)
    return picks

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
    client = JsonClient(state, refresh=args.refresh)
    source_objects = build_sources(args.sources, client)
    picks = collect_picks(args, tracks, library)
    report = args.report or (state_dir / "reports" / f"plan-{stamp}.csv")

    pending: list[Track] = []
    reused = 0
    retrying = 0
    for track in tracks:
        old = state.get_plan(str(track.path))
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
            if old["status"] in {"skip_existing", "ready", "instrumental"}:
                reused += 1
                continue
            if old["status"] == "error":
                # Transient provider failures are retried every run; they are
                # not a verdict and must never be reused as one.
                retrying += 1
                pending.append(track)
                continue
            if old["status"] in {"no_match", "ambiguous", "suspect"} and not args.retry_failed:
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
        f"{','.join(args.sources)}"
        + style(f"   复用 {reused}   查询 {len(pending)}   并发 {args.workers}", "90"),
    )
    if retrying:
        print_meta(
            "重试",
            f"{retrying} 首上次请求失败"
            + style("（网络/风控失败不作为结论，本次自动重查）", "90"),
        )
    if picks:
        ignored = sum(1 for track in tracks if str(track.path) in picks and track.has_lyrics)
        print_meta(
            "指定",
            f"{len(picks)} 首"
            + (style(f"   其中 {ignored} 首已有歌词会被跳过", "33") if ignored else ""),
        )

    kept_good = 0
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
                    state.upsert_plan(result, fingerprint)
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
    entries = [
        TreeEntry(Path(row["path"]), row["status"], plan_note(row))
        for row in rows
        if row["status"] in actionable
    ]
    print()
    if entries:
        totals = Counter(Path(row["path"]).parent for row in rows)
        print_tree(entries, library, args.tree_limit, totals)
    else:
        print("  " + style("所有曲目都已有歌词或无需处理。", "32"))
        print()

    print("  " + rule())
    print_summary(summary)
    for path in picks:
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
                + style(f"  {label} · {row['reason']}", "90")
            )
    if kept_good:
        print_meta(
            "保留",
            f"{kept_good} 条原 ready 计划"
            + style("（本次请求失败，未用失败结果覆盖）", "90"),
        )
    if errors:
        print_meta("标签", style(f"读取失败 {len(errors)} 首（详见 scan）", "33"))

    write_plan_report(rows, report)
    writable = {"ready", "applied", "skip_existing"}
    if getattr(args, "lrc", False):
        lrc_dir = (args.lrc_dir or default_lrc_dir()).expanduser()
        written, failed = export_lrc(
            library,
            [row for row in rows if row["status"] in writable],
            {str(track.path): track for track in tracks},
            lrc_dir,
        )
        suffix = style(f"（失败 {failed}）", "31") if failed else ""
        print_meta("LRC", f"{written} 个 -> {short_path(lrc_dir)}{suffix}")
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
    if pending:
        network = [item for item in health if item[1]]
        if network:
            bits = []
            for name, requests, failed_requests, cooldown in network:
                text = f"{name} {requests} 次"
                if failed_requests:
                    text += f" 失败 {failed_requests}"
                if cooldown:
                    text += f" 冷却 {cooldown}s"
                bits.append(text)
            print_meta("请求", "  ·  ".join(bits))
        else:
            print_meta("网络", "全部命中本地缓存，未发起请求")
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
            + style(f"lyrics.py candidates{suffix}", "36")
        )
        print(
            "  "
            + style("指定", "90")
            + "  选中后："
            + style(f"lyrics.py{suffix} --pick netease:歌曲ID", "36")
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
        alive = [name for name in args.sources if name not in failed_names]
        if len(args.sources) > 1 and 0 < len(alive) < len(args.sources):
            print(
                "  "
                + style("绕过", "90")
                + "  也可加 "
                + style(f"--sources {','.join(alive)}", "36")
                + " 暂时只用正常的源，避免等待失败源。"
            )
            print()
    print(
        "  "
        + style("未修改任何音频文件。", "32")
        + "确认后运行 "
        + style(f"lyrics.py apply{suffix}", "36")
    )
    print()
    return 0


def cmd_candidates(args: argparse.Namespace, state: State) -> int:
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
    client = JsonClient(state, refresh=args.refresh)
    sources = build_sources(args.sources, client)
    wanted = [track for track in tracks if args.all or not track.has_lyrics]

    if args.tsv:
        for track in wanted:
            for source in sources:
                try:
                    candidates = source.search(track)
                except Exception:
                    continue
                ranked = []
                for candidate in candidates:
                    score, diff = score_candidate(track, candidate, args.duration_tolerance)
                    metadata_score, _ = score_candidate(track, candidate, 999.0)
                    ranked.append((metadata_score, score, diff, candidate))
                ranked.sort(
                    key=lambda item: (-item[0], -item[1], item[2], -item[3].synced)
                )
                for _, score, diff, candidate in ranked[: args.top]:
                    print(
                        f"{track.relpath}\t{candidate.source}:{candidate.source_id}"
                        f"\t{score}\t{diff:.1f}\t{candidate.title}"
                    )
        return 0

    print_header(
        "candidates",
        f"{len(wanted)} 首  ·  每源最多 {args.top} 条  ·  源 {','.join(args.sources)}",
    )
    if not wanted:
        print("  " + style("范围内所有曲目都已有歌词；加 --all 可强制列出。", "32"))
        print()
        return 0

    groups: dict[Path, list[Track]] = {}
    for track in wanted:
        groups.setdefault(track.path.parent, []).append(track)
    for folder in sorted(groups, key=lambda item: str(item)):
        print("  " + style("▸ ", "36") + style(short_path(folder, library), "1;36"))
        items = sorted(groups[folder], key=lambda track: (track.path.name, str(track.path)))
        for index, track in enumerate(items):
            last_track = index == len(items) - 1
            print(
                style("     └─ " if last_track else "     ├─ ", "90")
                + style(track.path.name, "1")
                + style(f"  {track.duration:.0f}s", "90")
            )
            prefix = "        " if last_track else "     │  "
            rows: list[tuple[str, str, str, str]] = []
            for source in sources:
                try:
                    candidates = source.search(track)
                except Exception as exc:
                    rows.append(
                        ("✗", "1;31", f"{source.name}", f"搜索失败 {type(exc).__name__}: {exc}")
                    )
                    continue
                ranked = []
                for candidate in candidates:
                    score, diff = score_candidate(track, candidate, args.duration_tolerance)
                    # Rank by metadata first: a correct title/artist with a long
                    # duration gap should outrank a cover that happens to be closer.
                    metadata_score, _ = score_candidate(track, candidate, 999.0)
                    ranked.append((metadata_score, score, diff, candidate))
                ranked.sort(
                    key=lambda item: (-item[0], -item[1], item[2], -item[3].synced)
                )
                for metadata_score, score, diff, candidate in ranked[: args.top]:
                    if score >= 85:
                        symbol, code = "✓", "1;32"
                    elif metadata_score >= 85:
                        symbol, code = "!", "1;33"
                    else:
                        symbol, code = "·", "90"
                    if candidate.synced or lyric_kind(candidate.lyrics) == "lrc":
                        kind = "LRC"
                    elif candidate.lyrics:
                        kind = "TXT"
                    else:
                        kind = "待取词"
                    score_text = (
                        f"{score:>3}分"
                        if score
                        else ("超容差" if metadata_score else "不匹配")
                    )
                    diff_text = f"Δ{diff:.1f}s" if diff < 900 else "Δ?"
                    rest = (
                        f"{score_text} {diff_text} {pad_display(kind, 6)}"
                        f"  {candidate.title} — {candidate.artist_text} / {candidate.album}"
                    )
                    rows.append(
                        (
                            symbol,
                            code,
                            f"{candidate.source}:{candidate.source_id}",
                            rest,
                        )
                    )
            if not rows:
                print(style(prefix, "90") + style("└─ ", "90") + style("无候选", "90"))
            id_width = max((disp_width(ident) for _, _, ident, _ in rows), default=0)
            for row_index, (symbol, code, ident, rest) in enumerate(rows):
                branch = "└─ " if row_index == len(rows) - 1 else "├─ "
                line = (
                    style(prefix, "90")
                    + style(branch, "90")
                    + style(symbol, code)
                    + " "
                    + style(pad_display(ident, id_width), "36")
                )
                budget = screen_width() - disp_width(line) - 3
                line += "  " + style(
                    truncate_display(rest, max(16, budget)), "90"
                )
                print(line)
        print()

    print(
        "  "
        + style(f"共 {len(wanted)} 首。", "90")
        + "把选中的 SOURCE:ID 交给 "
        + style("plan --pick", "36")
        + "，或整理成 "
        + style("--pick-file", "36")
        + "。"
    )
    if errors:
        print_meta("标签", style(f"读取失败 {len(errors)} 首（详见 scan）", "33"))
    print()
    return 0

def select_plan_rows(
    state: State,
    library: Path,
    album: str | None,
    limit: int | None,
    paths: list[str] | None = None,
) -> list[sqlite3.Row]:
    selected = resolve_selection(library, paths, strict=False)
    rows = [
        row
        for row in state.list_plans("ready")
        if selection_matches(Path(row["path"]), library, album, selected)
    ]
    rows.sort(key=lambda row: (Path(row["path"]).name, row["path"]))
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

def cmd_apply(args: argparse.Namespace, state: State, state_dir: Path) -> int:
    library = args.library.expanduser().resolve()
    selected = resolve_selection(library, args.path, strict=False)
    scoped = [
        row
        for row in state.list_plans()
        if selection_matches(Path(row["path"]), library, args.album, selected)
    ]
    rows = select_plan_rows(state, library, args.album, args.limit, args.path)
    skipped = Counter(row["status"] for row in scoped if row["status"] != "ready")
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
    print_header(
        "apply",
        f"{truncate_display(scope, max(20, screen_width() - 30))}  ·  {len(rows)} 首",
    )
    print_meta(
        "方式",
        "临时副本验证后原子替换；失败则原文件不动"
        + style(f"   并发 {args.workers}", "90"),
    )
    if skipped:
        print_meta(
            "跳过",
            f"{sum(skipped.values())} 首非 ready：" + _skip_text(skipped),
        )
        print_meta("说明", "只写 ✓ 可写入；! 需确认、✗ 失败/无匹配都不会写入")

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
                pool.submit(apply_one, row, library): row for row in batch
            }
            for future in as_completed(futures):
                row = futures[future]
                path = Path(row["path"])
                source = row["source"] or "-"
                try:
                    journal = future.result()
                    state.add_journal(journal)
                    state.set_plan_status(str(path), "applied", "写入并校验成功")
                    applied_paths.add(str(path))
                    counts["applied"] += 1
                    sources[source] += 1
                    outcomes.append((path, True, source, ""))
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    if "file policy" in str(exc):
                        message += "（当前会话无权覆盖音乐库，请在终端运行 apply）"
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
    if failed and len(rows) > done:
        print_meta("未处理", f"{len(rows) - done} 首因失败停止而保持原样")
    print_meta("网络", "未发起请求，直接使用 plan 结果")
    if getattr(args, "lrc", False):
        lrc_dir = (args.lrc_dir or default_lrc_dir()).expanduser()
        written, failed_lrc = export_lrc(
            library,
            [row for row in rows if row["path"] in applied_paths],
            {},
            lrc_dir,
        )
        suffix = style(f"（失败 {failed_lrc}）", "31") if failed_lrc else ""
        print_meta("LRC", f"{written} 个 -> {short_path(lrc_dir)}{suffix}")
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
    print("  " + style("写入完成，可运行 lyrics.py verify 复核。", "32"))
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
        print_header(
            "rollback",
            f"{truncate_display(scope, max(20, screen_width() - 30))}"
            f"  ·  {len(rows)} 首  ·  预览",
        )
        print_meta("范围", "只处理 journal 中由本工具写入、且歌词之后未被改动过的曲目")
        print()
        print_tree(
            [TreeEntry(Path(row["path"]), "pending") for row in rows],
            library,
            args.tree_limit,
        )
        print("  " + rule())
        print_summary(Counter({"pending": len(rows)}), label="合计")
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
            try:
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
            entries.append(TreeEntry(path, "rolled_back", "已移除并校验"))
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
