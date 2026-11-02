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
from .models import TreeEntry
from .text import write_text_atomic


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
    for album in sorted(albums):
        items = sorted(albums[album], key=lambda row: (Path(row["path"]).name, row["path"]))
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

_COLOR_ENABLED = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and os.environ.get("TERM", "") != "dumb"
)
# Notes never wrap by default: a folded remark is easy to mistake for the next
# track's child. ``--wrap`` opts back into window folding.
_WRAP_ENABLED = False


def set_wrap(enabled: bool) -> None:
    """--wrap: fold long notes at the window edge (default: let them overflow)."""
    global _WRAP_ENABLED
    _WRAP_ENABLED = bool(enabled)


def wrap_enabled() -> bool:
    return _WRAP_ENABLED

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

def wrap_display(text: str, limit: int) -> list[str]:
    """Wrap plain text by terminal columns, preferring spaces and slashes."""
    if limit <= 2:
        return [text]
    lines: list[str] = []
    remaining = text
    while remaining:
        if disp_width(remaining) <= limit:
            lines.append(remaining)
            break
        width = 0
        cut = 0
        for index, char in enumerate(remaining):
            step = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
            if width + step > limit:
                break
            width += step
            cut = index + 1
        window = remaining[:cut]
        for separator in (" ", "-", "/", "\\", "；", "，", "。", "、"):
            position = window.rfind(separator)
            if position >= limit // 2:
                cut = position + 1
                break
        if cut <= 0:
            cut = 1
        lines.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return lines or [""]

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

def print_tree(
    entries: list[TreeEntry],
    library: Path,
    limit: int = 200,
    totals: Counter | None = None,
) -> None:
    """Album-grouped tree with file names and notes in fixed columns.

    Every note starts at the same column, and wrapped lines indent to that
    column, so a long reason never looks like a child of the next track.
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
            sorted(groups[folder], key=lambda item: (item.path.name, str(item.path))),
        )
        for folder in sorted(groups, key=lambda item: str(item))
    ]
    name_cap = max(28, min(64, screen_width() - 34))
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

        names = [truncate_display(item.path.name, name_cap) for item in visible]
        name_width = max((disp_width(name) for name in names), default=0)
        for index, item in enumerate(visible):
            branch = "    └─ " if index == len(visible) - 1 else "    ├─ "
            symbol, code, _ = status_style(item.status)
            name_cell = pad_display(names[index], name_width)
            prefix = style(branch, "90") + style(symbol, code) + " "
            note_col = disp_width(prefix) + name_width + 2
            line = prefix + name_cell
            if item.note:
                note_code = NOTE_STYLE.get(item.status, "90")
                if _WRAP_ENABLED:
                    chunks = wrap_display(
                        item.note, max(40, screen_width() - note_col)
                    )
                else:
                    chunks = [item.note]
                line += "  " + style(chunks[0], note_code)
                print(line.rstrip())
                for chunk in chunks[1:]:
                    print(" " * note_col + style(chunk, note_code))
            else:
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
    status = row["status"]
    if status in {"ready", "applied"}:
        bits: list[str] = []
        if row["source"]:
            bits.append(str(row["source"]))
        if row["score"]:
            bits.append(f"{row['score']}分")
        diff = row["duration_diff"]
        if diff is not None and diff < 900:
            bits.append(f"Δ{diff:.1f}s")
        kind = str(row["lyric_kind"] or "").upper()
        if kind:
            bits.append(f"{kind} {row['lyric_lines']}行" if row["lyric_lines"] else kind)
        return " · ".join(bits) or "匹配通过"
    if status == "skip_existing":
        return "已有歌词，未覆盖"
    if status == "instrumental":
        return "匹配为纯音乐"
    return str(row["reason"] or status_style(status)[2])

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
        + style("lyrics", "1;36")
        + style(f"  {APP_VERSION}", "90")
        + "   批量歌词：匹配 → 写入 → 校验 → 回滚"
    )
    print("  " + rule())
    print("  只改歌词字段；其他标签、封面与音频数据不动。默认先 plan，不写文件。")
    print("  默认音乐库 ~/Music/JMusic；路径相对它解析，也可以直接拖入目录。")
    print()
    print("  " + style("常用", "1"))
    rows = [
        ('lyrics.py "結束バンド"', "拖入目录 = 只读 plan"),
        ('lyrics.py "Ado" --html', "生成计划与 HTML 复核页"),
        ('lyrics.py candidates "Ado"', "查看候选 ID（配合 --pick）"),
        ('lyrics.py apply "Ado"', "只写入 ✓ ready 的曲目"),
        ('lyrics.py verify "Ado"', "校验歌词与其他标签"),
        ('lyrics.py rollback "Ado" --yes', "回滚本工具写入的歌词"),
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
        + "  lyrics.py -h"
        + style("      单个命令", "90")
        + "  lyrics.py plan -h"
    )
    print()
