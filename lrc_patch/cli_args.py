"""Argument surface: help text, parser construction, argv normalisation."""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
from pathlib import Path

from .config import (
    APP_VERSION,
    DEFAULT_DURATION_TOLERANCE,
    DEFAULT_LIBRARY,
    DEFAULT_SOURCES,
)
from .errors import ToolError
from .render import disp_width, style


COMMAND_NAMES = {"scan", "plan", "apply", "verify", "rollback", "candidates"}


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Two columns wide enough for ``--duration-tolerance SEC`` at 100 cols."""

    def __init__(self, prog: str, **kwargs: object):
        try:
            width = min(max(84, shutil.get_terminal_size((100, 24)).columns), 104)
        except OSError:
            width = 100
        super().__init__(prog, width=width, max_help_position=32, **kwargs)

    def _format_action(self, action: argparse.Action) -> str:
        # The command table lives in the epilog: argparse's own subparser list
        # repeats it and prints an empty "<命令>" placeholder line.
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return ""
        return super()._format_action(action)


def _disp_width(text: str) -> int:
    return disp_width(text)


_EXAMPLES = [
    ('lrcpatch "結束バンド"', "省略命令 = 只读 plan"),
    ('lrcpatch scan "Aimer" --summary', "只看格式汇总"),
    ('lrcpatch candidates "Ado" --top 3', "看候选 ID"),
    ('lrcpatch candidates "Aimer" --template', "整张专辑的 pick 模板"),
    ('lrcpatch plan "Aimer" --pick-file picks.tsv', "按模板批量指定"),
    ('lrcpatch plan "Ado/某首.m4a" --pick netease:123', "人工指定候选"),
    ('lrcpatch plan "Ado/某首.m4a" --pick netease:123 --apply', "指定并直接写入"),
    ('lrcpatch apply "Ado"', "只写 ✓ 可写入"),
    ('lrcpatch verify "Ado"', "写入后复核"),
    ('lrcpatch rollback "Ado" --yes', "回滚"),
]


def _example_block() -> str:
    width = max(_disp_width(command) for command, _ in _EXAMPLES)
    return "\n".join(
        "  " + style(command, "36")
        + " " * (width - _disp_width(command) + 2)
        + style(note, "90")
        for command, note in _EXAMPLES
    )


def _opt(name: str, metavar: str = "") -> tuple[str, str]:
    plain = f"{name} {metavar}".strip()
    colored = style(name, "1;32")
    if metavar:
        colored += " " + style(metavar, "33")
    return plain, colored


def _table(rows: list[tuple[str, str, str]], indent: str = "    ") -> str:
    width = max((disp_width(plain) for plain, _, _ in rows), default=0)
    lines: list[str] = []
    for plain, colored, note in rows:
        line = indent + colored
        if note:
            line += " " * (width - disp_width(plain) + 2) + note
        lines.append(line)
    return "\n".join(lines)


def _flag_block(rows: list[tuple[str, str]]) -> str:
    width = max(len(name) for name, _ in rows)
    lines: list[str] = []
    for name, flags in rows:
        chunks = flags.split("\n")
        lines.append(
            "  " + style(name.ljust(width), "1;36") + "  " + style(chunks[0], "32")
        )
        for chunk in chunks[1:]:
            lines.append(" " * (2 + width + 2) + style(chunk, "32"))
    return "\n".join(lines)


def main_help() -> str:
    """The top-level page: 命令 and 参数 are separate, and usage is command-first."""
    usage = _table(
        [
            (
                "lrcpatch 命令 [--参数 …] 路径 …",
                style("lrcpatch", "1;36")
                + " "
                + style("命令", "1;35")
                + " "
                + style("[--参数 …]", "32")
                + " "
                + style("路径 …", "33"),
                "",
            ),
            (
                "lrcpatch 路径 …",
                style("lrcpatch", "1;36") + " " + style("路径 …", "33"),
                style("省略命令 = plan（只读）", "90"),
            ),
        ],
        indent="  ",
    )

    commands = _table(
        [
            (name, style(name, "1;36"), note)
            for name, note in (
                ("scan", "只读扫描：格式汇总 + 逐文件明细，不联网"),
                ("plan", "查询候选并生成计划（默认命令），不写音频"),
                ("candidates", "列出候选 SOURCE:ID 与分数，供 --pick 使用"),
                ("apply", "写入 ✓ 可写入，逐文件校验后原子替换"),
                ("verify", "复核已写入的歌词、标签、封面、音频"),
                ("rollback", "移除本工具写入且之后未被改动的歌词"),
            )
        ],
        indent="  ",
    )

    common_rows: list[tuple[str, str, str]] = []
    for name, metavar, note in (
        ("--library", "DIR", "音乐库根目录（默认 ~/Music/JMusic）"),
        ("--limit", "N", "最多处理 N 首"),
        ("--tree-limit", "N", "明细行数（plan/apply 默认 200，scan 默认全部）"),
        ("--state-dir", "DIR", "状态、缓存与报告目录"),
        ("--no-color", "", "关闭颜色（也可 NO_COLOR=1）"),
    ):
        plain, colored = _opt(name, metavar)
        common_rows.append((plain, colored, note))
    common_rows.append(
        (
            "路径 …",
            style("路径 …", "33"),
            "文件或目录，可多个；目录递归；相对路径按音乐库解析",
        )
    )
    common = _table(common_rows, indent="  ")

    flags = _flag_block(
        [
            (
                "plan",
                "--pick SRC:ID  --pick-file FILE  --apply  --sources LIST  --workers N\n"
                "--refresh  --retry-failed  --html [FILE]  --lrc  --duration-tolerance SEC",
            ),
            (
                "candidates",
                "--top N  --sources LIST  --workers N  --template [FILE]  --tsv  --all  --refresh",
            ),
            ("apply", "--workers N  --no-preflight  --lrc"),
            ("scan", "--summary"),
            ("rollback", "--yes"),
        ]
    )

    header = (
        style("lrcpatch", "1;36")
        + style(f" {APP_VERSION}", "90")
        + "   "
        + style("批量歌词：匹配 → 写入 → 校验 → 回滚", "1")
    )

    return f"""{header}

{style("用法", "1;36")}
{usage}
  {style("选项和路径顺序随意；命令可省略，默认 plan。", "90")}

{style("命令", "1;36")}
{commands}

{style("通用参数", "1;36")}
{common}

{style("各命令参数", "1;36")}
{flags}
  {style("完整说明：lrcpatch 命令 -h（例如 lrcpatch plan -h）", "90")}

{style("示例", "1;36")}
{_example_block()}

{style("匹配与打分", "1;36")}
  按标题、歌手、专辑、时长打分，满分 100；≥85 自动写入，否则 ! 需确认。

  标题      完全相同 +55
            标题里的 feat. / with 名字已在歌手里 +55（同一个录音，不是两个版本）
            Netease 附的译名或假名读音 +50（Hz(ヘルツ)、标题 - English Title）
            仅版本后缀不同 +42（Live / Remaster / Da Capo Version / TV Size …）
            其它附加后缀 +35
            合作者不同 +30（feat. 版本）
            标题主体不同、纯音乐相反、语言版本相反 → 淘汰（0 分）

  歌手      标签里的名字全部对得上 +30
            少了一部分合作者 +26（"Aimer" 对上 "Aimer / 阿部真央"）
            只是名字片段相同 +18；完全无关 → 淘汰
            ; ／ ， 、 & feat. 都算并列分隔；AC/DC 这种不加空格的斜杠不拆

  专辑      完全相同 +15；同一专辑的另一个版本（Deluxe / Tour / Single / EP…）+12
            合集的一面（"insane dream / us" 对上 "us"）+12；包含关系 +8

  时长      超过容差（默认 2s）直接淘汰；容差内不加减分，只用来排序，
            并在写入后用 LRC 末行时间戳再核一次。

  专辑只做佐证，不压倒标题：版本变体的标题（+42）必须专辑也对得上才够 85。
  反过来，Deluxe 版专辑只比完全相同的专辑少 3 分，不会因为版本名不同而丢候选。

  常见组合
    100分   标题 + 歌手 + 专辑全中（含 feat. 名字已在歌手里）
     97分   专辑只是 Deluxe / Tour 等版本
     93分   标题附译名或读音
     93分   专辑名互为包含
     87分   标题是版本变体，专辑对得上（Beautiful World → Da Capo Version）
     85分   标题 + 歌手全中，专辑不符
     75分   合作者不同（feat. 版本）：一律 ! 需确认

  排序      先按不含时长的元数据分，再按含时长的分，再按时长差，最后偏好带 LRC 的
  待确认    分数不足 85，或时长差超过容差。用 candidates 看全部候选，再 --pick 指定。

{style("写入前检查", "1;36")}
  时长差 ≤ 容差；LRC 末行时间戳不超过音频时长 3s；LRC 覆盖 ≥55% 时长。
  后两条不符会判为 ! 时间轴可疑（常见于 TV size、剪辑版、整轨版）。

{style("歌词格式", "1;36")}
  {style("TXT", "36")}    纯文本，没有时间轴
  {style("LRC", "32")}    逐行时间轴，最常见的格式
  {style("LRC+", "1;32")}  逐字时间轴（Enhanced LRC，<mm:ss.xx> 标记）
  {style("YRC", "1;34")}    QQ 音乐逐字格式 [起始,时长](起始,时长,0)
  {style("TTML", "1;35")}   Apple Music 的 XML 歌词
  YRC / TTML 这类高级格式只做识别与颜色区分，不做时间轴校验。

{style("状态", "1;36")}
  ✓ 可写入      标题/艺术家/专辑/时长都过，apply 会写
  ✓ 已写入      已写入并通过逐项校验
  ! 需确认      分数不足 85 或时长差超容差，apply 不写
  ! 时间轴可疑  LRC 末尾时间戳与音频时长不符，apply 不写
  ✗ 无匹配      启用的源都正常回答，确实没有这首歌
  ✗ 失败        请求没成功（限流/超时/服务过载），下次运行自动重试
  · 已有歌词    不覆盖
  ♪ 纯音乐      源标记为 instrumental

{style("网络与缓存", "1;36")}
  每首曲目通常只有 1 次搜索 + 1 次取词。只有当第一条搜索里没有任何标题与
  艺术家都对得上的候选时，才补发 1 次仅用标题的搜索；已经能确定结果就不再
  追问。--top 只决定 candidates 显示几行，不影响请求数。
  netease 约 0.9s 一次，并发不会突破最小间隔。被限流（code 405/406、HTTP 429）
  时先放慢重试，仍失败就暂停该源约 180s，其余曲目立即失败而不是继续撞墙。
  服务过载（HTTP 500/502/503/504）走退避，两次耗尽后暂停 60s 起。
  请求失败记为 ✗ 失败，不是「无匹配」，不写缓存，也不覆盖已有的 ✓ 计划。
  plan 与 candidates 共用同一个 HTTP 缓存：candidates 查过的搜索，plan 不再
  请求。想强制重查加 --refresh。装了 curl_cffi 就默认用浏览器 TLS 指纹。

{style("环境", "1;36")}
  Python 3.9+；依赖 mutagen（写标签），可选 curl_cffi（浏览器指纹）。
  直接运行 lrcpatch 时 uv 会按文件头的 PEP 723 声明自动安装，无需手动 pip。
  没有 uv 也能跑：python3 lrcpatch 会切到已装依赖的解释器。
  状态、HTTP 缓存与报告在 ~/.local/state/lrc-patch，不要删除，否则丢失断点与回滚记录。
"""

PLAN_EPILOG = """\
示例
  lrcpatch plan "結束バンド"
  lrcpatch plan "Ado" "YOASOBI/(2023)THE BOOK 3" --workers 2
  lrcpatch plan "米津玄師" --lrc --lrc-dir ~/.cache/lrc-patch/lrc
  lrcpatch plan "結束バンド" --html
  lrcpatch plan "結束バンド/11 忘れてやらない.m4a" --pick netease:2003496467
  lrcpatch plan "結束バンド/11 忘れてやらない.m4a" --pick netease:2003496467 --apply

说明
  plan 只读：不写音频，只更新状态数据库、CSV 和可选的 HTML 复核页。
  ≥85 分且时长在容差内才判 ✓ 可写入；其余是 ! 需确认，需人工 --pick。
  --apply 会在 plan 之后直接写本次的 ✓；! 需确认、✗ 失败/无匹配仍不会写入。
  每首通常 1 次搜索 + 1 次取词；搜索已经能确定结果时不再补发标题搜索。
  打分规则见 lrcpatch -h 的「匹配与打分」。
  请求失败（✗）不是结论，下次 plan 会自动重试；也可 --retry-failed 重查其余失败类。
"""

APPLY_EPILOG = """\
示例
  lrcpatch apply "結束バンド"
  lrcpatch apply "Ado" --workers 2

说明
  只写状态为 ready 的曲目；已有歌词、需确认、无匹配、请求失败都不会写入，
  并在输出里列出跳过数量与原因。
  先写临时副本，回读校验歌词/其他标签/封面/音频后再原子替换；失败则原文件不动。
  开始前会检查目标目录可写且空间足够；确实需要跳过时加 --no-preflight。
"""

VERIFY_EPILOG = """\
示例
  lrcpatch verify "結束バンド"

说明
  逐首比对歌词、其他标签、封面和音频数据与写入时的记录。
  全部一致返回 0，有差异返回 1，可直接放进脚本。
"""

ROLLBACK_EPILOG = """\
示例
  lrcpatch rollback "結束バンド"         只预览，不改文件
  lrcpatch rollback "結束バンド" --yes   执行移除

说明
  只处理 journal 中由本工具写入、且歌词之后没有被改动过的曲目。
  歌词已被你改过的会判为 ! 拒绝回滚，不会覆盖你的修改。
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
        help="只处理某个专辑目录（旧参数；建议直接拖路径）",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="最多处理 N 首，用于试跑")
    parser.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        help="状态、缓存与报告目录（默认 $XDG_STATE_HOME/lrc-patch）",
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


def add_tree_option(
    parser: argparse.ArgumentParser,
    *,
    default: int = 200,
    help_text: str = "终端树状明细最多展开 N 行；0 表示不展开（默认 200）",
) -> None:
    parser.add_argument(
        "--tree-limit",
        type=int,
        default=default,
        metavar="N",
        help=help_text,
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


def add_network_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-impersonate",
        action="store_true",
        help="不用 curl_cffi 的浏览器指纹，改用标准库 urllib",
    )


_HELP_OPTION_RE = re.compile(r"^(\s{2,})(-{1,2}[A-Za-z][\w-]*)(.*)$")


def colorize_help(text: str) -> str:
    """Colour an argparse page after formatting, so alignment is untouched."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if not line.startswith(" ") and len(stripped) <= 12:
            lines.append(style(stripped, "1;36"))
            continue
        match = _HELP_OPTION_RE.match(line)
        if match:
            indent, flag, rest = match.groups()
            lines.append(indent + style(flag, "1;32") + rest)
            continue
        lines.append(line)
    return "\n".join(lines)


class ColorHelpAction(argparse.Action):
    """``-h`` prints the coloured page for the parser it belongs to."""

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str = "显示帮助并退出",
    ) -> None:
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        print(colorize_help(parser.format_help()))
        parser.exit()


def add_help_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action=ColorHelpAction, help="显示帮助并退出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lrcpatch",
        usage="lrcpatch 命令 [--参数 …] 路径 …",
        description=(
            f"lrcpatch {APP_VERSION} · 批量歌词：匹配 → 写入 → 校验 → 回滚"
            "（只改歌词字段）；不带命令时默认只读 plan。"
        ),
        formatter_class=HelpFormatter,
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
        prog="lrcpatch scan",
        usage="lrcpatch scan [--参数 …] 路径 …",
        help="只读扫描：逐文件列出格式、时长、大小与歌词状态",
        description="只读扫描：统计格式与数量，并逐文件列出格式、时长、大小和歌词状态。",
        add_help=False,
        formatter_class=HelpFormatter,
        epilog=(
            "示例\n"
            '  lrcpatch scan "結束バンド"\n'
            '  lrcpatch scan "結束バンド" --summary\n'
            "  lrcpatch scan --limit 20\n\n"
            "说明\n"
            "  明细按专辑目录分组，一行一首：✓ 已有歌词 / · 待补，后面是格式、\n"
            "  时长、大小、歌词类型与行数；标题或艺术家缺失会标出「缺 …」。\n"
            "  目录与文件名按 Finder 顺序排列（拼音、数字自然序）。\n"
            "  --summary 只看格式与数量汇总；--tree-limit N 限制明细行数（0 = 全部）。\n"
            "  只读；不打开状态数据库，可以和 plan/apply 同时运行。"
        ),
    )
    add_help_option(scan)
    add_common(scan)
    add_tree_option(
        scan,
        default=0,
        help_text="明细最多列出 N 行；0 表示全部（默认 0）",
    )
    scan.add_argument(
        "--summary",
        action="store_true",
        help="只看格式与数量汇总，不逐文件列出",
    )

    plan = sub.add_parser(
        "plan",
        prog="lrcpatch plan",
        usage="lrcpatch plan [--参数 …] 路径 …",
        help="生成计划，不修改音频",
        description="匹配歌词，写入状态数据库和 CSV；不修改音频。",
        epilog=PLAN_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(plan)
    add_common(plan)
    add_tree_option(plan)
    add_lrc_options(plan)
    add_network_options(plan)
    plan.add_argument(
        "--sources",
        metavar="LIST",
        default=",".join(DEFAULT_SOURCES),
        help="歌词源，逗号分隔（默认 netease；lrclib 可选但近期常不可用）",
    )
    plan.add_argument(
        "--workers", type=int, metavar="N", default=4, help="查询并发 1..10（默认 4）"
    )
    plan.add_argument("--no-plain", action="store_true", help="只接受带时间轴的 LRC")
    plan.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    plan.add_argument(
        "--retry-failed",
        action="store_true",
        help="重查 no_match/ambiguous/suspect；error 默认每次重试",
    )
    plan.add_argument("--report", type=Path, metavar="FILE", help="CSV 报告路径")
    plan.add_argument(
        "--pick",
        action="append",
        metavar="SOURCE:ID",
        help="人工指定候选；要求范围内恰好一首缺歌词的曲目",
    )
    plan.add_argument(
        "--pick-file",
        type=Path,
        metavar="FILE",
        help="批量人工指定：每行 曲目路径<TAB>SOURCE:ID",
    )
    plan.add_argument(
        "--apply",
        action="store_true",
        help="plan 完成后立即写入本次 ✓ 可写入的曲目（等价于 plan && apply）",
    )
    plan.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"时长容差（默认 {DEFAULT_DURATION_TOLERANCE:g} 秒）",
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
        prog="lrcpatch candidates",
        usage="lrcpatch candidates [--参数 …] 路径 …",
        help="列出候选 ID，供 --pick 使用（只读）",
        description="逐首查询并列出候选（ID、分数、时长差），不修改音频和计划。",
        epilog=(
            "示例\n"
            '  lrcpatch candidates "結束バンド" --top 3\n'
            '  lrcpatch candidates "Aimer" --template\n'
            '  lrcpatch candidates "Ado" --tsv > picks.tsv\n\n'
            "输出是一张按专辑对齐的表：状态 / 候选 ID / 分数 / 时长差 / 格式 /\n"
            "标题 / 歌手 / 专辑，差异写在候选下面那一行，不再挤进同一行。\n\n"
            "整张专辑要人工决定时用 --template：自动匹配的曲目直接写成可用行，\n"
            "需要确认的曲目连同候选一起注释掉，改完交给 plan --pick-file。"
        ),
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(candidates)
    add_common(candidates)
    add_tree_option(candidates)
    add_network_options(candidates)
    candidates.add_argument(
        "--sources",
        metavar="LIST",
        default=",".join(DEFAULT_SOURCES),
        help="歌词源，逗号分隔（默认 netease；lrclib 可选但近期常不可用）",
    )
    candidates.add_argument(
        "--top", type=int, metavar="N", default=3, help="每源最多显示 N 条"
    )
    candidates.add_argument("--all", action="store_true", help="连已有歌词的曲目一起列出")
    candidates.add_argument("--tsv", action="store_true", help="输出制表符分隔，便于做 pick-file")
    candidates.add_argument(
        "--template",
        nargs="?",
        const=True,
        metavar="FILE",
        help="同时生成 pick-file 模板：自动匹配项已填好，需确认项注释列出候选",
    )
    candidates.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    candidates.add_argument(
        "--workers", type=int, metavar="N", default=4, help="查询并发 1..10（默认 4）"
    )
    candidates.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"排序用时长容差（默认 {DEFAULT_DURATION_TOLERANCE:g} 秒）",
    )

    apply_cmd = sub.add_parser(
        "apply",
        prog="lrcpatch apply",
        usage="lrcpatch apply [--参数 …] 路径 …",
        help="执行 ready 计划并逐文件校验",
        description="把 ready 的歌词写入音频；先写临时副本，校验通过后替换原文件。",
        epilog=APPLY_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(apply_cmd)
    add_common(apply_cmd)
    add_tree_option(apply_cmd)
    add_lrc_options(apply_cmd)
    apply_cmd.add_argument(
        "--workers", type=int, metavar="N", default=4, help="写入并发 1..10（默认 4）"
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
        prog="lrcpatch verify",
        usage="lrcpatch verify [--参数 …] 路径 …",
        help="校验已写入文件的歌词、标签、封面和音频",
        description="逐首比对写入时的歌词、其他标签、封面和音频数据。",
        epilog=VERIFY_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(verify)
    add_common(verify)
    add_tree_option(verify)

    rollback = sub.add_parser(
        "rollback",
        prog="lrcpatch rollback",
        usage="lrcpatch rollback [--参数 …] 路径 …",
        help="移除本工具写入且未改动的歌词",
        description="移除 journal 中由本工具写入、且之后未被改动过的歌词。",
        epilog=ROLLBACK_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(rollback)
    add_common(rollback)
    add_tree_option(rollback)
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


def looks_like_pick_file(value: str) -> bool:
    """``--template`` takes an optional file; a bare track path is not one."""
    return not value.startswith("-") and value.lower().endswith(
        (".tsv", ".txt", ".pick", ".csv")
    )


def reject_unknown_options(options: list[str], specs: dict[str, int]) -> None:
    """Turn argparse's bare "unrecognized arguments" into a usable hint.

    A single typo (``--no-warp``) otherwise kills a run after the folder scan,
    which is exactly when the message is least useful.
    """
    known = set(specs) | {"-h", "--help", "--version"}
    messages: list[str] = []
    for token in options:
        name = token.split("=", 1)[0] if token.startswith("--") else token
        if not name.startswith("-") or name in known:
            continue
        # A negative number is an option *value* (--tree-limit -1), not a flag;
        # argparse accepts it, so it must not be reported as an unknown option.
        try:
            float(name)
        except ValueError:
            pass
        else:
            continue
        if "wrap" in name or "warp" in name:
            messages.append(
                f"{name} 已移除：备注固定单行输出，不再折行（把窗口拉宽即可）"
            )
            continue
        close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.55)
        if close:
            hint = f"未知选项 {name}；你是不是想用 {close[0]}？"
        else:
            hint = f"未知选项 {name}"
        messages.append(hint)
    if messages:
        raise ToolError("\n".join(messages) + "\n可用 lrcpatch -h 查看全部选项。")


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
            if name == "--template":
                if index + 1 < len(argv) and looks_like_pick_file(argv[index + 1]):
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
