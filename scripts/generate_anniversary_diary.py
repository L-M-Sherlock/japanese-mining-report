#!/usr/bin/env python3
"""Generate a data-backed, day-by-day Japanese-learning anniversary diary."""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.visualize_lapis_sources import (
    classify_source_category,
    extract_source_label,
    find_field_ord,
    find_note_type_id,
    guess_work_label,
    make_db_snapshot,
    normalize_novel_work_label,
    register_unicase,
    strip_misc_html,
)


TIME_ZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_START = date(2025, 8, 29)
DEFAULT_END = date(2026, 8, 28)
DEFAULT_ANKI_DB = Path(
    "/Users/jarrettye/Library/Application Support/Anki2/JarrettYe/collection.anki2"
)
DEFAULT_BOOKS_DIR = Path("/Users/jarrettye/Library/Application Support/Books")
DEFAULT_READING_SCRIPT = Path(
    "/Users/jarrettye/Codes/japanese-reading-stats/scripts/visualize_books.py"
)
DEFAULT_MINING_HISTORY_DIR = Path("/Users/jarrettye/Documents/mining_history")
DEFAULT_MOVIES_DIR = Path("/Users/jarrettye/Movies")
DEFAULT_OUTPUT = Path("output/japanese_learning_anniversary_diary.md")
POSITION_RE = re.compile(r"\((?:(\d+)h)?(\d+)m(\d+)s\)\s*$", re.IGNORECASE)
JLPT_RE = re.compile(r"(?:^|\s)JLPT::(N[1-5])(?:\s|$)", re.IGNORECASE)
SUBTITLE_EXT_RE = re.compile(r"\.(?:srt|ass|ssa|vtt)$", re.IGNORECASE)
MOVIE_MARKER = "gekijouban karakai jouzu no takagi-san"
YURU_YURI_REWATCH_MARKER = "yuru yuri"
WEEKDAYS_ZH = "一二三四五六日"


@dataclass(frozen=True)
class MiningRecord:
    note_id: int
    mined_at: datetime
    expression: str
    sentence: str
    source: str
    source_display: str
    work: str
    category: str
    jlpt: str
    position_seconds: int | None


@dataclass(frozen=True)
class AnimeEvent:
    mined_at: datetime
    source: str
    position_seconds: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_ANKI_DB)
    parser.add_argument("--books-dir", type=Path, default=DEFAULT_BOOKS_DIR)
    parser.add_argument("--reading-script", type=Path, default=DEFAULT_READING_SCRIPT)
    parser.add_argument(
        "--mining-history-dir", type=Path, default=DEFAULT_MINING_HISTORY_DIR
    )
    parser.add_argument("--movies-dir", type=Path, default=DEFAULT_MOVIES_DIR)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise SystemExit("--end must be on or after --start")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def day_bounds_ms(start: date, end: date) -> tuple[int, int]:
    start_dt = datetime.combine(start, time.min, TIME_ZONE)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, TIME_ZONE)
    return int(start_dt.timestamp() * 1000), int(end_exclusive.timestamp() * 1000)


def clean_field(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return " / ".join(line for line in lines if line)


def md_escape(value: str) -> str:
    value = (value or "").replace("\\", "\\\\")
    for token in ("`", "*", "_", "[", "]", "|", "<", ">", "#"):
        value = value.replace(token, "\\" + token)
    return value.replace("\n", " ").strip()


def compact_text(value: str, limit: int = 150) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_duration(seconds: float, *, seconds_precision: bool = False) -> str:
    if seconds <= 0:
        return "0 分钟"
    rounded = int(round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        pieces = [f"{hours} 小时"]
        if minutes:
            pieces.append(f"{minutes} 分钟")
        if seconds_precision and secs:
            pieces.append(f"{secs} 秒")
        return " ".join(pieces)
    if minutes:
        if seconds_precision and secs:
            return f"{minutes} 分 {secs} 秒"
        return f"{minutes} 分钟"
    return f"{max(1, secs)} 秒"


def format_minutes(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 0.5:
        return "不足 1 分钟"
    if minutes < 10 and abs(minutes - round(minutes)) >= 0.05:
        return f"约 {minutes:.1f} 分钟"
    return f"约 {minutes:.0f} 分钟"


def deck_label(deck_name: str | None) -> str:
    if not deck_name:
        return "jlab's beginner course"
    leaf = deck_name.split("\x1f")[-1]
    level_match = re.search(r"N([1-5])", leaf, re.IGNORECASE)
    if "Blue Book" in deck_name and "文法カード" in deck_name:
        level = level_match.group(1) if level_match else "?"
        return f"蓝宝书 N{level}·文法"
    if "Blue Book" in deck_name and "例文カード" in deck_name:
        level = level_match.group(1) if level_match else "?"
        return f"蓝宝书 N{level}·例文"
    if "expression" in deck_name.casefold():
        return "词句挖掘卡"
    if "kaishi" in deck_name.casefold():
        return "Kaishi 1.5k"
    if "numbers and counting" in deck_name.casefold():
        return "数字与计数"
    if deck_name == "ALL":
        return "根牌组（其他日语卡）"
    return leaf.replace("::", "·")


def load_anki_days(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[dict[date, dict[str, Any]], dict[str, Any]]:
    start_ms, end_ms = day_bounds_ms(start, end)
    days: dict[date, dict[str, Any]] = defaultdict(
        lambda: {
            "seconds": 0.0,
            "review_count": 0,
            "cards": set(),
            "times": [],
            "decks": defaultdict(
                lambda: {"seconds": 0.0, "reviews": 0, "cards": set()}
            ),
            "first_cards": Counter(),
        }
    )

    rows = conn.execute(
        """
        select r.id, r.cid, r.time, d.name
        from revlog r
        left join cards c on c.id = r.cid
        left join decks d on d.id = c.did
        where r.id >= ? and r.id < ?
        order by r.id
        """,
        (start_ms, end_ms),
    )
    for revlog_id, card_id, answer_ms, raw_deck in rows:
        reviewed_at = datetime.fromtimestamp(int(revlog_id) / 1000, TIME_ZONE)
        day = reviewed_at.date()
        label = deck_label(str(raw_deck) if raw_deck is not None else None)
        answer_seconds = max(0, int(answer_ms)) / 1000
        bucket = days[day]
        bucket["seconds"] += answer_seconds
        bucket["review_count"] += 1
        bucket["cards"].add(int(card_id))
        bucket["times"].append(reviewed_at)
        deck = bucket["decks"][label]
        deck["seconds"] += answer_seconds
        deck["reviews"] += 1
        deck["cards"].add(int(card_id))

    first_rows = conn.execute(
        """
        with first_reviews as (
          select cid, min(id) as first_id
          from revlog
          group by cid
        )
        select f.cid, f.first_id, d.name
        from first_reviews f
        left join cards c on c.id = f.cid
        left join decks d on d.id = c.did
        """
    )
    first_review_total = 0
    for _card_id, first_id, raw_deck in first_rows:
        if not start_ms <= int(first_id) < end_ms:
            continue
        reviewed_at = datetime.fromtimestamp(int(first_id) / 1000, TIME_ZONE)
        days[reviewed_at.date()]["first_cards"][
            deck_label(str(raw_deck) if raw_deck is not None else None)
        ] += 1
        first_review_total += 1

    summary = {
        "seconds": sum(bucket["seconds"] for bucket in days.values()),
        "review_count": sum(bucket["review_count"] for bucket in days.values()),
        "unique_cards": len(
            set().union(*(bucket["cards"] for bucket in days.values()))
        )
        if days
        else 0,
        "first_review_total": first_review_total,
        "active_days": sum(bucket["review_count"] > 0 for bucket in days.values()),
        "first_time": min(
            reviewed_at
            for bucket in days.values()
            for reviewed_at in bucket["times"]
        )
        if days
        else None,
    }
    return days, summary


def load_kaishi_days(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[dict[date, list[dict[str, Any]]], dict[str, Any]]:
    note_types = conn.execute(
        """
        select distinct nt.id
        from cards c
        join decks d on d.id = c.did
        join notes n on n.id = c.nid
        join notetypes nt on nt.id = n.mid
        where lower(d.name) like '%kaishi%'
        """
    ).fetchall()
    if len(note_types) != 1:
        raise SystemExit(
            f"Expected one Kaishi note type, found {len(note_types)}"
        )
    note_type_id = int(note_types[0][0])
    word_ord = find_field_ord(conn, note_type_id, "Word")
    reading_ord = find_field_ord(conn, note_type_id, "Word Reading")
    start_ms, end_ms = day_bounds_ms(start, end)
    days: dict[date, list[dict[str, Any]]] = defaultdict(list)

    rows = conn.execute(
        """
        select c.id, n.flds, min(r.id) as first_review_id
        from cards c
        join decks d on d.id = c.did
        join notes n on n.id = c.nid
        join revlog r on r.cid = c.id
        where lower(d.name) like '%kaishi%'
        group by c.id, n.flds
        order by first_review_id, c.id
        """
    )
    for card_id, flds, first_review_id in rows:
        if not start_ms <= int(first_review_id) < end_ms:
            continue
        fields = str(flds).split("\x1f")
        word = clean_field(fields[word_ord] if len(fields) > word_ord else "")
        reading = clean_field(fields[reading_ord] if len(fields) > reading_ord else "")
        display = word
        if reading and reading != word:
            display = f"{word}（{reading}）"
        learned_at = datetime.fromtimestamp(int(first_review_id) / 1000, TIME_ZONE)
        days[learned_at.date()].append(
            {
                "card_id": int(card_id),
                "learned_at": learned_at,
                "word": word,
                "reading": reading,
                "display": display or "（表达字段为空）",
            }
        )

    records = [record for records in days.values() for record in records]
    summary = {
        "count": len(records),
        "active_days": len(days),
        "unique_expressions": len(
            {(record["word"], record["reading"]) for record in records}
        ),
        "unique_written_forms": len({record["word"] for record in records}),
        "first_date": min(days) if days else None,
        "last_date": max(days) if days else None,
    }
    return days, summary


def parse_position(raw_source: str) -> int | None:
    match = POSITION_RE.search((raw_source or "").replace("\xa0", " "))
    if not match:
        return None
    return (
        int(match.group(1) or 0) * 3600
        + int(match.group(2)) * 60
        + int(match.group(3))
    )


def episode_marker(source: str) -> tuple[int | None, int] | None:
    patterns = (
        r"(?i)S(\d{1,2})[ ._-]*E(\d{1,3})",
        r"第\s*(\d{1,3})\s*話",
        r"\[(\d{1,2})\](?![0-9A-Fa-f])",
        r"\s-\s0*(\d{1,3})(?=\s|\.|\[|\(|$)",
        r"\s0*(\d{1,2})(?=\s*(?:\(|\[))",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, source)
        if not match:
            continue
        if index == 0:
            return int(match.group(1)), int(match.group(2))
        return None, int(match.group(1))
    return None


def remove_release_group(source: str) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", source).strip()


def short_anime_source(source: str) -> str:
    original = source.strip()
    cleaned = remove_release_group(original)
    cleaned = SUBTITLE_EXT_RE.sub("", cleaned)

    season_episode = re.search(r"(?i)S(\d{1,2})[ ._-]*E(\d{1,3})", cleaned)
    if season_episode:
        prefix = cleaned[: season_episode.start()].rstrip(" ._-")
        return (
            f"{prefix} S{int(season_episode.group(1)):02d}E"
            f"{int(season_episode.group(2)):02d}"
        ).strip()

    japanese_episode = re.search(r"第\s*(\d{1,3})\s*話", cleaned)
    if japanese_episode:
        prefix = cleaned[: japanese_episode.start()].rstrip(" ._-")
        return f"{prefix} 第{int(japanese_episode.group(1)):02d}话".strip()

    bracket_episode = re.search(r"\[(\d{1,2})\](?![0-9A-Fa-f])", cleaned)
    if bracket_episode:
        prefix = cleaned[: bracket_episode.start()].rstrip(" ._-")
        return f"{prefix} 第{int(bracket_episode.group(1)):02d}集".strip()

    hyphen_episode = re.search(r"\s-\s0*(\d{1,3})(?=\s|\.|\[|\(|$)", cleaned)
    if hyphen_episode:
        prefix = cleaned[: hyphen_episode.start()].rstrip(" ._-")
        return f"{prefix} 第{int(hyphen_episode.group(1)):02d}集".strip()

    plain_episode = re.search(r"\s0*(\d{1,2})(?=\s*(?:\(|\[))", cleaned)
    if plain_episode:
        prefix = cleaned[: plain_episode.start()].rstrip(" ._-")
        return f"{prefix} 第{int(plain_episode.group(1)):02d}集".strip()

    if MOVIE_MARKER in original.casefold():
        return "剧场版 擅长捉弄人的高木同学"
    cleaned = re.sub(r"\s*\([^)]*(?:1080|BD|WEB|HEVC|FLAC)[^)]*\)\s*$", "", cleaned, flags=re.I)
    return compact_text(cleaned, 110) or compact_text(original, 110)


def canonical_anime_key(source: str) -> str:
    lowered = source.casefold()
    marker = episode_marker(source)
    if "watashi no yuri wa oshigoto desu" in lowered and marker and marker[1] == 7:
        return "manual:watayuri:s01e07"
    if ("kaoru hana wa rin to saku" in lowered or "薫る花は凛と咲く" in source) and marker and marker[1] == 2:
        return "manual:kaoru-hana:s01e02"
    # This deliberately strips only the final subtitle extension. It joins the
    # three observed source-renaming pairs without merging unrelated videos.
    return re.sub(r"\s+", " ", SUBTITLE_EXT_RE.sub("", source).casefold()).strip()


def load_mining_records(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[dict[date, list[MiningRecord]], dict[str, Any]]:
    note_type_id = find_note_type_id(conn, "Lapis")
    expression_ord = find_field_ord(conn, note_type_id, "Expression")
    sentence_ord = find_field_ord(conn, note_type_id, "Sentence")
    misc_ord = find_field_ord(conn, note_type_id, "MiscInfo")
    start_ms, end_ms = day_bounds_ms(start, end)
    days: dict[date, list[MiningRecord]] = defaultdict(list)
    all_records: list[MiningRecord] = []

    rows = conn.execute(
        """
        select id, flds, tags
        from notes
        where mid = ? and id >= ? and id < ?
        order by id
        """,
        (note_type_id, start_ms, end_ms),
    )
    for note_id, flds, tags in rows:
        fields = str(flds).split("\x1f")
        misc = fields[misc_ord] if len(fields) > misc_ord else ""
        lines = strip_misc_html(misc)
        raw_source = lines[0] if lines else ""
        source = extract_source_label(lines)
        category = classify_source_category(raw_source, source, str(tags or ""))
        work = guess_work_label(source)
        if category == "novel":
            work = normalize_novel_work_label(work)
        source_display = (
            short_anime_source(source)
            if category == "anime"
            else compact_text(work or source, 110)
        )
        jlpt_match = JLPT_RE.search(" " + str(tags or "") + " ")
        mined_at = datetime.fromtimestamp(int(note_id) / 1000, TIME_ZONE)
        record = MiningRecord(
            note_id=int(note_id),
            mined_at=mined_at,
            expression=clean_field(fields[expression_ord] if len(fields) > expression_ord else ""),
            sentence=clean_field(fields[sentence_ord] if len(fields) > sentence_ord else ""),
            source=source,
            source_display=source_display,
            work=work,
            category=category,
            jlpt=jlpt_match.group(1).upper() if jlpt_match else "未标注",
            position_seconds=parse_position(raw_source),
        )
        days[mined_at.date()].append(record)
        all_records.append(record)

    summary = {
        "count": len(all_records),
        "active_days": len(days),
        "category_counts": Counter(record.category for record in all_records),
        "jlpt_counts": Counter(record.jlpt for record in all_records),
        "records": all_records,
    }
    return days, summary


def choose_canonical_label(events: list[AnimeEvent]) -> str:
    labels = Counter(short_anime_source(event.source) for event in events)
    return sorted(labels, key=lambda value: (-labels[value], len(value), value))[0]


def split_anime_viewings(
    key: str, events: list[AnimeEvent], subtitle_cutoff: datetime
) -> list[tuple[str, list[AnimeEvent], int, int]]:
    """Split a confirmed replay without treating ordinary source reuse as a replay."""
    is_yuru_yuri_first_season = any(
        YURU_YURI_REWATCH_MARKER in event.source.casefold() for event in events
    )
    if is_yuru_yuri_first_season:
        first_viewing = [event for event in events if event.mined_at < subtitle_cutoff]
        second_viewing = [event for event in events if event.mined_at >= subtitle_cutoff]
        if first_viewing and second_viewing:
            return [
                (f"{key}:viewing:1", first_viewing, 1, 2),
                (f"{key}:viewing:2", second_viewing, 2, 2),
            ]
    return [(key, events, 1, 1)]


def allocate_anime_days(
    records: Iterable[MiningRecord], start: date, end: date
) -> tuple[dict[date, list[dict[str, Any]]], dict[str, Any]]:
    groups: dict[str, list[AnimeEvent]] = defaultdict(list)
    for record in records:
        if record.category != "anime":
            continue
        groups[canonical_anime_key(record.source)].append(
            AnimeEvent(record.mined_at, record.source, record.position_seconds)
        )

    movie_first_times = [
        event.mined_at
        for events in groups.values()
        for event in events
        if MOVIE_MARKER in event.source.casefold()
    ]
    if not movie_first_times:
        raise SystemExit("Could not locate the Takagi-san movie subtitle record")
    subtitle_cutoff = min(movie_first_times)

    days: dict[date, list[dict[str, Any]]] = defaultdict(list)
    mode_seconds: Counter[str] = Counter()
    mode_entries: Counter[str] = Counter()
    works: set[str] = set()
    unique_entries = 0

    for key, group_events in groups.items():
        all_events = sorted(group_events, key=lambda event: event.mined_at)
        if not any(start <= event.mined_at.date() <= end for event in all_events):
            continue
        unique_entries += 1
        for viewing_key, events, viewing_index, viewing_count in split_anime_viewings(
            key, all_events, subtitle_cutoff
        ):
            is_movie = any(MOVIE_MARKER in event.source.casefold() for event in events)
            duration_seconds = (
                max((event.position_seconds or 0) for event in events)
                if is_movie
                else 24 * 60
            )
            if is_movie and duration_seconds <= 0:
                duration_seconds = 72 * 60 + 5
            mode = "无字幕" if events[0].mined_at >= subtitle_cutoff else "日语字幕"
            label = choose_canonical_label(events)
            works.add(guess_work_label(label))

            weights: Counter[date] = Counter()
            previous: int | None = None
            note_counts: Counter[date] = Counter()
            for event in events:
                current_day = event.mined_at.date()
                note_counts[current_day] += 1
                position = event.position_seconds
                if position is None:
                    continue
                if previous is None:
                    delta = max(0, position)
                elif position >= previous:
                    delta = position - previous
                else:
                    # A reset within one viewing usually means a source rename or
                    # a short replay. The weights remain normalized to one episode.
                    delta = max(0, position)
                if delta > 0:
                    weights[current_day] += delta
                previous = position
            if not weights:
                weights[events[0].mined_at.date()] = 1
            weight_total = sum(weights.values())
            for active_day, weight in sorted(weights.items()):
                if not start <= active_day <= end:
                    continue
                allocated = duration_seconds * weight / weight_total
                days[active_day].append(
                    {
                        "key": viewing_key,
                        "label": label,
                        "seconds": allocated,
                        "mode": mode,
                        "note_count": note_counts[active_day],
                        "movie": is_movie,
                        "viewing_index": viewing_index,
                        "viewing_count": viewing_count,
                    }
                )
            mode_seconds[mode] += duration_seconds
            mode_entries[mode] += 1

    for rows in days.values():
        rows.sort(key=lambda row: (row["mode"], row["label"]))
    mode_active_days: dict[str, set[date]] = defaultdict(set)
    for active_day, rows in days.items():
        for row in rows:
            mode_active_days[row["mode"]].add(active_day)
    summary = {
        "seconds": sum(mode_seconds.values()),
        "entries": sum(mode_entries.values()),
        "unique_entries": unique_entries,
        "repeat_entries": sum(mode_entries.values()) - unique_entries,
        "active_days": len(days),
        "mode_seconds": mode_seconds,
        "mode_entries": mode_entries,
        "mode_active_days": mode_active_days,
        "subtitle_cutoff": subtitle_cutoff,
        "raw_source_count": len(
            {record.source for record in records if record.category == "anime"}
        ),
        "work_labels": works,
    }
    return days, summary


def import_reading_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("anniversary_reading_stats", script_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not import reading parser: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_reading_days(
    books_dir: Path, reading_script: Path, start: date, end: date
) -> tuple[dict[date, list[dict[str, Any]]], dict[str, Any]]:
    module = import_reading_module(reading_script)
    library = module.load_library(books_dir, TIME_ZONE)
    nested: dict[date, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"seconds": 0.0, "characters": 0.0})
    )
    record_count = 0
    for stat in library.stats:
        stat_day = date.fromisoformat(stat.date_key)
        if not start <= stat_day <= end:
            continue
        row = nested[stat_day][stat.title]
        row["seconds"] += float(stat.reading_time_seconds)
        row["characters"] += float(stat.characters_read)
        record_count += 1

    days: dict[date, list[dict[str, Any]]] = {}
    for stat_day, titles in nested.items():
        rows = []
        for title, values in titles.items():
            seconds = values["seconds"]
            characters = values["characters"]
            rows.append(
                {
                    "title": title,
                    "seconds": seconds,
                    "characters": characters,
                    "speed": characters / (seconds / 3600) if seconds > 0 else 0,
                }
            )
        days[stat_day] = sorted(rows, key=lambda row: (-row["seconds"], row["title"]))

    all_rows = [row for rows in days.values() for row in rows]
    summary = {
        "seconds": sum(row["seconds"] for row in all_rows),
        "characters": sum(row["characters"] for row in all_rows),
        "titles": len({row["title"] for row in all_rows}),
        "active_days": len(days),
        "record_count": record_count,
    }
    return days, summary


def import_module_from_path(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not import module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def import_mining_history_modules(history_dir: Path):
    scripts_dir = history_dir / "scripts"
    analysis_path = scripts_dir / "analyze_mining_history.py"
    ratios_path = scripts_dir / "calculate_unclear_ratios.py"
    for path in (analysis_path, ratios_path):
        if not path.exists():
            raise SystemExit(f"挖词历史辅助脚本不存在：{path}")

    analysis = import_module_from_path(
        "anniversary_mining_history_analysis", analysis_path
    )
    previous_analysis = sys.modules.get("analyze_mining_history")
    sys.modules["analyze_mining_history"] = analysis
    try:
        ratios = import_module_from_path(
            "anniversary_mining_history_ratios", ratios_path
        )
    finally:
        if previous_analysis is None:
            sys.modules.pop("analyze_mining_history", None)
        else:
            sys.modules["analyze_mining_history"] = previous_analysis
    return analysis, ratios


def load_mining_history_days(
    history_dir: Path, movies_dir: Path, start: date, end: date
) -> tuple[dict[date, list[dict[str, Any]]], dict[str, Any]]:
    if not history_dir.exists():
        raise SystemExit(f"挖词历史目录不存在：{history_dir}")
    if not movies_dir.exists():
        raise SystemExit(f"Subtitle source directory not found: {movies_dir}")

    analysis, ratios = import_mining_history_modules(history_dir)
    _stats, all_stats, skipped = analysis.collect_episode_stats(history_dir, False)

    dated_stats: list[tuple[date, Any]] = []
    for stat in all_stats:
        try:
            stat_day = date.fromisoformat(analysis.snapshot_date(stat.snapshot))
        except ValueError:
            continue
        if start <= stat_day <= end:
            dated_stats.append((stat_day, stat))

    latest_by_episode: dict[tuple[str, int | None, int], tuple[date, Any]] = {}
    for stat_day, stat in dated_stats:
        key = (stat.anime, stat.season, stat.episode)
        previous = latest_by_episode.get(key)
        if previous is None or stat.snapshot > previous[1].snapshot:
            latest_by_episode[key] = (stat_day, stat)
    latest = sorted(
        latest_by_episode.values(),
        key=lambda item: (
            item[0],
            item[1].anime.casefold(),
            item[1].season or 0,
            item[1].episode,
        ),
    )

    subtitle_index = ratios.index_subtitles(movies_dir)
    paired_rows, missing = ratios.pair_rows(
        [stat for _stat_day, stat in latest], subtitle_index
    )
    paired_by_path = {row.stat.path: row for row in paired_rows}

    days: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for stat_day, stat in latest:
        paired = paired_by_path.get(stat.path)
        total_source_cues = int(paired.total_cues) if paired is not None else None
        days[stat_day].append(
            {
                "anime": stat.anime,
                "episode": stat.episode_label,
                "unclear_cues": int(stat.cues),
                "total_source_cues": total_source_cues,
                "unclear_ratio": (
                    int(stat.cues) / total_source_cues
                    if total_source_cues
                    else None
                ),
            }
        )
    for rows in days.values():
        rows.sort(key=lambda row: (row["anime"].casefold(), row["episode"]))

    paired = [
        row
        for rows in days.values()
        for row in rows
        if row["total_source_cues"]
    ]
    paired_unclear = sum(row["unclear_cues"] for row in paired)
    total_source_cues = sum(row["total_source_cues"] for row in paired)
    episode_ratios = [row["unclear_ratio"] for row in paired]
    summary = {
        "episodes": len(latest),
        "anime": len({stat.anime for _stat_day, stat in latest}),
        "unclear_cues": sum(int(stat.cues) for _stat_day, stat in latest),
        "paired_episodes": len(paired),
        "paired_unclear_cues": paired_unclear,
        "total_source_cues": total_source_cues,
        "weighted_unclear_ratio": (
            paired_unclear / total_source_cues if total_source_cues else None
        ),
        "median_episode_ratio": (
            median(episode_ratios) if episode_ratios else None
        ),
        "active_days": len(days),
        "first_date": min(days) if days else None,
        "last_date": max(days) if days else None,
        "snapshots": len(dated_stats),
        "duplicate_snapshots": len(dated_stats) - len(latest),
        "missing_source_subtitles": len(missing),
        "skipped_files": len(skipped),
    }
    return days, summary


def render_counter(counter: Counter[str], *, suffix: str = "") -> str:
    if not counter:
        return "无"
    return "、".join(f"{md_escape(key)} {value:,}{suffix}" for key, value in counter.most_common())


def render_anki_day(bucket: dict[str, Any] | None) -> str:
    if not bucket or not bucket["review_count"]:
        return "无可量化卡片学习记录。"
    times = bucket["times"]
    window = f"{min(times):%H:%M}—{max(times):%H:%M}"
    deck_parts = []
    for label, values in sorted(
        bucket["decks"].items(), key=lambda item: (-item[1]["seconds"], item[0])
    ):
        deck_parts.append(
            f"{md_escape(label)} {format_duration(values['seconds'])}"
            f"（{values['reviews']:,} 次、{len(values['cards']):,} 张）"
        )
    first_cards: Counter[str] = bucket["first_cards"]
    first_total = sum(first_cards.values())
    first_text = (
        f"；首次进入复习 {first_total:,} 张：{render_counter(first_cards, suffix=' 张')}"
        if first_total
        else "；今天没有新卡首次进入复习"
    )
    return (
        f"答题 {bucket['review_count']:,} 次、涉及 {len(bucket['cards']):,} 张卡，"
        f"实际答题计时 {format_duration(bucket['seconds'])}；最早/最晚答题为 {window}。"
        f"分项：{'；'.join(deck_parts)}{first_text}。"
    )


def mining_source_counter(records: list[MiningRecord]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for record in records:
        category = {"anime": "看番", "novel": "轻小说", "other": "其他"}.get(
            record.category, record.category
        )
        counter[f"{category}·{record.source_display}"] += 1
    return counter


def render_mining_summary(records: list[MiningRecord]) -> str:
    if not records:
        return "没有新建词句挖掘卡。"
    jlpt = Counter(record.jlpt for record in records)
    return (
        f"本日新建 {len(records):,} 张；JLPT 标记为 {render_counter(jlpt, suffix=' 张')}。"
    )


def render_mining_table(records: list[MiningRecord]) -> list[str]:
    if not records:
        return []
    lines = [
        f"**当天新建的词句挖掘卡（{len(records):,} 张）**",
        "",
    ]
    expressions = [
        md_escape(compact_text(record.expression or "（表达字段为空）", 100))
        for record in records
    ]
    for offset in range(0, len(expressions), 20):
        lines.append("- " + "、".join(expressions[offset : offset + 20]))
    return lines


def render_kaishi_list(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return []
    counts = Counter(record["display"] for record in records)
    unique_displays = list(dict.fromkeys(record["display"] for record in records))
    lines = [
        f"**Kaishi 当天新学表达列表（{len(unique_displays):,} 项；对应 {len(records):,} 张卡）**",
        "",
    ]
    expressions = [
        md_escape(display) + (f" ×{counts[display]}" if counts[display] > 1 else "")
        for display in unique_displays
    ]
    for offset in range(0, len(expressions), 20):
        lines.append("- " + "、".join(expressions[offset : offset + 20]))
    return lines


def render_anime_day(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "无可量化看番记录。"
    mode_totals: Counter[str] = Counter()
    details = []
    for row in rows:
        mode_totals[row["mode"]] += row["seconds"]
        movie = "，剧场版" if row["movie"] else ""
        viewing = (
            f"（第 {row['viewing_index']} 遍）"
            if row.get("viewing_count", 1) > 1
            else ""
        )
        details.append(
            f"《{md_escape(row['label'])}》{viewing}{format_minutes(row['seconds'])}"
            f"（{row['mode']}{movie}；挖词 {row['note_count']:,} 条）"
        )
    mode_text = "、".join(
        f"{mode} {format_duration(seconds)}" for mode, seconds in mode_totals.items()
    )
    return (
        f"估算 {format_duration(sum(mode_totals.values()))}，涉及 {len(rows):,} 个观看集次；"
        f"字幕方式：{mode_text}。明细：{'；'.join(details)}。"
    )


def render_reading_day(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "无可量化轻小说阅读记录。"
    total_seconds = sum(row["seconds"] for row in rows)
    total_characters = sum(row["characters"] for row in rows)
    speed = total_characters / (total_seconds / 3600) if total_seconds > 0 else 0
    details = "；".join(
        f"《{md_escape(row['title'])}》{format_duration(row['seconds'])}、"
        f"{row['characters']:,.0f} 字（{row['speed']:,.0f} 字/小时）"
        for row in rows
    )
    return (
        f"共读 {format_duration(total_seconds)}、{total_characters:,.0f} 字，"
        f"加权速度 {speed:,.0f} 字/小时。明细：{details}。"
    )


def render_listening_feedback_day(rows: list[dict[str, Any]]) -> str:
    unclear_cues = sum(row["unclear_cues"] for row in rows)
    paired = [row for row in rows if row["total_source_cues"]]
    paired_unclear = sum(row["unclear_cues"] for row in paired)
    total_source_cues = sum(row["total_source_cues"] for row in paired)
    pieces = [
        f"记录 {len(rows):,} 集、{len({row['anime'] for row in rows}):,} 部作品，"
        f"标记 {unclear_cues:,} 条未听懂台词"
    ]
    if paired:
        pieces.append(
            f"其中 {len(paired):,} 集配对原字幕，共 {paired_unclear:,}/{total_source_cues:,} 条，"
            f"加权占比 {paired_unclear / total_source_cues:.2%}"
        )
    if len(paired) < len(rows):
        pieces.append(f"另有 {len(rows) - len(paired):,} 集未找到原字幕")
    return "；".join(pieces) + "。"


def top_label(counter: Counter[str]) -> str:
    return counter.most_common(1)[0][0] if counter else ""


def daily_narrative(
    day: date,
    index: int,
    end: date,
    anki: dict[str, Any] | None,
    kaishi: list[dict[str, Any]],
    mining: list[MiningRecord],
    anime: list[dict[str, Any]],
    reading: list[dict[str, Any]],
    listening_feedback: list[dict[str, Any]],
    first_mining_day: date | None,
    first_reading_day: date | None,
    first_listening_feedback_day: date | None,
    movie_day: date,
) -> str:
    anki_seconds = anki["seconds"] if anki else 0
    anime_seconds = sum(row["seconds"] for row in anime)
    reading_seconds = sum(row["seconds"] for row in reading)
    total = anki_seconds + anime_seconds + reading_seconds
    parts = []
    if index == 1:
        parts.append("这是这一轮日语学习的第一天")
    if day == first_mining_day:
        parts.append("今天第一次新建词句挖掘卡")
    if day == first_reading_day:
        parts.append("今天开始出现可量化的轻小说阅读记录")
    if day == first_listening_feedback_day:
        parts.append("今天开始留下听力反馈记录")
    if day == movie_day:
        parts.append("今天看了《擅长捉弄人的高木同学》剧场版，并从这部起切换为无字幕看番")
    if day == end:
        if datetime.now(TIME_ZONE).date() <= end:
            parts.append("这是第 365 天；当天数据截至生成报告时，仍可能继续增长")
        else:
            parts.append("这是第 365 天")

    category_times = {
        "卡片学习": anki_seconds,
        "看番": anime_seconds,
        "轻小说": reading_seconds,
    }
    active = [(label, seconds) for label, seconds in category_times.items() if seconds > 0]
    if active:
        focus, focus_seconds = max(active, key=lambda item: item[1])
        parts.append(f"今天可量化学习共 {format_duration(total)}，时间最多的是{focus}（{format_duration(focus_seconds)}）")
    else:
        parts.append("今天没有检测到这三类工具中的可量化学习记录")

    if anki and anki["review_count"]:
        deck_seconds = Counter(
            {label: values["seconds"] for label, values in anki["decks"].items()}
        )
        parts.append(
            f"卡片复习的重心是“{top_label(deck_seconds)}”，共答题 {anki['review_count']:,} 次"
        )
    if kaishi:
        parts.append(f"Kaishi 首次学习 {len(kaishi):,} 张词汇卡")
    if mining:
        parts.append(f"我还新建了 {len(mining):,} 张词句挖掘卡")
    if anime:
        parts.append(f"看番涉及 {len(anime):,} 个观看集次")
    if reading:
        parts.append(f"轻小说主读《{reading[0]['title']}》")
    return "。".join(parts) + "。"


def render_summary_table(
    day_count: int,
    anki_summary: dict[str, Any],
    mining_summary: dict[str, Any],
    anime_summary: dict[str, Any],
    reading_summary: dict[str, Any],
) -> list[str]:
    total_seconds = (
        anki_summary["seconds"] + anime_summary["seconds"] + reading_summary["seconds"]
    )
    mode_seconds: Counter[str] = anime_summary["mode_seconds"]
    mode_entries: Counter[str] = anime_summary["mode_entries"]
    return [
        "| 类别 | 一周年累计 | 活跃天数 | 额外数量 |",
        "|---|---:|---:|---|",
        f"| 卡片学习 | {anki_summary['seconds'] / 3600:.1f} 小时 | {anki_summary['active_days']} 天 | "
        f"{anki_summary['review_count']:,} 次答题；{anki_summary['unique_cards']:,} 张不同卡；"
        f"{anki_summary['first_review_total']:,} 张首次进入复习 |",
        f"| 看番（日语字幕） | {mode_seconds['日语字幕'] / 3600:.1f} 小时 | "
        f"{len(anime_summary['mode_active_days']['日语字幕'])} 天 | "
        f"{mode_entries['日语字幕']:,} 个观看集次 |",
        f"| 看番（无字幕） | {mode_seconds['无字幕'] / 3600:.1f} 小时 | "
        f"{len(anime_summary['mode_active_days']['无字幕'])} 天 | "
        f"{mode_entries['无字幕']:,} 个观看集次 |",
        f"| 读轻小说 | {reading_summary['seconds'] / 3600:.1f} 小时 | {reading_summary['active_days']} 天 | "
        f"{reading_summary['titles']:,} 本；{reading_summary['characters']:,.0f} 字 |",
        f"| **合计** | **{total_seconds / 3600:.1f} 小时** | **{day_count} 个自然日** | "
        f"日均 **{total_seconds / day_count / 60:.0f} 分钟** |",
    ]


def generate_report(
    start: date,
    end: date,
    anki_days: dict[date, dict[str, Any]],
    anki_summary: dict[str, Any],
    kaishi_days: dict[date, list[dict[str, Any]]],
    kaishi_summary: dict[str, Any],
    mining_days: dict[date, list[MiningRecord]],
    mining_summary: dict[str, Any],
    anime_days: dict[date, list[dict[str, Any]]],
    anime_summary: dict[str, Any],
    reading_days: dict[date, list[dict[str, Any]]],
    reading_summary: dict[str, Any],
    listening_feedback_days: dict[date, list[dict[str, Any]]],
    listening_feedback_summary: dict[str, Any],
) -> str:
    all_days = date_range(start, end)
    total_seconds = (
        anki_summary["seconds"] + anime_summary["seconds"] + reading_summary["seconds"]
    )
    first_mining_day = min(mining_days) if mining_days else None
    first_reading_day = min(reading_days) if reading_days else None
    first_listening_feedback_day = (
        min(listening_feedback_days) if listening_feedback_days else None
    )
    movie_day = anime_summary["subtitle_cutoff"].date()

    daily_totals = {}
    for day in all_days:
        daily_totals[day] = (
            (anki_days.get(day) or {}).get("seconds", 0)
            + sum(row["seconds"] for row in anime_days.get(day, []))
            + sum(row["seconds"] for row in reading_days.get(day, []))
        )
    peak_day = max(daily_totals, key=daily_totals.get)
    peak_anki = max(all_days, key=lambda day: (anki_days.get(day) or {}).get("seconds", 0))
    peak_anime = max(all_days, key=lambda day: sum(row["seconds"] for row in anime_days.get(day, [])))
    peak_reading = max(all_days, key=lambda day: sum(row["seconds"] for row in reading_days.get(day, [])))

    lines = [
        f"# 从 {start:%Y-%m-%d} 到 {end:%Y-%m-%d}：我的日语学习一周年日记",
        "",
        f"> 数据补写版日记，共 {len(all_days)} 天。第一条卡片答题记录发生在 "
        f"{anki_summary['first_time']:%Y-%m-%d %H:%M}；本报告的数据快照生成于 "
        f"{datetime.now(TIME_ZONE):%Y-%m-%d %H:%M}。",
        "",
        "这一年，我把日语学习慢慢变成了三条并行的线：每天处理卡片，把看番时遇到的表达做成词句挖掘卡；"
        "从 2026 年 2 月起，再把轻小说阅读纳入稳定记录。下面不是凭印象补写，而是逐日还原我确实留下的数据。",
        "",
        "## 一周年总账",
        "",
        *render_summary_table(
            len(all_days), anki_summary, mining_summary, anime_summary, reading_summary
        ),
        "",
        f"- 这一年共新建 **{mining_summary['count']:,} 张词句挖掘卡**，有新建记录的日期为 "
        f"**{mining_summary['active_days']} 天**。",
        f"- Kaishi 1.5k 共首次学习 **{kaishi_summary['count']:,} 张词汇卡**，对应 "
        f"**{kaishi_summary['unique_expressions']:,} 种词面与读音组合**（"
        f"{kaishi_summary['unique_written_forms']:,} 种书写形式），分布在 "
        f"**{kaishi_summary['active_days']} 天**：{kaishi_summary['first_date']:%Y-%m-%d}—"
        f"{kaishi_summary['last_date']:%Y-%m-%d}。",
        f"- 看番按 TV/OVA 每个观看集次 24 分钟估算；《高木同学》剧场版按字幕进度的 "
        f"{format_duration(max(0, anime_summary['seconds'] - (anime_summary['entries'] - 1) * 24 * 60), seconds_precision=True)} "
        f"计。原始记录中有 {anime_summary['raw_source_count']:,} 个不同字幕源名，合并同一视频的 5 组别名后，"
        f"全年共 **{anime_summary['unique_entries']:,} 个独立视频条目**；《摇曳百合》第 1 季的 12 集各观看两遍，"
        f"因此合计 **{anime_summary['entries']:,} 个观看集次**。",
        f"- 字幕分界点是 **{anime_summary['subtitle_cutoff']:%Y-%m-%d %H:%M:%S}**：此前按日语字幕，"
        "从《擅长捉弄人的高木同学》剧场版起（含该片）按无字幕。",
        f"- 挖词历史中的听力反馈从 **{listening_feedback_summary['first_date']:%Y-%m-%d}** 起覆盖 "
        f"**{listening_feedback_summary['anime']:,} 部作品、{listening_feedback_summary['episodes']:,} 集**；"
        f"按每集最新快照统计，共标记 **{listening_feedback_summary['unclear_cues']:,} 条未听懂台词**。"
        f"已配对原字幕 {listening_feedback_summary['paired_episodes']:,} 集，标记条目为 "
        f"{listening_feedback_summary['paired_unclear_cues']:,}/"
        f"{listening_feedback_summary['total_source_cues']:,}，加权占比 "
        f"**{listening_feedback_summary['weighted_unclear_ratio']:.2%}**。",
        f"- 全年总学习时间约 **{total_seconds / 3600:.1f} 小时**，折合日均 "
        f"**{total_seconds / len(all_days) / 60:.0f} 分钟**。单日总时长最高是 "
        f"**{peak_day:%Y-%m-%d}（{format_duration(daily_totals[peak_day])}）**；"
        f"卡片最高日为 {peak_anki:%Y-%m-%d}，看番最高日为 {peak_anime:%Y-%m-%d}，"
        f"轻小说最高日为 {peak_reading:%Y-%m-%d}。",
        "",
        "## 口径说明",
        "",
        "- 日记按 Asia/Shanghai 的自然日 00:00—24:00 分组。卡片软件本身的换日时间是 05:00，"
        "所以极少数凌晨记录与软件界面中的“学习日”可能相差一天；这里统一采用日历日期，更适合作为日记。",
        "- 卡片时间沿用此前年度趋势图的口径：统计这一资料库在窗口内的全部答题历史。"
        "当前仍存在的非根牌组全部属于日语；无法从当前牌组表还原归属的历史卡，依据现有判断统一归为 "
        "jlab's beginner course。"
        "时间是每次答题的实际计时之和，不把两次答题之间的休息算进去；“首次进入复习”按该卡全部历史中的第一条答题记录判断。",
        "- Kaishi 的“新学表达”同样以每张卡全部历史中的第一条答题记录为准；列表保留词面，"
        "词面与读音不同时附上读音，不列释义、例句或来源。",
        "- 看番没有播放器观看时长日志，因此先把同一视频的别名合并，再按挖词时的字幕位置增量把固定片长分配到实际日期。"
        "这 5 组别名包括 3 组扩展名变化、1 组《我的百合乃工作是也！》第 7 集和 1 组《薰香花朵凛然绽放》第 2 集。"
        "《摇曳百合》第 1 季在字幕分界点前后各有一轮完整的 12 集来源记录，按两遍观看分别计时。"
        "这是估算时间，但作品、集数和挖词日期来自真实记录。",
        "- 挖词历史按动画、季度和集数合并重复快照，每集只采用时间最新的一份记录；"
        "“未听懂条目占比”是被标记的台词条目数除以配对原字幕条目数，只覆盖实际留下记录的集数，"
        "不能把未覆盖条目解释为已经听懂，也不能直接等同于标准化听力正确率。",
        "- 轻小说使用 Hoshi Reader 阅读统计中的时长与字符数；单条少于 60 秒的统计被阅读数据脚本过滤。",
        "- 每天新建的词句挖掘卡直接展开，只保留词句本身，不再逐条列出例句与来源；原始数据库没有被改动。",
        "",
        "## 逐日学习日记",
        "",
    ]

    for index, day in enumerate(all_days, start=1):
        anki = anki_days.get(day)
        kaishi = kaishi_days.get(day, [])
        mining = mining_days.get(day, [])
        anime = anime_days.get(day, [])
        reading = reading_days.get(day, [])
        listening_feedback = listening_feedback_days.get(day, [])
        day_lines = [
            f"### 第 {index:03d} 天｜{day:%Y-%m-%d}（周{WEEKDAYS_ZH[day.weekday()]}）",
            "",
            daily_narrative(
                day,
                index,
                end,
                anki,
                kaishi,
                mining,
                anime,
                reading,
                listening_feedback,
                first_mining_day,
                first_reading_day,
                first_listening_feedback_day,
                movie_day,
            ),
            "",
            f"- **卡片学习**：{render_anki_day(anki)}",
        ]
        if kaishi:
            unique_kaishi = len({record["display"] for record in kaishi})
            day_lines.append(
                f"- **Kaishi 新学表达**：本日首次学习 {len(kaishi):,} 张词汇卡，"
                f"对应 {unique_kaishi:,} 项词面与读音组合。"
            )
        if mining:
            day_lines.append(f"- **词句挖掘卡**：{render_mining_summary(mining)}")
        if anime:
            day_lines.append(f"- **看番**：{render_anime_day(anime)}")
        if reading:
            day_lines.append(f"- **读轻小说**：{render_reading_day(reading)}")
        if listening_feedback:
            day_lines.append(
                f"- **听力反馈记录**：{render_listening_feedback_day(listening_feedback)}"
            )
        day_lines.append("")
        lines.extend(day_lines)
        lines.extend(render_kaishi_list(kaishi))
        if kaishi:
            lines.append("")
        lines.extend(render_mining_table(mining))
        if mining:
            lines.append("")

    lines.extend(
        [
            "## 写在一周年",
            "",
            "如果只看任何一天，这一年常常只是几十分钟的卡片、几集动画、若干页小说；"
            "把 365 天铺开以后，它们却累积成了数百小时、两万多张词句挖掘卡，以及超过一百五十万字的原文阅读。"
            "真正发生变化的并不是某个突然开窍的瞬间，而是我每天都继续给这个系统添一点东西：复习旧卡、从内容里发现新表达，"
            "再回到更多原生内容里验证它们。",
            "",
            "前半程我还依赖日语字幕；从《高木同学》剧场版开始，我把无字幕变成了默认。"
            "后半程又加入轻小说，于是输入不再只有二十多分钟一集的对话，也有长篇叙事、内心独白和更密集的书面表达。"
            "这一周年最值得记录的，不只是总时长，而是学习方式已经从‘为了学日语而学’，逐渐变成‘用日语持续看、读、理解自己喜欢的作品’。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    all_days = date_range(args.start, args.end)
    if len(all_days) != 365:
        raise SystemExit(
            f"Expected a 365-day anniversary window, got {len(all_days)} days: "
            f"{args.start} through {args.end}"
        )
    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"Anki database not found: {db_path}")

    temp_dir, snapshot = make_db_snapshot(db_path)
    try:
        conn = sqlite3.connect(snapshot)
        register_unicase(conn)
        try:
            anki_days, anki_summary = load_anki_days(conn, args.start, args.end)
            kaishi_days, kaishi_summary = load_kaishi_days(
                conn, args.start, args.end
            )
            mining_days, mining_summary = load_mining_records(conn, args.start, args.end)
            anime_days, anime_summary = allocate_anime_days(
                mining_summary["records"], args.start, args.end
            )
        finally:
            conn.close()
    finally:
        temp_dir.cleanup()

    reading_days, reading_summary = load_reading_days(
        args.books_dir.expanduser().resolve(),
        args.reading_script.expanduser().resolve(),
        args.start,
        args.end,
    )
    listening_feedback_days, listening_feedback_summary = load_mining_history_days(
        args.mining_history_dir.expanduser().resolve(),
        args.movies_dir.expanduser().resolve(),
        args.start,
        args.end,
    )
    report = generate_report(
        args.start,
        args.end,
        anki_days,
        anki_summary,
        kaishi_days,
        kaishi_summary,
        mining_days,
        mining_summary,
        anime_days,
        anime_summary,
        reading_days,
        reading_summary,
        listening_feedback_days,
        listening_feedback_summary,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")

    summary = {
        "output": str(output),
        "days": len(all_days),
        "ankiHours": round(anki_summary["seconds"] / 3600, 3),
        "ankiReviews": anki_summary["review_count"],
        "kaishiNewExpressions": kaishi_summary["count"],
        "kaishiActiveDays": kaishi_summary["active_days"],
        "minedExpressions": mining_summary["count"],
        "animeHours": round(anime_summary["seconds"] / 3600, 3),
        "animeEntries": anime_summary["entries"],
        "animeUniqueVideos": anime_summary["unique_entries"],
        "listeningFeedbackEpisodes": listening_feedback_summary["episodes"],
        "listeningFeedbackAnime": listening_feedback_summary["anime"],
        "listeningUnclearCues": listening_feedback_summary["unclear_cues"],
        "listeningUnclearRatio": round(
            listening_feedback_summary["weighted_unclear_ratio"], 6
        ),
        "subtitledHours": round(anime_summary["mode_seconds"]["日语字幕"] / 3600, 3),
        "unsubtitledHours": round(anime_summary["mode_seconds"]["无字幕"] / 3600, 3),
        "readingHours": round(reading_summary["seconds"] / 3600, 3),
        "readingCharacters": round(reading_summary["characters"]),
        "totalHours": round(
            (
                anki_summary["seconds"]
                + anime_summary["seconds"]
                + reading_summary["seconds"]
            )
            / 3600,
            3,
        ),
        "markdownBytes": output.stat().st_size,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
