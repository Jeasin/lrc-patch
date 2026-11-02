"""Command dispatch: validate options, take the run lock, call the command."""
from __future__ import annotations

import sys

from . import commands
from .cli_args import (
    COMMAND_NAMES,
    build_parser,
    main_help,
    normalize_argv,
    option_specs,
    reject_unknown_options,
)
from .config import APP_VERSION, SOURCE_NAMES, default_state_dir
from .errors import ToolError
from .render import print_intro, set_color
from .state import State, state_lock


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    argv = sys.argv[1:]
    if not argv:
        print_intro()
        return 0
    if "--no-color" in argv:
        set_color(False)
    if argv[0] in {"help", "?"}:
        print(main_help())
        return 0
    if "--version" in argv:
        print(APP_VERSION)
        return 0

    has_command = any(token in COMMAND_NAMES for token in argv)
    if ("-h" in argv or "--help" in argv) and not has_command:
        print(main_help())
        return 0

    specs = option_specs(parser)
    command, options, paths = normalize_argv(argv, specs)
    reject_unknown_options(options, specs)
    normalized = [command or "plan", *options]
    if paths:
        normalized.extend(["--", *paths])
    args = parser.parse_args(normalized)

    if getattr(args, "no_color", False):
        set_color(False)
    args.path = list(getattr(args, "paths", []) or [])
    state_dir = (args.state_dir or default_state_dir()).resolve()
    args.sources = [
        item.strip() for item in getattr(args, "sources", "").split(",") if item.strip()
    ]
    unknown = set(args.sources) - SOURCE_NAMES
    if unknown:
        parser.error(f"未知歌词源: {', '.join(sorted(unknown))}")
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
            return commands.cmd_candidates(args, state)
        if args.command == "apply":
            return commands.cmd_apply(args, state, state_dir)
        if args.command == "verify":
            return commands.cmd_verify(args, state)
        if args.command == "rollback":
            return commands.cmd_rollback(args, state, state_dir)

    print(main_help())
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
