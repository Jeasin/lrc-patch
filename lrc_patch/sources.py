"""HTTP clients and the netease / LRCLIB providers.

Network failures are deliberately *not* treated as "no match": they raise
:class:`SourceError`, which the planner stores as ``error`` and retries on the
next run. Successful responses are cached for 30 days; failures are never
cached, so a transient 406/429/503 cannot freeze a track for a month.

Two kinds of refusal are distinguished:

* **rate limit** (netease ``code 405/406/429``, HTTP 403/405/406/429) — the
  provider is punishing this IP. The request gets one gentler retry, then the
  whole source is parked for a few minutes so the remaining tracks fail fast
  instead of deepening the block.
* **overload** (HTTP 500/502/503/504) — a busy server. Normal backoff, then a
  short cooldown after two exhausted requests.
"""
from __future__ import annotations

import gzip
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from typing import Any, Callable

from .config import HTTP_UA
from .errors import SourceError
from .lyric_fmt import (
    has_timing,
    lines_to_lrc,
    lines_to_plain,
    parse_native,
    parse_qrc,
    render_native,
)
from .matching import score_candidate
from .models import Candidate, FetchResult, Track
from .qrc import qrc_decrypt, qrc_lyric_text
from .state import State
from .text import TIMESTAMP_RE, is_placeholder, sanitize_lyrics, sha256_text


class _Retryable(Exception):
    """Internal marker: the same request may be attempted again."""


class _RateLimited(Exception):
    """Internal marker: the provider is rate limiting this client."""


def _gunzip(data: bytes) -> bytes:
    """Best-effort gzip decompress; returns input unchanged on failure."""
    try:
        return gzip.decompress(data)
    except (OSError, EOFError):
        return data


class UrllibTransport:
    """Standard-library HTTP; no external dependency, plain Python TLS."""

    name = "urllib"

    def request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | str | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        if isinstance(body, bytes):
            data: bytes | None = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = urllib.parse.urlencode(body).encode() if body else None
        request = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
                headers = dict(getattr(response, "headers", {}) or {})
                encoding = str(headers.get("Content-Encoding") or headers.get("content-encoding") or "")
                if "gzip" in encoding.lower():
                    raw = _gunzip(raw)
                return status, raw, headers
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b""
            return int(exc.code), payload, dict(exc.headers or {})


class CurlTransport:
    """curl_cffi transport: real browser TLS/JA3 fingerprint.

    Cloudflare-fronted providers (LRCLIB) and netease's WAF both fingerprint
    the TLS handshake; ``impersonate="chrome"`` makes the request look like a
    Chrome tab. Network errors are re-raised as OSError so the retry loop and
    the "not a no-match" rule apply unchanged.
    """

    name = "curl_cffi"

    def __init__(self, impersonate: str = "chrome"):
        from curl_cffi import requests as curl_requests

        self.impersonate = impersonate
        self._session = curl_requests.Session(impersonate=impersonate)

    def request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | str | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        try:
            response = self._session.request(
                method, url, data=body, headers=headers or {}, timeout=timeout
            )
        except Exception as exc:  # curl errors are not OSError subclasses
            raise OSError(f"{type(exc).__name__}: {exc}") from exc
        return (
            int(response.status_code),
            response.content or b"",
            dict(response.headers or {}),
        )


def make_transport(impersonate: bool = True) -> Any:
    """Prefer the browser-fingerprint transport; fall back silently."""
    if impersonate:
        try:
            return CurlTransport()
        except Exception:
            pass
    return UrllibTransport()


RATE_LIMIT_STATUS = {403, 405, 406, 429}
BUSY_STATUS = {500, 502, 503, 504}
RATE_LIMIT_CODES = {"405", "406", "429"}
BUSY_CODES = {"500", "502", "503", "504"}

# QQ request fields that change on every call; they must stay out of the cache
# key or the answer can never be reused.
_QQ_VOLATILE = {"search_id"}


class JsonClient:
    """JSON over HTTP with per-source spacing, retry and a circuit breaker.

    * starts of concurrent requests are spaced by ``min_interval`` (not just
      the gaps between lock acquisitions, which allowed bursts with workers>1);
    * rate-limit answers increase the spacing, back off harder, and park the
      source once the retries are exhausted;
    * two exhausted requests in one run open a cooldown for that source, so a
      blocked provider fails fast instead of stalling the whole album.
    """

    MAX_ATTEMPTS = 3
    FAILURE_THRESHOLD = 2
    BASE_COOLDOWN = 60.0
    RATE_LIMIT_COOLDOWN = 180.0

    def __init__(
        self,
        state: State,
        refresh: bool = False,
        transport: Any | None = None,
        impersonate: bool = True,
    ):
        self.state = state
        self.refresh = refresh
        self.transport = transport or make_transport(impersonate)
        self._last: dict[str, float] = {}
        self._lock_guard = threading.Lock()
        self._failures: dict[str, int] = {}
        self._trips: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._requests: dict[str, int] = {}
        self._failed_requests: dict[str, int] = {}
        self._cache_hits: dict[str, int] = {}
        # search vs lyric requests, so the run can show that one track costs
        # one search plus one lyric fetch - not one request per displayed row.
        self._kinds: dict[tuple[str, str], int] = {}

    # -- pacing ---------------------------------------------------------
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

    def cooldown(self, source: str) -> float:
        with self._lock_guard:
            until = self._cooldown_until.get(source, 0.0)
        return max(0.0, until - time.monotonic())

    def _check_cooldown(self, source: str) -> None:
        remaining = self.cooldown(source)
        if remaining > 0:
            raise SourceError(
                source,
                f"{source} 已暂停（限流/连续失败），约 {remaining:.0f}s 后自动解除；"
                "可稍后重跑，或用 --src 换源",
            )

    def _open_breaker(self, source: str, seconds: float) -> None:
        with self._lock_guard:
            until = max(self._cooldown_until.get(source, 0.0), time.monotonic() + seconds)
            self._cooldown_until[source] = until
            self._failures[source] = 0

    def _note_success(self, source: str) -> None:
        with self._lock_guard:
            self._failures[source] = 0
            self._trips[source] = 0
            self._cooldown_until.pop(source, None)
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
            self._penalty[source] = min(6.0, self._penalty.get(source, 0.0) + 2.0)

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

    def cache_hits(self) -> int:
        """Requests answered from the shared HTTP cache instead of the network."""
        with self._lock_guard:
            return sum(self._cache_hits.values())

    def breakdown(self) -> dict[str, dict[str, int]]:
        """Per source: how many real requests went to search vs lyric fetch."""
        with self._lock_guard:
            out: dict[str, dict[str, int]] = {}
            for (source, kind), count in self._kinds.items():
                out.setdefault(source, {})[kind] = count
            return out

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None = None) -> float:
        delay = min(20.0, 1.5 * (2 ** (attempt - 1)))
        if retry_after:
            delay = max(delay, min(retry_after, 30.0))
        return delay + random.uniform(0.0, 0.6)

    @staticmethod
    def _headers(source: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": HTTP_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if source == "netease":
            headers["Referer"] = "https://music.163.com/"
            headers["Origin"] = "https://music.163.com"
            headers["Cookie"] = "appver=2.10.13; os=osx; channel=netease"
        elif source == "lrclib":
            headers["Referer"] = "https://lrclib.net/"
        elif source == "qq":
            headers["User-Agent"] = "okhttp/3.14.9"
            headers["Content-Type"] = "application/json"
            headers["Accept-Encoding"] = "gzip"
            headers["Cookie"] = "tmeLoginType=-1;"
        if extra:
            headers.update(extra)
        return headers

    # -- requests -------------------------------------------------------
    def get(
        self,
        source: str,
        url: str,
        min_interval: float,
        cache_predicate: Callable[[Any], bool | float] | None = None,
        *,
        kind: str = "request",
    ) -> tuple[int, Any | None]:
        return self.request(
            source, url, min_interval, cache_predicate=cache_predicate, kind=kind
        )

    def request(
        self,
        source: str,
        url: str,
        min_interval: float,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        raw_body: str | bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        expect_code: str = "200",
        cache_predicate: Callable[[Any], bool | float] | None = None,
        cache_extra: str = "",
        kind: str = "request",
    ) -> tuple[int, Any | None]:
        key_source = url + (f"\n{cache_extra}" if cache_extra else "")
        cache_key = sha256_text(key_source)
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
            self._kinds[(source, kind)] = self._kinds.get((source, kind), 0) + 1

        last_error = ""
        rate_limited = False
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            retry_after: float | None = None
            try:
                headers = self._headers(source, extra_headers)
                transport_body: Any = raw_body if raw_body is not None else body
                status, raw, response_headers = self.transport.request(
                    method, url, body=transport_body, headers=headers, timeout=30.0
                )
                retry_after = _retry_after_seconds(response_headers)
                if status in (400, 404):
                    self.state.cache_http(
                        cache_key, {"status": status, "data": None}, 86400
                    )
                    self._note_success(source)
                    return status, None
                if status in RATE_LIMIT_STATUS:
                    self._note_rate_limit(source)
                    rate_limited = True
                    raise _RateLimited(f"HTTP {status}")
                if status in BUSY_STATUS:
                    raise _Retryable(
                        f"HTTP {status}" + ("（服务繁忙）" if status == 503 else "")
                    )
                if status >= 400:
                    raise SourceError(
                        source, f"{source} 请求失败: HTTP {status}", retryable=False
                    )
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _Retryable(f"返回内容不是 JSON（{len(raw)} 字节）") from exc
                code = data.get("code") if isinstance(data, dict) else None
                if code is not None and str(code) != expect_code:
                    text = str(code)
                    if text in RATE_LIMIT_CODES:
                        self._note_rate_limit(source)
                        rate_limited = True
                        raise _RateLimited(f"code {text}{_code_hint(data)}")
                    if text in BUSY_CODES:
                        raise _Retryable(f"code {text}（服务繁忙）")
                    raise SourceError(source, f"{source} 返回 code {code}")
                if cache_predicate is None:
                    ttl = 30 * 86400
                else:
                    verdict = cache_predicate(data)
                    if isinstance(verdict, bool):
                        ttl = 30 * 86400 if verdict else 0
                    else:
                        ttl = float(verdict)
                if ttl:
                    self.state.cache_http(
                        cache_key, {"status": 200, "data": data}, ttl
                    )
                self._note_success(source)
                return 200, data
            except _RateLimited as exc:
                last_error = str(exc)
            except _Retryable as exc:
                last_error = str(exc)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.MAX_ATTEMPTS:
                delay = self._backoff(attempt, retry_after)
                if rate_limited:
                    # A penalty box needs a real pause; 1.5s only makes it worse.
                    delay = max(delay, 4.0 * attempt)
                time.sleep(delay)

        if rate_limited:
            self._open_breaker(source, self.RATE_LIMIT_COOLDOWN)
            raise SourceError(
                source,
                f"{source} 限流（{last_error}），已暂停该源 "
                f"{self.RATE_LIMIT_COOLDOWN:.0f}s",
            )
        self._note_failure(source)
        raise SourceError(
            source, f"{source} 连续 {self.MAX_ATTEMPTS} 次失败：{last_error}"
        )


def _code_hint(payload: Any) -> str:
    if isinstance(payload, dict):
        message = str(payload.get("msg") or payload.get("message") or "").strip()
        if message:
            return f"（{message}）"
    return ""


def _retry_after_seconds(headers: dict[str, str] | None) -> float | None:
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() != "retry-after":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def netease_cache_ttl(payload: Any) -> float:
    """Cache a search for 30 days when it found songs, 1 day when it did not.

    An empty answer is a valid result, not a provider outage, so caching it
    saves candidates-then-plan from asking twice.  It gets a shorter TTL than
    a hit because new uploads do change the answer.  Failures are never
    cached at all.
    """
    songs = ((payload or {}).get("result") or {}).get("songs")
    return 30 * 86400 if songs else 86400


def _best_metadata(track: Track, candidates: list[Candidate]) -> int:
    """Best title+artist+album score in a batch, ignoring duration."""
    best = 0
    for candidate in candidates:
        score, _ = score_candidate(track, candidate, 999.0)
        best = max(best, score)
    return best


class NeteaseSource:
    """Search is POST /api/search/get; the old GET endpoint is deprecated.

    Pacing is deliberately slow: netease answers anonymous clients with
    ``code 405 操作频繁`` after roughly 30 fast requests and then keeps the IP
    blocked for minutes, which is far more expensive than 1s of waiting.
    """

    name = "netease"
    min_interval = 0.9

    def __init__(self, client: JsonClient, target_format: str | None = None):
        self.client = client
        self.target_format = target_format

    def search(self, track: Track, exhaustive: bool = False) -> list[Candidate]:
        """One search request per track in the normal case.

        The first query is "title artist".  A title-only fallback costs a
        second request and is only sent when that batch had nothing matching
        both fields; a 100-point hit (or anything ≥85, which is enough to
        plan) stops right there.  ``--top N`` only limits the rows printed, so
        it never multiplies requests.
        """
        base = "https://music.163.com/api/search/get"
        queries = [f"{track.title} {track.artist}".strip()]
        if track.artist:
            queries.append(track.title)
        seen: set[str] = set()
        candidates: list[Candidate] = []
        for query in queries:
            payload = {"s": query, "type": 1, "limit": 15, "offset": 0}
            try:
                code, data = self.client.request(
                    self.name,
                    base,
                    self.min_interval,
                    method="POST",
                    body=payload,
                    cache_predicate=netease_cache_ttl,
                    cache_extra=query,
                    kind="search",
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
            # Stop as soon as the first batch can be planned; a pick wants the
            # second query too (the chosen ID may only be in that page).
            if candidates and not exhaustive and _best_metadata(track, candidates) >= 85:
                break
        return candidates

    def lookup(self, ids: list[str]) -> list[Candidate]:
        """Resolve song IDs to title/artist/album/duration in one request.

        ``--pick`` may name several candidates at once; without their metadata
        there is no way to tell which track each one belongs to.
        """
        wanted = [item for item in ids if str(item).isdigit()]
        if not wanted:
            return []
        url = (
            "https://music.163.com/api/song/detail?"
            + urllib.parse.urlencode({"ids": "[" + ",".join(wanted) + "]"})
        )
        code, data = self.client.get(self.name, url, self.min_interval, kind="detail")
        if code != 200 or not isinstance(data, dict):
            return []
        out: list[Candidate] = []
        for song in data.get("songs") or []:
            sid = str(song.get("id") or "")
            if not sid:
                continue
            out.append(
                Candidate(
                    source=self.name,
                    source_id=sid,
                    title=str(song.get("name") or ""),
                    artists=[
                        str(artist.get("name") or "")
                        for artist in (song.get("artists") or [])
                        if artist.get("name")
                    ],
                    album=str((song.get("album") or {}).get("name") or ""),
                    duration=float(song.get("duration") or 0) / 1000.0,
                )
            )
        return out

    def fetch(self, candidate: Candidate) -> FetchResult:
        url = (
            "https://music.163.com/api/song/lyric?"
            + urllib.parse.urlencode(
                {"id": candidate.source_id, "lv": 1, "kv": 1, "tv": -1, "yv": 1}
            )
        )
        code, data = self.client.get(
            self.name, url, self.min_interval, kind="lyric"
        )
        if code != 200 or not data:
            return FetchResult(note=f"歌词接口 HTTP {code}")
        # One request carries both forms: the verbatim YRC lives under
        # ``data.yrc.lyric`` and the line-timed LRC under ``data.lrc.lyric``.
        # Both are kept, so a format change later is a conversion, not a
        # re-fetch.
        yrc_text = sanitize_lyrics((data.get("yrc") or {}).get("lyric") or "")
        lrc_text = sanitize_lyrics((data.get("lrc") or {}).get("lyric") or "")
        if yrc_text and is_placeholder(yrc_text):
            yrc_text = ""
        if lrc_text and is_placeholder(lrc_text):
            lrc_text = ""
        if not yrc_text and not lrc_text:
            instrumental = bool(data.get("nolyric") or data.get("pureMusic"))
            return FetchResult(instrumental=instrumental, note="无可用歌词")
        native_kind = "yrc" if yrc_text else "lrc"
        native = yrc_text or lrc_text
        note = ""
        if self.target_format in (None, "lrc"):
            # Prefer netease's own line-timed text over a flattened YRC: it is
            # what the provider intends players without word timing to show.
            lyrics = lrc_text or lines_to_lrc(parse_native(native, native_kind))
        else:
            lyrics, note = render_native(native, native_kind, self.target_format)
        lyrics = sanitize_lyrics(lyrics)
        if not lyrics:
            return FetchResult(note=note or "无可用歌词")
        return FetchResult(
            lyrics=lyrics,
            note=note,
            native_lyrics=native,
            native_kind=native_kind,
        )


class LrclibSource:
    name = "lrclib"
    min_interval = 0.7

    def __init__(self, client: JsonClient):
        self.client = client

    def lookup(self, ids: list[str]) -> list[Candidate]:
        """LRCLIB has no per-ID metadata endpoint; multi --pick cannot use it."""
        return []

    def search(self, track: Track, exhaustive: bool = False) -> list[Candidate]:
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
            cache_predicate=lambda payload: (
                0
                if not isinstance(payload, list)
                else (30 * 86400 if payload else 86400)
            ),
            kind="search",
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


class QqSource:
    """QQ Music, used as a fallback when netease has no match.

    The public endpoints are the ``musicu.fcg`` JSON-RPC used by the QQ Music
    lite client: a one-off ``GetSession`` registers an anonymous uid/sid, then
    search and lyric requests ride that session.  Lyrics arrive as QRC
    (verbatim, word-timed) encrypted with QQ's Triple-DES variant; this source
    decrypts them and, by default, flattens the verbatim timeline to plain
    line-timed LRC.  With ``--qrc`` it returns the native QRC, and with
    ``--a2``/``--eslrc``/``--ttml`` it converts to the chosen public format.

    QQ sits on the default path as an automatic fallback behind netease.
    """

    name = "qq"
    min_interval = 0.9
    _endpoint = "https://u.y.qq.com/cgi-bin/musicu.fcg"

    def __init__(self, client: JsonClient, target_format: str | None = None):
        self.client = client
        self.target_format = target_format
        self._comm: dict[str, Any] = {
            "ct": 11,
            "cv": "1003006",
            "v": "1003006",
            "os_ver": "15",
            "phonetype": "24122RKC7C",
            "rom": "Redmi/miro/miro:15/AE3A.240806.005/OS2.0.10.5.0.VOMCNXM:user/release-keys",
            "tmeAppID": "qqmusiclight",
            "nettype": "NETWORK_WIFI",
            "udid": "0",
        }
        self._uid = "0"
        self._sid = ""
        self._userip = ""

    def _rpc(
        self, method: str, module: str, param: dict[str, Any], kind: str = "request"
    ) -> Any:
        """POST one musicu.fcg request; returns the inner ``request.data``."""
        body = {
            "comm": {**self._comm, "uid": self._uid, "sid": self._sid, "userip": self._userip},
            "request": {"method": method, "module": module, "param": param},
        }
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        # The cache key must not contain anything that changes per call: the
        # session (uid/sid) is new every run, and search_id is randomised on
        # purpose.  Using the raw body as the key made QQ answers uncacheable,
        # so `candidates --src qq` re-searched everything plan had just asked.
        stable = {
            key: value for key, value in param.items() if key not in _QQ_VOLATILE
        }
        cache_extra = json.dumps(
            {"method": method, "param": stable}, ensure_ascii=False, sort_keys=True
        )
        code, data = self.client.request(
            self.name,
            self._endpoint,
            self.min_interval,
            method="POST",
            raw_body=raw,
            expect_code="0",
            cache_extra=cache_extra,
            cache_predicate=lambda payload: 30 * 86400
            if isinstance(payload, dict) and (payload.get("request") or {}).get("data")
            else 86400,
            kind=kind,
        )
        if code != 200 or not isinstance(data, dict):
            raise SourceError(self.name, f"qq 请求失败（HTTP {code}）")
        request = data.get("request") or {}
        if request.get("code") != 0:
            raise SourceError(self.name, f"qq 返回 code {request.get('code')}")
        return request.get("data")

    def _init(self) -> None:
        if self._sid:
            return
        data = self._rpc(
            "GetSession", "music.getSession.session", {"caller": 0, "uid": "0", "vkey": 0},
            kind="session",
        )
        session = data.get("session") or {}
        self._uid = str(session.get("uid") or "0")
        self._sid = str(session.get("sid") or "")
        self._userip = str(session.get("userip") or "")

    def search(self, track: Track, exhaustive: bool = False) -> list[Candidate]:
        try:
            self._init()
        except SourceError:
            raise
        query = f"{track.title} {track.artist}".strip() if track.artist else track.title
        param = {
            "search_id": str(random.randint(1, 20) * 18014398509481984 + random.randint(0, 4194304) * 4294967296 + round(time.time() * 1000) % 86400000),
            "remoteplace": "search.android.keyboard",
            "query": query,
            "search_type": 0,
            "num_per_page": 20,
            "page_num": 1,
            "highlight": 0,
            "nqc_flag": 0,
            "page_id": 1,
            "grp": 1,
        }
        data = self._rpc(
            "DoSearchForQQMusicLite",
            "music.search.SearchCgiService",
            param,
            kind="search",
        )
        songs = (data or {}).get("body", {}).get("item_song") or []
        seen: set[str] = set()
        out: list[Candidate] = []
        for song in songs:
            sid = str(song.get("id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            artists = [str(s.get("name") or "") for s in (song.get("singer") or []) if s.get("name")]
            out.append(
                Candidate(
                    source=self.name,
                    source_id=sid,
                    title=str(song.get("title") or ""),
                    artists=artists,
                    album=str((song.get("album") or {}).get("name") or ""),
                    duration=float(song.get("interval") or 0),
                )
            )
        return out

    def lookup(self, ids: list[str]) -> list[Candidate]:
        """QQ has no batch detail call; one get_song_detail per picked ID."""
        out: list[Candidate] = []
        for sid in ids:
            if not str(sid).isdigit():
                continue
            try:
                self._init()
                data = self._rpc(
                    "get_song_detail",
                    "music.pf_song_detail_svr",
                    {"song_id": int(sid)},
                    kind="detail",
                )
            except SourceError:
                raise
            track = (data or {}).get("track_info") or {}
            if not track:
                continue
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(track.get("id") or sid),
                    title=str(track.get("title") or track.get("name") or ""),
                    artists=[
                        str(singer.get("name") or "")
                        for singer in (track.get("singer") or [])
                        if singer.get("name")
                    ],
                    album=str((track.get("album") or {}).get("name") or ""),
                    duration=float(track.get("interval") or 0),
                )
            )
        return out

    def fetch(self, candidate: Candidate) -> FetchResult:
        try:
            self._init()
        except SourceError:
            raise
        if not candidate.title or not candidate.album or not candidate.source_id or not candidate.duration:
            return FetchResult(note="缺少必要参数，无法向 qq 取词")
        param = {
            "albumName": b64encode(candidate.album.encode()).decode(),
            "crypt": 1,
            "ct": 19,
            "cv": 2111,
            "interval": int(candidate.duration),
            "lrc_t": 0,
            "qrc": 1,
            "qrc_t": 0,
            "roma": 0,
            "roma_t": 0,
            "singerName": b64encode(candidate.artist_text.encode()).decode() if candidate.artist_text else b64encode(b"").decode(),
            "songID": int(candidate.source_id),
            "songName": b64encode(candidate.title.encode()).decode(),
            "trans": 0,
            "trans_t": 0,
            "type": 0,
        }
        data = self._rpc(
            "GetPlayLyricInfo",
            "music.musichallSong.PlayLyricInfo",
            param,
            kind="lyric",
        )
        if not isinstance(data, dict):
            return FetchResult(note="无可用歌词")
        encrypted = data.get("lyric") or ""
        if not encrypted:
            return FetchResult(note="无可用歌词")
        try:
            decrypted = qrc_decrypt(encrypted)
        except ValueError as exc:
            return FetchResult(note=f"QRC 解密失败：{exc}")
        qrc_text = qrc_lyric_text(decrypted)
        if not qrc_text.strip():
            return FetchResult(instrumental=False, note="无可用歌词")

        # Keep the native QRC and say honestly what it is: QQ sometimes ships a
        # single ``[0,0]`` block with the whole lyric and no line timing, which
        # used to be flattened into one ``[00:00.00]`` line and then judged a
        # "suspicious timeline" instead of a plain-text lyric.
        native = sanitize_lyrics(qrc_text)
        parsed = parse_qrc(native)
        if has_timing(parsed):
            native_kind = "qrc"
        elif TIMESTAMP_RE.search(native):
            native_kind = "lrc"
        else:
            native_kind = "plain"
            native = sanitize_lyrics(lines_to_plain(parsed))
        if not native:
            return FetchResult(instrumental=False, note="无可用歌词")
        if is_placeholder(native):
            return FetchResult(instrumental=False, note="无可用歌词")
        lyrics, note = render_native(native, native_kind, self.target_format)
        lyrics = sanitize_lyrics(lyrics)
        if not lyrics:
            return FetchResult(instrumental=False, note=note or "无可用歌词")
        return FetchResult(
            lyrics=lyrics,
            note=note,
            native_lyrics=native,
            native_kind=native_kind,
        )

    def fetch_plain(self, candidate: Candidate) -> FetchResult:
        """Fallback: prefer the plain LRC when the verbatim track is absent."""
        return self.fetch(candidate)


def build_sources(
    names: list[str], client: JsonClient, target_format: str | None = None
) -> list[Any]:
    registry = {"netease": NeteaseSource, "lrclib": LrclibSource, "qq": QqSource}
    return [
        registry[name](client, target_format)
        if name in {"netease", "qq"}
        else registry[name](client)
        for name in names
    ]
