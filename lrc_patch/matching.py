"""Candidate scoring, LRC sanity checks and the per-track plan."""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import DEFAULT_DURATION_TOLERANCE, SOURCE_NAMES
from .errors import ToolError
from .models import Candidate, FetchResult, PlanResult, Track
from .paths import canonical_path
from .text import (
    album_points,
    artist_matches,
    artist_parts,
    is_placeholder,
    lrc_timestamp_issue,
    lyric_kind,
    match_note,
    norm_loose,
    sanitize_lyrics,
    title_points,
)


def parse_pick(value: str) -> tuple[str, str]:
    """Parse SOURCE:ID, accepting a bare numeric ID as netease."""
    raw = (value or "").strip()
    if not raw:
        raise ToolError("--pick 不能为空，格式为 SOURCE:ID，例如 netease:2003496467")
    if ":" in raw:
        source, source_id = raw.split(":", 1)
        source = source.strip().lower()
    else:
        source, source_id = "netease", raw
    source_id = source_id.strip()
    if source not in SOURCE_NAMES:
        raise ToolError(f"未知歌词源：{source}（可用：netease,qq,lrclib）")
    if not source_id.isdigit():
        raise ToolError(f"候选 ID 应为数字：{value}")
    return source, source_id

def read_pick_file(path: Path, library: Path, tracks: list[Track]) -> dict[str, str]:
    """Read TAB-separated 'track path <TAB> SOURCE:ID' lines."""
    picks: dict[str, str] = {}
    by_name: dict[str, list[Track]] = {}
    for track in tracks:
        by_name.setdefault(track.path.name, []).append(track)
    try:
        lines = path.expanduser().read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ToolError(f"无法读取 --pick-file：{path}：{exc}") from exc

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" not in line:
            raise ToolError(
                f"{path}:{number} 缺少制表符；格式为：曲目路径<TAB>SOURCE:ID"
            )
        raw_path, raw_pick = line.split("\t", 1)
        raw_path = raw_path.strip()
        try:
            parse_pick(raw_pick)
        except ToolError as exc:
            raise ToolError(f"{path}:{number} {exc}") from exc
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = library / candidate
        candidate = canonical_path(candidate.resolve())
        if not candidate.is_file():
            matches = by_name.get(Path(raw_path).name, [])
            if len(matches) == 1:
                candidate = matches[0].path
            else:
                raise ToolError(
                    f"{path}:{number} 找不到曲目：{raw_path}"
                    + ("（同名文件不止一个，请写完整路径）" if matches else "")
                )
        picks[str(candidate)] = raw_pick.strip()
    if not picks:
        raise ToolError(f"--pick-file 没有有效条目：{path}")
    return picks

def score_candidate(
    track: Track, candidate: Candidate, tolerance: float = DEFAULT_DURATION_TOLERANCE
) -> tuple[int, float]:
    if not candidate.title or not candidate.duration:
        return 0, 999.0
    diff = abs(track.duration - candidate.duration)

    file_artists = artist_parts(track.artist)
    cand_artists = [p for a in candidate.artists for p in artist_parts(a)]
    # A feat./with name only counts as "already known" when the *candidate*
    # lists that collaborator; otherwise it is a different recording.
    title_score, title_note = title_points(track.title, candidate.title, set(cand_artists))
    if not title_score and title_note:
        # A rejection, not a partial match: an instrumental or language variant
        # is a different recording and must never accumulate points.
        return 0, diff
    artist_score, _ = artist_matches(file_artists, cand_artists)
    album_score, _ = album_points(track.album, candidate.album)
    # Every field contributes, so a near-miss prints 55 or 30 instead of a
    # naked 0.  The sum cannot reach the 85 auto-write line while the title
    # or the artist is missing (55 + 0 + 15 = 70 at most), so this is display
    # honesty, not a looser match.
    total = title_score + artist_score + album_score
    if diff > tolerance:
        # Duration is a gate, not a score: report the metadata total but keep
        # the candidate out of the auto-write path.
        return 0, diff
    return total, diff

def choose_candidate(
    track: Track,
    candidates: list[Candidate],
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> tuple[Candidate | None, int, float, str]:
    scored: list[tuple[int, float, Candidate]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (
            norm_loose(candidate.title),
            norm_loose(candidate.artist_text),
            round(candidate.duration, 1),
        )
        if key in seen:
            continue
        seen.add(key)
        score, diff = score_candidate(track, candidate, tolerance)
        if score:
            scored.append((score, diff, candidate))
    if not scored:
        return None, 0, 999.0, "无标题/艺术家/时长同时匹配的候选"
    scored.sort(key=lambda x: (-x[0], x[1], -x[2].synced))
    score, diff, candidate = scored[0]
    if score < 85:
        return None, score, diff, f"最佳候选仅 {score} 分，未达自动写入阈值"
    if diff > tolerance:
        return None, score, diff, f"时长差 {diff:.1f}s，超过容差 {tolerance:.1f}s"
    return candidate, score, diff, ""

def best_partial_candidate(
    track: Track, candidates: list[Candidate], tolerance: float
) -> tuple[tuple[int, float], Candidate, int, float, str] | None:
    """Pick the most plausible rejected candidate, with its real duration gap."""
    best: tuple[int, float, Candidate] | None = None
    for candidate in candidates:
        if not candidate.duration:
            continue
        metadata_score, diff = score_candidate(track, candidate, 999.0)
        key = (metadata_score, -diff)
        if best is None or key > (best[0], -best[1]):
            best = (metadata_score, diff, candidate)
    if best is None:
        return None
    metadata_score, diff, candidate = best
    score, _ = score_candidate(track, candidate, tolerance)
    if metadata_score < 85:
        reason = f"候选元数据仅 {metadata_score} 分（标题或艺术家不一致）"
    elif diff > tolerance:
        reason = f"时长差 {diff:.1f}s，超过容差 {tolerance:.1f}s"
    else:
        reason = "未达到自动写入条件"
    return (metadata_score, -diff), candidate, score, diff, reason

def local_sidecar(track: Track) -> FetchResult | None:
    sidecar = track.path.with_suffix(".lrc")
    if not sidecar.is_file():
        return None
    try:
        text = sanitize_lyrics(sidecar.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        text = sanitize_lyrics(sidecar.read_text(encoding="gb18030", errors="replace"))
    if not text or is_placeholder(text):
        return FetchResult(note="同名 LRC 为空")
    return FetchResult(lyrics=text)

def plan_from_lyrics(
    track: Track,
    candidate: Candidate,
    lyrics: str,
    score: int,
    diff: float | None,
    reason: str,
    check_timestamps: bool,
) -> PlanResult:
    """Common tail: timestamp sanity check, then ready or suspect."""
    if check_timestamps:
        issue = lrc_timestamp_issue(lyrics, track.duration)
        if issue:
            return PlanResult(
                track, "suspect", f"{reason}；{issue}", candidate, lyrics, score, diff
            )
    return PlanResult(track, "ready", reason, candidate, lyrics, score, diff)

# A picked ID only needs to be *identified*, not trusted: the score floor is
# how sure we must be that this ID belongs to that file.  A title that matches
# exactly (55) is enough; a random unrelated song (0..45) is not.
PICK_ASSIGN_FLOOR = 50
# Titles one character apart (印地安 / 印第安) score 0 in the auto-match rules,
# but they are obviously the same song when a human picked the ID.  Pairing
# uses this similarity bonus; the auto-write score is untouched.
_TITLE_ALIKE_BONUS = 25
_TITLE_ALIKE_RATIO = 0.8


def _titles_alike(left: str, right: str) -> bool:
    a, b = norm_loose(left), norm_loose(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _TITLE_ALIKE_RATIO


def assign_picks(
    tracks: list[Track],
    values: list[str],
    sources: list[Any],
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> tuple[dict[str, str], list[str], list[tuple[Track, Candidate, int, float]]]:
    """Pair a flat list of ``SOURCE:ID`` with the tracks they belong to.

    ``--pick qq:11 qq:22 netease:33 专辑`` states *which candidates* to use but
    not which file each belongs to.  Every ID is resolved to its metadata once
    (netease batch, QQ one call per ID) and then scored against each track that
    still needs lyrics; a greedy best-score assignment pairs them without
    depending on the order on the command line.
    """
    by_source: dict[str, list[str]] = {}
    for value in values:
        name, source_id = parse_pick(value)
        by_source.setdefault(name, []).append(source_id)

    candidates: list[Candidate] = []
    for name, ids in by_source.items():
        source = next((item for item in sources if item.name == name), None)
        if source is None:
            raise ToolError(
                f"--pick 指定的源未启用：{name}"
                f"（当前 --src {','.join(item.name for item in sources)}）"
            )
        lookup = getattr(source, "lookup", None)
        if lookup is None:
            raise ToolError(f"{name} 不支持按 ID 查详情，请改用单曲 --pick 或 --pick-file")
        candidates.extend(lookup(ids))

    eligible = [track for track in tracks if not track.has_lyrics]
    pairs: list[tuple[int, float, Track, Candidate]] = []
    for track in eligible:
        for candidate in candidates:
            score, diff = score_candidate(track, candidate, 999.0)
            identity = score + (
                _TITLE_ALIKE_BONUS if _titles_alike(track.title, candidate.title) else 0
            )
            if identity:
                pairs.append((identity, diff, track, candidate))
    pairs.sort(key=lambda item: (-item[0], item[1]))

    assignments: dict[str, str] = {}
    used: set[str] = set()
    preview: list[tuple[Track, Candidate, int, float]] = []
    for score, diff, track, candidate in pairs:
        ident = f"{candidate.source}:{candidate.source_id}"
        if str(track.path) in assignments or ident in used or score < PICK_ASSIGN_FLOOR:
            continue
        assignments[str(track.path)] = ident
        used.add(ident)
        preview.append((track, candidate, score, diff))

    unresolved = [value for value in dict.fromkeys(values) if value not in used]
    # Greedy order is by score, but the preview should read like the album does.
    order = {str(track.path): index for index, track in enumerate(tracks)}
    preview.sort(key=lambda item: order.get(str(item[0].path), len(order)))
    return assignments, unresolved, preview


def plan_with_pick(
    track: Track,
    sources: list[Any],
    pick: str,
    allow_plain: bool,
    check_timestamps: bool,
) -> PlanResult:
    """Force a user-chosen candidate; metadata is verified when search finds it."""
    try:
        source_name, source_id = parse_pick(pick)
    except ToolError as exc:
        return PlanResult(track, "error", str(exc))
    source = next((item for item in sources if item.name == source_name), None)
    if source is None:
        return PlanResult(track, "error", f"--pick 指定的源未启用：{source_name}")

    candidate: Candidate | None = None
    search_note = ""
    try:
        # The picked ID may live in the second (title-only) result page, so ask
        # for the exhaustive search when the user has named an exact candidate.
        results = source.search(track, exhaustive=True)
    except Exception as exc:
        results = []
        search_note = f"搜索失败 {type(exc).__name__}: {exc}"
    for item in results:
        if str(item.source_id) == source_id:
            candidate = item
            break

    verified = candidate is not None
    if candidate is None:
        candidate = Candidate(
            source=source_name,
            source_id=source_id,
            title=track.title,
            artists=[track.artist],
            album=track.album,
            duration=track.duration,
        )
    diff = abs(track.duration - candidate.duration) if verified else None
    label = f"人工指定 {source_name}:{source_id}"
    if verified:
        label += f"（时长已核验 Δ{diff:.1f}s）"
    else:
        label += "（未出现在搜索结果中，标题/时长未核验）"
        if search_note:
            label += f"；{search_note}"

    try:
        fetched = source.fetch(candidate)
    except Exception as exc:
        return PlanResult(
            track, "error", f"{label}；取词失败 {type(exc).__name__}: {exc}", candidate
        )
    if fetched.instrumental:
        return PlanResult(track, "instrumental", f"{label}；标记为纯音乐", candidate)
    if not fetched.lyrics:
        return PlanResult(
            track, "error", f"{label}；{fetched.note or '没有可用歌词'}", candidate
        )
    if lyric_kind(fetched.lyrics) == "plain" and not allow_plain:
        return PlanResult(track, "error", f"{label}；只有纯文本，已按 --no-plain 拒绝", candidate)
    return plan_from_lyrics(
        track, candidate, fetched.lyrics, 100, diff, label, check_timestamps
    )

def keep_previous_plan(
    previous: Any | None, fingerprint: str, result: PlanResult
) -> bool:
    """Never let a provider outage erase an already-good plan."""
    return bool(
        previous
        and previous["status"] in {"ready", "applied"}
        and previous["fingerprint"] == fingerprint
        and result.status in {"error", "no_match"}
    )


def _source_note(name: str, exc: Exception, prefix: str = "") -> str:
    """One short line per provider failure, without repeating the source name."""
    if isinstance(exc, ToolError):
        text = str(exc).strip()
    else:
        text = f"{type(exc).__name__}: {exc}"
    if not text.startswith(name):
        text = f"{name}: {text}"
    return f"{prefix} {text}".strip() if prefix else text


def plan_one(
    track: Track,
    sources: list[Any],
    allow_plain: bool,
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
    check_timestamps: bool = True,
    pick: str | None = None,
) -> PlanResult:
    if track.has_lyrics:
        return PlanResult(track, "skip_existing", "已有歌词，默认不覆盖")
    if pick:
        return plan_with_pick(track, sources, pick, allow_plain, check_timestamps)

    sidecar = local_sidecar(track)
    if sidecar and sidecar.lyrics:
        candidate = Candidate(
            source="local",
            source_id=str(track.path.with_suffix(".lrc")),
            title=track.title,
            artists=[track.artist],
            album=track.album,
            duration=track.duration,
            lyrics=sidecar.lyrics,
            synced=lyric_kind(sidecar.lyrics) == "lrc",
        )
        return plan_from_lyrics(
            track, candidate, sidecar.lyrics, 100, 0.0, "同名 LRC", check_timestamps
        )

    notes: list[str] = []
    failed: list[str] = []
    # What each provider actually offered, so a failed track names its source
    # instead of leaving the user to guess whether QQ was even tried.
    origin: list[str] = []
    best_partial: tuple[Candidate, int, float, str] | None = None
    best_partial_key: tuple[int, float] | None = None
    for source in sources:
        try:
            candidates = source.search(track)
        except Exception as exc:
            failed.append(source.name)
            origin.append(f"{source.name} 失败")
            notes.append(_source_note(source.name, exc))
            continue
        if not candidates:
            origin.append(f"{source.name} 无结果")
            notes.append(f"{source.name}: 无搜索结果")
            continue
        candidate, score, diff, reason = choose_candidate(track, candidates, tolerance)
        partial = best_partial_candidate(track, candidates, tolerance)
        origin.append(f"{source.name} {partial[0][0] if partial else 0}")
        if not candidate:
            if partial and (
                best_partial_key is None or partial[0] > best_partial_key
            ):
                best_partial_key = partial[0]
                best_partial = partial[1:]
            notes.append(f"{source.name}: {reason}")
            continue
        try:
            fetched = source.fetch(candidate)
        except Exception as exc:
            failed.append(source.name)
            notes.append(_source_note(source.name, exc, "取词失败"))
            continue
        if fetched.instrumental:
            return PlanResult(track, "instrumental", "匹配为纯音乐")
        if not fetched.lyrics:
            notes.append(f"{source.name}: {fetched.note or '无可用歌词'}")
            continue
        kind = lyric_kind(fetched.lyrics)
        if kind == "plain" and not allow_plain:
            notes.append(f"{source.name}: 仅纯文本，已按 --no-plain 跳过")
            continue
        note = match_note(track, candidate, tolerance)
        reason = candidate.artist_text
        if note:
            reason = f"{reason}；{note}" if reason else note
        return plan_from_lyrics(
            track,
            candidate,
            fetched.lyrics,
            score,
            diff,
            reason,
            check_timestamps,
        )

    if best_partial:
        candidate, score, diff, _reason = best_partial
        return PlanResult(
            track,
            "ambiguous",
            f"{' · '.join(origin)}；最佳 {candidate.source}:{candidate.source_id} "
            f"{candidate.title} / {candidate.artist_text} / "
            f"{candidate.duration:.0f}s",
            candidate,
            "",
            score,
            diff,
        )
    if failed:
        # A provider that never answered is not evidence that the lyrics do not
        # exist. Store it as error so the next plan retries instead of reusing
        # a stale "no match" forever.
        return PlanResult(track, "error", "请求失败：" + "; ".join(notes))
    return PlanResult(track, "no_match", "; ".join(notes) or "无结果")
