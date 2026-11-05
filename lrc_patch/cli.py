"""Command dispatch: validate options, take the run lock, call the command."""
from __future__ import annotations

import sys

from . import commands
from .clean import cmd_clean
from .cli_args import (
    COMMAND_NAMES,
    build_parser,
    main_help,
    normalize_argv,
    option_owners,
    option_specs,
    reject_unknown_options,
)
from .config import (
    APP_VERSION,
    SOURCE_NAMES,
    canonical_source,
    default_state_dir,
    load_config,
)
from .errors import ToolError
from .render import print_intro, set_color
from .state import State, state_lock


def _help_config():
    """The help page should still open if the config file is broken."""
    try:
        return load_config()
    except ToolError:
        return None


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    argv = sys.argv[1:]
    if not argv:
        config = _help_config()
        print_intro(config.library if config else None)
        return 0
    if "--no-color" in argv:
        set_color(False)
    if argv[0] in {"help", "?"}:
        print(main_help(_help_config()))
        return 0
    if "--version" in argv:
        print(APP_VERSION)
        return 0

    has_command = any(token in COMMAND_NAMES for token in argv)
    if ("-h" in argv or "--help" in argv) and not has_command:
        print(main_help(_help_config()))
        return 0

    config = load_config()
    parser = build_parser(config)
    specs = option_specs(parser)
    command, options, paths = normalize_argv(argv, specs)
    reject_unknown_options(options, specs, command, option_owners(parser))
    normalized = [command or "plan", *options]
    if paths:
        normalized.extend(["--", *paths])
    args = parser.parse_args(normalized)

    if getattr(args, "no_color", False):
        set_color(False)
    args.path = list(getattr(args, "paths", []) or [])
    state_dir = (getattr(args, "state_dir", None) or default_state_dir()).resolve()
    args.src = [
        canonical_source(item)
        for item in getattr(args, "src", "").split(",")
        if item.strip()
    ]
    unknown = set(args.src) - SOURCE_NAMES
    if unknown:
        parser.error(
            f"未知歌词源: {', '.join(sorted(unknown))}（可用 ne / qq / lrclib）"
        )
    # Exactly one verbatim-format flag may be set (argparse enforces this);
    # fold it into a single target-format string for the sources.  `output` is
    # the exception: it writes files, so several formats in one run is the
    # point — it keeps them as a list and converts locally.
    target = None
    if args.command == "output":
        args.formats = [
            name
            for name in ("lrc", "a2", "eslrc", "yrc", "qrc", "ttml")
            if getattr(args, name, False)
        ]
        args.target_format = None
    else:
        target = next(
            (
                name
                for name in ("a2", "eslrc", "yrc", "qrc", "ttml")
                if getattr(args, name, False)
            ),
            None,
        )
        args.target_format = target
    # Private formats are native to exactly one source; refuse a source list
    # that cannot produce them instead of silently downgrading.  apply does not
    # query anything — it converts from the plan's native lyric — so the check
    # only applies when sources are actually asked (or a --pick chains a plan).
    asks_network = args.command in {"plan", "candidates", "output"} or bool(
        getattr(args, "pick", None) or getattr(args, "pick_file", None)
    )
    if asks_network and target == "qrc" and "qq" not in args.src:
        parser.error("--qrc 是 QQ 音乐的私有逐字格式，请用 --src 包含 qq（或改用 --a2/--eslrc/--ttml）")
    if asks_network and target == "yrc" and "netease" not in args.src:
        parser.error("--yrc 是网易云的私有逐字格式，请用 --src 包含 ne（或改用 --a2/--eslrc/--ttml）")
    if getattr(args, "workers", 1) < 1 or getattr(args, "workers", 1) > 10:
        parser.error("workers 必须在 1..10 之间")
    tolerance = getattr(args, "duration_tolerance", None)
    if tolerance is not None and not 0.5 <= tolerance <= 15:
        parser.error("--duration-tolerance 必须在 0.5..15 秒之间")
    if getattr(args, "top", 1) < 1:
        parser.error("--top 必须大于 0")
    if getattr(args, "tree_limit", 200) < 0:
        parser.error("--tree-limit 不能小于 0")
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")

    # scan is read-only and does not touch the state database, so it may run
    # while another plan/apply holds the lock.
    if args.command == "scan":
        return commands.cmd_scan(args, state_dir)
    if args.command == "config":
        return commands.cmd_config(args)
    if args.command == "status":
        # Read-only, and deliberately not under the run lock: "what is in the
        # database" must be answerable while a long plan is running.
        return commands.cmd_status(args, state_dir)

    with state_lock(state_dir):
        state = State(state_dir / "lyrics.sqlite3")
        if args.command == "plan":
            code = commands.cmd_plan(args, state, state_dir)
            if code == 0 and getattr(args, "apply", False):
                # One-shot review-then-write: the plan has just been printed, so
                # the user has seen exactly which tracks are ready.
                args.report = None
                args.no_preflight = False
                args.progress_every = 10
                return commands.cmd_apply(args, state, state_dir)
            return code
        if args.command == "candidates":
            return commands.cmd_candidates(args, state, state_dir)
        if args.command == "output":
            return commands.cmd_output(args, state, state_dir)
        if args.command == "apply":
            if getattr(args, "pick", None) or getattr(args, "pick_file", None):
                # "write these ones" — plan the explicit picks first (a pick is
                # already a human decision, so the 85-point gate does not apply)
                # and then chain into the writer, exactly like plan --apply.
                args.apply = True
                code = commands.cmd_plan(args, state, state_dir)
                if code != 0:
                    return code
                args.report = None
                args.no_preflight = False
                args.progress_every = 10
            return commands.cmd_apply(args, state, state_dir)
        if args.command == "verify":
            return commands.cmd_verify(args, state)
        if args.command == "rollback":
            return commands.cmd_rollback(args, state, state_dir)
        if args.command == "clean":
            return cmd_clean(args, state, state_dir)

    print(main_help(config))
    return 2

def run() -> int:
    """Console-script entry point: turn expected failures into one clear line."""
    try:
        return main()
    except ToolError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断；已完成的写入均已校验，未替换的文件保持原样。", file=sys.stderr)
        return 130
