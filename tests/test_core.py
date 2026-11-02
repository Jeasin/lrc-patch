"""Fast regressions that do not touch a real library or the network."""
from __future__ import annotations

import io
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
    option_specs,
    reject_unknown_options,
)
from lrc_patch.errors import SourceError, ToolError
from lrc_patch.matching import assign_picks, keep_previous_plan, plan_one, score_candidate
from lrc_patch.models import Candidate, Track, TreeEntry
from lrc_patch.state import State
from lrc_patch.text import (
    album_points,
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


class JsonClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tempdir.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

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
        self.assertEqual(lyric_kind("[1234,567]word(1234,567)"), "qrc")
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
        import zlib

        from lrc_patch.qrc import QRC_KEY, _tripledes_crypt, _tripledes_key_setup, _ENCRYPT

        qrc_plain = '<Lyric_1 LyricType="1" LyricContent="[0,5000](0,200)我(200,300)爱"/>'
        data = bytearray(zlib.compress(qrc_plain.encode()))
        schedule = _tripledes_key_setup(QRC_KEY, _ENCRYPT)
        out = bytearray()
        for off in range(0, len(data), 8):
            block = data[off : off + 8]
            out += _tripledes_crypt(block, schedule) if len(block) == 8 else block
        encrypted = bytes(out).hex()

        lyric_resp = (200, {"code": 0, "request": {"code": 0, "data": {"lyric": encrypted}}})
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
        self.assertIn("我爱", result.lyrics)

    def test_fetch_qrc_target_keeps_native_qrc(self) -> None:
        import zlib

        from lrc_patch.qrc import QRC_KEY, _tripledes_crypt, _tripledes_key_setup, _ENCRYPT

        qrc_plain = '<Lyric_1 LyricType="1" LyricContent="[0,5000](0,200)我(200,300)爱"/>'
        data = bytearray(zlib.compress(qrc_plain.encode()))
        schedule = _tripledes_key_setup(QRC_KEY, _ENCRYPT)
        out = bytearray()
        for off in range(0, len(data), 8):
            block = data[off : off + 8]
            out += _tripledes_crypt(block, schedule) if len(block) == 8 else block
        encrypted = bytes(out).hex()

        lyric_resp = (200, {"code": 0, "request": {"code": 0, "data": {"lyric": encrypted}}})
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

    def __init__(self, name: str, candidates: list[Candidate]) -> None:
        self.name = name
        self._candidates = candidates

    def search(self, track: Track, exhaustive: bool = False) -> list[Candidate]:
        return list(self._candidates)

    def lookup(self, ids: list[str]) -> list[Candidate]:
        wanted = set(ids)
        return [item for item in self._candidates if item.source_id in wanted]


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
        self.assertIn("netease 72", result.reason)
        self.assertIn("qq 87", result.reason)
        self.assertIn("qq:244115386", result.reason)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
