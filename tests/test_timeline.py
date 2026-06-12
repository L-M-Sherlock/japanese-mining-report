from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scripts.visualize_lapis_sources import (
    Record,
    aggregate_timeline_counts,
    build_period_labels,
    mined_datetime_from_note_id,
    timeline_summary,
    unique_note_records,
)


def make_record(
    note_id: int,
    mined_at: datetime,
    work: str,
    source: str,
    *,
    card_id: int | None = None,
) -> Record:
    mined_date, mined_week, mined_month = build_period_labels(mined_at)
    return Record(
        card_id=card_id or note_id,
        note_id=note_id,
        mined_at=mined_at,
        mined_date=mined_date,
        mined_week=mined_week,
        mined_month=mined_month,
        source=source,
        work=work,
        domain="(no url)",
        url="",
        deck="Japanese",
        studied=False,
    )


class TimelineTests(unittest.TestCase):
    def test_note_id_uses_requested_timezone(self) -> None:
        note_time = datetime(2026, 1, 4, 17, 0, tzinfo=timezone.utc)
        note_id = int(note_time.timestamp() * 1000)

        mined_at = mined_datetime_from_note_id(note_id, ZoneInfo("Asia/Shanghai"))
        mined_date, mined_week, mined_month = build_period_labels(mined_at)

        self.assertEqual(mined_date, "2026-01-05")
        self.assertEqual(mined_week, "2026-W02")
        self.assertEqual(mined_month, "2026-01")

    def test_unique_note_records_deduplicates_multi_card_notes(self) -> None:
        mined_at = datetime(2026, 6, 12, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        records = [
            make_record(1000, mined_at, "Work A", "Source A", card_id=1),
            make_record(1000, mined_at, "Work A", "Source A", card_id=2),
            make_record(2000, mined_at, "Work B", "Source B", card_id=3),
        ]

        unique_records = unique_note_records(records)

        self.assertEqual([record.note_id for record in unique_records], [1000, 2000])

    def test_aggregate_timeline_counts_by_period_and_source_grain(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        records = [
            make_record(1, datetime(2026, 6, 1, 9, 0, tzinfo=tz), "Work A", "Episode 1"),
            make_record(2, datetime(2026, 6, 1, 10, 0, tzinfo=tz), "Work A", "Episode 2"),
            make_record(3, datetime(2026, 6, 2, 10, 0, tzinfo=tz), "Work B", "Episode 3"),
        ]

        by_day_work = aggregate_timeline_counts(records, "day", "work")
        by_week_source = aggregate_timeline_counts(records, "week", "source")

        self.assertEqual(
            by_day_work,
            [
                ("2026-06-01", 2, [("Work A", 2)]),
                ("2026-06-02", 1, [("Work B", 1)]),
            ],
        )
        self.assertEqual(
            by_week_source,
            [
                (
                    "2026-W23",
                    3,
                    [("Episode 1", 1), ("Episode 2", 1), ("Episode 3", 1)],
                )
            ],
        )

    def test_timeline_summary_uses_active_days(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        records = [
            make_record(1, datetime(2026, 6, 1, 9, 0, tzinfo=tz), "Work A", "A"),
            make_record(2, datetime(2026, 6, 1, 10, 0, tzinfo=tz), "Work A", "B"),
            make_record(3, datetime(2026, 6, 3, 10, 0, tzinfo=tz), "Work B", "C"),
        ]

        summary = timeline_summary(records)

        self.assertEqual(summary["totalWords"], 3)
        self.assertEqual(summary["activeDays"], 2)
        self.assertEqual(summary["dateStart"], "2026-06-01")
        self.assertEqual(summary["dateEnd"], "2026-06-03")
        self.assertEqual(summary["maxDay"], "2026-06-01")
        self.assertEqual(summary["maxDayWords"], 2)
        self.assertEqual(summary["averagePerActiveDay"], 1.5)


if __name__ == "__main__":
    unittest.main()
