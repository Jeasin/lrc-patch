"""Argument surface: help text, parser construction, argv normalisation."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import APP_VERSION, DEFAULT_DURATION_TOLERANCE, DEFAULT_LIBRARY


COMMAND_NAMES = {"scan", "plan", "apply", "verify", "rollback", "candidates"}

MAIN_EPILOG = """\
依赖
  Python 3.9+，唯一第三方包是 mutagen，已用 PEP 723 写在文件头。
  直接运行 lyrics.py 时 uv 会自动把它装进自己的缓存，不需要手动 pip。
  sqlite3、urllib、csv 等全部是标准库，也不需要 sqlite 命令行工具。
  没有 uv 也能跑：python3 lyrics.py 会自动切换到装有 mutagen 的解释器。

用法
  lyrics.py [命令] [选项] [路径 ...]
  命令可省略（默认只读 plan）；选项和路径顺序不限；路径可以一次拖入多个。
  路径是文件或目录，目录递归；相对路径按音乐库根目录解析。

示例
  lyrics.py "/Users/jeasinl/Music/JMusic/結束バンド"
  lyrics.py "/Music/Ado" "/Music/YOASOBI/(2023)THE BOOK 3" --workers 2
  lyrics.py --workers 2 "/Music/Ado"           选项写在路径前也可以
  lyrics.py plan "/Music/Ado" --html
  lyrics.py candidates "/Music/Ado" --top 3
  lyrics.py "/Music/Ado/某首.m4a" --pick netease:123
  lyrics.py apply "/Music/Ado"
  lyrics.py verify "/Music/Ado"
  lyrics.py rollback "/Music/Ado"          只预览
  lyrics.py rollback "/Music/Ado" --yes    执行
  lyrics.py plan "/Music/Ado" --lrc        额外在缓存目录留 .lrc

范围
  路径 ...                文件或目录；可多个；目录递归；相对路径按音乐库解析
  --library DIR           音乐库根目录，默认 ~/Music/JMusic
  --album DIR             只选单个专辑目录（旧参数，建议直接拖路径）
  --limit N               最多处理 N 首，用于试跑
  --state-dir DIR         状态/缓存/报告目录，默认 ~/.local/state/lrc-patch

输出
  --tree-limit N          终端树状明细最多 N 行，默认 200；0 表示不展开
  --no-wrap               备注不换行，超出窗口就横向溢出（便于复制/审查）
  --no-color              关闭颜色（也可设 NO_COLOR=1）
  --lrc                   同时把歌词保存为 .lrc（plan/apply）
  --lrc-dir DIR           .lrc 目录，默认 ~/.cache/lrc-patch/lrc

plan 专用
  --sources LIST          源顺序，默认 netease,lrclib；也可只写一个
  --workers N             查询并发 1..10，默认 4
  --no-plain              只接受带时间轴的 LRC
  --duration-tolerance S  时长容差，默认 2 秒
  --no-timestamp-check    不检查 LRC 末尾时间轴是否贴合音频时长
  --pick SOURCE:ID        人工指定候选；要求范围内恰好一首缺歌词的曲目
  --pick-file FILE        批量指定：每行 曲目路径<TAB>SOURCE:ID
  --html [FILE]           生成按专辑分组的复核页，省略文件名时写入 reports/
  --html-file FILE        指定复核页路径（等价于 --html FILE）
  --refresh               忽略 HTTP 缓存重查
  --retry-failed          重查 no_match/ambiguous/suspect；error 每次都会重试
  --report FILE           CSV 报告路径
  --progress-every N      每 N 首打印一次进度

状态与下一步
  ✓ 可写入   ! 需确认   ! 时间轴可疑   ✗ 无匹配/失败   · 已有歌词   ♪ 纯音乐
  apply 只写 ✓ ready；! 与 ✗ 都不会写入，但会在 apply 输出里列出数量。
  待确认：先 lyrics.py candidates <路径> 查 SOURCE:ID，再加 --pick 重新 plan。
  请求失败：不是“无匹配”，下次运行同一 plan 会自动重试，不会覆盖已有 ready。
"""

PLAN_EPILOG = """\
示例
  lyrics.py plan "結束バンド"
  lyrics.py "Ado" "YOASOBI/(2023)THE BOOK 3" --workers 2
  lyrics.py "米津玄師" --lrc --lrc-dir ~/.cache/lrc-patch/lrc
  lyrics.py "結束バンド" --html
  lyrics.py "結束バンド/11 忘れてやらない.m4a" --pick netease:2003496467

plan 只读：不写音频，只更新状态数据库、CSV 和可选的 HTML 复核页。
ambiguous / suspect 不会自动写入；先看 HTML，再用 --pick 逐首确认。
"""

APPLY_EPILOG = """\
示例
  lyrics.py apply "結束バンド"
  lyrics.py apply "Ado" --workers 2

只写状态为 ready 的曲目；已有歌词、需确认、无匹配、请求失败都不会写入。
开始前会检查目标目录可写且空间足够；确实需要跳过时加 --no-preflight。
"""

VERIFY_EPILOG = """\
示例
  lyrics.py verify "結束バンド"

逐首比对歌词、其他标签、封面和音频数据与写入时的记录。
"""

ROLLBACK_EPILOG = """\
示例
  lyrics.py rollback "結束バンド"         只预览，不改文件
  lyrics.py rollback "結束バンド" --yes   执行移除

只处理 journal 中由本工具写入、且歌词之后没有被改动过的曲目。
"""


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--library",
        type=Path,
        default=DEFAULT_LIBRARY,
        metavar="DIR",
        help=f"音乐库根目录（默认 {DEFAULT_LIBRARY}）",
    )
    parser.add_argument(
        "--album",
        metavar="DIR",
        help="只处理某个专辑目录（绝对路径或相对音乐库）；建议直接拖路径",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="最多处理 N 首，用于试跑")
    parser.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        help="状态、缓存与报告目录（默认 $XDG_STATE_HOME/lrc-patch）",
    )
    parser.add_argument(
        "--tree-limit",
        type=int,
        default=200,
        metavar="N",
        help="终端树状明细最多展开 N 行；0 表示不展开（默认 200）",
    )
    parser.add_argument(
        "--no-wrap",
        action="store_true",
        help="备注不按窗口换行，超宽时直接溢出（便于横向审查）",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出（也可设 NO_COLOR=1）",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="路径",
        help="文件或目录；可拖入多个；目录递归；相对路径按音乐库解析",
    )


def add_lrc_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lrc",
        action="store_true",
        help="同时把歌词保存为 .lrc 文件",
    )
    parser.add_argument(
        "--lrc-dir",
        type=Path,
        metavar="DIR",
        help=".lrc 输出目录（默认 $XDG_CACHE_HOME/lrc-patch/lrc）",
    )


def add_help_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyrics.py",
        usage="lyrics.py [命令] [选项] [路径 ...]",
        description="批量歌词：匹配 → 写入 → 校验 → 回滚（只改歌词字段）",
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(parser)
    parser.add_argument(
        "--version", action="version", version=APP_VERSION, help="显示版本并退出"
    )
    sub = parser.add_subparsers(
        dest="command", required=True, title="命令", metavar="<命令>"
    )

    scan = sub.add_parser(
        "scan",
        prog="lyrics.py scan",
        help="只读扫描格式和已有歌词",
        description="只读扫描：统计格式、数量与已有歌词，不修改任何文件。",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例\n"
            '  lyrics.py scan "結束バンド"\n'
            '  lyrics.py scan --limit 20\n\n'
            "只读；可以和其他命令同时运行（不打开状态数据库）。"
        ),
    )
    add_help_option(scan)
    add_common(scan)

    plan = sub.add_parser(
        "plan",
        prog="lyrics.py plan",
        help="生成计划，不修改音频",
        description="匹配歌词并写入状态数据库和 CSV；不修改音频。",
        epilog=PLAN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(plan)
    add_common(plan)
    add_lrc_options(plan)
    plan.add_argument(
        "--sources",
        metavar="LIST",
        default="netease,lrclib",
        help="歌词源顺序，逗号分隔：netease,lrclib（默认）",
    )
    plan.add_argument("--workers", type=int, metavar="N", default=4, help="查询并发数，默认 4")
    plan.add_argument("--no-plain", action="store_true", help="只接受带时间轴的 LRC")
    plan.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    plan.add_argument(
        "--retry-failed",
        action="store_true",
        help="重查 no_match/ambiguous/suspect；请求失败（error）默认每次重试",
    )
    plan.add_argument("--report", type=Path, metavar="FILE", help="CSV 报告路径")
    plan.add_argument(
        "--pick",
        action="append",
        metavar="SOURCE:ID",
        help="人工指定候选，要求范围内恰好一首缺歌词的曲目；可写 netease:123",
    )
    plan.add_argument(
        "--pick-file",
        type=Path,
        metavar="FILE",
        help="批量人工指定：每行 曲目路径<TAB>SOURCE:ID，# 开头为注释",
    )
    plan.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"时长容差秒数，默认 {DEFAULT_DURATION_TOLERANCE:g}",
    )
    plan.add_argument(
        "--no-timestamp-check",
        action="store_true",
        help="不检查 LRC 末尾时间轴与音频时长是否相符",
    )
    plan.add_argument(
        "--html",
        nargs="?",
        const=True,
        metavar="FILE",
        help="生成按专辑分组的 HTML 复核页；省略文件名时写入 reports/",
    )
    plan.add_argument(
        "--html-file",
        type=Path,
        metavar="FILE",
        help="指定 HTML 复核页路径（等价于 --html FILE）",
    )
    plan.add_argument(
        "--progress-every", type=int, metavar="N", default=25, help="每 N 首打印进度"
    )

    candidates = sub.add_parser(
        "candidates",
        prog="lyrics.py candidates",
        help="列出候选 ID，供 --pick 使用（只读）",
        description="逐首查询并列出候选（含 ID、分数、时长差），不修改音频和计划。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        epilog=(
            "示例\n"
            '  lyrics.py candidates "結束バンド" --top 3\n'
            '  lyrics.py candidates "Ado" --tsv > picks.tsv\n\n'
            "输出中的 SOURCE:ID 可直接给 plan --pick，或整理成 --pick-file。"
        ),
    )
    add_help_option(candidates)
    add_common(candidates)
    candidates.add_argument(
        "--sources",
        metavar="LIST",
        default="netease,lrclib",
        help="歌词源顺序，逗号分隔（默认 netease,lrclib）",
    )
    candidates.add_argument("--top", type=int, metavar="N", default=3, help="每源最多显示 N 条")
    candidates.add_argument("--all", action="store_true", help="连已有歌词的曲目一起列出")
    candidates.add_argument("--tsv", action="store_true", help="输出制表符分隔，便于做 pick-file")
    candidates.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    candidates.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"排序用时长容差，默认 {DEFAULT_DURATION_TOLERANCE:g}",
    )

    apply_cmd = sub.add_parser(
        "apply",
        prog="lyrics.py apply",
        help="执行 ready 计划并逐文件校验",
        description="把 ready 的歌词写入音频；先写临时副本，校验通过后替换原文件。",
        epilog=APPLY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(apply_cmd)
    add_common(apply_cmd)
    add_lrc_options(apply_cmd)
    apply_cmd.add_argument(
        "--workers", type=int, metavar="N", default=4, help="写入并发数，默认 4"
    )
    apply_cmd.add_argument("--report", type=Path, metavar="FILE", help="CSV 报告路径")
    apply_cmd.add_argument(
        "--no-preflight",
        action="store_true",
        help="跳过写入前的目录权限与空间检查",
    )
    apply_cmd.add_argument(
        "--progress-every", type=int, metavar="N", default=10, help="每 N 首打印进度"
    )

    verify = sub.add_parser(
        "verify",
        prog="lyrics.py verify",
        help="校验已写入文件的歌词、标签、封面和音频",
        description="逐首比对写入时的歌词、其他标签、封面和音频数据。",
        epilog=VERIFY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(verify)
    add_common(verify)

    rollback = sub.add_parser(
        "rollback",
        prog="lyrics.py rollback",
        help="移除本工具写入且未改动的歌词",
        description="移除 journal 中由本工具写入、且之后未被改动过的歌词。",
        epilog=ROLLBACK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    add_help_option(rollback)
    add_common(rollback)
    rollback.add_argument(
        "--yes", action="store_true", help="确认执行移除（不加只预览）"
    )

    # Keep the generated help consistent with the hand-written sections.
    for item in (parser, scan, plan, candidates, apply_cmd, verify, rollback):
        item._optionals.title = "选项"  # noqa: SLF001
        item._positionals.title = "路径"  # noqa: SLF001

    return parser


def option_specs(parser: argparse.ArgumentParser) -> dict[str, int]:
    """Map every option string to how many following tokens it consumes."""
    specs: dict[str, int] = {}

    def collect(current: argparse.ArgumentParser) -> None:
        for action in current._actions:  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                continue
            if not action.option_strings:
                continue
            if action.nargs is None and action.const is None:
                count = 1
            elif isinstance(action.nargs, int):
                count = action.nargs
            else:
                count = 0
            for option in action.option_strings:
                specs[option] = count
        for action in current._actions:  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for subparser in action.choices.values():
                    collect(subparser)

    collect(parser)
    return specs


def looks_like_html(value: str) -> bool:
    return not value.startswith("-") and value.lower().endswith((".html", ".htm"))


def normalize_argv(
    argv: list[str], specs: dict[str, int]
) -> tuple[str | None, list[str], list[str]]:
    """Split argv into (command, options, paths), regardless of their order.

    ``--path``/``--dir`` are accepted as a deprecated alias and folded into the
    positional paths, so old shell history keeps working without advertising
    the flag in ``-h``.
    """
    command: str | None = None
    options: list[str] = []
    paths: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            paths.extend(argv[index + 1 :])
            break
        if token in {"--path", "--dir"}:
            index += 1
            while index < len(argv) and not argv[index].startswith("-"):
                paths.append(argv[index])
                index += 1
            continue
        if token.startswith(("--path=", "--dir=")):
            paths.append(token.split("=", 1)[1])
            index += 1
            continue
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            options.append(token)
            if "=" in token:
                index += 1
                continue
            if name == "--html":
                if index + 1 < len(argv) and looks_like_html(argv[index + 1]):
                    options.append(argv[index + 1])
                    index += 2
                else:
                    index += 1
                continue
            take = specs.get(name, 0)
            if take:
                options.extend(argv[index + 1 : index + 1 + take])
                index += 1 + take
            else:
                index += 1
            continue
        if command is None and token in COMMAND_NAMES and not paths:
            command = token
            index += 1
            continue
        paths.append(token)
        index += 1
    return command, options, paths


