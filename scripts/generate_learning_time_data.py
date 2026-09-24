#!/usr/bin/env python3
"""Export daily, weekly and cumulative study time using the diary's definitions."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.generate_anniversary_diary import (
    DEFAULT_ANKI_DB,
    DEFAULT_BACKUPS_DIR,
    DEFAULT_BOOKS_DIR,
    DEFAULT_READING_SCRIPT,
    DEFAULT_START,
    TIME_ZONE,
    allocate_anime_days,
    date_range,
    find_missing_card_ids,
    load_anki_days,
    load_mining_records,
    load_reading_days,
    make_db_snapshot,
    recover_historical_card_identities,
    register_unicase,
)

SERIES = ("a", "j", "n", "r", "b")


def summarize_daily(daily: list[dict]) -> dict:
    """Keep seconds as the source of truth, including incomplete final weeks."""
    if not daily:
        raise ValueError("No days to summarize")
    for previous, current in zip(daily, daily[1:]):
        if date.fromisoformat(current["d"]) - date.fromisoformat(previous["d"]) != timedelta(days=1):
            raise ValueError("Daily rows must be contiguous and ordered")
    if any(not math.isfinite(row[key]) or row[key] < 0 for row in daily for key in SERIES):
        raise ValueError("Study seconds must be finite and non-negative")
    totals = {key: math.fsum(row[key] for row in daily) for key in SERIES}
    weekly = []
    for offset in range(0, len(daily), 7):
        rows = daily[offset : offset + 7]
        weekly.append({
            "d": rows[0]["d"],
            "end": rows[-1]["d"],
            "days": len(rows),
            **{key: math.fsum(row[key] for row in rows) / len(rows) / 60 for key in SERIES},
        })
    cumulative = [{
        "d": (date.fromisoformat(daily[0]["d"]) - timedelta(days=1)).isoformat(),
        **{key: 0.0 for key in SERIES},
    }]
    running = {key: 0.0 for key in SERIES}
    for row in daily:
        for key in SERIES:
            running[key] += row[key]
        cumulative.append({"d": row["d"], **{key: running[key] / 3600 for key in SERIES}})
    recent = daily[-28:]
    peak = max(weekly, key=lambda row: sum(row[key] for key in SERIES))
    return {
        "dailySeconds": daily,
        "weeklyMinutesPerDay": weekly,
        "cumulativeHours": cumulative,
        "totalsSeconds": totals,
        "totalsHours": {key: value / 3600 for key, value in totals.items()},
        "totalHours": math.fsum(totals.values()) / 3600,
        "meanMinutesPerDay": math.fsum(totals.values()) / len(daily) / 60,
        "recent28Days": {
            "start": recent[0]["d"],
            "end": recent[-1]["d"],
            "days": len(recent),
            "meanMinutesPerDay": math.fsum(row[key] for row in recent for key in SERIES) / len(recent) / 60,
        },
        "peakWeek": {**peak, "totalMinutesPerDay": sum(peak[key] for key in SERIES)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(TIME_ZONE).date() - timedelta(days=1))
    parser.add_argument("--db", type=Path, default=DEFAULT_ANKI_DB)
    parser.add_argument("--backups-dir", type=Path, default=DEFAULT_BACKUPS_DIR)
    parser.add_argument("--books-dir", type=Path, default=DEFAULT_BOOKS_DIR)
    parser.add_argument("--reading-script", type=Path, default=DEFAULT_READING_SCRIPT)
    parser.add_argument("--output", type=Path, default=Path("output/japanese_learning_time_data.json"))
    args = parser.parse_args()
    days = date_range(args.start, args.end)
    extracted_at = datetime.now(TIME_ZONE)
    db = args.db.expanduser().resolve()
    print("Reading collection snapshot and restoring historical identities", file=sys.stderr, flush=True)
    temp_dir, snapshot = make_db_snapshot(db)
    try:
        conn = sqlite3.connect(snapshot)
        register_unicase(conn)
        try:
            missing_ids = find_missing_card_ids(conn, args.start, args.end)
            historical = recover_historical_card_identities(args.backups_dir.expanduser().resolve(), missing_ids)
            card_days, card_summary = load_anki_days(conn, args.start, args.end, historical)
            # Preserve the movie cutoff and the full span of cross-boundary episodes.
            history_start = min(DEFAULT_START, args.start)
            history_end = max(args.end, extracted_at.date())
            _, mining_summary = load_mining_records(
                conn, history_start, history_end, books_dir=args.books_dir.expanduser().resolve()
            )
            anime_days, anime_summary = allocate_anime_days(mining_summary["records"], args.start, args.end)
            latest_feedback_ms = conn.execute("select max(id) from revlog").fetchone()[0]
        finally:
            conn.close()
    finally:
        temp_dir.cleanup()
    reading_days, reading_summary = load_reading_days(
        args.books_dir.expanduser().resolve(), args.reading_script.expanduser().resolve(), args.start, args.end
    )
    daily = []
    for day in days:
        anime = anime_days.get(day, [])
        daily.append({
            "d": day.isoformat(),
            "a": card_days.get(day, {}).get("seconds", 0.0),
            "j": math.fsum(row["seconds"] for row in anime if row["mode"] == "日语字幕"),
            "n": math.fsum(row["seconds"] for row in anime if row["mode"] == "无字幕"),
            "r": math.fsum(row["seconds"] for row in reading_days.get(day, []) if row["category"] == "novel"),
            "b": math.fsum(row["seconds"] for row in reading_days.get(day, []) if row["category"] == "audiobook"),
        })
    summary = summarize_daily(daily)
    assert math.isclose(summary["totalsSeconds"]["a"], card_summary["seconds"])
    assert math.isclose(summary["totalsSeconds"]["r"] + summary["totalsSeconds"]["b"], reading_summary["seconds"])
    for key in SERIES:
        assert math.isclose(summary["cumulativeHours"][-1][key], summary["totalsHours"][key])
    payload = {
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "dayCount": len(days),
        "timezone": str(TIME_ZONE),
        "extractedAt": extracted_at.isoformat(),
        "subtitleCutoff": anime_summary["subtitle_cutoff"].isoformat(),
        "series": {"a": "背单词", "j": "看番（日语字幕）", "n": "看番（无字幕）", "r": "读轻小说", "b": "听有声书"},
        "bookCategories": reading_summary["categories"],
        "audiobookTitles": reading_summary["audiobook_titles"],
        "sources": {"collection": str(db), "books": str(args.books_dir.expanduser().resolve())},
        "coverage": {
            "latestCollectionFeedback": datetime.fromtimestamp(latest_feedback_ms / 1000, TIME_ZONE).isoformat(),
            "firstCardFeedbackInRange": card_summary["first_time"].isoformat() if card_summary["first_time"] else None,
            "lastCardDayInRange": max(card_days).isoformat() if card_days else None,
            "lastAnimeDayInRange": max(anime_days).isoformat() if anime_days else None,
            "lastReadingDayInRange": max((day.isoformat() for day, rows in reading_days.items() if any(row["category"] == "novel" for row in rows)), default=None),
            "lastAudiobookDayInRange": max((day.isoformat() for day, rows in reading_days.items() if any(row["category"] == "audiobook" for row in rows)), default=None),
            "cardActiveDays": card_summary["active_days"],
            "readingActiveDays": reading_summary["categories"]["novel"]["active_days"],
            "audiobookActiveDays": reading_summary["categories"]["audiobook"]["active_days"],
            "incompleteBookMediaTitles": reading_summary["incomplete_media_titles"],
            "recoveredHistoricalCards": len(historical),
            "unresolvedHistoricalCards": len(missing_ids - historical.keys()),
            "excludedHistoricalReviews": card_summary["excluded_review_count"],
            "excludedHistoricalSeconds": card_summary["excluded_seconds"],
        },
        "method": {
            "cards": "反馈计时，沿用日记牌组范围与历史英文课程排除规则",
            "anime": "普通 TV/OVA 24 分钟/集；高木剧场版按字幕进度估算；按完整来源进度分配后截取日期",
            "reading": "Hoshi Reader readingTime；charactersRead > 0 且单条时间至少 60 秒",
            "audiobook": "有效 Sasayaki 音频绑定及文字对齐的卷册，其 readingTime 从轻小说移入听有声书；只计一次；书籍级归类，不能拆分纯听与听读，未使用播放位置估算时长",
            "dailyZeros": "无相应可量化记录计为已记录投入 0，不代表没有实际学习",
            "weeks": "从开始日每七天一组；末组按实际天数计算；最近四周为截至结束日的 28 个自然日",
        },
        **summary,
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("start", "end", "dayCount", "totalHours", "totalsHours", "meanMinutesPerDay", "recent28Days", "peakWeek", "coverage")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
