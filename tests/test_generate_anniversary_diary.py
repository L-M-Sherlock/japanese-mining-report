from datetime import date, datetime, timedelta
import sqlite3
import unittest

from scripts.generate_anniversary_diary import (
    HistoricalCardIdentity,
    MiningRecord,
    TIME_ZONE,
    allocate_anime_days,
    anime_edition_label,
    deck_label,
    exclude_historical_card,
    load_anki_days,
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
        self.assertEqual(summary["editions"], 2)
        self.assertEqual(summary["edition_viewings"], 3)
        self.assertEqual(summary["mode_editions"]["日语字幕"], {"Yuru Yuri"})
        self.assertEqual(
            summary["mode_editions"]["无字幕"],
            {"Yuru Yuri", "剧场版 擅长捉弄人的高木同学"},
        )

    def test_anime_edition_label_keeps_seasons_separate(self) -> None:
        self.assertEqual(
            anime_edition_label("Karakai Jouzu no Takagi-san 2 第03集"),
            "Karakai Jouzu no Takagi-san 2",
        )
        self.assertEqual(
            anime_edition_label("剧场版 擅长捉弄人的高木同学"),
            "剧场版 擅长捉弄人的高木同学",
        )


class HistoricalCardTests(unittest.TestCase):
    def identity(self, deck_name: str) -> HistoricalCardIdentity:
        return HistoricalCardIdentity(
            card_id=1,
            note_id=1,
            deck_name=deck_name,
            note_type="Example",
            template_ord=0,
            fields="",
            tags="",
            backup_name="backup.colpkg",
        )

    def test_missing_cards_are_not_assumed_to_be_jlab(self) -> None:
        self.assertEqual(deck_label(None), "历史卡（来源未恢复）")
        self.assertEqual(
            deck_label(
                "ALL\x1fLearning\x1f日本語\x1fJlab's beginner course\x1f"
                "Part 1: Listening comprehension"
            ),
            "jlab's beginner course",
        )

    def test_confirmed_english_history_is_excluded(self) -> None:
        bitcoin = self.identity(
            "michaelnielsen.org - How the bitcoin protocol actually works"
        )
        jlab = self.identity(
            "ALL\x1fLearning\x1f日本語\x1fJlab's beginner course"
        )
        self.assertTrue(exclude_historical_card(bitcoin))
        self.assertFalse(exclude_historical_card(jlab))
        self.assertFalse(exclude_historical_card(None))

    def test_load_anki_days_filters_confirmed_english_history(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            create table revlog (id integer, cid integer, time integer);
            create table cards (id integer, did integer);
            create table decks (id integer, name text);
            """
        )
        first = int(datetime(2025, 8, 29, 12, tzinfo=TIME_ZONE).timestamp() * 1000)
        conn.executemany(
            "insert into revlog (id, cid, time) values (?, ?, ?)",
            [(first, 1, 10_000), (first + 1, 2, 20_000)],
        )
        bitcoin = self.identity(
            "michaelnielsen.org - How the bitcoin protocol actually works"
        )
        unknown = HistoricalCardIdentity(
            card_id=2,
            note_id=2,
            deck_name="",
            note_type="",
            template_ord=0,
            fields="",
            tags="",
            backup_name="backup.colpkg",
        )

        days, summary = load_anki_days(
            conn,
            date(2025, 8, 29),
            date(2025, 8, 29),
            {1: bitcoin, 2: unknown},
        )
        conn.close()

        self.assertEqual(summary["review_count"], 1)
        self.assertEqual(summary["first_review_total"], 1)
        self.assertEqual(summary["excluded_cards"], 1)
        self.assertEqual(summary["excluded_review_count"], 1)
        self.assertEqual(days[date(2025, 8, 29)]["first_cards"], {"历史卡（来源未恢复）": 1})
