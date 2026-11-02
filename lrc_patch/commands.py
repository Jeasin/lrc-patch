"""The six user-facing commands: scan, plan, candidates, apply, verify, rollback."""
from __future__ import annotations

import argparse
import datetime as dt
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
from .models import Candidate, PlanResult, Track, TreeEntry
from .paths import resolve_selection, selection_matches
from .render import (
    ProgressLine,
    default_report_path,
    disp_width,
    format_diff,
    format_duration,
    pad_display,
    plan_html_path,
    plan_note,
    print_header,
    print_meta,
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
from .state import State
from .text import lyric_hash, lyric_kind, lyric_tag, match_notes


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
        text = f"{name} {requests} 次"
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
    print_header(
        "scan",
        f"{truncate_display(scan_scope(args, library), max(20, screen_width() - 30))}"
        f"  ·  {len(tracks)} 首",
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
        print_scan_list(tracks, library, getattr(args, "tree_limit", 0) or 0)

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
    client = JsonClient(
        state,
        refresh=args.refresh,
        impersonate=not getattr(args, "no_impersonate", False),
    )
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
            f"{retrying} 首上次未完成"
            + style("（失败或匹配规则更新，本次自动重查）", "90"),
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
                + style(f"  {label}；{row['reason']}", "90")
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
                f"lrcpatch{suffix} --pick netease:歌曲ID"
                + (" --apply" if getattr(args, "apply", False) else ""),
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

    @property
    def pick(self) -> str:
        return self.ident


def candidate_rows(track: Track, results: list, args: argparse.Namespace) -> list[CandRow]:
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
        ranked = []
        for candidate in candidates:
            score, diff = score_candidate(track, candidate, args.duration_tolerance)
            # Rank by metadata first: a correct title/artist with a long
            # duration gap should outrank a cover that happens to be closer.
            metadata_score, _ = score_candidate(track, candidate, 999.0)
            ranked.append((metadata_score, score, diff, candidate))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2], -item[3].synced))
        for metadata_score, score, diff, candidate in ranked[: args.top]:
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
            elif candidate.lyrics:
                kind = lyric_tag(lyric_kind(candidate.lyrics))
            elif candidate.synced:
                kind = "LRC"
            else:
                kind = "待取词"
            notes = match_notes(track, candidate, args.duration_tolerance)
            if metadata_score < 85:
                notes.insert(0, "标题或歌手与文件不一致")
            rows.append(
                CandRow(
                    symbol=symbol,
                    code=code,
                    ident=f"{candidate.source}:{candidate.source_id}",
                    score=str(score) if score else "-",
                    diff=format_diff(candidate.duration - track.duration, signed=True)
                    if diff < 900
                    else "Δ?",
                    kind=kind,
                    title=candidate.title,
                    artist=candidate.artist_text,
                    album=candidate.album,
                    notes=notes,
                    metadata_score=metadata_score,
                    over_tolerance=bool(metadata_score) and not score,
                    score_value=score,
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
        pad_display("格式", w_kind),
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
        style(pad_display(row.kind, w_kind), "90"),
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


def candidate_note_lines(row: CandRow, prefix: str, last: bool) -> list[str]:
    """Explanations hang under their own candidate, not under the next one."""
    rail = "   " if last else "│  "
    lines = []
    for index, note in enumerate(row.notes):
        glyph = "└" if index == len(row.notes) - 1 else "├"
        lines.append(style(prefix + rail + f"{glyph} {note}", "90"))
    return lines


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
    sources = build_sources(args.sources, client)
    wanted = [track for track in tracks if args.all or not track.has_lyrics]

    if not args.tsv:
        print_header(
            "candidates",
            f"{len(wanted)} 首  ·  每源最多 {args.top} 条  ·  源 {','.join(args.sources)}",
        )
        if not wanted:
            print("  " + style("范围内所有曲目都已有歌词；加 --all 可强制列出。", "32"))
            print()
            return 0

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

    rows_by_track: dict[str, list[CandRow]] = {
        str(track.path): candidate_rows(track, results[str(track.path)], args)
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
    rows_by_track: dict[str, list[CandRow]] = {
        str(track.path): candidate_rows(track, results[str(track.path)], args)
        for track in wanted
    }
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
                for line in candidate_note_lines(row, prefix, last_row):
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
                f"lrcpatch plan{f' {scope_args}' if scope_args else ''} "
                f"--pick-file {shlex.quote(str(template_path))}",
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
