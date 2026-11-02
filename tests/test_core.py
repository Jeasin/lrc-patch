"""Fast regressions that do not touch a real library or the network."""
from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lrc_patch import render, sorting, sources
from lrc_patch.cli_args import (
    build_parser,
    normalize_argv,
    option_specs,
    reject_unknown_options,
)
from lrc_patch.errors import SourceError, ToolError
from lrc_patch.matching import keep_previous_plan, plan_one, score_candidate
from lrc_patch.models import Candidate, Track, TreeEntry
from lrc_patch.state import State
from lrc_patch.text import (
    lrc_timestamp_issue,
    lyric_kind,
    lyric_tag,
    match_note,
    sha256_text,
    strip_edition,
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

    def test_lrclib_is_not_a_default_source(self) -> None:
        args = build_parser().parse_args(["plan"])
        self.assertEqual(args.sources, "netease")

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

    def test_notes_start_at_same_column_and_wrap_below_it(self) -> None:
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
        note_lines = lines[lines.index(second) + 1 :]
        wrapped = [line for line in note_lines if line.strip() and line.startswith(" ")]
        self.assertTrue(wrapped)
        for line in wrapped:
            self.assertEqual(len(line) - len(line.lstrip()), column)

    def test_long_notes_always_wrap(self) -> None:
        long_note = "错误：" + "x" * 300
        lines = self._render(
            [TreeEntry(Path("/library/A/01 track.m4a"), "error", long_note)]
        )
        matching = [line for line in lines if "错误：" in line]
        self.assertEqual(len(matching), 1)
        self.assertLess(len(matching[0]), 120)
        self.assertGreater(len(lines), 2)

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
            lyric_kind("[00:12.34]<00:12.34>word <00:12.80>two"), "enhanced"
        )
        self.assertEqual(lyric_kind("[1234,567](1234,567,0)word"), "yrc")
        self.assertEqual(
            lyric_kind('<?xml version="1.0"?><tt xmlns="http://www.w3.org/ns/ttml">'),
            "ttml",
        )

    def test_tags_and_colours_differ(self) -> None:
        self.assertEqual(lyric_tag("plain"), "TXT")
        self.assertEqual(lyric_tag("lrc"), "LRC")
        self.assertEqual(lyric_tag("enhanced"), "LRC+")
        colours = {
            render.LYRIC_COLORS[kind]
            for kind in ("plain", "lrc", "enhanced", "yrc", "ttml")
        }
        self.assertEqual(len(colours), 5)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
