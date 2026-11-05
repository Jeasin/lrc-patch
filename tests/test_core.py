"""Fast regressions that do not touch a real library or the network."""
from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrc_patch import commands, render, sorting, sources
from lrc_patch import qrc as qrc_mod
from lrc_patch.audio import lrc_dest
from lrc_patch.cli_args import (
    build_parser,
    normalize_argv,
    option_owners,
    option_specs,
    reject_unknown_options,
)
from lrc_patch.config import (
    Config,
    canonical_source,
    load_config,
    source_label,
    write_config_template,
)
from lrc_patch.errors import SourceError, ToolError
from lrc_patch.lyric_fmt import (
    count_ruby,
    has_timing,
    lines_to_a2,
    lines_to_eslrc,
    lines_to_lrc,
    lines_to_plain,
    lines_to_ttml,
    parse_a2,
    parse_eslrc,
    parse_qrc,
    parse_ttml,
    parse_yrc,
    render_native,
    strip_provider_tags,
)
from lrc_patch.matching import (
    assign_picks,
    best_partial_candidate,
    keep_previous_plan,
    parse_pick,
    pick_alternatives,
    plan_from_lyrics,
    plan_one,
    plan_with_pick,
    score_candidate,
)
from lrc_patch.models import Candidate, FetchResult, PlanResult, Track, TreeEntry
from lrc_patch.state import State, read_plan_cache, read_state_summary, row_get
from lrc_patch.text import (
    album_points,
    kind_set,
    kind_set_label,
    artist_parts,
    enhanced_to_line_lrc,
    lrc_timestamp_issue,
    lyric_kind,
    lyric_tag,
    match_note,
    sha256_text,
    strip_edition,
    title_points,
)


def make_track(**overrides) -> Track:
    values = {
        "path": Path("/library/Artist/Album/01 track.m4a"),
        "relpath": "Artist/Album/01 track.m4a",
        "fmt": "m4a",
        "title": "Track",
        "artist": "Artist",
        "album": "Album",
        "albumartist": "Artist",
        "duration": 200.0,
        "existing_lyrics": "",
        "size": 1,
        "mtime_ns": 1,
    }
    values.update(overrides)
    return Track(**values)


class EmptySource:
    name = "empty"

    def search(self, track: Track) -> list[Candidate]:
        return []


class FailingSource:
    name = "failing"

    def search(self, track: Track) -> list[Candidate]:
        raise SourceError("failing", "failing 连续 3 次失败：HTTP 503（服务繁忙）")


def qq_encrypt(plaintext: str) -> str:
    """Encrypt a QRC payload the way QQ Music serves it (zlib + DES variant)."""
    import zlib

    from lrc_patch.qrc import QRC_KEY, _ENCRYPT, _tripledes_crypt, _tripledes_key_setup

    data = bytearray(zlib.compress(plaintext.encode()))
    schedule = _tripledes_key_setup(QRC_KEY, _ENCRYPT)
    out = bytearray()
    for offset in range(0, len(data), 8):
        block = data[offset : offset + 8]
        out += _tripledes_crypt(block, schedule) if len(block) == 8 else block
    return bytes(out).hex()


def qq_lyric_payload(plain, *, encryption=True) -> tuple[int, dict]:
    """A ``GetPlayLyricInfo`` answer holding one QRC payload."""
    cipher = qq_encrypt(plain) if encryption else plain
    return 200, {"code": 0, "request": {"code": 0, "data": {"lyric": cipher}}}


class RecordingQqClient:
    """QQ stand-in: answers the session call, then replays canned payloads."""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.cache_keys: list[str] = []

    def request(self, source, url, min_interval=0.0, **kwargs):
        self.cache_keys.append(str(kwargs.get("cache_extra", "")))
        if kwargs.get("kind") == "session":
            data = {"session": {"uid": "1", "sid": "s", "userip": "ip"}}
        else:
            data = self.payloads.pop(0)
        return 200, {"code": 0, "request": {"code": 0, "data": data}}


class DefaultsTests(unittest.TestCase):
    def test_wrap_flags_are_gone(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["plan", "--wrap"])

    def test_unknown_option_is_suggested(self) -> None:
        specs = option_specs(build_parser())
        with self.assertRaises(ToolError) as ctx:
            reject_unknown_options(["--no-warp"], specs)
        message = str(ctx.exception)
        self.assertIn("已移除", message)
        self.assertIn("折行", message)

    def test_known_options_pass(self) -> None:
        specs = option_specs(build_parser())
        reject_unknown_options(["--workers", "2", "--pick", "netease:1"], specs)

    def test_negative_option_value_is_not_a_flag(self) -> None:
        specs = option_specs(build_parser())
        reject_unknown_options(["--tree-limit", "-1"], specs)

    def test_default_source_is_netease_plus_qq_fallback(self) -> None:
        args = build_parser().parse_args(["plan"])
        self.assertEqual(args.src, "netease,qq")

    def test_pick_then_apply_is_one_command(self) -> None:
        args = build_parser().parse_args(
            ["plan", "--pick", "netease:1", "--apply", "A"]
        )
        self.assertTrue(args.apply)
        self.assertEqual(args.paths, ["A"])


class ConfigTests(unittest.TestCase):
    def test_missing_file_means_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "config.toml")
        self.assertFalse(config.exists)
        self.assertIsNone(config.library)
        self.assertEqual(config.src, ())
        self.assertIsNone(config.workers)

    def test_reads_strings_arrays_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "# 注释\n"
                'library = "~/Music/Test"  # 行尾注释\n'
                'src = ["qq", "netease"]\n'
                "workers = 2\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertTrue(config.exists)
        self.assertEqual(config.library, Path.home() / "Music" / "Test")
        self.assertEqual(config.src, ("qq", "netease"))
        self.assertEqual(config.workers, 2)

    def test_unknown_key_is_named_with_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('library = "/x"\ncolour = "blue"\n', encoding="utf-8")
            with self.assertRaises(ToolError) as ctx:
                load_config(path)
        self.assertIn("未知配置项 colour", str(ctx.exception))
        self.assertIn(":2", str(ctx.exception))

    def test_bad_values_get_a_readable_error(self) -> None:
        cases = [
            ("workers = 0\n", "1..10"),
            ("workers = \"four\"\n", "整数"),
            ("src = [\"spotify\"]\n", "未知歌词源"),
            ("library = 3\n", "非空字符串"),
        ]
        for text, expected in cases:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.toml"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ToolError) as ctx:
                    load_config(path)
            self.assertIn(expected, str(ctx.exception))

    def test_parser_defaults_follow_the_config(self) -> None:
        config = Config(
            Path("/config.toml"),
            True,
            library=Path("/music/lib"),
            src=("qq",),
            workers=2,
        )
        args = build_parser(config).parse_args(["plan"])
        self.assertEqual(args.library, Path("/music/lib"))
        self.assertEqual(args.src, "qq")
        self.assertEqual(args.workers, 2)

    def test_init_writes_a_template_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "config.toml"
            self.assertTrue(write_config_template(path))
            self.assertIn("library", path.read_text(encoding="utf-8"))
            path.write_text("workers = 3\n", encoding="utf-8")
            self.assertFalse(write_config_template(path))
            self.assertEqual(path.read_text(encoding="utf-8"), "workers = 3\n")


class NormalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.specs = option_specs(build_parser())

    def test_options_before_paths(self) -> None:
        command, options, paths = normalize_argv(
            ["--workers", "2", "plan", "A", "B"], self.specs
        )
        self.assertEqual(command, "plan")
        self.assertEqual(options, ["--workers", "2"])
        self.assertEqual(paths, ["A", "B"])

    def test_options_between_paths(self) -> None:
        command, options, paths = normalize_argv(
            ["A", "--workers", "2", "B"], self.specs
        )
        self.assertIsNone(command)
        self.assertEqual(options, ["--workers", "2"])
        self.assertEqual(paths, ["A", "B"])

    def test_option_value_is_not_a_path(self) -> None:
        command, options, paths = normalize_argv(
            ["--pick", "netease:1", "--limit", "5", "A"], self.specs
        )
        self.assertIsNone(command)
        self.assertEqual(options, ["--pick", "netease:1", "--limit", "5"])
        self.assertEqual(paths, ["A"])

    def test_deprecated_path_flag(self) -> None:
        command, options, paths = normalize_argv(
            ["--path", "A", "B"], self.specs
        )
        self.assertIsNone(command)
        self.assertEqual(options, [])
        self.assertEqual(paths, ["A", "B"])

    def test_html_optional_filename(self) -> None:
        _, options, paths = normalize_argv(
            ["A", "--html", "review.html"], self.specs
        )
        self.assertEqual(options, ["--html", "review.html"])
        self.assertEqual(paths, ["A"])

    def test_html_without_filename_keeps_next_path(self) -> None:
        _, options, paths = normalize_argv(["A", "--html", "B"], self.specs)
        self.assertEqual(options, ["--html"])
        self.assertEqual(paths, ["A", "B"])

    def test_double_dash_stops_option_parsing(self) -> None:
        _, options, paths = normalize_argv(
            ["A", "--", "--workers", "B"], self.specs
        )
        self.assertEqual(options, [])
        self.assertEqual(paths, ["A", "--workers", "B"])


class PlanClassificationTests(unittest.TestCase):
    def test_provider_failure_is_error_not_no_match(self) -> None:
        result = plan_one(make_track(), [FailingSource(), EmptySource()], True)
        self.assertEqual(result.status, "error")
        self.assertIn("请求失败", result.reason)

    def test_empty_answers_are_no_match(self) -> None:
        result = plan_one(make_track(), [EmptySource()], True)
        self.assertEqual(result.status, "no_match")

    def test_existing_lyrics_short_circuits(self) -> None:
        track = make_track(existing_lyrics="[00:01.00]hello")
        result = plan_one(track, [FailingSource()], True)
        self.assertEqual(result.status, "skip_existing")

    def test_failure_does_not_replace_a_ready_plan(self) -> None:
        ready = {"status": "ready", "fingerprint": "fp"}
        result = plan_one(make_track(), [FailingSource()], True)
        self.assertTrue(keep_previous_plan(ready, "fp", result))
        self.assertFalse(keep_previous_plan(ready, "changed", result))
        self.assertFalse(keep_previous_plan({"status": "error"}, "fp", result))


class MatchingTests(unittest.TestCase):
    def test_duration_gate(self) -> None:
        track = make_track(duration=200.0)
        good = Candidate("s", "1", "Track", ["Artist"], "Album", 200.5)
        far = Candidate("s", "2", "Track", ["Artist"], "Album", 203.0)
        self.assertEqual(score_candidate(track, good), (100, 0.5))
        self.assertEqual(score_candidate(track, far)[0], 0)

    def test_timestamp_checks(self) -> None:
        good = "[00:10.00]a\n[03:20.00]b"
        self.assertFalse(lrc_timestamp_issue(good, 200.0))
        self.assertIn("超过", lrc_timestamp_issue("[00:10.00]a\n[05:00.00]b", 200.0))
        self.assertIn("TV size", lrc_timestamp_issue("[00:10.00]a\n[00:30.00]b", 200.0))


class VersionScoringTests(unittest.TestCase):
    """Version suffixes must not wipe out the right candidate."""

    def setUp(self) -> None:
        self.track = make_track(
            title="Beautiful World",
            artist="宇多田ヒカル",
            album="One Last Kiss",
            duration=358.9,
        )

    def _score(self, title: str, album: str = "One Last Kiss", duration: float = 359.0) -> int:
        candidate = Candidate(
            "netease", "1", title, ["宇多田ヒカル"], album, duration
        )
        return score_candidate(self.track, candidate)[0]

    def test_version_suffix_matches_with_album(self) -> None:
        self.assertEqual(self._score("Beautiful World (Da Capo Version)"), 87)

    def test_instrumental_variant_is_rejected(self) -> None:
        self.assertEqual(
            self._score("Beautiful World (Da Capo Version [Instrumental])"), 0
        )

    def test_feat_variant_stays_below_threshold(self) -> None:
        self.assertEqual(self._score("Beautiful World (feat. Someone)"), 75)

    def test_album_edition_is_only_three_points_lower(self) -> None:
        self.assertEqual(self._score("Beautiful World", "One Last Kiss (Deluxe)"), 97)

    def test_symbol_album_with_edition(self) -> None:
        track = make_track(
            title="Tides", artist="Ed Sheeran", album="= (Tour Edition)", duration=195.8
        )
        candidate = Candidate(
            "netease", "1", "Tides", ["Ed Sheeran"], "= (Christmas Edition)", 195.8
        )
        self.assertEqual(score_candidate(track, candidate)[0], 97)

    def test_match_note_explains_the_deduction(self) -> None:
        candidate = Candidate(
            "netease",
            "1",
            "Beautiful World (Da Capo Version)",
            ["宇多田ヒカル"],
            "One Last Kiss",
            359.0,
        )
        self.assertIn("Da Capo Version", match_note(self.track, candidate))

    def test_album_year_prefix_and_bare_year(self) -> None:
        self.assertEqual(strip_edition("(2021)One Last Kiss"), "One Last Kiss")
        self.assertEqual(strip_edition("1989"), "1989")
        self.assertEqual(strip_edition("1989 (Deluxe Edition)"), "1989")


class ArtistParsingTests(unittest.TestCase):
    """Every separator a hand-edited artist tag may use splits the same way."""

    def test_common_separators_are_equivalent(self) -> None:
        expected = ["aimer", "阿部真央"]
        for value in (
            "Aimer; 阿部真央",
            "Aimer；阿部真央",
            "Aimer／阿部真央",
            "Aimer / 阿部真央",
            "Aimer、阿部真央",
            "Aimer & 阿部真央",
            "Aimer feat. 阿部真央",
            "Aimer ft 阿部真央",
        ):
            self.assertEqual(artist_parts(value), expected, value)

    def test_latin_slash_stays_whole(self) -> None:
        self.assertEqual(artist_parts("AC/DC"), ["acdc"])

    def test_collaborator_in_artist_list_is_the_same_recording(self) -> None:
        track = make_track(
            title="for ロンリー (with 阿部真央)",
            artist="Aimer; 阿部真央",
            album="daydream",
            duration=292.0,
        )
        candidate = Candidate(
            "netease", "1", "for ロンリー", ["Aimer", "阿部真央"], "daydream", 292.1
        )
        self.assertEqual(score_candidate(track, candidate)[0], 100)

    def test_collaborator_missing_from_artist_list_is_a_different_take(self) -> None:
        track = make_track(
            title="ninelie (with chelly)",
            artist="Aimer; chelly",
            album="daydream",
            duration=262.0,
        )
        candidate = Candidate("netease", "1", "ninelie", ["Aimer"], "daydream", 262.1)
        self.assertLess(score_candidate(track, candidate)[0], 85)


class TitleDecorationTests(unittest.TestCase):
    def _score(self, file_title: str, cand_title: str, artist: str = "Aimer") -> int:
        track = make_track(title=file_title, artist=artist, album="daydream", duration=289.0)
        candidate = Candidate("netease", "1", cand_title, [artist], "daydream", 289.1)
        return score_candidate(track, candidate)[0]

    def test_kana_reading_is_not_a_version(self) -> None:
        self.assertEqual(self._score("Hz", "Hz(ヘルツ)"), 95)
        self.assertIn("读音", title_points("Hz", "Hz(ヘルツ)")[1])

    def test_dash_translation_is_not_a_different_song(self) -> None:
        file_title = "太陽が昇らない世界"
        cand_title = "太陽が昇らない世界 - A World Where the Sun Never Rises"
        self.assertEqual(self._score(file_title, cand_title), 95)
        self.assertIn("译名", title_points(file_title, cand_title)[1])

    def test_cover_tag_is_not_translation(self) -> None:
        self.assertEqual(self._score("closer", "Closer——（cover:Aimer）"), 80)
        self.assertIn("附注", title_points("closer", "Closer——（cover:Aimer）")[1])

    def test_album_split_single_matches_its_side(self) -> None:
        self.assertEqual(album_points("us", "insane dream / us")[0], 12)
        self.assertEqual(album_points("daydream", "insane dream / us")[0], 0)


class PickTemplateTests(unittest.TestCase):
    """`candidates --template` hands back an editable pick-file."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.track = make_track()

    def _row(self, **overrides) -> "commands.CandRow":
        values = {
            "symbol": "✓",
            "code": "1;32",
            "ident": "netease:1",
            "score": "100",
            "diff": "Δ+0.1s",
            "kind": "待取词",
            "title": "Track",
            "artist": "Artist",
            "album": "Album",
        }
        values.update(overrides)
        return commands.CandRow(**values)

    def test_auto_matched_rows_are_ready_to_use(self) -> None:
        path = commands.write_pick_template(
            self.dir / "picks.tsv", [self.track], {str(self.track.path): [self._row()]}
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        active = [
            line for line in lines if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(active, ["Artist/Album/01 track.m4a\tnetease:1"])

    def test_uncertain_rows_are_commented_with_alternatives(self) -> None:
        rows = [
            self._row(
                symbol="!",
                ident="netease:2",
                score="-",
                diff="Δ+9.0s",
                metadata_score=95,
                notes=["时长差 9.0s，超过容差 2.0s"],
            ),
            self._row(symbol="·", ident="netease:3", score="72"),
        ]
        path = commands.write_pick_template(
            self.dir / "picks.tsv", [self.track], {str(self.track.path): rows}
        )
        text = path.read_text(encoding="utf-8")
        active = [
            line
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(active, [])
        self.assertIn("# Artist/Album/01 track.m4a", text)
        self.assertIn("netease:2", text)
        self.assertIn("netease:3", text)


class PlanNoteFormatTests(unittest.TestCase):
    def test_new_reason_splits_artist_and_notes(self) -> None:
        self.assertEqual(
            render._reason_parts("Aimer；标题附读音「ヘルツ」"),
            ("Aimer", "标题附读音「ヘルツ」"),
        )

    def test_legacy_reason_does_not_leak_the_lyric_tag(self) -> None:
        self.assertEqual(render._reason_parts("LRC / Aimer"), ("Aimer", ""))
        self.assertEqual(
            render._reason_parts("LRC / Aimer · 标题附加信息（Live）"),
            ("Aimer", "标题附加信息（Live）"),
        )

    def test_picked_row_keeps_the_label_as_explanation(self) -> None:
        label = "人工指定 netease:1（时长已核验 Δ0.1s）"
        self.assertEqual(render._reason_parts(label), ("", label))


class TreeAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        render.set_color(False)
        self._width = render.screen_width
        render.screen_width = lambda: 100

    def tearDown(self) -> None:
        render.screen_width = self._width

    def _render(self, entries: list[TreeEntry]) -> list[str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render.print_tree(entries, Path("/library"), 200)
        return [render.strip_ansi(line) for line in buffer.getvalue().splitlines()]

    def test_notes_start_at_same_column_and_stay_on_one_line(self) -> None:
        long_note = "请求失败：" + "非常长的原因，" * 20
        entries = [
            TreeEntry(Path("/library/A/01 short.m4a"), "ready", "短备注"),
            TreeEntry(Path("/library/A/02 a much longer name.m4a"), "error", long_note),
        ]
        lines = self._render(entries)
        first = next(line for line in lines if "短备注" in line)
        second = next(line for line in lines if "请求失败" in line)
        column = first.index("短备注")
        self.assertEqual(column, second.index("请求失败"))
        # The note is not wrapped: the whole reason is on that row and no
        # indented continuation follows it.
        self.assertTrue(second.rstrip().endswith("非常长的原因，"))
        self.assertEqual(len(second.rstrip()), column + len(long_note))

    def test_long_notes_are_never_wrapped(self) -> None:
        long_note = "错误：" + "x" * 300
        lines = self._render(
            [TreeEntry(Path("/library/A/01 track.m4a"), "error", long_note)]
        )
        matching = [line for line in lines if "错误：" in line]
        self.assertEqual(len(matching), 1)
        self.assertEqual(len(matching[0].rstrip()), matching[0].index("错误：") + len(long_note))

    def test_line_counts_align_within_an_album(self) -> None:
        entries = [
            TreeEntry(Path("/library/A/01 a.m4a"), "ready", "netease · 100分 · LRC 6行"),
            TreeEntry(Path("/library/A/02 b.m4a"), "ready", "netease · 100分 · LRC 23行"),
        ]
        lines = self._render(entries)
        columns = {line.index("行") for line in lines if "LRC" in line}
        self.assertEqual(len(columns), 1)

    def test_plan_cells_share_one_column_each(self) -> None:
        rows = [
            {
                "status": "ready",
                "source": "netease",
                "score": 7,
                "duration_diff": 10.3,
                "lyric_kind": "lrc",
                "lyric_lines": 6,
                "reason": "周杰伦",
            },
            {
                "status": "ready",
                "source": "qq",
                "score": 100,
                "duration_diff": 0.3,
                "lyric_kind": "lrc",
                "lyric_lines": 101,
                "reason": "周杰伦",
            },
        ]
        entries = []
        for index, row in enumerate(rows):
            cells, note = render.plan_fields(row)
            entries.append(
                TreeEntry(Path(f"/library/A/0{index + 1} song.m4a"), "ready", note, cells)
            )
        lines = self._render(entries)
        for marker in ("分", "LRC", "行", "周杰伦"):
            columns = {line.index(marker) for line in lines if marker in line}
            self.assertEqual(len(columns), 1, f"{marker} 没有对齐：{columns}")
        # A wider gap keeps its seconds column (and decimal point) aligned, so
        # "Δ0.3s" gets the extra leading space rather than pushing "s" right.
        ends = {
            line.index("s", line.index("Δ")) for line in lines if "Δ" in line
        }
        self.assertEqual(len(ends), 1, f"时长差没有对齐：{ends}")
        # Same start column for the source cell, so "qq" sits under "netease".
        starts = {
            line.index("qq") if "qq" in line else line.index("netease")
            for line in lines
            if "qq" in line or "netease" in line
        }
        self.assertEqual(len(starts), 1)

    def test_explanation_rows_stay_a_single_note(self) -> None:
        cells, note = render.plan_fields(
            {
                "status": "suspect",
                "source": "qq",
                "score": 100,
                "duration_diff": 0.2,
                "lyric_kind": "lrc",
                "lyric_lines": 20,
                "reason": "LRC 末尾 04:20 超过音频 03:10",
            }
        )
        self.assertEqual(cells, ())
        self.assertEqual(note, "qq 100分；LRC 末尾 04:20 超过音频 03:10")


class StateMigrationTests(unittest.TestCase):
    def test_legacy_network_no_match_becomes_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            State(path)
            with State(path).connection() as conn:
                conn.executemany(
                    "INSERT INTO plans(path,fingerprint,planned_at,status,reason) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (
                            "/legacy.m4a",
                            "fp",
                            "now",
                            "no_match",
                            "netease: ToolError: netease 连续请求失败: 返回 code 406",
                        ),
                        ("/genuine.m4a", "fp", "now", "no_match", "netease: 无搜索结果"),
                    ],
                )
            state = State(path)
            self.assertEqual(state.get_plan("/legacy.m4a")["status"], "error")
            self.assertEqual(state.get_plan("/genuine.m4a")["status"], "no_match")

    def test_legacy_database_gains_the_new_columns(self) -> None:
        """2.10 databases must keep working without a manual migration."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE plans (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,"
                " planned_at TEXT NOT NULL, status TEXT NOT NULL,"
                " reason TEXT NOT NULL DEFAULT '', source TEXT, source_id TEXT,"
                " match_title TEXT, match_artist TEXT, match_album TEXT,"
                " match_duration REAL, duration_diff REAL,"
                " score INTEGER NOT NULL DEFAULT 0, lyric TEXT, lyric_sha TEXT,"
                " lyric_kind TEXT, lyric_lines INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO plans(path,fingerprint,planned_at,status,reason,"
                "score,lyric,lyric_kind,lyric_lines) VALUES(?,?,?,?,?,?,?,?,?)",
                ("/old.m4a", "fp", "2026-01-01T00:00:00+00:00", "ready", "Artist",
                 100, "[00:01.00]x\n", "lrc", 1),
            )
            conn.commit()
            conn.close()

            state = State(path)
            row = state.get_plan("/old.m4a")
            self.assertEqual(row["status"], "ready")
            self.assertEqual(row_get(row, "src_set", ""), "")
            self.assertEqual(row_get(row, "native_kind", ""), "")
            with self.assertRaises(ToolError):
                commands.lyric_for_target(row, "a2")


class JsonClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tempdir.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_bodyless_http_error_reports_its_status(self) -> None:
        # urlopen raises HTTPError with fp=None: exc.read() must not explode on
        # 3.9 (KeyError: 'file') and hide the 503 we want to retry on.
        def fail(request, timeout=30):
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)

        with mock.patch.object(sources.urllib.request, "urlopen", fail):
            status, payload, headers = sources.UrllibTransport().request(
                "GET", "https://example.invalid/x"
            )
        self.assertEqual(status, 503)
        self.assertEqual(payload, b"")
        self.assertEqual(headers, {})

    def test_retries_then_raises_and_does_not_cache(self) -> None:
        calls: list[str] = []

        class Response:
            def __init__(self, payload: bytes):
                self.payload = payload

            def read(self) -> bytes:
                return self.payload

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args) -> None:
                return None

        def fail(request, timeout=30):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)

        url = "https://lrclib.net/api/search?track_name=x"
        with mock.patch.object(sources.urllib.request, "urlopen", fail), mock.patch.object(
            sources.time, "sleep", lambda _seconds: None
        ):
            client = sources.JsonClient(
                self.state, transport=sources.UrllibTransport()
            )
            with self.assertRaises(SourceError):
                client.get("lrclib", url, 0.0)

        self.assertEqual(len(calls), sources.JsonClient.MAX_ATTEMPTS)
        self.assertIsNone(self.state.cached_http(sha256_text(url)))
        health = client.health()
        self.assertEqual(health[0][0], "lrclib")
        self.assertEqual(health[0][1:3], (1, 1))
        self.assertEqual(health[0][3], 0)

        # A second exhausted request opens the per-source cooldown.
        with mock.patch.object(sources.urllib.request, "urlopen", fail), mock.patch.object(
            sources.time, "sleep", lambda _seconds: None
        ):
            with self.assertRaises(SourceError):
                client.get("lrclib", url, 0.0)
        self.assertGreaterEqual(client.health()[0][3], 58)
        self.assertEqual(len(calls), sources.JsonClient.MAX_ATTEMPTS * 2)

    def test_rate_limit_parks_the_source(self) -> None:
        """netease answers 200 + {"code":405} when it throttles; that is not a verdict."""

        class Response:
            def read(self) -> bytes:
                return b'{"code": 405, "msg": "\\u64cd\\u4f5c\\u9891\\u7e41"}'

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args) -> None:
                return None

        url = "https://music.163.com/api/search/get"
        with mock.patch.object(
            sources.urllib.request, "urlopen", lambda request, timeout=30: Response()
        ), mock.patch.object(sources.time, "sleep", lambda _seconds: None):
            client = sources.JsonClient(
                self.state, transport=sources.UrllibTransport()
            )
            with self.assertRaises(SourceError) as ctx:
                client.get("netease", url, 0.0)
        self.assertIn("限流", str(ctx.exception))
        self.assertGreaterEqual(client.cooldown("netease"), 170)

        # Every later track fails fast instead of hammering a blocked provider.
        with mock.patch.object(
            sources.urllib.request,
            "urlopen",
            side_effect=AssertionError("must not hit the network while parked"),
        ):
            with self.assertRaises(SourceError) as parked:
                client.get("netease", url, 0.0)
        self.assertIn("已暂停", str(parked.exception))

    def test_success_is_cached(self) -> None:
        class Response:
            def read(self) -> bytes:
                return b'{"code": 200, "result": {"songs": [{"id": 1}]}}'

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args) -> None:
                return None

        url = "https://music.163.com/api/search/get/web?s=x"
        with mock.patch.object(
            sources.urllib.request, "urlopen", lambda request, timeout=30: Response()
        ):
            client = sources.JsonClient(
                self.state, transport=sources.UrllibTransport()
            )
            code, data = client.get(
                "netease", url, 0.0, lambda payload: bool(payload["result"]["songs"])
            )
        self.assertEqual(code, 200)
        self.assertEqual(data["result"]["songs"][0]["id"], 1)
        self.assertIsNotNone(self.state.cached_http(sha256_text(url)))


    def test_empty_search_is_cached_for_a_day(self) -> None:
        """candidates then plan must not ask the same empty query twice."""

        class Response:
            def read(self) -> bytes:
                return b'{"code": 200, "result": {"songs": []}}'

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args) -> None:
                return None

        url = "https://music.163.com/api/search/get"
        with mock.patch.object(
            sources.urllib.request, "urlopen", lambda request, timeout=30: Response()
        ):
            client = sources.JsonClient(
                self.state, transport=sources.UrllibTransport()
            )
            code, _ = client.get(
                "netease", url, 0.0, cache_predicate=sources.netease_cache_ttl
            )
        self.assertEqual(code, 200)
        cached = self.state.cached_http(sha256_text(url))
        self.assertIsNotNone(cached)
        self.assertEqual(cached["data"]["result"]["songs"], [])


class ScanListTests(unittest.TestCase):
    def _track(
        self,
        folder: str,
        name: str,
        *,
        lyrics: str = "",
        album: str = "Album",
        title: str = "Title",
    ) -> Track:
        return Track(
            path=Path(folder) / name,
            relpath=name,
            fmt="m4a",
            title=title,
            artist="Artist",
            album=album,
            albumartist="Artist",
            duration=213.0,
            existing_lyrics=lyrics,
            size=1024 * 1024,
            mtime_ns=0,
        )

    def test_duration_and_size_units(self) -> None:
        self.assertEqual(render.format_duration(213), "03:33")
        self.assertEqual(render.format_duration(3725), "1:02:05")
        self.assertEqual(render.format_size(1024), "1.0 KB")
        self.assertEqual(render.format_size(1536), "1.5 KB")

    def test_scan_list_marks_lyrics_and_tag_gaps(self) -> None:
        tracks = [
            self._track(
                "/lib/A/(2020)Album",
                "01 A.m4a",
                lyrics="[00:01.00]x\n[00:02.00]y",
            ),
            self._track("/lib/A/(2020)Album", "02 B.m4a", title=""),
        ]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render.print_scan_list(tracks, Path("/lib"))
        out = buffer.getvalue()
        self.assertIn("✓ 01 A.m4a", out)
        self.assertIn("LRC 2 行", out)
        self.assertIn("· 02 B.m4a", out)
        self.assertIn("无歌词", out)
        self.assertIn("缺 标题", out)
        # Singles have no album tag on purpose: that must not be reported.
        self.assertNotIn("缺 专辑", out)

        # Every row's format column starts at the same display column.
        offsets = set()
        for line in out.splitlines():
            if "M4A" not in line:
                continue
            plain = render.strip_ansi(line)
            offsets.add(render.disp_width(plain[: plain.index("M4A")]))
        self.assertEqual(len(offsets), 1)

    def test_scan_line_counts_align_within_an_album(self) -> None:
        tracks = [
            self._track("/lib/A/Album", "01 A.m4a", lyrics="[00:01.00]x"),
            self._track(
                "/lib/A/Album",
                "02 B.m4a",
                lyrics="\n".join(f"[00:{i:02d}.00]x" for i in range(12)),
            ),
        ]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render.print_scan_list(tracks, Path("/lib"))
        rows = [
            render.strip_ansi(line)
            for line in buffer.getvalue().splitlines()
            if "LRC" in line
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row.index("行") for row in rows}), 1)

    def test_scan_shows_a_cached_plan_next_to_missing_lyrics(self) -> None:
        track = self._track("/lib/A/(2020)Album", "01 A.m4a")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render.print_scan_list(
                [track], Path("/lib"), 0, {str(track.path): ("qq", 100, "ready", "qrc")}
            )
        out = render.strip_ansi(buffer.getvalue())
        self.assertIn("无歌词", out)
        self.assertIn("qq:100", out)

    def test_scan_shows_a_plan_over_existing_lyrics_too(self) -> None:
        # `plan --force` on a file that already carries lyrics still leaves a
        # verdict in the database; hiding it made the row look like the plan
        # never happened (feedback 20261105-193209).
        track = self._track(
            "/lib/A/(2020)Album", "01 A.m4a", lyrics="[00:01.00]x"
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render.print_scan_list(
                [track], Path("/lib"), 0, {str(track.path): ("qq", 100, "ready", "qrc")}
            )
        out = render.strip_ansi(buffer.getvalue())
        self.assertIn("LRC 1 行", out)
        self.assertIn("qq:100", out)

    def test_scan_uses_finder_folder_order(self) -> None:
        tracks = [
            self._track("/lib/Aimer/Album", "01 A.m4a"),
            self._track("/lib/五月天/(2016)自传", "01 B.m4a"),
        ]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render.print_scan_list(tracks, Path("/lib"))
        out = render.strip_ansi(buffer.getvalue())
        self.assertLess(out.index("五月天"), out.index("Aimer"))

    def test_scan_lists_everything_by_default(self) -> None:
        parser = build_parser()
        scan = parser.parse_args(["scan", "--", "somewhere"])
        plan = parser.parse_args(["plan", "--", "somewhere"])
        self.assertEqual(scan.tree_limit, 0)
        self.assertEqual(plan.tree_limit, 200)


class LyricFormatTests(unittest.TestCase):
    def test_formats_are_distinguished(self) -> None:
        self.assertEqual(lyric_kind("just words\nno timing"), "plain")
        self.assertEqual(lyric_kind("[00:12.34]line\n[00:15.00]two"), "lrc")
        self.assertEqual(
            lyric_kind("[00:12.34]<00:12.34>word <00:12.80>two"), "a2"
        )
        self.assertEqual(
            lyric_kind("[00:12.34]你好[00:13.00]世界"), "eslrc"
        )
        self.assertEqual(lyric_kind("[1234,567](1234,567,0)word"), "yrc")
        self.assertEqual(lyric_kind("[1234,567](1234,567)word"), "qrc")
        self.assertEqual(
            lyric_kind('<?xml version="1.0"?><tt xmlns="http://www.w3.org/ns/ttml">'),
            "ttml",
        )

    def test_tags_and_colours_differ(self) -> None:
        self.assertEqual(lyric_tag("plain"), "TXT")
        self.assertEqual(lyric_tag("lrc"), "LRC")
        self.assertEqual(lyric_tag("a2"), "A2")
        self.assertEqual(lyric_tag("eslrc"), "ESLRC")
        self.assertEqual(lyric_tag("yrc"), "YRC")
        self.assertEqual(lyric_tag("qrc"), "QRC")
        self.assertEqual(lyric_tag("ttml"), "TTML")
        colours = {
            render.LYRIC_COLORS[kind]
            for kind in ("plain", "lrc", "a2", "eslrc", "yrc", "qrc", "ttml")
        }
        self.assertEqual(len(colours), 7)

    def test_timeline_check_skips_advanced_formats(self) -> None:
        yrc = "[1000,500](1000,500,0)word"
        self.assertEqual(lrc_timestamp_issue(yrc, 200.0), "")
        self.assertIn(
            "超过", lrc_timestamp_issue("[00:10.00]a\n[05:00.00]b", 200.0)
        )


class SortingTests(unittest.TestCase):
    def test_script_groups_follow_finder(self) -> None:
        names = ["YOASOBI", "ヨルシカ", "Aimer", "『ユイカ』", "88rising", "五月天"]
        self.assertEqual(
            sorting.sort_names(names),
            ["『ユイカ』", "88rising", "五月天", "Aimer", "YOASOBI", "ヨルシカ"],
        )

    @unittest.skipIf(
        sorting.collation_name() == "code-point", "zh_CN collation not installed"
    )
    def test_han_names_sort_by_pinyin(self) -> None:
        names = ["周杰伦", "五月天", "久石譲", "宮脇咲良", "林俊杰", "张杰"]
        self.assertEqual(
            sorting.sort_names(names),
            ["宮脇咲良", "久石譲", "林俊杰", "五月天", "张杰", "周杰伦"],
        )

    def test_numbers_compare_naturally(self) -> None:
        self.assertEqual(
            sorting.sort_names(["10 x.m4a", "2 x.m4a", "1 x.m4a"]),
            ["1 x.m4a", "2 x.m4a", "10 x.m4a"],
        )


class SearchRequestCountTests(unittest.TestCase):
    """One search per track unless the first batch has nothing usable."""

    class Client:
        def __init__(self, batches: list[list[dict]]) -> None:
            self.batches = batches
            self.queries: list[str] = []

        def request(self, source, url, min_interval, **kwargs):
            self.queries.append(kwargs.get("body", {}).get("s", ""))
            index = min(len(self.queries) - 1, len(self.batches) - 1)
            return 200, {"code": 200, "result": {"songs": self.batches[index]}}

        def get(self, *args, **kwargs):  # pragma: no cover - search only
            raise AssertionError("search must not fetch lyrics")

    @staticmethod
    def song(title: str, artist: str = "Artist", album: str = "Album") -> dict:
        return {
            "id": 1,
            "name": title,
            "artists": [{"name": artist}],
            "album": {"name": album},
            "duration": 200000,
        }

    def test_perfect_first_batch_costs_one_request(self) -> None:
        client = self.Client([[self.song("Track")], [self.song("Track")]])
        source = sources.NeteaseSource(client)
        found = source.search(make_track(title="Track", artist="Artist", album="Album"))
        self.assertEqual(len(client.queries), 1)
        self.assertEqual(found[0].title, "Track")

    def test_unrelated_first_batch_triggers_the_fallback(self) -> None:
        client = self.Client(
            [[self.song("Something Else", artist="Nobody")], [self.song("Track")]]
        )
        source = sources.NeteaseSource(client)
        source.search(make_track(title="Track", artist="Artist", album="Album"))
        self.assertEqual(len(client.queries), 2)
        self.assertEqual(client.queries[1], "Track")

    def test_force_search_keeps_both_queries_for_pick(self) -> None:
        client = self.Client([[self.song("Track")], [self.song("Track")]])
        source = sources.NeteaseSource(client)
        source.search(
            make_track(title="Track", artist="Artist", album="Album"), exhaustive=True
        )
        self.assertEqual(len(client.queries), 2)


class QrcDecryptTests(unittest.TestCase):
    """The Triple-DES port must round-trip against the LDDC reference."""

    def _reference(self, plaintext: bytes) -> str:
        # Re-encrypt with the reference (ENCRYPT direction) so the test does
        # not depend on a canned ciphertext.  Uses the same key schedule the
        # decryptor uses, only in the encrypt mode.
        import zlib

        from lrc_patch.qrc import QRC_KEY, _tripledes_crypt, _tripledes_key_setup, _ENCRYPT

        data = bytearray(zlib.compress(plaintext))
        schedule = _tripledes_key_setup(QRC_KEY, _ENCRYPT)
        out = bytearray()
        for off in range(0, len(data), 8):
            block = data[off : off + 8]
            out += _tripledes_crypt(block, schedule) if len(block) == 8 else block
        return bytes(out).hex()

    def test_roundtrip_short(self) -> None:
        plaintext = "<Lyric_1 LyricType=\"1\" LyricContent=\"[0,5000](0,200)词\"/>"
        self.assertEqual(qrc_mod.qrc_decrypt(self._reference(plaintext.encode())), plaintext)

    def test_roundtrip_multi_block(self) -> None:
        plaintext = "测试歌词" * 200
        self.assertEqual(
            qrc_mod.qrc_decrypt(self._reference(plaintext.encode())), plaintext
        )

    def test_bad_hex_raises(self) -> None:
        with self.assertRaises(ValueError):
            qrc_mod.qrc_decrypt("zzzz")

    def test_qrc_to_enhanced_converts_words(self) -> None:
        qrc_text = "[0,5000](0,200)我(200,300)爱(500,200)你\n[5000,3000](5000,200)中(5200,200)国"
        enhanced = qrc_mod.qrc_to_enhanced(qrc_text)
        self.assertIn("[00:00.00]<00:00.00>我<00:00.20>爱<00:00.50>你", enhanced)
        self.assertIn("[00:05.00]<00:05.00>中<00:05.20>国", enhanced)

    def test_qrc_to_enhanced_keeps_tags(self) -> None:
        qrc_text = "[ti:Hz(ヘルツ)]\n[0,5000](0,200)词(200,300)语"
        enhanced = qrc_mod.qrc_to_enhanced(qrc_text)
        self.assertIn("[ti:Hz(ヘルツ)]", enhanced)

    def test_a2_is_detected_as_a2_kind(self) -> None:
        a2 = "[00:00.00]<00:00.00>我<00:00.20>爱"
        self.assertEqual(lyric_kind(a2), "a2")

    def test_enhanced_to_line_lrc_flattens(self) -> None:
        a2 = "[00:00.00]<00:00.00>我<00:00.20>爱<00:00.50>你\n[00:05.00]<00:05.00>中<00:05.20>国"
        line = enhanced_to_line_lrc(a2)
        self.assertIn("[00:00.00]我爱你", line)
        self.assertNotIn("<00:00.20>", line)
        self.assertEqual(lyric_kind(line), "lrc")

    def test_enhanced_to_line_lrc_passthrough_plain(self) -> None:
        plain = "[00:00.00]我爱你"
        self.assertEqual(enhanced_to_line_lrc(plain), plain)

    def test_eslrc_flattens_to_line_lrc(self) -> None:
        eslrc = "[00:00.00]我[00:00.20]爱[00:00.50]你"
        line = enhanced_to_line_lrc(eslrc)
        self.assertEqual(lyric_kind(line), "lrc")
        self.assertIn("我爱你", line)


class EsLrcSuffixTests(unittest.TestCase):
    def test_a2_uses_alrc_suffix(self) -> None:
        dest = lrc_dest(
            Path("/library/Aimer/(2016)daydream/08 Hz.m4a"),
            Path("/library"),
            Path("/cache/lrc"),
            kind="a2",
        )
        self.assertTrue(str(dest).endswith("08 Hz.alrc"))

    def test_eslrc_uses_eslrc_suffix(self) -> None:
        dest = lrc_dest(
            Path("/library/Aimer/(2016)daydream/08 Hz.m4a"),
            Path("/library"),
            Path("/cache/lrc"),
            kind="eslrc",
        )
        self.assertTrue(str(dest).endswith("08 Hz.eslrc"))

    def test_yrc_uses_yrc_suffix(self) -> None:
        dest = lrc_dest(
            Path("/library/Aimer/(2016)daydream/08 Hz.m4a"),
            Path("/library"),
            Path("/cache/lrc"),
            kind="yrc",
        )
        self.assertTrue(str(dest).endswith("08 Hz.yrc"))

    def test_qrc_uses_qrc_suffix(self) -> None:
        dest = lrc_dest(
            Path("/library/Aimer/(2016)daydream/08 Hz.m4a"),
            Path("/library"),
            Path("/cache/lrc"),
            kind="qrc",
        )
        self.assertTrue(str(dest).endswith("08 Hz.qrc"))

    def test_plain_lrc_uses_lrc_suffix(self) -> None:
        dest = lrc_dest(
            Path("/library/Aimer/(2016)daydream/08 Hz.m4a"),
            Path("/library"),
            Path("/cache/lrc"),
            kind="lrc",
        )
        self.assertTrue(str(dest).endswith("08 Hz.lrc"))


class QqSourceTests(unittest.TestCase):
    """QQ source shaping: search parsing and the target-format toggle, no network."""

    def _client(self, responses):
        class FakeClient:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            def request(self, *args, **kwargs):
                self.calls.append(kwargs)
                return self.responses.pop(0)

        return FakeClient(responses)

    def _search_payload(self, songs):
        return (200, {"code": 0, "request": {"code": 0, "data": {"body": {"item_song": songs}}}})

    def test_search_parses_song_fields(self) -> None:
        songs = [
            {
                "id": 108166949,
                "title": "Hz(ヘルツ) (赫兹)",
                "singer": [{"name": "Aimer"}],
                "album": {"name": "デイドリーム"},
                "interval": 289,
            }
        ]
        client = self._client(
            [
                (200, {"code": 0, "request": {"code": 0, "data": {"session": {"uid": "1", "sid": "s", "userip": "ip"}}}}),
                self._search_payload(songs),
            ]
        )
        qq = sources.QqSource(client)
        found = qq.search(make_track(title="Hz", artist="Aimer", album="daydream"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, "qq")
        self.assertEqual(found[0].source_id, "108166949")
        self.assertEqual(found[0].artists, ["Aimer"])
        self.assertEqual(found[0].album, "デイドリーム")
        self.assertEqual(found[0].duration, 289.0)

    def test_fetch_default_flattens_to_line_lrc(self) -> None:
        qrc_plain = '<Lyric_1 LyricType="1" LyricContent="[0,5000](0,200)我(200,300)爱"/>'
        lyric_resp = qq_lyric_payload(qrc_plain)

        client = self._client(
            [
                (200, {"code": 0, "request": {"code": 0, "data": {"session": {"uid": "1", "sid": "s", "userip": "ip"}}}}),
                lyric_resp,
            ]
        )
        qq = sources.QqSource(client, target_format=None)
        result = qq.fetch(
            Candidate(source="qq", source_id="1", title="Hz", artists=["Aimer"], album="daydream", duration=289.0)
        )
        self.assertTrue(result.lyrics)
        self.assertEqual(lyric_kind(result.lyrics), "lrc")
        # The word markers are timing, not text: no "(0,200)" may survive.
        self.assertEqual(result.lyrics.strip(), "[00:00.00]我爱")
        # The native QRC is kept next to the flattened one, so apply can still
        # write --qrc/--a2 later without asking the network again.
        self.assertEqual(result.native_kind, "qrc")
        self.assertIn("(200,300)爱", result.native_lyrics)

    def test_fetch_qrc_target_keeps_native_qrc(self) -> None:
        qrc_plain = '<Lyric_1 LyricType="1" LyricContent="[0,5000](0,200)我(200,300)爱"/>'
        lyric_resp = qq_lyric_payload(qrc_plain)

        client = self._client(
            [
                (200, {"code": 0, "request": {"code": 0, "data": {"session": {"uid": "1", "sid": "s", "userip": "ip"}}}}),
                lyric_resp,
            ]
        )
        qq = sources.QqSource(client, target_format="qrc")
        result = qq.fetch(
            Candidate(source="qq", source_id="1", title="Hz", artists=["Aimer"], album="daydream", duration=289.0)
        )
        self.assertTrue(result.lyrics)
        self.assertEqual(lyric_kind(result.lyrics), "qrc")
        self.assertIn("(200,300)爱", result.lyrics)


class ScoreComponentTests(unittest.TestCase):
    """Partial matches print what they have instead of a bare 0."""

    def test_artist_only_reports_30(self) -> None:
        track = make_track(duration=200.0)
        candidate = Candidate("qq", "1", "完全不同", ["Artist"], "Other", 200.0)
        self.assertEqual(score_candidate(track, candidate)[0], 30)

    def test_exact_title_without_artist_reports_55(self) -> None:
        track = make_track(duration=200.0)
        candidate = Candidate("qq", "1", "Track", ["Someone Else"], "Other", 200.0)
        self.assertEqual(score_candidate(track, candidate)[0], 55)

    def test_near_title_stays_below_the_auto_line(self) -> None:
        track = make_track(
            title="印地安老斑鸠", artist="周杰伦", album="Jay", duration=305.0
        )
        candidate = Candidate("qq", "97757", "印第安老斑鸠", ["周杰伦"], "Jay", 305.0)
        score, diff = score_candidate(track, candidate)
        self.assertEqual(score, 45)  # title 0 + artist 30 + album 15
        self.assertLess(score, 85)
        self.assertAlmostEqual(diff, 0.0)

    def test_duration_gap_still_zeroes_the_gated_score(self) -> None:
        track = make_track(duration=305.0)
        candidate = Candidate("qq", "1", "Track", ["Artist"], "Album", 208.0)
        self.assertEqual(score_candidate(track, candidate)[0], 0)
        self.assertEqual(score_candidate(track, candidate, 999.0)[0], 100)


class _FixedSource:
    """A provider stub that returns prepared candidates and nothing else."""

    def __init__(
        self,
        name: str,
        candidates: list[Candidate],
        lyrics: str = "[00:01.00]第一句\n[03:10.00]第二句\n",
        native_kind: str = "lrc",
    ) -> None:
        self.name = name
        self._candidates = candidates
        self._lyrics = lyrics
        self._native_kind = native_kind

    def search(self, track: Track, exhaustive: bool = False) -> list[Candidate]:
        return list(self._candidates)

    def lookup(self, ids: list[str]) -> list[Candidate]:
        wanted = set(ids)
        return [item for item in self._candidates if item.source_id in wanted]

    def fetch(self, candidate: Candidate) -> FetchResult:
        return FetchResult(
            lyrics=self._lyrics,
            native_lyrics=self._lyrics,
            native_kind=self._native_kind,
        )


class AmbiguousReasonTests(unittest.TestCase):
    def test_reason_names_every_source_and_the_best_id(self) -> None:
        track = make_track(
            title="印地安老斑鸠", artist="周杰伦", album="Jay", duration=305.0
        )
        live = Candidate(
            "qq", "244115386", "印地安老斑鸠 (Live)", ["周杰伦"], "Jay", 208.0
        )
        netease = _FixedSource(
            "netease",
            [Candidate("netease", "1400391963", "印地安老斑鸠 (Live)", ["周杰伦"], "巡演", 208.0)],
        )
        qq = _FixedSource("qq", [live])
        result = plan_one(track, [netease, qq], True)
        self.assertEqual(result.status, "ambiguous")
        # The table name is short, but a miss must still name every source.
        self.assertIn("ne 72", result.reason)
        self.assertNotIn("netease", result.reason)
        self.assertIn("qq 87", result.reason)
        self.assertIn("qq:244115386", result.reason)
        # The album and the real gap are what a human needs to decide.
        self.assertIn("Jay", result.reason)
        self.assertIn("Δ97.0s", result.reason)

    def test_empty_source_is_named_as_such(self) -> None:
        track = make_track(title="Track", artist="Artist", album="Album", duration=200.0)
        result = plan_one(track, [EmptySource()], True)
        self.assertEqual(result.status, "no_match")
        self.assertIn("empty", result.reason)


class PickAssignmentTests(unittest.TestCase):
    def _tracks(self) -> list[Track]:
        return [
            make_track(path=Path("/lib/Album/01 Alpha.m4a"), title="Alpha", artist="A", album="X", duration=200.0),
            make_track(path=Path("/lib/Album/02 Beta.m4a"), title="Beta", artist="B", album="Y", duration=180.0),
        ]

    def test_ids_are_paired_by_metadata_score(self) -> None:
        alpha = Candidate("qq", "101", "Alpha", ["A"], "X", 200.0)
        beta = Candidate("qq", "202", "Beta", ["B"], "Y", 180.0)
        source = _FixedSource("qq", [beta, alpha])  # deliberately reversed order
        assignments, unresolved, preview = assign_picks(
            self._tracks(), ["qq:202", "qq:101"], [source], 2.0
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(assignments[str(Path("/lib/Album/01 Alpha.m4a"))], "qq:101")
        self.assertEqual(assignments[str(Path("/lib/Album/02 Beta.m4a"))], "qq:202")
        self.assertEqual([row[1].source_id for row in preview], ["101", "202"])

    def test_unrelated_pick_is_reported(self) -> None:
        source = _FixedSource("qq", [Candidate("qq", "999", "Zeta", ["Z"], "Z", 100.0)])
        assignments, unresolved, _ = assign_picks(
            self._tracks(), ["qq:999"], [source], 2.0
        )
        self.assertEqual(assignments, {})
        self.assertEqual(unresolved, ["qq:999"])

    def test_near_title_pairing_uses_similarity_not_score(self) -> None:
        """印地安 / 印第安 scores 45, but a human pick still lands on the right file."""
        track = make_track(
            path=Path("/lib/Jay/08 印地安老斑鸠.m4a"),
            title="印地安老斑鸠",
            artist="周杰伦",
            album="Jay",
            duration=305.0,
        )
        other = make_track(
            path=Path("/lib/Jay/01 可爱女人.m4a"),
            title="可爱女人",
            artist="周杰伦",
            album="Jay",
            duration=239.0,
        )
        picked = Candidate("qq", "97757", "印第安老斑鸠", ["周杰伦"], "Jay", 305.0)
        source = _FixedSource("qq", [picked])
        assignments, unresolved, _ = assign_picks(
            [other, track], ["qq:97757"], [source], 2.0
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(assignments, {str(track.path): "qq:97757"})

    def test_source_must_be_enabled(self) -> None:
        with self.assertRaises(ToolError):
            assign_picks(self._tracks(), ["netease:1"], [_FixedSource("qq", [])], 2.0)

    def test_short_source_names_are_not_reported_as_unresolved(self) -> None:
        """`--pick ne:1` paired with `netease:1`; the flag spelling must not matter."""
        alpha = Candidate("netease", "101", "Alpha", ["A"], "X", 200.0)
        beta = Candidate("netease", "202", "Beta", ["B"], "Y", 180.0)
        source = _FixedSource("netease", [alpha, beta])
        for spelling in ("ne", "netease"):
            assignments, unresolved, _ = assign_picks(
                self._tracks(), [f"{spelling}:101", f"{spelling}:202"], [source], 2.0
            )
            self.assertEqual(unresolved, [], spelling)
            self.assertEqual(len(assignments), 2, spelling)

    def test_force_makes_tracks_with_lyrics_eligible(self) -> None:
        """`plan --force --pick a b` must pair files that already have lyrics."""
        tracks = [
            make_track(
                path=Path("/lib/Album/01 Alpha.m4a"),
                title="Alpha",
                artist="A",
                album="X",
                duration=200.0,
                existing_lyrics="[00:01.00]旧",
            ),
            make_track(
                path=Path("/lib/Album/02 Beta.m4a"),
                title="Beta",
                artist="B",
                album="Y",
                duration=180.0,
                existing_lyrics="[00:01.00]旧",
            ),
        ]
        source = _FixedSource(
            "qq",
            [
                Candidate("qq", "101", "Alpha", ["A"], "X", 200.0),
                Candidate("qq", "202", "Beta", ["B"], "Y", 180.0),
            ],
        )
        self.assertEqual(
            assign_picks(tracks, ["qq:101", "qq:202"], [source], 2.0),
            ({}, ["qq:101", "qq:202"], []),
        )
        assignments, unresolved, _ = assign_picks(
            tracks, ["qq:101", "qq:202"], [source], 2.0, force=True
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(assignments[str(tracks[0].path)], "qq:101")
        self.assertEqual(assignments[str(tracks[1].path)], "qq:202")


class MultiPickArgvTests(unittest.TestCase):
    def test_one_flag_accepts_several_ids(self) -> None:
        parser = build_parser()
        command, options, paths = normalize_argv(
            ["plan", "--pick", "qq:1", "netease:2", "/lib"], option_specs(parser)
        )
        args = parser.parse_args([command, *options, "--", *paths])
        self.assertEqual(args.pick, ["qq:1", "netease:2"])
        self.assertEqual(args.paths, ["/lib"])

    def test_a_path_after_the_picks_is_not_swallowed(self) -> None:
        parser = build_parser()
        _, options, paths = normalize_argv(
            ["--pick", "qq:1", "/lib/Album"], option_specs(parser)
        )
        self.assertEqual(options, ["--pick", "qq:1"])
        self.assertEqual(paths, ["/lib/Album"])


class CleanTests(unittest.TestCase):
    def test_only_old_files_are_removed(self) -> None:
        import time as _time

        from lrc_patch import clean as clean_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "reports"
            root.mkdir()
            old = root / "old.csv"
            old.write_text("x")
            stale = _time.time() - 40 * 86400
            old.touch()
            import os as _os

            _os.utime(old, (stale, stale))
            fresh = root / "new.csv"
            fresh.write_text("y")
            count, size = clean_mod._clean_files(root, _time.time() - 30 * 86400, False)
            self.assertEqual(count, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertGreater(size, 0)

    def test_dry_run_keeps_everything(self) -> None:
        import time as _time

        from lrc_patch import clean as clean_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = root / "old.csv"
            item.write_text("x")
            stale = _time.time() - 40 * 86400
            import os as _os

            _os.utime(item, (stale, stale))
            count, _ = clean_mod._clean_files(root, _time.time() - 30 * 86400, True)
            self.assertEqual(count, 1)
            self.assertTrue(item.exists())

    def test_all_ignores_the_age_of_every_file(self) -> None:
        import time as _time

        from lrc_patch import clean as clean_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = root / "today.csv"
            fresh.write_text("x")
            count, size = clean_mod._clean_files(root, None, False)
            self.assertEqual(count, 1)
            self.assertGreater(size, 0)
            self.assertFalse(fresh.exists())
            # The age limit still applies when a cutoff is given.
            fresh.write_text("y")
            self.assertEqual(clean_mod._clean_files(root, _time.time() - 86400, False), (0, 0))

    def test_http_cache_drops_only_expired_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "s.sqlite3")
            state.cache_http("expired", {"a": 1}, 0)
            state.cache_http("live", {"b": 2}, 3600)
            removed, _ = state.purge_http_cache(expired_only=True)
            self.assertEqual(removed, 1)
            self.assertIsNone(state.cached_http("expired"))
            self.assertIsNotNone(state.cached_http("live"))
            remaining, _ = state.purge_http_cache(expired_only=False)
            self.assertEqual(remaining, 1)
            self.assertEqual(state.count_http_cache(expired_only=False), 0)


class CliDispatchTests(unittest.TestCase):
    """`apply` must always reach the writer — 2.8 once let it fall into the help."""

    def _run(self, argv: list[str], plan_code: int = 0) -> tuple[int, str, mock.Mock, mock.Mock]:
        from lrc_patch import cli

        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            full = ["lrcpatch", "--state-dir", tmp, *argv]
            with mock.patch.object(sys, "argv", full), redirect_stdout(out):
                with mock.patch.object(commands, "cmd_apply", return_value=0) as apply_mock:
                    with mock.patch.object(
                        commands, "cmd_plan", return_value=plan_code
                    ) as plan_mock:
                        code = cli.main()
        return code, out.getvalue(), apply_mock, plan_mock

    def test_apply_without_pick_reaches_writer(self) -> None:
        code, text, apply_mock, plan_mock = self._run(["apply", "/tmp"])
        self.assertEqual(code, 0)
        self.assertTrue(apply_mock.called)
        self.assertFalse(plan_mock.called)
        self.assertNotIn("用法", text)

    def test_apply_with_pick_plans_then_writes(self) -> None:
        code, text, apply_mock, plan_mock = self._run(["apply", "--pick", "qq:1", "/tmp"])
        self.assertEqual(code, 0)
        self.assertTrue(plan_mock.called)
        self.assertTrue(apply_mock.called)
        self.assertNotIn("用法", text)

    def test_plan_failure_stops_before_writing(self) -> None:
        code, _text, apply_mock, plan_mock = self._run(
            ["apply", "--pick", "qq:1", "/tmp"], plan_code=1
        )
        self.assertEqual(code, 1)
        self.assertTrue(plan_mock.called)
        self.assertFalse(apply_mock.called)


class LibraryScopeTests(unittest.TestCase):
    """One state file serves every --library, so selection must respect it."""

    def test_plans_from_another_library_do_not_leak(self) -> None:
        from lrc_patch.paths import resolve_selection, selection_matches

        with tempfile.TemporaryDirectory() as tmp:
            lib_a = Path(tmp) / "A"
            lib_b = Path(tmp) / "B"
            lib_a.mkdir()
            lib_b.mkdir()
            self.assertTrue(selection_matches(lib_a / "a.m4a", lib_a, None, []))
            self.assertFalse(selection_matches(lib_b / "a.m4a", lib_a, None, []))
            selected = resolve_selection(lib_a, [str(lib_a)])
            self.assertTrue(selection_matches(lib_a / "a.m4a", lib_a, None, selected))
            self.assertFalse(selection_matches(lib_b / "a.m4a", lib_a, None, selected))


class SourceLabelTests(unittest.TestCase):
    """Tables show ``ne``; the database and the wire keep ``netease``."""

    def test_display_name_is_short(self) -> None:
        self.assertEqual(source_label("netease"), "ne")
        self.assertEqual(source_label("qq"), "qq")
        self.assertEqual(source_label("lrclib"), "lrclib")

    def test_short_name_is_accepted_where_a_source_is_typed(self) -> None:
        self.assertEqual(canonical_source("ne"), "netease")
        self.assertEqual(canonical_source("NE"), "netease")
        self.assertEqual(parse_pick("ne:123"), ("netease", "123"))
        self.assertEqual(parse_pick("netease:123"), ("netease", "123"))
        self.assertEqual(parse_pick("qq:9"), ("qq", "9"))

    def test_ready_row_prints_the_short_name(self) -> None:
        cells, _ = render.plan_fields(
            {
                "status": "ready",
                "source": "netease",
                "score": 100,
                "duration_diff": 0.3,
                "lyric_kind": "lrc",
                "lyric_lines": 66,
                "reason": "周杰伦",
            }
        )
        self.assertEqual(cells[0], "ne")

    def test_suspect_row_starts_with_the_short_name(self) -> None:
        _, note = render.plan_fields(
            {
                "status": "suspect",
                "source": "netease",
                "score": 100,
                "reason": "LRC 末尾 04:20 超过音频 03:10",
            }
        )
        self.assertTrue(note.startswith("ne 100分；"), note)


class CandidateScopeTests(unittest.TestCase):
    """candidates should answer "what needs a decision", not "what is in the library"."""

    def _state_dir(self, rows: list[tuple[str, str, str, int, str]]) -> Path:
        tmp = Path(tempfile.mkdtemp())
        conn = sqlite3.connect(tmp / "lyrics.sqlite3")
        conn.execute(
            "CREATE TABLE plans(path TEXT PRIMARY KEY, status TEXT, source TEXT,"
            " score INTEGER, fingerprint TEXT)"
        )
        conn.executemany("INSERT INTO plans VALUES(?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return tmp

    def test_only_the_open_questions_are_listed(self) -> None:
        settled = make_track(path=Path("/lib/A/01.m4a"), relpath="A/01.m4a")
        pending = make_track(path=Path("/lib/A/02.m4a"), relpath="A/02.m4a")
        written = make_track(
            path=Path("/lib/A/03.m4a"),
            relpath="A/03.m4a",
            existing_lyrics="[00:01]x",
        )
        state_dir = self._state_dir(
            [
                (str(settled.path), "ready", "netease", 100, settled.fingerprint()),
                (str(pending.path), "ambiguous", "qq", 72, pending.fingerprint()),
            ]
        )
        scope = commands.open_tracks([settled, pending, written], state_dir)
        self.assertEqual([track.path for track in scope.tracks], [pending.path])
        self.assertEqual(scope.mode, "questions")
        self.assertEqual(scope.settled, 1)
        self.assertEqual(scope.with_lyrics, 1)

    def test_suspect_counts_as_an_open_question(self) -> None:
        track = make_track()
        state_dir = self._state_dir(
            [(str(track.path), "suspect", "netease", 100, track.fingerprint())]
        )
        scope = commands.open_tracks([track], state_dir)
        self.assertEqual([item.path for item in scope.tracks], [track.path])

    def test_an_open_question_survives_existing_lyrics(self) -> None:
        # `plan --force` on a file that already has lyrics: the plan says !
        # 需确认, so it stays on the list even though the file is not empty.
        track = make_track(existing_lyrics="[00:01.00]旧歌词")
        state_dir = self._state_dir(
            [(str(track.path), "ambiguous", "qq", 72, track.fingerprint())]
        )
        scope = commands.open_tracks([track], state_dir)
        self.assertEqual([item.path for item in scope.tracks], [track.path])

    def test_a_scope_that_was_never_planned_lists_the_unplanned(self) -> None:
        track = make_track()
        scope = commands.open_tracks([track], self._state_dir([]))
        self.assertEqual([item.path for item in scope.tracks], [track.path])
        self.assertEqual(scope.mode, "unplanned")

    def test_open_questions_win_over_unplanned_tracks(self) -> None:
        # Nine unplanned tracks must not bury the one the last plan could not
        # settle: that is the whole point of the default scope.
        settled = make_track(path=Path("/lib/A/01.m4a"), relpath="A/01.m4a")
        unplanned = [
            make_track(path=Path(f"/lib/A/{index:02d}.m4a"), relpath=f"A/{index:02d}.m4a")
            for index in range(2, 5)
        ]
        state_dir = self._state_dir(
            [(str(settled.path), "ambiguous", "qq", 72, settled.fingerprint())]
        )
        scope = commands.open_tracks([settled, *unplanned], state_dir)
        self.assertEqual([item.path for item in scope.tracks], [settled.path])
        self.assertEqual(scope.unplanned, 3)

    def test_a_changed_file_is_offered_again(self) -> None:
        track = make_track(path=Path("/lib/A/01.m4a"), relpath="A/01.m4a")
        changed = make_track(
            path=track.path, relpath=track.relpath, mtime_ns=track.mtime_ns + 1
        )
        state_dir = self._state_dir(
            [(str(track.path), "ready", "netease", 100, track.fingerprint())]
        )
        scope = commands.open_tracks([changed], state_dir)
        self.assertEqual([item.path for item in scope.tracks], [changed.path])
        self.assertEqual(scope.mode, "unplanned")

    def test_all_lists_every_track_including_settled_ones(self) -> None:
        track = make_track(existing_lyrics="[00:01]x")
        state_dir = self._state_dir(
            [(str(track.path), "ready", "netease", 100, track.fingerprint())]
        )
        scope = commands.open_tracks([track], state_dir, everything=True)
        self.assertEqual(scope.tracks, [track])
        self.assertEqual(scope.mode, "all")

    def test_a_named_file_is_listed_whatever_its_plan_says(self) -> None:
        # `candidates <路径>` used to answer "0 首" for a settled file, so the
        # only way to see it was --all — which made --refresh look pointless
        # (feedback 20261105-1925).
        track = make_track(existing_lyrics="[00:01]x")
        state_dir = self._state_dir(
            [(str(track.path), "ready", "netease", 100, track.fingerprint())]
        )
        scope = commands.open_tracks([track], state_dir, named=True)
        self.assertEqual(scope.tracks, [track])
        self.assertEqual(scope.mode, "named")
        self.assertIn("你点名的", commands.scope_line(scope))

    def test_naming_a_file_does_not_need_the_database(self) -> None:
        # Named scope must work even before the first plan ever ran.
        track = make_track()
        scope = commands.open_tracks([track], Path(tempfile.mkdtemp()), named=True)
        self.assertEqual(scope.tracks, [track])


class ApplyFormatGuardTests(unittest.TestCase):
    def test_only_the_requested_format_is_accepted(self) -> None:
        rows = [{"lyric_kind": "lrc", "path": "/lib/a.m4a"}]
        self.assertEqual(len(commands.format_mismatch(rows, "qrc")), 1)
        self.assertEqual(commands.format_mismatch(rows, "lrc"), [])
        # Old rows may have no kind at all; those need a re-plan too.
        self.assertEqual(
            len(commands.format_mismatch([{"lyric_kind": None, "path": "x"}], "a2")), 1
        )


class RepeatedLineStampTests(unittest.TestCase):
    """A chorus line carries several stamps: that is still plain LRC."""

    def test_repeated_stamps_stay_line_timing(self) -> None:
        text = "[00:26.22][01:10.69][02:18.93]Rose\n[00:30.00]another line"
        self.assertEqual(lyric_kind(text), "lrc")

    def test_a_stamp_after_the_text_is_still_word_timing(self) -> None:
        self.assertEqual(lyric_kind("[00:12.34]你好[00:13.00]世界"), "eslrc")
        # The leading stamp is the line's own; the one after the text is a word.
        self.assertEqual(lyric_kind("[00:12.34][00:12.34]你[00:13.00]世界"), "eslrc")

    def test_a_repeated_line_exports_as_plain_lrc(self) -> None:
        kind = lyric_kind("[00:26.22][01:10.69]Rose")
        dest = lrc_dest(Path("/lib/Album/01 Rose.m4a"), Path("/lib"), Path("/cache"), kind=kind)
        self.assertEqual(dest.suffix, ".lrc")


class QqTimelineTests(unittest.TestCase):
    """QQ sometimes answers one ``[0,0]`` block: plain text, not a bad timeline."""

    def _fetch(self, payload: str, target: str | None = None):
        qq = sources.QqSource(
            RecordingQqClient([{"lyric": qq_encrypt(payload)}]), target_format=target
        )
        candidate = Candidate("qq", "1", "Track", ["Artist"], "Album", 200.0)
        return qq.fetch(candidate)

    def test_a_single_untimed_block_is_plain_text(self) -> None:
        payload = '<Lyric_1 LyricType="1" LyricContent="[0,0]第一句\n第二句"/>'
        result = self._fetch(payload)
        self.assertEqual(result.native_kind, "plain")
        self.assertEqual(lyric_kind(result.lyrics), "plain")
        self.assertIn("第一句", result.lyrics)
        self.assertIn("第二句", result.lyrics)
        self.assertNotIn("[00:00.00]", result.lyrics)

    def test_a_timed_block_stays_qrc(self) -> None:
        payload = '<Lyric_1 LyricType="1" LyricContent="[1000,500](1000,200)我"/>'
        result = self._fetch(payload)
        self.assertEqual(result.native_kind, "qrc")

    def test_a_plain_lyric_needs_force_to_be_written(self) -> None:
        track = make_track()
        candidate = Candidate("qq", "1", "Track", ["Artist"], "Album", 200.0)
        result = plan_from_lyrics(track, candidate, "第一句\n第二句", 100, 0.2, "qq", True)
        self.assertEqual(result.status, "suspect")
        self.assertIn("没有时间轴", result.reason)


class QqCacheKeyTests(unittest.TestCase):
    """QQ answers must be cacheable: the random ``search_id`` is not the key."""

    def test_two_searches_share_one_cache_key(self) -> None:
        client = RecordingQqClient([{"body": {"item_song": []}} for _ in range(3)])
        qq = sources.QqSource(client)
        qq.search(make_track(title="Track"))
        qq.search(make_track(title="Track"))
        qq.search(make_track(title="Another"))
        searches = [
            key for key in client.cache_keys if "DoSearchForQQMusicLite" in key
        ]
        self.assertEqual(len(searches), 3)
        self.assertEqual(searches[0], searches[1])
        self.assertNotEqual(searches[0], searches[2])
        self.assertNotIn("search_id", searches[0])


class ForcePlanTests(unittest.TestCase):
    """``--force`` re-plans a file that already carries lyrics (TXT, wrong LRC)."""

    def _source(self) -> _FixedSource:
        return _FixedSource("qq", [Candidate("qq", "7", "Track", ["Artist"], "Album", 200.0)])

    def test_existing_lyrics_are_skipped_by_default(self) -> None:
        track = make_track(existing_lyrics="[00:01.00]旧歌词")
        self.assertEqual(plan_one(track, [self._source()], True).status, "skip_existing")

    def test_force_replans_over_existing_lyrics(self) -> None:
        track = make_track(existing_lyrics="[00:01.00]旧歌词")
        result = plan_one(track, [self._source()], True, force=True)
        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.source_id, "7")

    def test_force_also_reaches_the_candidate_list(self) -> None:
        track = make_track(existing_lyrics="[00:01.00]旧歌词")
        scope = commands.open_tracks(
            [track], Path(tempfile.mkdtemp()), everything=True
        )
        self.assertEqual(scope.tracks, [track])
        self.assertEqual(scope.with_lyrics, 1)


class SourceScopeTests(unittest.TestCase):
    """``--src qq`` must not be blocked by a ✓ plan that came from netease."""

    def _planned(self, root: Path, track: Track) -> Path:
        state = State(root / "lyrics.sqlite3")
        candidate = Candidate("netease", "11", "Track", ["Artist"], "Album", 200.0)
        state.upsert_plan(
            PlanResult(
                track,
                "ready",
                "Artist",
                candidate,
                "[00:01.00]x\n",
                100,
                0.0,
                native_lyrics="[1000,500](1000,200,0)x",
                native_kind="yrc",
            ),
            track.fingerprint(),
            src_set="netease,qq",
        )
        return root

    def test_a_ready_plan_from_another_source_is_offered_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            track = make_track()
            state_dir = self._planned(Path(tmp), track)
            scope = commands.open_tracks([track], state_dir, sources=["qq"])
            self.assertEqual([item.path for item in scope.tracks], [track.path])
            self.assertEqual(scope.settled, 0)
            self.assertEqual(scope.other_source, 1)
            kept = commands.open_tracks([track], state_dir, sources=["netease"])
            self.assertEqual(kept.tracks, [])
            self.assertEqual(kept.settled, 1)

    def test_the_native_lyric_and_src_set_survive_a_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            track = make_track()
            state_dir = self._planned(Path(tmp), track)
            row = read_plan_cache(state_dir / "lyrics.sqlite3")[str(track.path)]
            self.assertEqual(row["src_set"], "netease,qq")
            self.assertEqual(row["native_kind"], "yrc")
            self.assertEqual(commands.lyric_for_target(row, "a2")[1], "a2")

    def test_skip_existing_rows_belong_to_no_source(self) -> None:
        self.assertTrue(commands.plan_uses_source({"source": None}, ["qq"]))
        self.assertFalse(commands.plan_uses_source({"source": "netease"}, ["qq"]))
        self.assertTrue(commands.plan_uses_source({"source": "netease"}, None))


class NativeFormatTests(unittest.TestCase):
    """The format is chosen at apply time, from the native text plan stored."""

    def _row(self, **overrides) -> dict:
        row = {
            "path": "/lib/A/01.m4a",
            "lyric": "[00:00.00]我爱\n",
            "lyric_kind": "lrc",
            "native_lyric": "[0,5000](0,200)我(400,200)爱\n",
            "native_kind": "qrc",
        }
        row.update(overrides)
        return row

    def test_apply_converts_the_stored_native_lyric(self) -> None:
        lyrics, kind = commands.lyric_for_target(self._row(), "a2")
        self.assertEqual(kind, "a2")
        self.assertIn("<00:00.40>爱", lyrics)

    def test_the_native_format_itself_is_written_verbatim(self) -> None:
        lyrics, kind = commands.lyric_for_target(self._row(), "qrc")
        self.assertEqual(kind, "qrc")
        self.assertEqual(lyrics.strip(), "[0,5000](0,200)我(400,200)爱")

    def test_no_target_reuses_the_planned_lyric(self) -> None:
        lyrics, kind = commands.lyric_for_target(self._row(), None)
        self.assertEqual((lyrics.strip(), kind), ("[00:00.00]我爱", "lrc"))

    def test_an_old_plan_without_native_text_is_refused(self) -> None:
        row = self._row(native_lyric=None, native_kind=None)
        with self.assertRaises(ToolError) as caught:
            commands.lyric_for_target(row, "a2")
        self.assertIn("重新取词", str(caught.exception))

    def test_a_format_the_native_cannot_reach_is_refused(self) -> None:
        row = self._row(
            lyric="第一句\n", lyric_kind="plain", native_lyric="第一句\n", native_kind="plain"
        )
        with self.assertRaises(ToolError):
            commands.lyric_for_target(row, "a2")


class CandidateRankingTests(unittest.TestCase):
    """A candidate inside the tolerance outranks a better-looking live take."""

    def test_a_translated_title_inside_the_tolerance_wins(self) -> None:
        track = make_track(title="歩く", artist="Aimer", album="Open α", duration=240.0)
        live = Candidate("qq", "1", "歩く (LIVE)", ["Aimer"], "月光", 253.2)
        translated = Candidate("qq", "2", "歩く (漫步)", ["Aimer"], "Open α", 240.8)
        result = plan_one(track, [_FixedSource("qq", [live, translated])], True)
        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.source_id, "2")
        self.assertGreaterEqual(result.score, 85)

    def test_the_partial_pick_prefers_a_candidate_inside_the_tolerance(self) -> None:
        track = make_track(title="X", artist="A", album="B", duration=240.0)
        far = Candidate("qq", "1", "X", ["A"], "B", 260.0)
        near = Candidate("qq", "2", "X (譯名)", ["Nobody"], "Elsewhere", 241.0)
        best = best_partial_candidate(track, [far, near], 2.0)
        self.assertIsNotNone(best)
        self.assertEqual(best[1].source_id, "2")

    def test_the_ambiguous_note_names_the_album_and_the_gap(self) -> None:
        track = make_track(title="歩く", artist="Aimer", album="Open α", duration=240.0)
        live = Candidate("qq", "1", "歩く (LIVE)", ["Aimer"], "月光", 253.2)
        result = plan_one(track, [_FixedSource("qq", [live])], True)
        self.assertEqual(result.status, "ambiguous")
        self.assertIn("月光", result.reason)
        self.assertIn("Δ", result.reason)


class ScanPlansTests(unittest.TestCase):
    """``scan --plans`` answers "which 14 tracks?", not just "14".."""

    def _args(self, **overrides) -> object:
        import argparse

        values = {
            "library": Path("/lib"),
            "album": None,
            "limit": None,
            "path": [],
            "summary": False,
            "tree_limit": 0,
            "plans": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_only_tracks_with_a_plan_are_listed(self) -> None:
        planned = make_track(
            path=Path("/lib/A/01 planned.m4a"), relpath="A/01 planned.m4a"
        )
        loose = make_track(path=Path("/lib/A/02 loose.m4a"), relpath="A/02 loose.m4a")
        cache = {
            str(planned.path): {
                "fingerprint": planned.fingerprint(),
                "source": "netease",
                "score": 100,
                "status": "ready",
            }
        }
        buffer = io.StringIO()
        with mock.patch.object(
            commands, "load_tracks", return_value=([planned, loose], [])
        ), mock.patch.object(commands, "read_plan_cache", return_value=cache):
            with redirect_stdout(buffer):
                code = commands.cmd_scan(self._args(), Path("/state"))
        out = render.strip_ansi(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("planned.m4a", out)
        self.assertNotIn("loose.m4a", out)
        self.assertIn("只看有计划", out)

    def test_without_the_flag_every_track_is_listed(self) -> None:
        loose = make_track(path=Path("/lib/A/02 loose.m4a"), relpath="A/02 loose.m4a")
        buffer = io.StringIO()
        with mock.patch.object(
            commands, "load_tracks", return_value=([loose], [])
        ), mock.patch.object(commands, "read_plan_cache", return_value={}):
            with redirect_stdout(buffer):
                commands.cmd_scan(self._args(plans=False), Path("/state"))
        out = render.strip_ansi(buffer.getvalue())
        self.assertIn("loose.m4a", out)
        self.assertNotIn("只看有计划", out)


class PickWithoutLyricsTests(unittest.TestCase):
    """A picked ID with no lyric must explain itself, not just refuse."""

    def test_the_message_names_the_next_step(self) -> None:
        track = make_track(title="Takeaway", artist="The Chainsmokers", duration=209.0)
        source = _FixedSource(
            "qq", [Candidate("qq", "1", "Takeaway", ["A"], "B", 209.0)], lyrics=""
        )
        result = plan_with_pick(track, [source], "qq:1", True, True)
        self.assertEqual(result.status, "no_match")
        self.assertIn("qq 上这个 ID 没有歌词", result.reason)
        # The only candidate in the search was the pick itself: still no dead end.
        self.assertIn("--src ne", result.reason)

    def test_other_ids_from_the_same_search_are_offered(self) -> None:
        track = make_track(duration=200.0)
        source = _FixedSource(
            "qq",
            [
                Candidate("qq", "1", "Track", ["A"], "B", 200.0),
                Candidate("qq", "2", "Track (Remix)", ["A"], "B", 201.0),
            ],
            lyrics="",
        )
        result = plan_with_pick(track, [source], "qq:1", True, True)
        self.assertIn("qq:2", result.reason)
        self.assertIn("--pick", result.reason)

    def test_alternatives_prefer_the_duration_tolerance(self) -> None:
        track = make_track(duration=200.0)
        results = [
            Candidate("qq", "1", "A", [], "", 200.0),
            Candidate("qq", "9", "Live", [], "", 260.0),
            Candidate("qq", "2", "B", [], "", 201.0),
        ]
        self.assertEqual(
            pick_alternatives(results, "1", track),
            ["qq:2 B (201s)", "qq:9 Live (260s)"],
        )


class StatusCommandTests(unittest.TestCase):
    """`status` shows what the database holds, and never creates it."""

    def _args(self, **overrides):
        values = {"library": None, "json": False, "no_color": True}
        values.update(overrides)
        return mock.Mock(**values)

    def _planned_dir(self, root: Path) -> Track:
        track = make_track()
        state = State(root / "lyrics.sqlite3")
        state.upsert_plan(
            PlanResult(
                track,
                "ready",
                "",
                Candidate("netease", "1", "Track", ["Artist"], "Album", 200.0),
                "[00:01.00]x\n",
                100,
                0.0,
            ),
            track.fingerprint(),
            src_set="netease,qq",
        )
        state.cache_http("key", {"ok": True}, 3600)
        return track

    def test_plan_and_cache_counts_are_shown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._planned_dir(root)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = commands.cmd_status(self._args(), root)
            out = render.strip_ansi(buffer.getvalue())
            self.assertEqual(code, 0)
            self.assertIn("✓ 可写入 1", out)
            self.assertIn("来源  ne 1", out)
            self.assertIn("缓存   1 条", out)

    def test_a_missing_database_is_reported_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = commands.cmd_status(self._args(), root)
            self.assertEqual(code, 1)
            self.assertIn("还没有建立", render.strip_ansi(buffer.getvalue()))
            self.assertFalse((root / "lyrics.sqlite3").exists())

    def test_json_is_machine_readable(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._planned_dir(root)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                commands.cmd_status(self._args(json=True), root)
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["summary"]["plans"]["total"], 1)
            self.assertEqual(data["summary"]["cache"]["rows"], 1)


class StateSummaryTests(unittest.TestCase):
    def test_statuses_sources_and_expired_rows_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            track = make_track()
            state = State(root / "lyrics.sqlite3")
            state.upsert_plan(
                PlanResult(track, "ambiguous", "q", score=72),
                track.fingerprint(),
                src_set="qq",
            )
            state.cache_http("fresh", {"a": 1}, 3600)
            state.cache_http("stale", {"a": 2}, 0.0)
            summary = read_state_summary(root / "lyrics.sqlite3")
            self.assertEqual(summary["plans"]["total"], 1)
            self.assertEqual(summary["plans"]["by_status"], {"ambiguous": 1})
            self.assertEqual(summary["cache"]["rows"], 2)
            self.assertEqual(summary["cache"]["expired"], 1)
            self.assertTrue(summary["plans"]["latest"])


class LyricFormatTests(unittest.TestCase):
    """Native → public verbatim conversion, and back again.

    QQ and netease both ship a second, syllable-timed ``[kana:…]`` track for
    Japanese songs.  Nothing but QRC can hold it, and it used to be written out
    as one 2 KB lyric line at 00:00 in every converted file.
    """

    NATIVE = (
        "[ti:ノーチラス]\n"
        "[ar:ヨルシカ]\n"
        "[kana:1と1けい(6723,229)1な(6952,190)1ま]\n"
        "[1000,2000](1000,180)時(1180,200)計(1380,300)が(1680,200)鳴(1880,320)った\n"
        "[3200,1500](3200,200)や(3400,200)っと(3600,300)眼(3900,800)\n"
    )

    def _converted(self) -> dict[str, str]:
        lines = parse_qrc(self.NATIVE)
        return {
            "lrc": lines_to_lrc(lines),
            "a2": lines_to_a2(lines),
            "eslrc": lines_to_eslrc(lines),
            "ttml": lines_to_ttml(lines),
        }

    def test_ruby_never_reaches_a_converted_file(self) -> None:
        for name, text in self._converted().items():
            with self.subTest(format=name):
                self.assertNotIn("kana:", text)
                self.assertNotIn("6723", text)

    def test_standard_tags_travel_but_xml_has_no_tags(self) -> None:
        converted = self._converted()
        for name in ("lrc", "a2", "eslrc"):
            self.assertIn("[ti:ノーチラス]", converted[name])
        self.assertNotIn("[ti:", converted["ttml"])

    def test_count_ruby_sees_the_kana_tag(self) -> None:
        self.assertEqual(count_ruby(self.NATIVE, "qrc"), 1)
        self.assertEqual(count_ruby("[00:01.00]歌詞\n", "lrc"), 0)

    def test_a2_round_trip_keeps_every_word_start(self) -> None:
        a2 = lines_to_a2(parse_qrc(self.NATIVE))
        source = [line for line in parse_qrc(self.NATIVE) if line.words]
        back = [line for line in parse_a2(a2) if line.words]
        self.assertEqual(len(back), len(source))
        for before, after in zip(source, back):
            self.assertEqual(
                [word.start for word in before.words],
                [word.start for word in after.words],
            )
            self.assertEqual(
                "".join(word.text for word in before.words),
                "".join(word.text for word in after.words),
            )

    def test_eslrc_round_trip_keeps_every_word_start(self) -> None:
        eslrc = lines_to_eslrc(parse_qrc(self.NATIVE))
        back = [line for line in parse_eslrc(eslrc) if line.words]
        self.assertEqual([word.text for word in back[0].words][:3], ["時", "計", "が"])
        self.assertEqual([word.start for word in back[0].words][:3], [1000, 1180, 1380])

    def test_ttml_round_trip_is_exact_to_the_millisecond(self) -> None:
        ttml = lines_to_ttml(parse_qrc(self.NATIVE))
        back = [line for line in parse_ttml(ttml) if line.words]
        self.assertEqual(back[0].words[0].start, 1000)
        self.assertEqual(back[0].words[0].dur, 180)
        self.assertEqual(back[0].dur, 2000)

    def test_ttml_carries_word_spans_and_a_language(self) -> None:
        ttml = self._converted()["ttml"]
        self.assertIn('xml:lang="ja"', ttml)
        self.assertIn("<div>", ttml)
        self.assertIn(
            '<span begin="00:00:01.000" end="00:00:01.180">時</span>', ttml
        )
        # Clock time, not offset time: offset time is legal TTML but Apple and
        # every lyric player in practice read the clock form.
        self.assertIn('begin="00:00:01.000"', ttml)
        self.assertNotIn('begin="1.000s"', ttml)

    def test_a2_quantises_to_centiseconds(self) -> None:
        # The format writes mm:ss.cc, so 1203 ms has to become 1200: saying so
        # keeps the round-trip tolerance honest instead of pretending the
        # conversion is exact.
        a2 = lines_to_a2(parse_qrc("[0,1000](0,100)あ(1203,100)い\n"))
        self.assertIn("<00:01.20>い", a2)

    def test_rounding_a_stamp_up_does_not_produce_second_60(self) -> None:
        a2 = lines_to_a2(parse_qrc("[59999,1000](59999,100)あ\n"))
        self.assertTrue(a2.startswith("[01:00.00]"), a2)

    def test_ttml_reads_offset_time_too(self) -> None:
        xml = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
            '<p begin="12.34s" end="13.5s">hi</p>'
            "</div></body></tt>"
        )
        lines = parse_ttml(xml)
        self.assertEqual((lines[0].start, lines[0].dur), (12340, 1160))

    def test_a_line_timed_lyric_cannot_be_dressed_as_ttml(self) -> None:
        lyrics, note = render_native("[00:01.00]我爱\n", "lrc", "ttml")
        self.assertIn("逐字", note)
        self.assertEqual(lyrics.strip(), "[00:01.00]我爱")

    def test_a_timed_line_without_words_keeps_its_stamp(self) -> None:
        # QRC can carry "[1000,2000]plain" with no syllables; the stamp used to
        # be dropped on the way to LRC, turning a timed line into an untimed one.
        self.assertEqual(
            lines_to_lrc(parse_qrc("[1000,2000]纯文本\n")).strip(),
            "[00:01.00]纯文本",
        )

    def test_legacy_plans_are_cleaned_without_a_replan(self) -> None:
        legacy = "[ti:x]\n[kana:1あ]\n[00:01.00]詞\n"
        cleaned = strip_provider_tags(legacy, "lrc")
        self.assertNotIn("kana:", cleaned)
        self.assertIn("[00:01.00]詞", cleaned)
        # A native QRC keeps its tag: that is what --qrc is for.
        native = "[0,100](0,10)あ\n[kana:1あ]\n"
        self.assertEqual(strip_provider_tags(native, "qrc"), native)

    def test_netease_json_credits_are_not_lyrics(self) -> None:
        # The newer YRC mixes JSON credit lines into the timeline; they used to
        # come out as a lyric line beginning with '{"t":0'.
        yrc = (
            '{"t":0,"c":[{"tx":"作词 : Ayase"}]}\n'
            "[1000,2000](1000,300,0)時(1300,300,0)計\n"
        )
        lines = parse_yrc(yrc)
        self.assertEqual([line.text for line in lines if line.tag], ['{"t":0,"c":[{"tx":"作词 : Ayase"}]}'])
        self.assertNotIn('{"t"', lines_to_a2(lines))
        self.assertEqual(lines_to_a2(lines).strip(), "[00:01.00]<00:01.00>時<00:01.30>計")


class FormatColorTests(unittest.TestCase):
    """`scan` has to tell seven formats apart at a glance."""

    def test_every_format_gets_its_own_colour(self) -> None:
        colours = list(render.LYRIC_COLORS.values())
        self.assertEqual(len(colours), len(set(colours)))

    def test_a2_is_not_a_shade_of_lrc(self) -> None:
        # The original pair was green and *bold* green: same hue, same
        # terminal column, indistinguishable on purpose-built dark themes.
        self.assertNotEqual(render.LYRIC_COLORS["a2"], render.LYRIC_COLORS["lrc"])

    def test_the_help_table_reads_the_palette_instead_of_literal_codes(self) -> None:
        # Two places paint the format tags: `scan` and `§ 格式` in the help.
        # They drifted once (both hardcoded `1;32` for A2); reading the same
        # map is the only thing that keeps them together.
        import inspect

        from lrc_patch import cli_args

        source = inspect.getsource(cli_args)
        for key in ("plain", "lrc", "a2", "eslrc", "yrc", "qrc", "ttml"):
            with self.subTest(format=key):
                self.assertIn(f'LYRIC_COLORS["{key}"]', source)


class PlanRowFieldsTests(unittest.TestCase):
    """One plan line has to say which release, from whom, in what format."""

    def _row(self, **overrides):
        values = {
            "path": "/lib/x.m4a",
            "status": "ready",
            "source": "netease",
            "score": 85,
            "duration_diff": 0.0,
            "lyric_kind": "lrc",
            "lyric_lines": 63,
            "reason": "ヨルシカ",
            "match_artist": "ヨルシカ",
            "match_album": "創作",
            "native_kind": "lrc",
        }
        values.update(overrides)
        return values

    def test_the_matched_album_is_a_column(self) -> None:
        # 春泥棒 sits on three releases with three different uploads; the album
        # is what tells an 85 from a 100 apart (feedback 20261105-1925).
        cells, _ = render.plan_fields(self._row())
        self.assertEqual(cells[-1], "創作")

    def test_an_applied_row_shows_the_artist_not_the_verdict(self) -> None:
        # apply overwrites `reason` with 写入并校验成功; the artist lives in
        # match_artist and used to be replaced by that verdict.
        cells, note = render.plan_fields(
            self._row(status="applied", reason="写入并校验成功")
        )
        self.assertEqual(cells[4], "ヨルシカ")
        self.assertIn("写入并校验成功", note)

    def test_the_lyric_column_is_the_set_the_provider_serves(self) -> None:
        # netease answers one request with YRC *and* LRC; printing "LRC 63 行"
        # plus a trailing "原生 YRC（逐字…）" said it twice and made the table
        # read like two different facts (feedback 20261105-1947).
        cells, note = render.plan_fields(self._row(native_kinds="yrc,lrc"))
        self.assertEqual(cells[3], "YRC/LRC 63 行")
        self.assertNotIn("原生", note)

    def test_one_served_format_needs_no_slash(self) -> None:
        cells, _ = render.plan_fields(self._row(native_kinds="qrc"))
        self.assertEqual(cells[3], "QRC 63 行")

    def test_an_old_row_falls_back_to_its_single_native_kind(self) -> None:
        cells, _ = render.plan_fields(self._row(native_kind="yrc"))
        self.assertEqual(cells[3], "YRC 63 行")

    def test_the_kana_notice_survives_once(self) -> None:
        reason = (
            "ヨルシカ；注音（kana）1 行放不进 LRC，已丢弃；"
            "原生 QRC 仍在状态库，apply --qrc 可取回"
        )
        _, note = render.plan_fields(self._row(native_kind="qrc", reason=reason))
        self.assertEqual(note.count("原生 QRC"), 1)


class CandidatesNativeKindTests(unittest.TestCase):
    """Do not promise YRC: netease serves it only when the song has one."""

    def _rows(self, track, known):
        args = build_parser().parse_args(["candidates", "--library", "/lib"])
        candidate = Candidate(
            source="netease",
            source_id="1810759765",
            title=track.title,
            artists=["Artist"],
            album="Album",
            duration=track.duration,
        )
        return commands.candidate_rows(
            track, [("netease", [candidate], None, False)], args, known
        )

    def test_an_unfetched_candidate_is_marked_unknown(self) -> None:
        # It used to read "YRC/LRC", which the user read as "the lyrics have
        # word timing" — and plan then said LRC (feedback 20261105-1925).
        rows = self._rows(make_track(), {})
        self.assertEqual(rows[0].kind, "?")

    def test_a_previous_plan_supplies_the_real_format(self) -> None:
        rows = self._rows(make_track(), {("netease", "1810759765"): "yrc"})
        self.assertEqual(rows[0].kind, "YRC")

    def test_a_previous_plan_can_name_more_than_one_format(self) -> None:
        rows = self._rows(make_track(), {("netease", "1810759765"): "yrc,lrc"})
        self.assertEqual(rows[0].kind, "YRC/LRC")

    def test_known_native_kinds_reads_the_plan_table(self) -> None:
        track = make_track()
        state_dir = Path(tempfile.mkdtemp())
        state = State(state_dir / "lyrics.sqlite3")
        state.upsert_plan(
            PlanResult(
                track,
                "ready",
                "Artist",
                Candidate(
                    source="netease",
                    source_id="42",
                    title="Track",
                    artists=["Artist"],
                    album="Album",
                    duration=200.0,
                ),
                lyrics="[00:00.00]あ",
                score=100,
                duration_diff=0.0,
                native_lyrics="[0,1000](0,10)あ",
                native_kind="yrc",
            ),
            track.fingerprint(),
        )
        self.assertEqual(
            commands.known_native_sets(state_dir), {("netease", "42"): "yrc"}
        )


class CandidatesCommandTests(unittest.TestCase):
    def test_listing_rows_does_not_crash(self) -> None:
        # UnboundLocalError on `settled`: the count label was read off a local
        # of the empty-scope branch, so every listing that had rows crashed.
        track = make_track(path=Path("/lib/A/01.m4a"), relpath="A/01.m4a")
        state_dir = Path(tempfile.mkdtemp())
        state = State(state_dir / "lyrics.sqlite3")
        args = build_parser().parse_args(["candidates", "--library", "/lib"])
        # cli.main normalises the positional list into `path`; do the same.
        args.paths = args.path = [str(track.path)]
        candidate = Candidate(
            source="netease",
            source_id="7",
            title="Track",
            artists=["Artist"],
            album="Album",
            duration=200.0,
        )
        with mock.patch.object(commands, "load_tracks", return_value=([track], [])):
            with mock.patch.object(
                commands, "build_sources", return_value=[EmptySource()]
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = commands.cmd_candidates(args, state, state_dir)
        self.assertEqual(code, 0)
        self.assertIn("共 1 首", render.strip_ansi(buffer.getvalue()))


class KindSetTests(unittest.TestCase):
    """A provider can serve two formats in one answer; say so, in order."""

    def test_the_order_is_most_detailed_first(self) -> None:
        self.assertEqual(kind_set("lrc,yrc"), ("yrc", "lrc"))
        self.assertEqual(kind_set_label("lrc,yrc"), "YRC/LRC")

    def test_tuples_and_nothing_are_both_accepted(self) -> None:
        self.assertEqual(kind_set(("qrc",)), ("qrc",))
        self.assertEqual(kind_set(""), ())
        self.assertEqual(kind_set(None), ())

    def test_duplicates_collapse(self) -> None:
        self.assertEqual(kind_set_label("yrc,yrc,lrc"), "YRC/LRC")

    def test_a_served_set_reaches_the_plan_table(self) -> None:
        self.assertEqual(kind_set_label(kind_set("yrc,lrc")), "YRC/LRC")


class _OutputSource:
    """A source that answers from memory: one netease id, one bare one."""

    name = "netease"
    min_interval = 0.0

    def __init__(self, kinds: tuple[str, ...]) -> None:
        self.kinds = kinds
        self.fetches = 0

    def lookup(self, ids: list[str]) -> list[Candidate]:
        return [
            Candidate(
                source="netease",
                source_id=str(item),
                title="春泥棒",
                artists=["ヨルシカ"],
                album="創作",
                duration=290.0,
            )
            for item in ids
        ]

    def fetch(self, candidate: Candidate) -> FetchResult:
        self.fetches += 1
        if "yrc" in self.kinds:
            return FetchResult(
                lyrics="[1000,2000](1000,200,0)あ(1200,300,0)い\n",
                native_lyrics="[1000,2000](1000,200,0)あ(1200,300,0)い\n",
                native_kind="yrc",
                native_kinds=self.kinds,
            )
        return FetchResult(
            lyrics="[00:00.00]あ\n",
            native_lyrics="[00:00.00]あ\n",
            native_kind="lrc",
            native_kinds=self.kinds,
        )


class OutputCommandTests(unittest.TestCase):
    """`output` writes files and nothing else; apply writes the audio."""

    def _run(self, source, argv: list[str], state_dir: Path) -> str:
        args = build_parser().parse_args(["output", *argv])
        args.formats = [
            name
            for name in ("lrc", "a2", "eslrc", "yrc", "qrc", "ttml")
            if getattr(args, name, False)
        ]
        args.path = list(args.paths)
        state = State(state_dir / "lyrics.sqlite3")
        buffer = io.StringIO()
        with mock.patch.object(commands, "build_sources", return_value=[source]):
            with redirect_stdout(buffer):
                commands.cmd_output(args, state, state_dir)
        return render.strip_ansi(buffer.getvalue())

    def test_no_format_flag_means_the_native_text(self) -> None:
        out_dir = Path(tempfile.mkdtemp())
        out = self._run(
            _OutputSource(("yrc", "lrc")),
            ["ne:1", "--out", str(out_dir)],
            Path(tempfile.mkdtemp()),
        )
        self.assertIn("YRC", out)
        written = list(out_dir.iterdir())
        self.assertEqual([path.suffix for path in written], [".yrc"])
        self.assertIn("[ne:1]", written[0].name)

    def test_several_formats_are_written_at_once(self) -> None:
        out_dir = Path(tempfile.mkdtemp())
        self._run(
            _OutputSource(("yrc", "lrc")),
            ["ne:1", "--lrc", "--a2", "--ttml", "--out", str(out_dir)],
            Path(tempfile.mkdtemp()),
        )
        self.assertEqual(
            sorted(path.suffix for path in out_dir.iterdir()),
            [".alrc", ".lrc", ".ttml"],
        )

    def test_a_private_format_is_refused_instead_of_faked(self) -> None:
        # ne:1 only has LRC; asking for QRC must not write an .qrc full of LRC.
        out_dir = Path(tempfile.mkdtemp())
        out = self._run(
            _OutputSource(("lrc",)),
            ["ne:1", "--qrc", "--out", str(out_dir)],
            Path(tempfile.mkdtemp()),
        )
        self.assertIn("只能由源原样提供", out)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_an_existing_file_is_skipped_until_forced(self) -> None:
        out_dir = Path(tempfile.mkdtemp())
        state_dir = Path(tempfile.mkdtemp())
        source = _OutputSource(("yrc", "lrc"))
        self._run(source, ["ne:1", "--out", str(out_dir)], state_dir)
        out = self._run(source, ["ne:1", "--out", str(out_dir)], state_dir)
        self.assertIn("已存在", out)

    def test_a_planned_id_never_touches_the_network(self) -> None:
        track = make_track()
        state_dir = Path(tempfile.mkdtemp())
        state = State(state_dir / "lyrics.sqlite3")
        state.upsert_plan(
            PlanResult(
                track,
                "ready",
                "ヨルシカ",
                Candidate(
                    source="netease",
                    source_id="9",
                    title="春泥棒",
                    artists=["ヨルシカ"],
                    album="創作",
                    duration=290.0,
                ),
                lyrics="[00:00.00]あ",
                score=100,
                duration_diff=0.0,
                native_lyrics="[1000,2000](1000,200,0)あ",
                native_kind="yrc",
                native_kinds=("yrc", "lrc"),
            ),
            track.fingerprint(),
        )
        out_dir = Path(tempfile.mkdtemp())
        source = _OutputSource(("yrc", "lrc"))
        out = self._run(source, ["ne:9", "--out", str(out_dir)], state_dir)
        self.assertEqual(source.fetches, 0)
        self.assertIn("状态库", out)
        self.assertIn("YRC/LRC", out)

    def test_the_served_lrc_is_exported_instead_of_a_flattening(self) -> None:
        # netease ships a hand-written LRC next to the YRC; flattening the YRC
        # gives a slightly different file than the one apply would write.
        track = make_track()
        state_dir = Path(tempfile.mkdtemp())
        state = State(state_dir / "lyrics.sqlite3")
        state.upsert_plan(
            PlanResult(
                track,
                "ready",
                "ヨルシカ",
                Candidate(
                    source="netease",
                    source_id="9",
                    title="春泥棒",
                    artists=["ヨルシカ"],
                    album="創作",
                    duration=290.0,
                ),
                lyrics="[00:00.00]源给的逐行\n",
                score=100,
                duration_diff=0.0,
                native_lyrics="[1000,2000](1000,200,0)あ",
                native_kind="yrc",
                native_kinds=("yrc", "lrc"),
            ),
            track.fingerprint(),
        )
        out_dir = Path(tempfile.mkdtemp())
        self._run(
            _OutputSource(("yrc", "lrc")), ["ne:9", "--lrc", "--out", str(out_dir)],
            state_dir,
        )
        written = next(out_dir.iterdir())
        self.assertEqual(written.read_text(encoding="utf-8"), "[00:00.00]源给的逐行\n")

    def test_a_bare_id_means_netease(self) -> None:
        out_dir = Path(tempfile.mkdtemp())
        self._run(
            _OutputSource(("lrc",)), ["42", "--out", str(out_dir)],
            Path(tempfile.mkdtemp()),
        )
        self.assertEqual(
            [path.name for path in out_dir.iterdir()],
            ["ヨルシカ - 春泥棒 [ne:42].lrc"],
        )

    def test_a_track_path_is_rejected_with_the_way_out(self) -> None:
        args = build_parser().parse_args(["output", "A/01.m4a"])
        args.path = list(args.paths)
        args.formats = []
        state = State(Path(tempfile.mkdtemp()) / "lyrics.sqlite3")
        with self.assertRaises(ToolError) as caught:
            commands.cmd_output(args, state, Path(tempfile.mkdtemp()))
        self.assertIn("candidates", str(caught.exception))


class WrongCommandOptionTests(unittest.TestCase):
    def test_a_flag_from_another_command_is_named(self) -> None:
        parser = build_parser()
        specs = option_specs(parser)
        with self.assertRaises(ToolError) as caught:
            reject_unknown_options(
                ["--tsv"], specs, "plan", option_owners(parser)
            )
        self.assertIn("candidates", str(caught.exception))

    def test_the_removed_export_flag_points_at_output(self) -> None:
        parser = build_parser()
        specs = option_specs(parser)
        with self.assertRaises(ToolError) as caught:
            reject_unknown_options(
                ["--lrc-dir"], specs, "apply", option_owners(parser)
            )
        self.assertIn("output", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
