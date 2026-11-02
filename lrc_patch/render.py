"""Terminal rendering (ANSI, trees) plus CSV and HTML review reports."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import Counter
from html import escape as html_escape
from pathlib import Path

from .config import APP_VERSION
from .models import Track, TreeEntry
from .sorting import name_key, sort_paths
from .text import lyric_kind, lyric_line_count, lyric_tag, write_text_atomic


def default_report_path(state_dir: Path, prefix: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return state_dir / "reports" / f"{prefix}-{stamp}.csv"

def plan_html_path(state_dir: Path, stamp: str) -> Path:
    return state_dir / "reports" / f"plan-{stamp}.html"

def write_plan_report(rows: list[sqlite3.Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "path",
        "status",
        "reason",
        "source",
        "source_id",
        "match_title",
        "match_artist",
        "match_album",
        "match_duration",
        "duration_diff",
        "score",
        "lyric_kind",
        "lyric_lines",
        "lyric_sha",
        "planned_at",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))

HTML_STATUS = {
    "ready": ("可写入", "ok"),
    "applied": ("已写入", "ok"),
    "skip_existing": ("已有歌词", "skip"),
    "ambiguous": ("需确认", "warn"),
    "suspect": ("时间轴可疑", "suspect"),
    "no_match": ("无匹配", "miss"),
    "instrumental": ("纯音乐", "skip"),
    "error": ("失败", "bad"),
    "rolled_back": ("已回滚", "skip"),
}

def candidate_url(source: str | None, source_id: str | None) -> str:
    if not source or not source_id:
        return ""
    if source == "netease" and source_id.isdigit():
        return f"https://music.163.com/song?id={source_id}"
    return ""

def write_plan_html(rows: list[sqlite3.Row], path: Path, library: Path) -> None:
    """Album-grouped review page: scan, filter, then pick fixes for the rest."""
    counts = Counter(row["status"] for row in rows)
    albums: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        try:
            parent = str(Path(row["path"]).parent.relative_to(library))
        except ValueError:
            parent = "_external"
        albums.setdefault(parent, []).append(row)

    chips = []
    for status, (label, _) in HTML_STATUS.items():
        if counts.get(status):
            chips.append(
                f'<button data-filter="{status}">{html_escape(label)} '
                f'<b>{counts[status]}</b></button>'
            )
    chips.insert(0, '<button data-filter="all" class="active">全部 <b>%d</b></button>' % len(rows))
    chips.append('<button data-filter="review">待处理 <b>%d</b></button>' % (
        counts.get("ambiguous", 0)
        + counts.get("suspect", 0)
        + counts.get("no_match", 0)
        + counts.get("error", 0)
    ))

    sections = []
    for album in sorted(albums, key=name_key):
        items = sorted(
            albums[album], key=lambda row: name_key(Path(row["path"]).name)
        )
        album_counts = Counter(row["status"] for row in items)
        summary = " · ".join(
            f"{HTML_STATUS.get(status, (status, ''))[0]} {count}"
            for status, count in album_counts.items()
        )
        body = []
        for row in items:
            status = row["status"]
            label, tone = HTML_STATUS.get(status, (status, ""))
            title = row["match_title"] or ""
            artist = row["match_artist"] or ""
            match = html_escape(title)
            if artist:
                match += f'<span class="artist">{html_escape(artist)}</span>'
            if not match:
                match = '<span class="muted">—</span>'
            diff = row["duration_diff"]
            diff_text = f"{diff:.1f}s" if diff is not None else "—"
            score = row["score"] or 0
            source = row["source"] or ""
            source_id = row["source_id"] or ""
            link = candidate_url(source, source_id)
            source_cell = html_escape(f"{source}:{source_id}" if source_id else source)
            if link:
                source_cell = (
                    f'<a href="{html_escape(link)}" target="_blank" rel="noreferrer">'
                    f"{source_cell}</a>"
                )
            body.append(
                f'<tr data-status="{html_escape(status)}">'
                f'<td><span class="badge {tone}">{html_escape(label)}</span></td>'
                f'<td class="file">{html_escape(Path(row["path"]).name)}</td>'
                f'<td>{match}</td>'
                f'<td class="num">{html_escape(diff_text)}</td>'
                f'<td class="num">{score if score else "—"}</td>'
                f'<td class="src">{source_cell}</td>'
                f'<td class="reason">{html_escape(row["reason"] or "")}</td>'
                "</tr>"
            )
        sections.append(
            f'<section class="album" data-album="{html_escape(album)}">'
            f"<h2>{html_escape(album)}<span>{html_escape(summary)}</span></h2>"
            '<table><thead><tr><th>状态</th><th>文件</th><th>匹配</th>'
            "<th>Δ时长</th><th>分数</th><th>来源</th><th>说明</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></section>"
        )

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>歌词计划复核 · {html_escape(library.name)}</title>
<style>
  :root {{
    --bg: #f6f6f4; --panel: #fff; --text: #1c1c1a; --muted: #6b6b66;
    --line: #e4e3df; --ok: #1f7a4d; --okbg: #e3f3ea;
    --warn: #8a5a00; --warnbg: #fdf0d5; --suspect: #9a4b00; --suspectbg: #fde7d3;
    --miss: #6b6b66; --missbg: #ecece9; --bad: #a12828; --badbg: #fbe3e3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #171715; --panel: #1f1f1c; --text: #ecece7; --muted: #a3a39c;
      --line: #35352f; --ok: #7fd3a6; --okbg: #1e3a2c;
      --warn: #f0c26a; --warnbg: #3d3118; --suspect: #f2a86b; --suspectbg: #3d2718;
      --miss: #a3a39c; --missbg: #2b2b27; --bad: #f19a9a; --badbg: #3d1f1f;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 28px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "SF Pro Text", "Helvetica Neue", "Hiragino Sans", sans-serif; }}
  header {{ max-width: 1180px; margin: 0 auto 18px; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  .meta {{ color: var(--muted); font-size: 12px; }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 20px; }}
  .toolbar button, .toolbar input {{ font: inherit; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); border-radius: 999px; padding: 6px 12px;
    cursor: pointer; }}
  .toolbar button.active {{ border-color: var(--text); }}
  .toolbar input {{ border-radius: 8px; min-width: 240px; cursor: text; }}
  section.album {{ max-width: 1180px; margin: 0 auto 18px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px; overflow: auto; }}
  section.album h2 {{ font-size: 14px; margin: 0; padding: 12px 16px;
    border-bottom: 1px solid var(--line); display: flex; gap: 12px;
    justify-content: space-between; flex-wrap: wrap; }}
  section.album h2 span {{ color: var(--muted); font-weight: 400; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line);
    vertical-align: top; font-size: 13px; }}
  th {{ color: var(--muted); font-weight: 500; font-size: 11px; letter-spacing: .04em;
    text-transform: uppercase; }}
  tr:last-child td {{ border-bottom: 0; }}
  .file {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }}
  .artist {{ display: block; color: var(--muted); font-size: 12px; }}
  .num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .src {{ font-size: 12px; overflow-wrap: anywhere; }}
  .reason {{ color: var(--muted); font-size: 12px; max-width: 360px; }}
  .muted {{ color: var(--muted); }}
  a {{ color: inherit; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; white-space: nowrap; }}
  .badge.ok {{ color: var(--ok); background: var(--okbg); }}
  .badge.warn {{ color: var(--warn); background: var(--warnbg); }}
  .badge.suspect {{ color: var(--suspect); background: var(--suspectbg); }}
  .badge.miss, .badge.skip {{ color: var(--miss); background: var(--missbg); }}
  .badge.bad {{ color: var(--bad); background: var(--badbg); }}
  tr.hidden, section.hidden {{ display: none; }}
</style>
</head>
<body>
<header>
  <h1>歌词计划复核</h1>
  <div class="meta">{html_escape(str(library))} · 生成于 {html_escape(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))} · 共 {len(rows)} 首</div>
  <div class="toolbar">
    {''.join(chips)}
    <input id="q" type="search" placeholder="筛选文件名 / 曲名 / 艺术家 / 说明">
  </div>
</header>
{''.join(sections)}
<script>
const buttons = Array.from(document.querySelectorAll('[data-filter]'));
const rows = Array.from(document.querySelectorAll('tr[data-status]'));
const albums = Array.from(document.querySelectorAll('section.album'));
const query = document.getElementById('q');
let filter = 'all';
function apply() {{
  const q = query.value.trim().toLowerCase();
  rows.forEach(row => {{
    const status = row.dataset.status;
    const matchFilter = filter === 'all'
      || (filter === 'review' ? ['ambiguous','suspect','no_match','error'].includes(status) : status === filter);
    const matchText = !q || row.textContent.toLowerCase().includes(q);
    row.classList.toggle('hidden', !(matchFilter && matchText));
  }});
  albums.forEach(album => {{
    const visible = album.querySelectorAll('tr[data-status]:not(.hidden)').length;
    album.classList.toggle('hidden', visible === 0);
  }});
}}
buttons.forEach(button => button.addEventListener('click', () => {{
  filter = button.dataset.filter;
  buttons.forEach(item => item.classList.toggle('active', item === button));
  apply();
}}));
query.addEventListener('input', apply);
</script>
</body>
</html>
"""
    write_text_atomic(path, document)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
# "LRC 42行" / "LRC 6 行": the count is right-aligned per album group.
_NOTE_LINES_RE = re.compile(r"(LRC\+?|YRC|TTML|TXT) (\d+)( ?行)")

_COLOR_ENABLED = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and os.environ.get("TERM", "") != "dumb"
)


def set_color(enabled: bool) -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = (
        bool(enabled)
        and sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )

def style(text: str, code: str) -> str:
    if not _COLOR_ENABLED or not text:
        return text
    return f"\033[{code}m{text}\033[0m"

def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

def disp_width(text: str) -> int:
    """Terminal columns, ignoring ANSI escapes and counting CJK as two."""
    width = 0
    for char in strip_ansi(text):
        width += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
    return width

def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))

def truncate_display(text: str, limit: int) -> str:
    if limit <= 1 or disp_width(text) <= limit:
        return text
    out: list[str] = []
    width = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        if width + step > limit - 1:
            break
        out.append(char)
        width += step
    return "".join(out) + "…"

def screen_width() -> int:
    try:
        return max(64, shutil.get_terminal_size((100, 24)).columns)
    except OSError:
        return 100

def rule() -> str:
    return style("─" * min(screen_width() - 4, 76), "90")

def print_header(title: str, subtitle: str = "") -> None:
    print()
    head = style(title, "1;36")
    if subtitle:
        head += style("  " + subtitle, "90")
    print("  " + head)
    print("  " + rule())

def print_meta(label: str, value: str) -> None:
    print("  " + style(label, "90") + "  " + value)

def selection_label(args: argparse.Namespace) -> str:
    if getattr(args, "path", None):
        return " · ".join(args.path)
    album = getattr(args, "album", None)
    return f"专辑 {album}" if album else "整个音乐库"

def short_path(path: Path, library: Path | None = None) -> str:
    try:
        path = path.resolve()
    except OSError:
        pass
    if library is not None:
        try:
            rel = path.relative_to(library.resolve())
            return str(rel) if str(rel) != "." else "（音乐库根目录）"
        except (ValueError, OSError):
            pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)

STATUS_STYLE: dict[str, tuple[str, str, str]] = {
    "ready": ("✓", "1;32", "可写入"),
    "applied": ("✓", "1;32", "已写入"),
    "ok": ("✓", "1;32", "通过"),
    "skip_existing": ("·", "90", "已有歌词"),
    "instrumental": ("♪", "36", "纯音乐"),
    "ambiguous": ("!", "1;33", "需确认"),
    "suspect": ("!", "1;35", "时间轴可疑"),
    "no_match": ("✗", "1;31", "无匹配"),
    "error": ("✗", "1;31", "失败"),
    "missing": ("✗", "1;31", "文件丢失"),
    "changed": ("!", "1;33", "已变化"),
    "conflict": ("!", "1;33", "拒绝回滚"),
    "rolled_back": ("↩", "36", "已回滚"),
    "pending": ("↩", "36", "待移除"),
}

def status_style(status: str) -> tuple[str, str, str]:
    return STATUS_STYLE.get(status, ("•", "0", status))

CHIP_GROUPS = [
    (("ready", "applied", "ok"), "✓", "1;32"),
    (("ambiguous", "suspect", "changed", "conflict"), "!", "1;33"),
    (("no_match", "error", "missing"), "✗", "1;31"),
    (("rolled_back", "pending"), "↩", "36"),
    (("skip_existing", "instrumental"), "·", "90"),
]

NOTE_STYLE = {
    "ready": "90",
    "applied": "90",
    "ok": "90",
    "skip_existing": "90",
    "instrumental": "36",
    "ambiguous": "33",
    "suspect": "35",
    "no_match": "31",
    "error": "31",
    "missing": "31",
    "changed": "33",
    "conflict": "33",
    "rolled_back": "36",
    "pending": "36",
}

def align_note_numbers(notes: list[str]) -> list[str]:
    """Right-align ``LRC 6行`` under ``LRC 23行`` within one album group."""
    widths: dict[str, int] = {}
    for note in notes:
        for match in _NOTE_LINES_RE.finditer(note):
            tag = match.group(1)
            widths[tag] = max(widths.get(tag, 0), len(match.group(2)))
    if not widths:
        return notes
    aligned: list[str] = []
    for note in notes:
        def replace(match: re.Match) -> str:
            tag, number, suffix = match.group(1), match.group(2), match.group(3)
            return f"{tag} {number.rjust(widths[tag])}{suffix}"

        aligned.append(_NOTE_LINES_RE.sub(replace, note))
    return aligned


def print_tree(
    entries: list[TreeEntry],
    library: Path,
    limit: int = 200,
    totals: Counter | None = None,
) -> None:
    """Album-grouped tree with file names and notes in fixed columns.

    Every note starts at the same column.  Notes are printed on one line and
    never wrapped: the terminal cannot scroll sideways, but the window can be
    widened, and a self-chosen width beats a judgement call made here.
    """
    if limit == 0:
        print("  " + style("明细已按 --tree-limit 0 关闭。", "90"))
        print()
        return
    if not entries:
        print("  " + style("没有可显示的明细。", "90"))
        return
    groups: dict[Path, list[TreeEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.path.parent, []).append(entry)
    plan = [
        (
            folder,
            sorted(groups[folder], key=lambda item: name_key(item.path.name)),
        )
        for folder in sort_paths(groups, library)
    ]
    shown = 0
    for folder, items in plan:
        room = len(items) if not limit else limit - shown
        if room <= 0:
            break
        visible = items[:room]
        counts = Counter(item.status for item in items)
        chips: list[str] = []
        for statuses, symbol, code in CHIP_GROUPS:
            total = sum(counts.get(status, 0) for status in statuses)
            if total:
                chips.append(style(f"{symbol}{total}", code))
        header = "  " + style("▸ ", "36") + style(short_path(folder, library), "1;36")
        total_tracks = totals.get(folder, len(items)) if totals else len(items)
        count_text = (
            f"{len(items)}/{total_tracks} 首"
            if total_tracks > len(items)
            else f"{len(items)} 首"
        )
        header += style(f"   {count_text}", "90")
        if chips:
            header += "  " + "  ".join(chips)
        print(header)

        names = [item.path.name for item in visible]
        name_width = max((disp_width(name) for name in names), default=0)
        notes = align_note_numbers([item.note for item in visible])
        for index, item in enumerate(visible):
            branch = "    └─ " if index == len(visible) - 1 else "    ├─ "
            symbol, code, _ = status_style(item.status)
            name_cell = pad_display(names[index], name_width)
            prefix = style(branch, "90") + style(symbol, code) + " "
            line = prefix + name_cell
            note = notes[index]
            if note:
                note_code = NOTE_STYLE.get(item.status, "90")
                line += "  " + style(note, note_code)
            print(line.rstrip())
            shown += 1
        print()
    omitted = len(entries) - shown
    if omitted > 0:
        print(
            "  "
            + style(
                f"… 另有 {omitted} 行未展开；--tree-limit 可调，或查看 HTML 复核页。",
                "33",
            )
        )
        print()


def format_diff(seconds: float, signed: bool = False) -> str:
    """Duration gap: ``Δ1.3s``, or ``Δ+1.3s`` when the sign matters.

    A positive sign means the local file is longer than the provider's copy,
    which is what tells a different master apart from parser rounding.
    """
    value = float(seconds or 0.0)
    if abs(value) < 0.05:
        value = 0.0  # avoid "-0.0s" from a rounding artefact
    if signed:
        return f"Δ{value:+.1f}s"
    return f"Δ{abs(value):.1f}s"


def format_duration(seconds: float) -> str:
    """mm:ss, or h:mm:ss for the rare album-length track."""
    total = int(round(max(0.0, float(seconds or 0.0))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


LYRIC_COLORS = {
    "plain": "36",
    "lrc": "32",
    "enhanced": "1;32",
    "yrc": "1;34",
    "ttml": "1;35",
}


def lyric_summary(text: str, number_width: int = 0) -> tuple[str, str]:
    """(kind, label) for the scan column; the kind decides the colour.

    ``number_width`` right-aligns the line count inside a group, so ``LRC 6 行``
    and ``LRC 23 行`` keep the 行 column aligned instead of drifting.
    """
    if not (text or "").strip():
        return "", "无歌词"
    kind = lyric_kind(text)
    tag = lyric_tag(kind)
    lines = lyric_line_count(text, kind)
    if not lines:
        return kind, tag
    return kind, f"{tag} {str(lines).rjust(number_width)} 行"


def tag_gaps(track: Track) -> str:
    """Missing title/artist.  Album is intentionally ignored: singles in this
    library never carry one, so flagging it only produced noise."""
    missing = [
        name
        for name, value in (("标题", track.title), ("艺术家", track.artist))
        if not (value or "").strip()
    ]
    return "缺 " + "、".join(missing) if missing else ""


def _lyric_counts(with_lyrics: int, missing: int) -> str:
    parts = []
    if with_lyrics:
        parts.append(style(f"✓{with_lyrics}", "1;32"))
    if missing:
        parts.append(style(f"·{missing}", "90"))
    return "  " + "  ".join(parts) if parts else ""


def print_scan_list(tracks: list[Track], library: Path, limit: int = 0) -> None:
    """Per-file scan detail, one row per track, grouped by album folder.

    Columns are fixed across the whole listing rather than per folder, so
    format/duration/size/lyrics line up all the way down the page.  Folder and
    file order follows Finder, and the lyric line count is right-aligned inside
    each album, so ``LRC  6 行`` never drifts away from ``LRC 23 行``.
    """
    if not tracks:
        print("  " + style("没有可列出的音频文件。", "90"))
        print()
        return

    groups: dict[Path, list[Track]] = {}
    for track in tracks:
        groups.setdefault(track.path.parent, []).append(track)
    folders = sort_paths(groups, library)

    name_cap = max(28, min(56, screen_width() - 52))
    names = {id(track): truncate_display(track.path.name, name_cap) for track in tracks}
    name_width = max(disp_width(name) for name in names.values())

    kind_of: dict[int, str] = {}
    count_of: dict[int, int] = {}
    tag_of: dict[int, str] = {}
    for track in tracks:
        kind, _ = lyric_summary(track.existing_lyrics, 0)
        kind_of[id(track)] = kind
        tag_of[id(track)] = lyric_tag(kind) if kind else "无歌词"
        count_of[id(track)] = lyric_line_count(track.existing_lyrics, kind) if kind else 0

    digits_all = max((len(str(value)) for value in count_of.values()), default=0)
    lyric_width = max(
        6,
        max(
            disp_width(f"{tag_of[id(track)]} {str(count_of[id(track)]).rjust(digits_all)} 行")
            if kind_of[id(track)]
            else disp_width("无歌词")
            for track in tracks
        ),
    )
    formats = {id(track): track.fmt.upper() for track in tracks}
    durations = {id(track): format_duration(track.duration) for track in tracks}
    sizes = {id(track): format_size(track.size) for track in tracks}
    gaps = {id(track): tag_gaps(track) for track in tracks}
    fmt_width = max(3, max(len(value) for value in formats.values()))
    dur_width = max(5, max(len(value) for value in durations.values()))
    size_width = max(8, max(len(value) for value in sizes.values()))

    with_lyrics = sum(1 for track in tracks if track.has_lyrics)
    print(
        "  "
        + style("▸ ", "36")
        + style("明细", "1;36")
        + style(f"   {len(tracks)} 首", "90")
        + _lyric_counts(with_lyrics, len(tracks) - with_lyrics)
    )
    print()

    shown = 0
    for folder in folders:
        if limit and shown >= limit:
            break
        items = sorted(groups[folder], key=lambda track: name_key(track.path.name))
        room = len(items) if not limit else max(0, limit - shown)
        visible = items[:room]
        here = sum(1 for track in items if track.has_lyrics)
        header = (
            "  "
            + style("▸ ", "36")
            + style(short_path(folder, library), "1;36")
            + style(f"   {len(items)} 首", "90")
            + _lyric_counts(here, len(items) - here)
        )
        print(header)

        # Align the line count against the widest one in this album only.
        group_digits = max((len(str(count_of[id(track)])) for track in items), default=0)
        for index, track in enumerate(visible):
            branch = "    └─ " if index == len(visible) - 1 else "    ├─ "
            symbol, code = ("✓", "1;32") if track.has_lyrics else ("·", "90")
            kind = kind_of[id(track)]
            if kind:
                lyric = f"{tag_of[id(track)]} {str(count_of[id(track)]).rjust(group_digits)} 行"
                tone = LYRIC_COLORS.get(kind, "32")
            else:
                lyric = "无歌词"
                tone = "90"
            line = (
                style(branch, "90")
                + style(symbol, code)
                + " "
                + pad_display(names[id(track)], name_width)
                + "  "
                + style(formats[id(track)].ljust(fmt_width), "90")
                + "  "
                + style(durations[id(track)].rjust(dur_width), "90")
                + "  "
                + style(sizes[id(track)].rjust(size_width), "90")
                + "  "
                + style(pad_display(lyric, lyric_width), tone)
            )
            if gaps[id(track)]:
                line += "  " + style(gaps[id(track)], "33")
            print(line.rstrip())
        print()
        shown += len(visible)

    if limit and len(tracks) > shown:
        print(
            "  "
            + style(
                f"… 另有 {len(tracks) - shown} 首未列出；--tree-limit 0 可列出全部。",
                "33",
            )
        )
        print()


class ProgressLine:
    """Animated status line; silent when its stream is not a TTY.

    ``stream`` lets a command keep machine-readable output on stdout (TSV) while
    still showing progress on stderr.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout
        self.enabled = (
            self._stream.isatty() and os.environ.get("TERM", "") != "dumb"
        )
        self._visible = False
        self._text = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = time.monotonic()
        self._frame = 0

    def start(self, text: str, started: float | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._text = text
            self._started = started if started is not None else time.monotonic()
            self._render()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._render()

    def update(self, text: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._text = text
            self._render()

    def _render(self) -> None:
        if self._stop.is_set():
            return
        frame = self.FRAMES[self._frame % len(self.FRAMES)]
        self._frame += 1
        elapsed = time.monotonic() - self._started
        line = (
            "\r  "
            + style(frame, "36")
            + " "
            + self._text
            + style(f"  ·  {elapsed:.0f}s", "90")
        )
        self._stream.write(line + "\033[K")
        self._stream.flush()
        self._visible = True

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=1)
            self._thread = None
        if self.enabled and self._visible:
            with self._lock:
                self._stream.write("\r\033[K")
                self._stream.flush()
                self._visible = False

def plan_note(row: sqlite3.Row) -> str:
    """One line: 源 分数 时长差 格式行数 歌手；差异说明."""
    status = row["status"]
    if status in {"ready", "applied"}:
        bits: list[str] = []
        if row["source"]:
            bits.append(str(row["source"]))
        if row["score"]:
            bits.append(f"{row['score']}分")
        diff = row["duration_diff"]
        if diff is not None and diff < 900:
            bits.append(format_diff(diff))
        kind = str(row["lyric_kind"] or "")
        if kind:
            tag = lyric_tag(kind)
            bits.append(f"{tag} {row['lyric_lines']} 行" if row["lyric_lines"] else tag)
        artist, notes = _reason_parts(str(row["reason"] or ""))
        if artist:
            bits.append(artist)
        line = "  ".join(part for part in bits if part)
        if notes:
            line = f"{line}；{notes}" if line else notes
        return line or "匹配通过"
    if status == "skip_existing":
        return "已有歌词，未覆盖"
    if status == "instrumental":
        return "匹配为纯音乐"
    return str(row["reason"] or status_style(status)[2])


_LEGACY_REASON_RE = re.compile(r"^(?:TXT|LRC\+?|YRC|TTML)\s*/\s*(.+)$")


def _reason_parts(reason: str) -> tuple[str, str]:
    """Split a stored reason into (artist, explanation).

    Ready rows store ``歌手；差异说明`` since 2.5.  Older rows used
    ``LRC / 歌手 · 说明``; those are still understood so an upgrade does not
    render the lyric tag as if it were an artist.
    """
    artist, sep, notes = reason.partition("；")
    if sep:
        return artist.strip(), notes.strip()
    if reason.startswith("人工指定"):
        # A hand-picked candidate has no artist field; keep the whole label
        # in the explanation slot instead of faking an artist.
        return "", reason.strip()
    head, dot, tail = reason.partition(" · ")
    legacy = _LEGACY_REASON_RE.match(head.strip())
    if legacy:
        return legacy.group(1).strip(), tail.strip() if dot else ""
    return head.strip(), tail.strip() if dot else ""

def print_summary(
    counter: Counter, label: str = "汇总", order: list[str] | None = None
) -> None:
    order = order or [
        "ready",
        "applied",
        "ambiguous",
        "suspect",
        "no_match",
        "error",
        "ok",
        "changed",
        "missing",
        "conflict",
        "rolled_back",
        "pending",
        "skip_existing",
        "instrumental",
    ]
    parts: list[str] = []
    for key in order:
        value = counter.get(key, 0)
        if value:
            symbol, code, name = status_style(key)
            parts.append(style(f"{symbol} {name}", code) + " " + str(value))
    for key, value in counter.items():
        if key not in order and value:
            parts.append(f"{key} {value}")
    if not parts:
        print("  " + style(label, "90") + "  " + style("无", "90"))
        return
    print("  " + style(label, "90") + "  " + "   ".join(parts))

def print_intro() -> None:
    print()
    print(
        "  "
        + style("lrcpatch", "1;36")
        + style(f" {APP_VERSION}", "90")
        + "   批量歌词：匹配 → 写入 → 校验 → 回滚"
    )
    print("  " + rule())
    print("  只改歌词字段；其他标签、封面与音频数据不动。默认先 plan，不写文件。")
    print("  默认音乐库 ~/Music/JMusic；路径相对它解析，也可以直接拖入目录。")
    print()
    print("  " + style("常用", "1"))
    rows = [
        ('lrcpatch plan "結束バンド"', "拖入目录 = 只读 plan"),
        ('lrcpatch plan "Ado" --html', "生成计划与 HTML 复核页"),
        ('lrcpatch candidates "Ado"', "查看候选 ID（配合 --pick）"),
        ('lrcpatch apply "Ado"', "只写入 ✓ ready 的曲目"),
        ('lrcpatch verify "Ado"', "校验歌词与其他标签"),
        ('lrcpatch rollback "Ado" --yes', "回滚本工具写入的歌词"),
    ]
    width = max(disp_width(command) for command, _ in rows)
    for command, note in rows:
        gap = " " * (width - disp_width(command) + 2)
        print("    " + style(command, "36") + gap + style(note, "90"))
    print()
    print(
        "  "
        + style("命令", "1")
        + "  "
        + "  ".join(
            style(name, "36")
            for name in ("scan", "plan", "candidates", "apply", "verify", "rollback")
        )
    )
    print(
        "  "
        + style("详细参数", "90")
        + "  lrcpatch -h"
        + style("      单个命令", "90")
        + "  lrcpatch plan -h"
    )
    print()
