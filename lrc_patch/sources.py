"""HTTP clients and the netease / LRCLIB providers.

Network failures are deliberately *not* treated as "no match": they raise
:class:`SourceError`, which the planner stores as ``error`` and retries on the
next run. Successful responses are cached for 30 days; failures are never
cached, so a transient 406/429/503 cannot freeze a track for a month.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import HTTP_UA
from .errors import SourceError
from .models import Candidate, FetchResult, Track
from .state import State
from .text import is_placeholder, sanitize_lyrics, sha256_text


class _Retryable(Exception):
    """Internal marker: the same request may be attempted again."""


class JsonClient:
    """JSON over HTTP with per-source spacing, retry and a circuit breaker.

    * starts of concurrent requests are spaced by ``min_interval`` (not just
      the gaps between lock acquisitions, which allowed bursts with workers>1);
    * 406/429 increase the spacing and back off harder;
    * two exhausted requests in one run open a cooldown for that source, so a
      blocked provider fails fast instead of stalling the whole album.
    """

    MAX_ATTEMPTS = 3
    FAILURE_THRESHOLD = 2
    BASE_COOLDOWN = 60.0

    def __init__(self, state: State, refresh: bool = False):
        self.state = state
        self.refresh = refresh
        self._last: dict[str, float] = {}
        self._lock_guard = threading.Lock()
        self._failures: dict[str, int] = {}
        self._trips: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._requests: dict[str, int] = {}
        self._failed_requests: dict[str, int] = {}
        self._cache_hits: dict[str, int] = {}

    def _reserve_slot(self, source: str, min_interval: float) -> None:
        """Reserve the next start slot so parallel workers stay spaced."""
        with self._lock_guard:
            interval = min_interval + self._penalty.get(source, 0.0)
            now = time.monotonic()
            slot = max(now, self._last.get(source, 0.0) + interval)
            self._last[source] = slot
        wait = slot - now
        if wait > 0:
            time.sleep(wait)

    def _check_cooldown(self, source: str) -> None:
        with self._lock_guard:
            until = self._cooldown_until.get(source, 0.0)
        remaining = until - time.monotonic()
        if remaining > 0:
            raise SourceError(
                source,
                f"{source} 暂时不可用（风控/限流），约 {remaining:.0f}s 后自动重试",
            )

    def _note_success(self, source: str) -> None:
        with self._lock_guard:
            self._failures[source] = 0
            self._trips[source] = 0
            penalty = self._penalty.get(source, 0.0)
            if penalty:
                self._penalty[source] = max(0.0, penalty * 0.5 - 0.05)

    def _note_failure(self, source: str) -> None:
        with self._lock_guard:
            self._failed_requests[source] = self._failed_requests.get(source, 0) + 1
            failures = self._failures.get(source, 0) + 1
            self._failures[source] = failures
            if failures < self.FAILURE_THRESHOLD:
                return
            trips = self._trips.get(source, 0) + 1
            self._trips[source] = trips
            cooldown = min(600.0, self.BASE_COOLDOWN * 2 ** (trips - 1))
            self._cooldown_until[source] = time.monotonic() + cooldown
            self._failures[source] = 0

    def _note_rate_limit(self, source: str) -> None:
        with self._lock_guard:
            self._penalty[source] = min(5.0, self._penalty.get(source, 0.0) + 1.5)

    def health(self) -> list[tuple[str, int, int, int]]:
        """(source, requests, failed requests, cooldown seconds)."""
        with self._lock_guard:
            now = time.monotonic()
            names = sorted(set(self._requests) | set(self._cache_hits))
            return [
                (
                    name,
                    self._requests.get(name, 0),
                    self._failed_requests.get(name, 0),
                    max(0, int(round(self._cooldown_until.get(name, 0.0) - now))),
                )
                for name in names
            ]

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None = None) -> float:
        delay = min(20.0, 1.5 * (2 ** (attempt - 1)))
        if retry_after:
            delay = max(delay, min(retry_after, 30.0))
        return delay + random.uniform(0.0, 0.6)

    def get(
        self,
        source: str,
        url: str,
        min_interval: float,
        cache_predicate: Callable[[Any], bool] | None = None,
    ) -> tuple[int, Any | None]:
        cache_key = sha256_text(url)
        if not self.refresh:
            cached = self.state.cached_http(cache_key)
            if cached is not None:
                with self._lock_guard:
                    self._cache_hits[source] = self._cache_hits.get(source, 0) + 1
                return int(cached["status"]), cached.get("data")

        self._check_cooldown(source)
        self._reserve_slot(source, min_interval)
        self._check_cooldown(source)
        with self._lock_guard:
            self._requests[source] = self._requests.get(source, 0) + 1

        last_error = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            retry_after: float | None = None
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": HTTP_UA,
                        "Accept": "application/json",
                        "Referer": "https://music.163.com/"
                        if source == "netease"
                        else "https://lrclib.net/",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as response:
                    raw = response.read()
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _Retryable(
                        f"返回内容不是 JSON（{len(raw)} 字节）"
                    ) from exc
                code = data.get("code") if isinstance(data, dict) else None
                if code is not None and str(code) != "200":
                    if str(code) in {"406", "429"}:
                        self._note_rate_limit(source)
                        raise _Retryable(f"返回 code {code}（限流）")
                    raise SourceError(source, f"{source} 返回 code {code}")
                if cache_predicate is None or cache_predicate(data):
                    self.state.cache_http(
                        cache_key, {"status": 200, "data": data}, 30 * 86400
                    )
                self._note_success(source)
                return 200, data
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 404):
                    self.state.cache_http(
                        cache_key, {"status": exc.code, "data": None}, 86400
                    )
                    self._note_success(source)
                    return exc.code, None
                if exc.code in (406, 429):
                    self._note_rate_limit(source)
                    retry_after = _retry_after_seconds(exc)
                elif exc.code not in (500, 502, 503, 504):
                    raise SourceError(
                        source, f"{source} 请求失败: HTTP {exc.code}", retryable=False
                    ) from exc
                last_error = f"HTTP {exc.code}"
                if exc.code == 503:
                    last_error += "（服务繁忙）"
            except _Retryable as exc:
                last_error = str(exc)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.MAX_ATTEMPTS:
                time.sleep(self._backoff(attempt, retry_after))

        self._note_failure(source)
        raise SourceError(
            source, f"{source} 连续 {self.MAX_ATTEMPTS} 次失败：{last_error}"
        )


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def netease_has_songs(payload: Any) -> bool:
    """Do not freeze a transient empty search response in the 30-day cache."""
    return bool(((payload or {}).get("result") or {}).get("songs"))


class NeteaseSource:
    name = "netease"
    min_interval = 0.35

    def __init__(self, client: JsonClient):
        self.client = client

    def search(self, track: Track) -> list[Candidate]:
        base = "https://music.163.com/api/search/get/web?"
        queries = [f"{track.title} {track.artist}".strip()]
        if track.artist:
            queries.append(track.title)
        seen: set[str] = set()
        candidates: list[Candidate] = []
        for query in queries:
            url = base + urllib.parse.urlencode(
                {"s": query, "type": 1, "limit": 15, "offset": 0}
            )
            try:
                code, data = self.client.get(
                    self.name, url, self.min_interval, netease_has_songs
                )
            except SourceError:
                if candidates:
                    break
                raise
            if code != 200 or not data:
                continue
            if not isinstance(data, dict):
                continue
            songs = (data.get("result") or {}).get("songs") or []
            for song in songs:
                sid = str(song.get("id") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                candidates.append(
                    Candidate(
                        source=self.name,
                        source_id=sid,
                        title=str(song.get("name") or ""),
                        artists=[
                            str(a.get("name") or "")
                            for a in (song.get("artists") or [])
                            if a.get("name")
                        ],
                        album=str((song.get("album") or {}).get("name") or ""),
                        duration=float(song.get("duration") or 0) / 1000.0,
                    )
                )
            if candidates:
                break
        return candidates

    def fetch(self, candidate: Candidate) -> FetchResult:
        url = (
            "https://music.163.com/api/song/lyric?"
            + urllib.parse.urlencode(
                {"id": candidate.source_id, "lv": 1, "kv": 1, "tv": -1}
            )
        )
        code, data = self.client.get(self.name, url, self.min_interval)
        if code != 200 or not data:
            return FetchResult(note=f"歌词接口 HTTP {code}")
        raw = (data.get("lrc") or {}).get("lyric") or ""
        lyrics = sanitize_lyrics(raw)
        if not lyrics or is_placeholder(lyrics):
            instrumental = bool(data.get("nolyric") or data.get("pureMusic"))
            return FetchResult(instrumental=instrumental, note="无可用歌词")
        return FetchResult(lyrics=lyrics)


class LrclibSource:
    name = "lrclib"
    min_interval = 0.6

    def __init__(self, client: JsonClient):
        self.client = client

    def search(self, track: Track) -> list[Candidate]:
        params = {"track_name": track.title}
        if track.artist:
            params["artist_name"] = track.artist
        if track.album:
            params["album_name"] = track.album
        url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
        code, data = self.client.get(
            self.name,
            url,
            self.min_interval,
            cache_predicate=lambda payload: isinstance(payload, list),
        )
        if code == 404:
            return []
        if code != 200:
            return []
        if not isinstance(data, list):
            raise SourceError(self.name, "lrclib 返回格式异常（不是列表）")
        out: list[Candidate] = []
        for item in data:
            lyrics = sanitize_lyrics(
                item.get("syncedLyrics") or item.get("plainLyrics") or ""
            )
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(item.get("id") or ""),
                    title=str(item.get("trackName") or item.get("name") or ""),
                    artists=[str(item.get("artistName") or "")],
                    album=str(item.get("albumName") or ""),
                    duration=float(item.get("duration") or 0),
                    lyrics=lyrics,
                    synced=bool(item.get("syncedLyrics")),
                    instrumental=bool(item.get("instrumental")),
                )
            )
        return out

    def fetch(self, candidate: Candidate) -> FetchResult:
        if candidate.instrumental:
            return FetchResult(instrumental=True, note="纯音乐")
        if candidate.lyrics and not is_placeholder(candidate.lyrics):
            return FetchResult(lyrics=candidate.lyrics)
        return FetchResult(note="无可用歌词")


def build_sources(names: list[str], client: JsonClient) -> list[Any]:
    registry = {"netease": NeteaseSource, "lrclib": LrclibSource}
    return [registry[name](client) for name in names]
