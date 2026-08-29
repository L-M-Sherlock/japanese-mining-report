from datetime import date, datetime, timedelta
import unittest

from scripts.generate_anniversary_diary import (
    MiningRecord,
    TIME_ZONE,
    allocate_anime_days,
)


def anime_record(when: datetime, source: str, position_seconds: int) -> MiningRecord:
    return MiningRecord(
        note_id=int(when.timestamp() * 1000),
        mined_at=when,
        expression="",
        sentence="",
        source=source,
        source_display=source,
        work="",
        category="anime",
        jlpt="未标注",
        position_seconds=position_seconds,
    )


class AnimeViewingTests(unittest.TestCase):
    def test_yuru_yuri_viewings_are_split_across_subtitle_cutoff(self) -> None:
        first = datetime(2026, 1, 18, 12, tzinfo=TIME_ZONE)
        second = datetime(2026, 4, 7, 12, tzinfo=TIME_ZONE)
        movie = datetime(2026, 2, 21, 12, tzinfo=TIME_ZONE)
        episode = "[VCB-Studio] Yuru Yuri [01][Ma10p_1080p][x265_flac].srt"
        records = [
            anime_record(first, episode, 100),
            anime_record(first + timedelta(minutes=20), episode, 1_300),
            anime_record(second, episode, 100),
            anime_record(second + timedelta(minutes=20), episode, 1_300),
            anime_record(
                movie,
                "Gekijouban Karakai Jouzu no Takagi-san.srt",
                4_325,
            ),
        ]

        days, summary = allocate_anime_days(
            records, date(2026, 1, 1), date(2026, 4, 30)
        )

        self.assertEqual(summary["unique_entries"], 2)
        self.assertEqual(summary["entries"], 3)
        self.assertEqual(summary["repeat_entries"], 1)
        self.assertEqual(summary["mode_entries"], {"日语字幕": 1, "无字幕": 2})
        self.assertEqual(summary["seconds"], 2 * 24 * 60 + 4_325)

        first_rows = [
            row for row in days[first.date()] if "Yuru Yuri" in row["label"]
        ]
        second_rows = [
            row for row in days[second.date()] if "Yuru Yuri" in row["label"]
        ]
        self.assertEqual({row["viewing_index"] for row in first_rows}, {1})
        self.assertEqual({row["viewing_index"] for row in second_rows}, {2})
        self.assertEqual({row["mode"] for row in first_rows}, {"日语字幕"})
        self.assertEqual({row["mode"] for row in second_rows}, {"无字幕"})
