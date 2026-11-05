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
    Config,
    source_label,
)
from .errors import ToolError
from .render import LYRIC_COLORS, disp_width, style


COMMAND_NAMES = {
    "scan",
    "plan",
    "apply",
    "output",
    "verify",
    "rollback",
    "candidates",
    "status",
    "clean",
    "config",
}


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Two columns wide enough for ``--duration-tolerance SEC`` at 100 cols."""

    def __init__(self, prog: str, **kwargs: object):
        try:
            # Follow the window instead of guessing: only the very narrow case
            # needs a floor, and an over-wide terminal is capped so the two
            # columns do not drift apart.
            width = min(max(72, shutil.get_terminal_size((100, 24)).columns), 104)
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
    ('lrcpatch scan --summary "Aimer"', "只看格式汇总"),
    ('lrcpatch candidates --top 3 "Ado"', "看候选 ID"),
    ('lrcpatch candidates --template "Aimer"', "整张专辑的 pick 模板"),
    ('lrcpatch plan --pick-file picks.tsv "Aimer"', "按模板批量指定"),
    ('lrcpatch plan --pick ne:123 "Ado/某首.m4a"', "人工指定候选"),
    ('lrcpatch plan --pick ne:123 --apply "Ado/某首.m4a"', "指定并直接写入"),
    ('lrcpatch apply "Ado"', "只写 ✓ 可写入"),
    ('lrcpatch output --pick ne:1815109506 --lrc', "导出歌词文件（不动音频）"),
    ('lrcpatch verify "Ado"', "写入后复核"),
    ('lrcpatch status', "看状态库与缓存分布"),
    ('lrcpatch rollback --yes "Ado"', "回滚"),
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
    """One line per group of flags: the terminal wraps, the page does not.

    A command may carry several lines (``plan`` groups its flags by purpose);
    continuation lines stay indented under the first line's flag column.
    """
    width = max(len(name) for name, _ in rows)
    lines: list[str] = []
    for name, flags in rows:
        for index, chunk in enumerate(flags.split("\n")):
            if index == 0:
                lines.append(
                    "  "
                    + style(name.ljust(width), "1;36")
                    + "  "
                    + style(chunk, "32")
                )
            else:
                lines.append(" " * (2 + width + 2) + style(chunk, "32"))
    return "\n".join(lines)


def _tilde(path: Path) -> str:
    try:
        relative = path.expanduser().resolve().relative_to(Path.home())
    except (ValueError, OSError):
        return str(path)
    return "~/" + str(relative)


def main_help(config: Config | None = None) -> str:
    """The top-level page: 命令 and 参数 are separate, and usage is command-first."""
    library = config.library if config and config.library else DEFAULT_LIBRARY
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
                ("output", "导出歌词文件（--pick 指定，可一次导多个格式）"),
                ("verify", "复核已写入的歌词、标签、封面、音频"),
                ("rollback", "移除本工具写入且之后未被改动的歌词"),
                ("status", "预览状态库里的计划、缓存与报告（只读）"),
                ("clean", "清理过期缓存、旧报告与旧导出（安全清理）"),
                ("config", "查看生效的默认值；--init 生成配置文件"),
            )
        ],
        indent="  ",
    )

    common_rows: list[tuple[str, str, str]] = []
    for name, metavar, note in (
        ("--library", "DIR", f"音乐库根目录（默认 {_tilde(library)}，可写进配置）"),
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
                "--pick SRC:ID  --pick-file FILE  --apply  --html [FILE]\n"
                "--src LIST  --workers N  --refresh  --force  --retry-failed\n"
                "--duration-tolerance SEC  --no-timestamp-check\n"
                "--a2  --eslrc  --yrc  --qrc  --ttml",
            ),
            (
                "candidates",
                "--top N  --probe N  --src LIST  --workers N  --template [FILE]  --tsv  --all  --refresh",
            ),
            (
                "apply",
                "--pick SRC:ID  --pick-file FILE  --force\n"
                "--src LIST  --workers N  --refresh  --no-preflight\n"
                "--a2  --eslrc  --yrc  --qrc  --ttml",
            ),
            (
                "output",
                "--pick SOURCE:ID  --out DIR  --lrc  --a2  --eslrc  --yrc  --qrc  --ttml  --force  --refresh",
            ),
            ("scan", "--summary  --plans"),
            ("status", "--json"),
            ("rollback", "--yes"),
            (
                "clean",
                "--dry-run  --days N  --http  --reports  --lyr  --picks  --plans  --journal  --all",
            ),
            ("config", "--init"),
        ]
    )

    formats = _table(
        [
            ("TXT", style("TXT", LYRIC_COLORS["plain"]), "纯文本，没有时间轴"),
            ("LRC", style("LRC", LYRIC_COLORS["lrc"]), "逐行时间轴，最常见的格式"),
            ("A2", style("A2", LYRIC_COLORS["a2"]), "LRC A2 逐字（尖括号 <mm:ss.xx> 标记）"),
            (
                "ESLRC",
                style("ESLRC", LYRIC_COLORS["eslrc"]),
                "ESLyric / SPL 逐字（行内方括号 [mm:ss.xx]）",
            ),
            ("YRC", style("YRC", LYRIC_COLORS["yrc"]), "网易云逐字 [起始,时长](起始,时长,0)"),
            ("QRC", style("QRC", LYRIC_COLORS["qrc"]), "QQ 音乐逐字 [起始,时长]文本(起始,时长)"),
            ("TTML", style("TTML", LYRIC_COLORS["ttml"]), "Apple Music 的 XML 歌词"),
        ],
        indent="  ",
    )

    states = _table(
        [
            ("✓ 可写入", style("✓", "32") + " 可写入", "标题/艺术家/专辑/时长都过，apply 会写"),
            ("✓ 已写入", style("✓", "32") + " 已写入", "已写入并通过逐项校验"),
            ("! 需确认", style("!", "33") + " 需确认", "分数不足 85 或时长差超容差，apply 不写"),
            ("! 时间轴可疑", style("!", "33") + " 时间轴可疑", "LRC 末尾时间戳与音频时长不符，apply 不写"),
            ("✗ 无匹配", style("✗", "31") + " 无匹配", "启用的源都正常回答，确实没有这首歌"),
            ("✗ 失败", style("✗", "31") + " 失败", "请求没成功（限流/超时/服务过载），下次运行自动重试"),
            ("· 已有歌词", style("·", "90") + " 已有歌词", "不覆盖"),
            ("♪ 纯音乐", style("♪", "36") + " 纯音乐", "源标记为 instrumental"),
        ],
        indent="  ",
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
  {style("建议参数在前、路径在最后；命令可省略，默认 plan。", "90")}

{style("命令", "1;36")}
{commands}

{style("通用参数", "1;36")}
{common}

{style("各命令参数", "1;36")}
{flags}
  {style("完整说明：lrcpatch 命令 -h（例如 lrcpatch plan -h）", "90")}
  {style("plan 的几行依次是 指定与写入 / 查询 / 歌词格式。", "90")}

{style("示例", "1;36")}
{_example_block()}

{style("匹配与打分", "1;36")}
  按标题、歌手、专辑、时长打分，满分 100；≥85 自动写入，否则 ! 需确认。

  标题      完全相同 +55
            标题里的 feat. / with 名字已在歌手里 +55（同一个录音，不是两个版本）
            Netease 附的译名或假名读音 +50（Hz(ヘルツ)、歩く (漫步)、标题 - English Title）
            仅版本后缀不同 +42（Live / Remaster / Da Capo Version / TV Size …）
            其它附加后缀 +35
            合作者不同 +30（feat. 版本）
            标题主体不同 +0（歌手/专辑仍会加分，但最多 70，到不了 85）
            纯音乐相反、语言版本相反 → 整条淘汰（0 分）

  歌手      标签里的名字全部对得上 +30
            少了一部分合作者 +26（"Aimer" 对上 "Aimer / 阿部真央"）
            只是名字片段相同 +18；完全无关 +0（标题与专辑仍会累加）
            ; ／ ， 、 & feat. 都算并列分隔；AC/DC 这种不加空格的斜杠不拆

  专辑      完全相同 +15；同一专辑的另一个版本（Deluxe / Tour / Single / EP…）+12
            合集的一面（"insane dream / us" 对上 "us"）+12；包含关系 +8

  时长      超过容差（默认 2s）直接淘汰；容差内不加减分，只用来排序，写入后再用 LRC 末行时间戳核一次。

  专辑只做佐证，不压倒标题：版本变体的标题（+42）必须专辑也对得上才够 85；反过来 Deluxe 版专辑只比完全相同的专辑少 3 分，不会因为版本名不同而丢候选。

  常见组合
    100分   标题 + 歌手 + 专辑全中（含 feat. 名字已在歌手里）
     97分   专辑只是 Deluxe / Tour 等版本
     93分   标题附译名或读音
     93分   专辑名互为包含
     87分   标题是版本变体，专辑对得上（Beautiful World → Da Capo Version）
     85分   标题 + 歌手全中，专辑不符
     75分   合作者不同（feat. 版本）：一律 ! 需确认

  排序      先看是否在时长容差内（容差外的一律排在后面），再按不含时长的元数据分，
            再按含时长的分，再按时长差，最后偏好带 LRC 的。
            现场版比正片长 13 秒时，即使标题一字不差也不会排到专辑对得上的那条前面。
  待确认    分数不足 85，或时长差超过容差；用 candidates 看全部候选，再 --pick 指定。
            ! 行会写清每个源给了多少分，以及最佳候选的 ID、专辑与时长差，可直接拿去 --pick：
            ! 08 印地安老斑鸠.m4a  ne 72 · qq 72；最佳 qq:244115386 印地安老斑鸠 (Live) / 周杰伦 / 巡演 / 208s / Δ97.0s

{style("写入前检查", "1;36")}
  时长差 ≤ 容差；只有纯文本（没有时间轴）判为 ! 需确认；LRC 末行时间戳不超过音频时长 3s；LRC 覆盖 ≥55% 时长。
  后两条不符会判为 ! 时间轴可疑（常见于 TV size、剪辑版、整轨版），纯文本同理。
  这些都能用 --force 越过（连同"已有歌词不覆盖"一起），见下方「强制与刷新」。

{style("歌词格式", "1;36")}
{formats}
  plan 取词时把源的原生歌词存成文件（$XDG_CACHE_HOME/lrc-patch/lyr/歌手 - 标题 [SOURCE:ID].yrc），
  并记下源一次给了哪些格式：netease 常常同时给逐字 YRC 和逐行 LRC，所以 plan 的格式列写 YRC/LRC
  这样的集合（只给一种就只写一种），apply 从这些文件里挑一种写进音频——数据库里只存路径，不存文本。
  apply 时再按 --a2 / --eslrc / --ttml / --qrc 转换，所以想换格式不必重新联网，`lrcpatch apply --a2` 就够。
  私有格式（--yrc / --qrc）只在原生就是它时才可能；原生是逐行 LRC 时无法凭空生成逐字，会明说而不是硬写。
  A2 / ESLRC 与普通 LRC 共用行时间轴校验；YRC / QRC / TTML 只识别不校验。
  只想拿到歌词文件、不动音频，用 lrcpatch output（可一次导多种格式）；后缀按格式走：
  .alrc / .eslrc / .yrc / .qrc / .ttml，逐行用 .lrc，纯文本用 .txt。

{style("强制与刷新", "1;36")}
  --refresh   只作用于网络：忽略 HTTP 缓存，重新请求。计划里已有的结论仍然复用。
  --force     只作用于现状：忽略“文件已有歌词”和“状态库里已有计划”，强制重查重排；
              apply --force 连 ! 时间轴可疑一起写，并覆盖已有歌词（原歌词进回滚记录，rollback 会还原）。
  --force 不会自动刷新网络缓存（要更彻底就两个一起给）。它用于原本是 TXT、或原本写错了的歌词。

{style("状态", "1;36")}
{states}

{style("网络与缓存", "1;36")}
  每首曲目通常只有 1 次搜索 + 1 次取词：只有当第一条搜索里没有任何标题与艺术家都对得上的候选时，才补发 1 次仅用标题的搜索；已经能确定结果就不再追问。--top 只决定 candidates 显示几行；那几行要不要取词确认格式由 candidates 的 --probe 决定，plan 始终只取首选那条。
  ne 约 0.9s 一次，并发不会突破最小间隔；被限流（code 405/406、HTTP 429）时先放慢重试，仍失败就暂停该源约 180s，其余曲目立即失败而不是继续撞墙。
  服务过载（HTTP 500/502/503/504）走退避，两次耗尽后暂停 60s 起。
  请求失败记为 ✗ 失败，不是「无匹配」，不写缓存，也不覆盖已有的 ✓ 计划。
  plan 与 candidates 共用同一个 HTTP 缓存：candidates 查过的搜索，plan 不再请求，反过来也一样；
  ne 与 qq 的搜索、取词都进缓存（qq 的请求 id 每次不同，已从缓存键里剔除）。想强制重查加 --refresh；装了 curl_cffi 就默认用浏览器 TLS 指纹。

{style("环境", "1;36")}
  Python 3.9+；依赖 mutagen（写标签），可选 curl_cffi（浏览器指纹）。
  直接运行 lrcpatch 时 uv 会按文件头的 PEP 723 声明自动安装，无需手动 pip；没有 uv 也能跑，python3 lrcpatch 会切到已装依赖的解释器。
  状态、HTTP 缓存与报告在 ~/.local/state/lrc-patch，不要删除，否则丢失断点与回滚记录。

{style("配置", "1;36")}
  默认值写在 ~/.config/lrc-patch/config.toml（可用 LRCPATCH_CONFIG 指向别的文件）：library / src / workers。
  命令行参数优先于配置文件；lrcpatch config 查看当前生效值与来源，lrcpatch config --init 生成带注释的模板。
  --library 只决定相对路径的锚点和「整个库」的范围，数据库里存的是绝对路径；换一个库跑 --library DIR 不会串到别的库的计划。
"""

PLAN_EPILOG = """\
示例
  lrcpatch plan "結束バンド"
  lrcpatch plan --workers 2 "Ado" "YOASOBI/(2023)THE BOOK 3"
  lrcpatch plan --apply "米津玄師"
  lrcpatch plan --html "結束バンド"
  lrcpatch plan --pick ne:2003496467 "結束バンド/11 忘れてやらない.m4a"
  lrcpatch plan --pick qq:123 qq:456 "周杰伦/(2000)Jay"
  lrcpatch plan --pick ne:2003496467 --apply "結束バンド/11 忘れてやらない.m4a"
  lrcpatch plan --force --apply "某人/(2018)老专辑"      已有 TXT 或写错的歌词，强制重写

说明
  plan 只读：不写音频，只更新状态数据库、CSV 和可选的 HTML 复核页。
  ≥85 分且时长在容差内才判 ✓ 可写入；其余是 ! 需确认，需人工 --pick。
  一个 --pick 可连写多个 SOURCE:ID；程序先查每个 ID 的标题/歌手/时长，再按分数配对到范围内缺歌词的曲目，并把配对结果打印出来。
  SOURCE 写 ne 或 netease 都可以；ne 只是表格里的短名。
  --apply 会在 plan 之后直接写本次的 ✓；! 需确认、✗ 失败/无匹配仍不会写入。
  --refresh 只重新联网；--force 才忽略“已有歌词/已有计划”重排。两者可以一起给。
  每首通常 1 次搜索 + 1 次取词；搜索已经能确定结果时不再补发标题搜索。
  取词时把源的原生歌词写进歌词库（$XDG_CACHE_HOME/lrc-patch/lyr/歌手 - 标题 [SOURCE:ID].yrc），
  数据库只记文件名；所以之后 apply 换格式是从文件现场转，不用重查。
  --src 换源会按新源重排：来自别的源的 ✓ 会被重查（答案通常已缓存，很快就回来）；只想看另一边的候选、
  不想动已有结论，用 candidates --src qq。
  打分规则见 lrcpatch -h 的「匹配与打分」。
  请求失败（✗）不是结论，下次 plan 会自动重试；也可 --retry-failed 重查其余失败类。
"""

APPLY_EPILOG = """\
示例
  lrcpatch apply "結束バンド"
  lrcpatch apply --a2 "結束バンド"            用计划里的原生歌词现场转 A2
  lrcpatch apply --pick qq:123 qq:456 "周杰伦/(2000)Jay"
  lrcpatch apply --force "某人/某专辑"          连同 ! 时间轴可疑一起写，允许覆盖
  lrcpatch apply --workers 2 "Ado"

说明
  只写状态为 ready 的曲目；已有歌词、需确认、无匹配、请求失败都不会写入，并在输出里列出跳过数量与原因。
  --force 把 ! 时间轴可疑也算进来，并允许覆盖已有歌词；原歌词会存进回滚记录，rollback 会把它还原而不是清空。
  范围是整个音乐库或你给出的路径；状态库是所有音乐库共用的，但属于其他 --library 的计划不会被写入。
  带 --pick / --pick-file 时先按人工指定重新 plan（指定的候选天然越过 85 分门槛，但仍检查时间轴），再写入，不需要先单独跑一次 plan。
  --a2 / --eslrc / --ttml / --yrc / --qrc 在这里是“转换”：读歌词库里的原生文件现场转换，不联网。
  歌词库被 clean 清空时会明说「库里没有」，重新 plan 一次即可恢复。
  转不出来的（原生只有逐行 LRC）会列在「跳过格式」里，其余照写；整批都转不出来才报错。
  先写临时副本，回读校验歌词/其他标签/封面/音频后再原子替换；失败则原文件不动。
  开始前会检查目标目录可写且空间足够；确实需要跳过时加 --no-preflight。
"""

OUTPUT_EPILOG = """\
示例
  lrcpatch output --pick ne:1815109506                    源的原生格式，不转换
  lrcpatch output --pick ne:1815109506 --lrc --a2         同时导出 .lrc 与 .alrc
  lrcpatch output --pick ne:1815109735 --pick qq:293055026 --ttml
  lrcpatch output --pick ne:1815109506 ne:1815109735      一个 --pick 带多个 ID
  lrcpatch output --pick ne:1815109506 --out ~/Desktop/lyr

说明
  output 只写歌词文件，不动音频；apply 才是写进音频。两者都从源的原生歌词出发，所以格式转换不联网。
  不给格式参数 = 导出源的原生格式（ne 通常是 YRC，qq 是 QRC，lrclib 是 LRC/TXT），这是信息最全的一版。
  可以一次给多个格式，每个格式写一个文件，后缀按格式走：.lrc / .alrc / .eslrc / .yrc / .qrc / .ttml / .txt。
  转不出来的会明说原因而不是写一个错后缀的文件：私有格式（YRC/QRC）只能由源原样提供，
  逐字格式也要求原生带逐字时间轴；逐行 LRC 永远转得出来。
  默认目录就是歌词库 $XDG_CACHE_HOME/lrc-patch/lyr：plan 取到的原生歌词也存这里，apply 从这里读，
  所以同一个 ID 重复导出会显示「已是最新」而不是再写一遍；--force 才覆盖。
  文件名是「歌手 - 标题 [SOURCE:ID]」，plan 用过的名字在这里可以直接找到。
  之前 plan 取过的 ID 直接从歌词库读，不会再请求；--refresh 强制重查。--out 只是导出到别处。
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
  lrcpatch rollback --yes "結束バンド"   执行移除

说明
  只处理 journal 中由本工具写入、且歌词之后没有被改动过的曲目。
  apply --force 覆盖过的曲目会还原成原来那版歌词；原来没有歌词的才清空。预览里分别写「还原」和「移除」。
  歌词已被你改过的会判为 ! 拒绝回滚，不会覆盖你的修改。
"""


CLEAN_EPILOG = """\
示例
  lrcpatch clean --dry-run            只预览，不删除
  lrcpatch clean                      清过期缓存、30 天前的报告/导出/模板
  lrcpatch clean --days 7             只清 7 天前的
  lrcpatch clean --http               只清已过期的 HTTP 缓存
  lrcpatch clean --all                清全部，且不限天数

说明
  清理对象分三处：状态库（HTTP 缓存 / 计划 / 回滚记录）、reports/（CSV 与 HTML 复核页）、cache/lyr（歌词库：plan 取到的原生歌词与 output 导出都在这里，计划还在用的文件会保留）。
  默认只清可再生的内容：已过期缓存、超过 --days（默认 30 天）的报告、导出与 pick 模板。
  --all 不看时间：所有类别都清空，包括刚生成的报告和刚查好的计划。
  计划状态用于断点续跑，回滚记录用于 rollback，因此只在明确指定 --plans / --journal 或 --all 时才删除；删了就要重新查询、且无法回滚。
  每行都会说清删了几项、释放多少，以及"因为未满 --days 而没动"这件事。
  删除记录后会执行 VACUUM，空间按实际收缩后的数据库大小报告。
"""


def add_common(
    parser: argparse.ArgumentParser, library: Path = DEFAULT_LIBRARY
) -> None:
    parser.add_argument(
        "--library",
        type=Path,
        default=library,
        metavar="DIR",
        help=f"音乐库根目录（默认 {_tilde(library)}；配置文件里也能设）",
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


# The six formats ``output`` can write, in the order they are offered.  ``lrc``
# is missing from the target flags of plan/apply on purpose: line-timed LRC is
# the default there, not a conversion to ask for.
FORMAT_HELP = {
    "lrc": "逐行 LRC（.lrc）",
    "a2": "逐字：LRC A2（尖括号 <mm:ss.xx>，.alrc）",
    "eslrc": "逐字：ESLyric / SPL（方括号 [mm:ss.xx]，.eslrc）",
    "yrc": "逐字：网易云原生 YRC（仅 ne 源，.yrc）",
    "qrc": "逐字：QQ 原生 QRC（仅 qq 源，.qrc）",
    "ttml": "逐字：TTML（Apple Music XML，.ttml）",
}


def add_lrc_options(parser: argparse.ArgumentParser) -> None:
    """The one format to write; mutually exclusive, because apply writes one."""
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--a2",
        action="store_true",
        help="逐字：把源歌词转成 LRC A2（尖括号 <mm:ss.xx>）内嵌",
    )
    fmt.add_argument(
        "--eslrc",
        action="store_true",
        help="逐字：转成 ESLyric（方括号 [mm:ss.xx]）内嵌",
    )
    fmt.add_argument(
        "--yrc",
        action="store_true",
        help="逐字：保留网易云原生 YRC（仅 ne 源）",
    )
    fmt.add_argument(
        "--qrc",
        action="store_true",
        help="逐字：保留 QQ 原生 QRC（仅 qq 源）",
    )
    fmt.add_argument(
        "--ttml",
        action="store_true",
        help="逐字：转成 TTML（Apple Music XML）",
    )


def add_output_format_options(parser: argparse.ArgumentParser) -> None:
    """The formats to export; several at once, each to its own file."""
    for name, help_text in FORMAT_HELP.items():
        parser.add_argument(f"--{name}", action="store_true", help=help_text)


def add_network_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-impersonate",
        action="store_true",
        help="不用 curl_cffi 的浏览器指纹，改用标准库 urllib",
    )


def add_source_option(
    parser: argparse.ArgumentParser, sources: tuple[str, ...]
) -> None:
    parser.add_argument(
        "--src",
        metavar="LIST",
        default=",".join(sources),
        help=f"歌词源，逗号分隔（默认 {','.join(source_label(name) for name in sources)}；ne = netease；配置文件可改）",
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


def build_parser(config: Config | None = None) -> argparse.ArgumentParser:
    """Build the parser; ``config`` only changes the defaults, never the flags."""
    library_default = (
        config.library if config and config.library else DEFAULT_LIBRARY
    )
    source_default = config.src if config and config.src else DEFAULT_SOURCES
    workers_default = config.workers if config and config.workers else 4
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
            '  lrcpatch scan --summary "結束バンド"\n'
            "  lrcpatch scan --plans\n"
            "  lrcpatch scan --limit 20\n\n"
            "说明\n"
            "  明细按专辑目录分组，一行一首：✓ 已有歌词 / · 待补，后面是格式、时长、大小、歌词类型与行数；标题或艺术家缺失会标出「缺 …」。\n"
            "  歌词列是「标签 + 行数」，标签左对齐、行数右对齐，所以 LRC / ESLRC / TXT 混在一张专辑里时“行”也是齐的。\n"
            "  待补的曲目若已有匹配好的计划，会多出一列 qq:100（源:分数），说明可以直接 apply，不用再查询；qq? 表示缓存里是需确认的结果，apply 还不会写。\n"
            "  --plans 只列出已有计划的曲目，用来回答“apply 到底会写哪几首”。\n"
            "  目录与文件名按 Finder 顺序排列（拼音、数字自然序）。\n"
            "  --summary 只看格式与数量汇总；--tree-limit N 限制明细行数（0 = 全部）。\n"
            "  只读；以只读方式看一眼计划库（不改状态），可以和 plan/apply 同时运行。"
        ),
    )
    add_help_option(scan)
    add_common(scan, library_default)
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
    scan.add_argument(
        "--plans",
        action="store_true",
        help="只列出状态库里已有计划的曲目（含 plan --force 重排过的、文件本来就有歌词的）",
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
    add_common(plan, library_default)
    add_tree_option(plan)
    add_lrc_options(plan)
    add_network_options(plan)
    add_source_option(plan, source_default)
    plan.add_argument(
        "--workers",
        type=int,
        metavar="N",
        default=workers_default,
        help=f"查询并发 1..10（默认 {workers_default}）",
    )
    plan.add_argument("--no-plain", action="store_true", help="只接受带时间轴的 LRC")
    plan.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存并重查")
    plan.add_argument(
        "--force",
        action="store_true",
        help="忽略已有歌词与已有计划，强制重查重排（apply 时配合写入）",
    )
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
            '  lrcpatch candidates --top 3 "結束バンド"\n'
            '  lrcpatch candidates --template "Aimer"\n'
            '  lrcpatch candidates --tsv "Ado" > picks.tsv\n'
            '  lrcpatch candidates "ヨルシカ/(2021)創作/02 春泥棒.m4a" --refresh\n\n'
            "范围（决定列哪些曲目）\n"
            "  给了路径或 --album  → 你点名的那些，照单全列，不管它的计划是不是已经 ✓；此时 --all 没有额外作用。\n"
            "  什么都没给          → 上一次 plan 留下的 ! 需确认（含 ! 时间轴可疑）。plan 之后直接跑 candidates，看到的就是那几首真正需要你决定，而不是整个库。\n"
            "  库里还有没 plan 过的曲目时，会单独报一行「未 plan N」，要一起查加 --all；--all 连已有歌词、已经 ✓ 的曲目也列出来。\n"
            "  计划来自的源不在当前 --src 里时，那首也算未决：用 --src qq 就能看到 qq 那边的候选。\n\n"
            "--refresh（只管网络）忽略 HTTP 缓存重新请求，不改范围：想看某首歌最新的候选就点名那个文件再加 --refresh，不必 --all。\n\n"
            "--all 不带路径 = 整个音乐库里所有曲目（含已有歌词、已经 ✓ 的），不是只查上次 plan 那批。\n\n"
            "状态库按文件路径逐条记录，每首取它自己最后一次 plan 的结果（文件大小或修改时间变了就不算数），不存在「整库最近一次」的快照；因此不同专辑可能来自不同次运行。用 status 看整体分布。\n\n"
            "输出是一张按专辑对齐的表：状态 / 候选 ID / 分数 / 时长差 / 原生 / 标题 / 歌手 / 专辑。分数是标题+歌手+专辑的总分（满分 100），时长差单独一列，超过容差显示红色。"
            "排序先看是否在容差内，容差外的不会排在可用的前面。\n\n"
            "“原生”是源会给出的格式：qq 的取词接口是个 QRC 容器、lrclib 在搜索结果里就带同步标记、ne 同一首可能给逐字 YRC 也可能只有逐行 LRC。"
            "默认不取词（表里 `?` 表示还没问过），加 --probe N 才会为每源前 N 条真的取一次词，把实际格式写上去；取词走 HTTP 缓存，随后的 plan 直接复用。"
            "qq 的行一律写 QRC——那是它唯一的容器——只有解码出词时间轴才用粗体；解出来是逐行或纯文本的不加粗，但仍然是 QRC。"
            "TTML / A2 / ESLRC 这类分不出真假的格式也不加粗，粗细只回答一个问题：这份词有没有逐字时间轴。"
            "纯文本写成 TXT，跟存下来的 .txt 一致。"
            "apply 时再决定转成什么。\n\n"
            "整张专辑要人工决定时用 --template：自动匹配的曲目直接写成可用行，需要确认的曲目连同候选一起注释掉，改完交给 plan --pick-file。"
        ),
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(candidates)
    add_common(candidates, library_default)
    add_tree_option(candidates)
    add_network_options(candidates)
    add_source_option(candidates, source_default)
    candidates.add_argument(
        "--top", type=int, metavar="N", default=3, help="每源最多显示 N 条"
    )
    candidates.add_argument(
        "--probe",
        type=int,
        metavar="N",
        default=0,
        help="取词确认每源前 N 条的真实格式（默认 0 = 不取；有逐字时间轴的加粗）",
    )
    candidates.add_argument(
        "--all",
        action="store_true",
        help="全库所有曲目（含已 ✓、已有歌词），不只看上次 plan 的存疑项；给了路径就不需要它",
    )
    candidates.add_argument("--tsv", action="store_true", help="输出制表符分隔，便于做 pick-file")
    candidates.add_argument(
        "--template",
        nargs="?",
        const=True,
        metavar="FILE",
        help="同时生成 pick-file 模板：自动匹配项已填好，需确认项注释列出候选",
    )
    candidates.add_argument(
        "--refresh",
        action="store_true",
        help="忽略 HTTP 缓存重新请求（只影响网络，不改列哪些曲目）",
    )
    candidates.add_argument(
        "--workers",
        type=int,
        metavar="N",
        default=workers_default,
        help=f"查询并发 1..10（默认 {workers_default}）",
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
    add_common(apply_cmd, library_default)
    add_tree_option(apply_cmd)
    add_lrc_options(apply_cmd)
    add_network_options(apply_cmd)
    add_source_option(apply_cmd, source_default)
    apply_cmd.add_argument(
        "--pick",
        action="append",
        metavar="SOURCE:ID",
        help="直接写入选中的候选（可一次给多个，自动配对到缺词曲目）",
    )
    apply_cmd.add_argument(
        "--pick-file",
        type=Path,
        metavar="FILE",
        help="批量人工指定：每行 曲目路径<TAB>SOURCE:ID",
    )
    apply_cmd.add_argument(
        "--refresh", action="store_true", help="配合 --pick 时忽略 HTTP 缓存并重查"
    )
    apply_cmd.add_argument(
        "--force",
        action="store_true",
        help="连 ! 时间轴可疑一起写，并允许覆盖已有歌词（原歌词存入回滚记录）",
    )
    apply_cmd.add_argument("--no-plain", action="store_true", help="只接受带时间轴的 LRC")
    apply_cmd.add_argument(
        "--retry-failed",
        action="store_true",
        help="配合 --pick 时重查 no_match/ambiguous/suspect",
    )
    apply_cmd.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        metavar="SEC",
        help=f"时长容差（默认 {DEFAULT_DURATION_TOLERANCE:g} 秒）",
    )
    apply_cmd.add_argument(
        "--no-timestamp-check",
        action="store_true",
        help="不检查 LRC 末尾时间轴与音频时长是否相符",
    )
    apply_cmd.add_argument(
        "--workers",
        type=int,
        metavar="N",
        default=workers_default,
        help=f"写入并发 1..10（默认 {workers_default}）",
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

    output_cmd = sub.add_parser(
        "output",
        prog="lrcpatch output",
        usage="lrcpatch output [--参数 …] --pick SOURCE:ID …",
        help="把歌词导出成文件，可一次导出多种格式",
        description="按 SOURCE:ID 导出歌词文件，不碰音频；可一次给出多个格式。",
        epilog=OUTPUT_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(output_cmd)
    output_cmd.add_argument(
        "--pick",
        action="append",
        metavar="SOURCE:ID",
        help="要导出的候选 ID，例如 ne:1815109506；可重复，也可一次跟多个",
    )
    output_cmd.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="导出目录（默认歌词库 $XDG_CACHE_HOME/lrc-patch/lyr）",
    )
    output_cmd.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        help="状态、缓存与报告目录（默认 $XDG_STATE_HOME/lrc-patch）",
    )
    output_cmd.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出（也可设 NO_COLOR=1）",
    )
    add_output_format_options(output_cmd)
    add_network_options(output_cmd)
    output_cmd.add_argument(
        "--refresh", action="store_true", help="忽略 HTTP 缓存并重查"
    )
    output_cmd.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的同名文件（默认跳过）",
    )
    output_cmd.add_argument(
        "paths",
        nargs="*",
        metavar="SOURCE:ID",
        help="旧的写法（等价于 --pick），保留给已有命令历史",
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
    add_common(verify, library_default)
    add_tree_option(verify)

    rollback = sub.add_parser(
        "rollback",
        prog="lrcpatch rollback",
        usage="lrcpatch rollback [--参数 …] 路径 …",
        help="移除本工具写入且未改动的歌词",
        description="撤销写入：移除 journal 中由本工具写入、且之后未被改动过的歌词；apply --force 覆盖过的还原原歌词。",
        epilog=ROLLBACK_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(rollback)
    add_common(rollback, library_default)
    add_tree_option(rollback)
    rollback.add_argument(
        "--yes", action="store_true", help="确认执行移除（不加只预览）"
    )

    status = sub.add_parser(
        "status",
        prog="lrcpatch status",
        usage="lrcpatch status [--参数 …]",
        help="预览状态库：计划、缓存、回滚记录与报告（只读）",
        description="看一眼状态库里现在存了什么：各状态的计划、各源条数、HTTP 缓存量与过期数、可回滚记录、报告体积。",
        epilog=(
            "示例\n"
            "  lrcpatch status                 概览\n"
            "  lrcpatch status --json          给脚本用的 JSON\n\n"
            "说明\n"
            "  只读：打开状态库用只读连接，不建库、不迁移、不抢锁，可以和 plan/apply 同时跑。\n"
            "  这里统计的是状态库（每首一条计划），不是音乐库；曲目与歌词格式请看 lrcpatch scan。\n"
            "  ! 需确认的曲目可以接着用 lrcpatch candidates 看候选，想重新匹配用 lrcpatch plan --force。\n"
            "  缓存与报告占的空间可以用 lrcpatch clean --dry-run 预估、clean 释放。\n"
            '  状态库还没有时（没跑过 plan）会直接说明，不会顺手创建它。'
        ),
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(status)
    status.add_argument(
        "--library",
        type=Path,
        metavar="DIR",
        help="只影响这里显示的音乐库路径（默认取配置或 ~/Music/JMusic）",
    )
    status.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        help="状态、缓存与报告目录（默认 $XDG_STATE_HOME/lrc-patch）",
    )
    status.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出（也可设 NO_COLOR=1）",
    )
    status.add_argument(
        "--json", action="store_true", help="输出 JSON，便于脚本处理"
    )

    clean = sub.add_parser(
        "clean",
        prog="lrcpatch clean",
        usage="lrcpatch clean [--参数 …]",
        help="清理过期缓存、旧报告与旧导出（安全清理）",
        description="像 brew cleanup：默认只清过期或超期的生成物，并打印释放量。",
        epilog=CLEAN_EPILOG,
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(clean)
    clean.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        help="状态、缓存与报告目录（默认 $XDG_STATE_HOME/lrc-patch）",
    )
    clean.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="output 的导出目录（默认 $XDG_CACHE_HOME/lrc-patch/lyr）",
    )
    clean.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出（也可设 NO_COLOR=1）",
    )
    clean.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="只清理 N 天前的报告、导出、模板与记录（默认 30）",
    )
    clean.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    clean.add_argument(
        "--http", action="store_true", help="清 HTTP 缓存（默认只清已过期的）"
    )
    clean.add_argument("--reports", action="store_true", help="清旧 CSV 报告与 HTML 复核页")
    clean.add_argument(
        "--lyr",
        "--lrc",
        dest="lyr",
        action="store_true",
        help="清 output 导出的歌词（含旧的 lrc 目录）",
    )
    clean.add_argument("--picks", action="store_true", help="清旧的 pick 模板")
    clean.add_argument(
        "--plans", action="store_true", help="清旧计划状态（之后需要重新查询）"
    )
    clean.add_argument(
        "--journal", action="store_true", help="清旧回滚记录（之后无法 rollback）"
    )
    clean.add_argument(
        "--all",
        action="store_true",
        help="清全部，且不限 --days（包括计划状态与回滚记录）",
    )

    config_cmd = sub.add_parser(
        "config",
        prog="lrcpatch config",
        usage="lrcpatch config [--参数 …]",
        help="查看生效的默认值；--init 生成配置文件",
        description="显示配置文件位置，以及音乐库、歌词源与并发的生效值与来源。",
        epilog=(
            "示例\n"
            "  lrcpatch config            查看当前生效值\n"
            "  lrcpatch config --init     生成带注释的配置文件（已存在则不覆盖）\n\n"
            "说明\n"
            "  配置文件默认是 ~/.config/lrc-patch/config.toml（$XDG_CONFIG_HOME），"
            "可用环境变量 LRCPATCH_CONFIG 指向别的文件。\n"
            "  这里只提供默认值：命令行参数（--library / --src / --workers）永远优先。"
        ),
        formatter_class=HelpFormatter,
        add_help=False,
    )
    add_help_option(config_cmd)
    config_cmd.add_argument(
        "--init",
        action="store_true",
        help="写入带注释的配置模板；文件已存在时不覆盖",
    )
    config_cmd.add_argument(
        "--no-color",
        action="store_true",
        help="关闭彩色输出（也可设 NO_COLOR=1）",
    )

    # Keep the generated help consistent with the hand-written sections.
    for item in (
        parser,
        scan,
        plan,
        candidates,
        apply_cmd,
        output_cmd,
        verify,
        rollback,
        clean,
        config_cmd,
    ):
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


_PICK_RE = re.compile(r"^[A-Za-z]+:\d+$")


def looks_like_pick(value: str) -> bool:
    """``--pick qq:1 qq:2 path`` — picks may be given in one run."""
    return bool(_PICK_RE.match(value))


def looks_like_pick_file(value: str) -> bool:
    """``--template`` takes an optional file; a bare track path is not one."""
    return not value.startswith("-") and value.lower().endswith(
        (".tsv", ".txt", ".pick", ".csv")
    )


def option_owners(parser: argparse.ArgumentParser) -> dict[str, set[str]]:
    """Which commands accept which option, so "wrong command" gets said out loud.

    ``--lrc`` is a real flag of ``clean`` but a removed one of ``apply``; the
    global option table cannot tell those apart, and argparse only says
    "unrecognized arguments".
    """
    owners: dict[str, set[str]] = {}
    for action in parser._actions:  # noqa: SLF001
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        for name, subparser in action.choices.items():
            for sub in subparser._actions:  # noqa: SLF001
                for option in sub.option_strings:
                    owners.setdefault(option, set()).add(name)
    return owners


def reject_unknown_options(
    options: list[str],
    specs: dict[str, int],
    command: str | None = None,
    owners: dict[str, set[str]] | None = None,
) -> None:
    """Turn argparse's bare "unrecognized arguments" into a usable hint.

    A single typo (``--no-warp``) otherwise kills a run after the folder scan,
    which is exactly when the message is least useful.
    """
    known = set(specs) | {"-h", "--help", "--version"}
    messages: list[str] = []
    for token in options:
        name = token.split("=", 1)[0] if token.startswith("--") else token
        if not name.startswith("-"):
            continue
        if name in known:
            # Accepted somewhere, maybe not here: `apply --lrc` is a removed
            # flag, `plan --tsv` belongs to candidates.
            allowed = (owners or {}).get(name)
            if command and allowed and command not in allowed:
                if name in {"--lrc", "--lrc-dir"} and "clean" in allowed:
                    messages.append(
                        f"{name} 是 clean 的参数（清导出目录）；"
                        "想把歌词导出成文件，用 lrcpatch output --lrc <SOURCE:ID>"
                    )
                else:
                    messages.append(
                        f"{name} 是 {'/'.join(sorted(allowed))} 的参数，"
                        f"{command} 不接受它"
                    )
            continue
        # A negative number is an option *value* (--tree-limit -1), not a flag;
        # argparse accepts it, so it must not be reported as an unknown option.
        try:
            float(name)
        except ValueError:
            pass
        else:
            continue
        if name in {"--lrc", "--lrc-dir"}:
            messages.append(
                f"{name} 已移除：导出歌词文件改用 lrcpatch output"
                "（可一次导多种格式，不动音频）"
            )
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
            if "=" in token:
                options.append(token)
                index += 1
                continue
            if name == "--pick":
                # One flag may carry a whole album's decisions:
                #   --pick qq:11 qq:22 netease:33  ~/专辑
                # argparse stores one value per occurrence, so the flag is
                # repeated for each value here.
                index += 1
                taken = False
                while index < len(argv) and looks_like_pick(argv[index]):
                    options.extend(["--pick", argv[index]])
                    index += 1
                    taken = True
                if not taken:
                    options.append(name)
                continue
            options.append(token)
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
