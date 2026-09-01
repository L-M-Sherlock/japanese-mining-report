#!/usr/bin/env python3
"""Measure subtitle cues that produced at least one anime mining note."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.generate_anniversary_diary import (
    DEFAULT_ANKI_DB,
    DEFAULT_END,
    DEFAULT_MINING_HISTORY_DIR,
    DEFAULT_MOVIES_DIR,
    DEFAULT_START,
    MOVIE_MARKER,
    YURU_YURI_REWATCH_MARKER,
    AnimeEvent,
    MiningRecord,
    anime_edition_label,
    canonical_anime_key,
    choose_canonical_label,
    episode_marker,
    import_mining_history_modules,
    load_mining_records,
    make_db_snapshot,
    register_unicase,
    short_anime_source,
)


DEFAULT_EPISODE_OUTPUT = Path("output/anime_vocabulary_ratio_by_episode.csv")
DEFAULT_WORK_OUTPUT = Path("output/anime_vocabulary_ratio_by_work.csv")
DEFAULT_JSON_OUTPUT = Path("output/anime_vocabulary_ratio_summary.json")
SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt")
SRT_TIMING_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{2,3})\s+-->\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{2,3})"
)
VTT_TIMING_RE = re.compile(
    r"^\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.](\d{3})\s+-->\s+"
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.](\d{3})"
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


@dataclass(frozen=True)
class SubtitleCue:
    start_seconds: float
    end_seconds: float
    text: str
    identity: tuple[Any, ...]


@dataclass(frozen=True)
class CueMatch:
    cue_index: int | None
    method: str


@dataclass(frozen=True)
class EpisodeResult:
    edition: str
    viewing_index: int
    mode: str
    episode: str
    first_date: date
    last_date: date
    source_record: str
    subtitle_path: Path | None
    pair_method: str | None
    cards: int
    matched_cards: int
    mined_cues: frozenset[int]
    total_cues: int | None
    match_methods: Counter[str]

    @property
    def vocabulary_ratio(self) -> float | None:
        if not self.total_cues:
            return None
        return len(self.mined_cues) / self.total_cues


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_ANKI_DB)
    parser.add_argument("--movies-dir", type=Path, default=DEFAULT_MOVIES_DIR)
    parser.add_argument(
        "--mining-history-dir", type=Path, default=DEFAULT_MINING_HISTORY_DIR
    )
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument(
        "--episode-output", type=Path, default=DEFAULT_EPISODE_OUTPUT
    )
    parser.add_argument("--work-output", type=Path, default=DEFAULT_WORK_OUTPUT)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    return parser.parse_args()


def normalize_subtitle_text(value: str) -> str:
    text = html.unescape(value or "")
    text = HTML_TAG_RE.sub("", text)
    text = ASS_OVERRIDE_RE.sub("", text)
    text = text.replace(r"\N", " ").replace(r"\n", " ")
    text = text.replace("\u3000", " ").replace("/", " ")
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", text).strip()


def parse_clock(
    hour: str | None, minute: str, second: str, fraction: str
) -> float:
    milliseconds = int(fraction.ljust(3, "0")[:3])
    return (
        int(hour or 0) * 3600
        + int(minute) * 60
        + int(second)
        + milliseconds / 1000
    )


def parse_timing_line(line: str) -> tuple[float, float] | None:
    match = SRT_TIMING_RE.match(line)
    if match:
        groups = match.groups()
        return parse_clock(*groups[:4]), parse_clock(*groups[4:])
    match = VTT_TIMING_RE.match(line)
    if match:
        groups = match.groups()
        return parse_clock(*groups[:4]), parse_clock(*groups[4:])
    return None


def parse_ass_clock(value: str) -> float:
    hour, minute, second = value.split(":")
    return int(hour) * 3600 + int(minute) * 60 + float(second)


def parse_ass_cues(lines: Iterable[str]) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw_line in lines:
        if not raw_line.startswith("Dialogue:"):
            continue
        fields = raw_line.rstrip("\r\n").split(",", maxsplit=9)
        if len(fields) != 10:
            continue
        start, end, style, text = fields[1], fields[2], fields[3], fields[9]
        identity = (start, end, style, text)
        visible = ASS_OVERRIDE_RE.sub("", text)
        visible = visible.replace(r"\N", "").replace(r"\n", "").strip()
        if not visible or identity in seen:
            continue
        seen.add(identity)
        cues.append(
            SubtitleCue(
                start_seconds=parse_ass_clock(start),
                end_seconds=parse_ass_clock(end),
                text=text,
                identity=identity,
            )
        )
    return sorted(cues, key=lambda cue: cue.start_seconds)


def parse_srt_or_vtt_cues(lines: list[str]) -> list[SubtitleCue]:
    timing_rows = [
        (index, timing)
        for index, line in enumerate(lines)
        if (timing := parse_timing_line(line)) is not None
    ]
    cues: list[SubtitleCue] = []
    for offset, (line_index, (start, end)) in enumerate(timing_rows):
        stop = timing_rows[offset + 1][0] if offset + 1 < len(timing_rows) else len(lines)
        text_lines = [
            line
            for line in lines[line_index + 1 : stop]
            if line.strip() and not re.fullmatch(r"\d+", line.strip())
        ]
        text = " ".join(text_lines)
        cues.append(
            SubtitleCue(
                start_seconds=start,
                end_seconds=end,
                text=text,
                identity=(start, end, text),
            )
        )
    return sorted(cues, key=lambda cue: cue.start_seconds)


def parse_subtitle_cues(path: Path) -> list[SubtitleCue]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    if path.suffix.casefold() in {".ass", ".ssa"}:
        return parse_ass_cues(lines)
    return parse_srt_or_vtt_cues(lines)


class CueMatcher:
    def __init__(self, cues: list[SubtitleCue]) -> None:
        self.cues = cues
        self.starts = [cue.start_seconds for cue in cues]
        self.normalized = [normalize_subtitle_text(cue.text) for cue in cues]
        self.text_index: dict[str, list[int]] = defaultdict(list)
        for index, text in enumerate(self.normalized):
            if text:
                self.text_index[text].append(index)

    def _closest(self, indices: Iterable[int], position: int) -> int:
        return min(
            indices,
            key=lambda index: abs(self.cues[index].start_seconds - position),
        )

    def match(
        self, record: MiningRecord, *, global_text_fallback: bool = False
    ) -> CueMatch:
        sentence = normalize_subtitle_text(record.sentence)
        position = record.position_seconds
        exact = self.text_index.get(sentence, []) if sentence else []
        if exact:
            if position is not None:
                return CueMatch(self._closest(exact, position), "exact_text")
            if len(exact) == 1:
                return CueMatch(exact[0], "exact_text_without_position")
            return CueMatch(None, "ambiguous_text_without_position")
        if position is None:
            return CueMatch(None, "missing_position")

        insertion = bisect_left(self.starts, position)
        nearby = range(max(0, insertion - 10), min(len(self.cues), insertion + 7))
        candidates = list(range(len(self.cues))) if global_text_fallback else list(nearby)

        contained = [
            index
            for index in candidates
            if sentence
            and self.normalized[index]
            and (
                sentence in self.normalized[index]
                or self.normalized[index] in sentence
            )
            and min(len(sentence), len(self.normalized[index])) >= 4
        ]
        if contained:
            return CueMatch(self._closest(contained, position), "contained_text")

        fuzzy: list[tuple[float, float, int]] = []
        for index in candidates:
            cue_text = self.normalized[index]
            if not sentence or not cue_text:
                continue
            score = SequenceMatcher(
                None, sentence, cue_text, autojunk=False
            ).ratio()
            if score >= 0.72:
                fuzzy.append(
                    (score, -abs(self.cues[index].start_seconds - position), index)
                )
        if fuzzy:
            return CueMatch(max(fuzzy)[2], "fuzzy_text")

        if not global_text_fallback:
            active = [
                index
                for index in nearby
                if self.cues[index].start_seconds - 1
                <= position
                <= self.cues[index].end_seconds + 1
            ]
            if active:
                return CueMatch(self._closest(active, position), "timestamp")
        return CueMatch(None, "unmatched")


def normalized_edition_key(filename: str) -> str:
    label = anime_edition_label(short_anime_source(filename))
    normalized = unicodedata.normalize("NFKC", label).casefold()
    return "".join(character for character in normalized if character.isalnum())


def episode_identity(filename: str) -> tuple[str, int | None, int] | None:
    marker = episode_marker(filename)
    if marker is None:
        return None
    season, episode = marker
    return normalized_edition_key(filename), season, episode


class SubtitleLocator:
    def __init__(self, subtitle_index: dict[str, list[Path]], ratios: Any) -> None:
        self.subtitle_index = subtitle_index
        self.ratios = ratios
        self.by_episode: dict[tuple[str, int | None, int], list[Path]] = defaultdict(list)
        self.by_title_episode: dict[tuple[str, int], list[Path]] = defaultdict(list)
        unique_paths = {path for paths in subtitle_index.values() for path in paths}
        for path in unique_paths:
            identity = episode_identity(path.name)
            if identity is None:
                continue
            title, season, episode = identity
            self.by_episode[identity].append(path)
            self.by_title_episode[(title, episode)].append(path)
        for paths in [*self.by_episode.values(), *self.by_title_episode.values()]:
            paths.sort(key=ratios.source_rank)

    def locate(self, records: list[MiningRecord]) -> tuple[Path | None, str | None]:
        exact: list[Path] = []
        for record in records:
            exact.extend(
                self.subtitle_index.get(
                    self.ratios.normalized_filename(record.source), []
                )
            )
        if exact:
            return min(set(exact), key=self.ratios.source_rank), "exact_filename"

        appended: list[Path] = []
        for record in records:
            if Path(record.source).suffix.casefold() in SUBTITLE_SUFFIXES:
                continue
            for suffix in SUBTITLE_SUFFIXES:
                appended.extend(
                    self.subtitle_index.get(
                        self.ratios.normalized_filename(record.source + suffix), []
                    )
                )
        if appended:
            return min(set(appended), key=self.ratios.source_rank), "appended_extension"

        episode_candidates: list[Path] = []
        for record in records:
            identity = episode_identity(record.source)
            if identity is None:
                continue
            episode_candidates.extend(self.by_episode.get(identity, []))
            episode_candidates.extend(
                self.by_title_episode.get((identity[0], identity[2]), [])
            )
        if episode_candidates:
            return (
                min(set(episode_candidates), key=self.ratios.source_rank),
                "episode_identity",
            )
        return None, None


def split_viewings(
    grouped: dict[str, list[MiningRecord]], subtitle_cutoff: Any
) -> list[tuple[str, int, list[MiningRecord]]]:
    viewings: list[tuple[str, int, list[MiningRecord]]] = []
    for key, records in grouped.items():
        ordered = sorted(records, key=lambda record: record.mined_at)
        is_yuru_yuri_first_season = any(
            YURU_YURI_REWATCH_MARKER in record.source.casefold()
            for record in ordered
        )
        if is_yuru_yuri_first_season:
            before = [record for record in ordered if record.mined_at < subtitle_cutoff]
            after = [record for record in ordered if record.mined_at >= subtitle_cutoff]
            if before and after:
                viewings.extend([(key, 1, before), (key, 2, after)])
                continue
        viewings.append((key, 1, ordered))
    return viewings


def episode_label(records: list[MiningRecord]) -> str:
    markers = [episode_marker(record.source) for record in records]
    marker = next((item for item in markers if item is not None), None)
    if marker is None:
        return "电影"
    season, episode = marker
    return f"S{season:02d}E{episode:02d}" if season is not None else f"E{episode:02d}"


def analyze_records(
    records: Iterable[MiningRecord], subtitle_index: dict[str, list[Path]], ratios: Any
) -> list[EpisodeResult]:
    grouped: dict[str, list[MiningRecord]] = defaultdict(list)
    for record in records:
        if record.category == "anime":
            grouped[canonical_anime_key(record.source)].append(record)
    movie_times = [
        record.mined_at
        for group in grouped.values()
        for record in group
        if MOVIE_MARKER in record.source.casefold()
    ]
    if not movie_times:
        raise SystemExit("Could not locate the subtitle-mode cutoff movie")
    subtitle_cutoff = min(movie_times)
    locator = SubtitleLocator(subtitle_index, ratios)
    cue_cache: dict[Path, list[SubtitleCue]] = {}
    results: list[EpisodeResult] = []

    for _key, viewing_index, viewing_records in split_viewings(
        grouped, subtitle_cutoff
    ):
        label = choose_canonical_label(
            [
                AnimeEvent(
                    record.mined_at, record.source, record.position_seconds
                )
                for record in viewing_records
            ]
        )
        edition = anime_edition_label(label)
        mode = "无字幕" if viewing_records[0].mined_at >= subtitle_cutoff else "日语字幕"
        subtitle_path, pair_method = locator.locate(viewing_records)
        methods: Counter[str] = Counter()
        mined_cues: set[int] = set()
        matched_cards = 0
        total_cues: int | None = None
        if subtitle_path is not None:
            cues = cue_cache.setdefault(subtitle_path, parse_subtitle_cues(subtitle_path))
            matcher = CueMatcher(cues)
            total_cues = len(cues)
            for record in viewing_records:
                match = matcher.match(
                    record,
                    global_text_fallback=pair_method == "episode_identity",
                )
                methods[match.method] += 1
                if match.cue_index is not None:
                    matched_cards += 1
                    mined_cues.add(match.cue_index)
        results.append(
            EpisodeResult(
                edition=edition,
                viewing_index=viewing_index,
                mode=mode,
                episode=episode_label(viewing_records),
                first_date=min(record.mined_at.date() for record in viewing_records),
                last_date=max(record.mined_at.date() for record in viewing_records),
                source_record=viewing_records[0].source,
                subtitle_path=subtitle_path,
                pair_method=pair_method,
                cards=len(viewing_records),
                matched_cards=matched_cards,
                mined_cues=frozenset(mined_cues),
                total_cues=total_cues,
                match_methods=methods,
            )
        )
    return sorted(
        results,
        key=lambda row: (
            row.last_date,
            row.first_date,
            row.edition.casefold(),
            row.viewing_index,
            row.episode,
        ),
    )


def aggregate_works(results: list[EpisodeResult]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str], list[EpisodeResult]] = defaultdict(list)
    for result in results:
        grouped[(result.edition, result.viewing_index, result.mode)].append(result)
    rows: list[dict[str, Any]] = []
    for (edition, viewing_index, mode), episodes in grouped.items():
        paired = [episode for episode in episodes if episode.total_cues is not None]
        total_cues = sum(episode.total_cues or 0 for episode in paired)
        mined_cues = sum(len(episode.mined_cues) for episode in paired)
        rows.append(
            {
                "edition": edition,
                "viewing_index": viewing_index,
                "mode": mode,
                "first_date": min(episode.first_date for episode in episodes),
                "last_date": max(episode.last_date for episode in episodes),
                "viewings": len(episodes),
                "paired_viewings": len(paired),
                "cards": sum(episode.cards for episode in episodes),
                "matched_cards": sum(episode.matched_cards for episode in episodes),
                "mined_cues": mined_cues,
                "total_cues": total_cues,
                "vocabulary_ratio": mined_cues / total_cues if total_cues else None,
            }
        )
    return sorted(rows, key=lambda row: (row["last_date"], row["first_date"]))


def summarize(results: list[EpisodeResult]) -> dict[str, Any]:
    paired = [result for result in results if result.total_cues is not None]
    methods: Counter[str] = Counter()
    for result in paired:
        methods.update(result.match_methods)
    total_cues = sum(result.total_cues or 0 for result in paired)
    mined_cues = sum(len(result.mined_cues) for result in paired)
    mode_summary: dict[str, dict[str, Any]] = {}
    for mode in ("日语字幕", "无字幕"):
        mode_rows = [result for result in results if result.mode == mode]
        mode_paired = [result for result in mode_rows if result.total_cues is not None]
        mode_total = sum(result.total_cues or 0 for result in mode_paired)
        mode_mined = sum(len(result.mined_cues) for result in mode_paired)
        mode_summary[mode] = {
            "viewings": len(mode_rows),
            "pairedViewings": len(mode_paired),
            "minedCues": mode_mined,
            "totalCues": mode_total,
            "ratio": mode_mined / mode_total if mode_total else None,
        }
    return {
        "viewings": len(results),
        "pairedViewings": len(paired),
        "coverage": len(paired) / len(results) if results else None,
        "editions": len({result.edition for result in results}),
        "pairedEditions": len({result.edition for result in paired}),
        "cards": sum(result.cards for result in results),
        "pairedCards": sum(result.cards for result in paired),
        "matchedCards": sum(result.matched_cards for result in paired),
        "minedCues": mined_cues,
        "totalCues": total_cues,
        "ratio": mined_cues / total_cues if total_cues else None,
        "matchMethods": dict(sorted(methods.items())),
        "modeSummary": mode_summary,
        "missingSources": [
            {
                "edition": result.edition,
                "episode": result.episode,
                "viewingIndex": result.viewing_index,
                "source": result.source_record,
                "cards": result.cards,
            }
            for result in results
            if result.total_cues is None
        ],
    }


def write_episode_csv(path: Path, results: list[EpisodeResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "edition",
                "viewing_index",
                "mode",
                "episode",
                "first_date",
                "last_date",
                "cards",
                "matched_cards",
                "mined_cues",
                "total_source_cues",
                "vocabulary_ratio",
                "vocabulary_percent",
                "pair_method",
                "source_record",
                "source_subtitle_file",
            ]
        )
        for result in results:
            ratio = result.vocabulary_ratio
            writer.writerow(
                [
                    result.edition,
                    result.viewing_index,
                    result.mode,
                    result.episode,
                    result.first_date.isoformat(),
                    result.last_date.isoformat(),
                    result.cards,
                    result.matched_cards,
                    len(result.mined_cues),
                    result.total_cues if result.total_cues is not None else "",
                    f"{ratio:.8f}" if ratio is not None else "",
                    f"{ratio * 100:.2f}%" if ratio is not None else "",
                    result.pair_method or "",
                    result.source_record,
                    str(result.subtitle_path) if result.subtitle_path else "",
                ]
            )


def write_work_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "edition",
            "viewing_index",
            "mode",
            "first_date",
            "last_date",
            "viewings",
            "paired_viewings",
            "cards",
            "matched_cards",
            "mined_cues",
            "total_cues",
            "vocabulary_ratio",
            "vocabulary_percent",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            ratio = row["vocabulary_ratio"]
            writer.writerow(
                {
                    **row,
                    "first_date": row["first_date"].isoformat(),
                    "last_date": row["last_date"].isoformat(),
                    "vocabulary_ratio": f"{ratio:.8f}" if ratio is not None else "",
                    "vocabulary_percent": (
                        f"{ratio * 100:.2f}%" if ratio is not None else ""
                    ),
                }
            )


def main() -> None:
    args = parse_args()
    if args.end < args.start:
        raise SystemExit("--end must be on or after --start")
    db_path = args.db.expanduser().resolve()
    movies_dir = args.movies_dir.expanduser().resolve()
    history_dir = args.mining_history_dir.expanduser().resolve()
    temp_dir, snapshot = make_db_snapshot(db_path)
    try:
        conn = sqlite3.connect(snapshot)
        register_unicase(conn)
        try:
            _mining_days, mining_summary = load_mining_records(
                conn, args.start, args.end
            )
        finally:
            conn.close()
    finally:
        temp_dir.cleanup()

    _analysis, ratios = import_mining_history_modules(history_dir)
    subtitle_index = ratios.index_subtitles(movies_dir)
    results = analyze_records(mining_summary["records"], subtitle_index, ratios)
    work_rows = aggregate_works(results)
    summary = {
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        **summarize(results),
    }

    episode_output = args.episode_output.expanduser().resolve()
    work_output = args.work_output.expanduser().resolve()
    json_output = args.json_output.expanduser().resolve()
    write_episode_csv(episode_output, results)
    write_work_csv(work_output, work_rows)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**summary, "episodeOutput": str(episode_output), "workOutput": str(work_output), "jsonOutput": str(json_output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
