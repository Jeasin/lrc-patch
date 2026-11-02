"""Fast regressions that do not touch a real library or the network."""
from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lrc_patch import render, sources
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
    def test_notes_do_not_wrap_by_default(self) -> None:
        self.assertFalse(render._WRAP_ENABLED)

    def test_unknown_option_is_suggested(self) -> None:
        specs = option_specs(build_parser())
        with self.assertRaises(ToolError) as ctx:
            reject_unknown_options(["--no-warp"], specs)
        message = str(ctx.exception)
        self.assertIn("--no-wrap", message)
        self.assertIn("默认已经不换行", message)

    def test_known_options_pass(self) -> None:
        specs = option_specs(build_parser())
        reject_unknown_options(["--workers", "2", "--pick", "netease:1"], specs)

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
        render.set_wrap(True)
        self._width = render.screen_width
        render.screen_width = lambda: 100

    def tearDown(self) -> None:
        render.screen_width = self._width
        render.set_wrap(False)

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

    def test_no_wrap_keeps_note_on_one_line(self) -> None:
        render.set_wrap(False)
        long_note = "错误：" + "x" * 300
        lines = self._render(
            [TreeEntry(Path("/library/A/01 track.m4a"), "error", long_note)]
        )
        matching = [line for line in lines if "错误：" in line]
        self.assertEqual(len(matching), 1)
        self.assertGreater(len(matching[0]), 200)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
