from __future__ import annotations

import subprocess
import tempfile
import unittest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.visualize_lapis_sources import (
    Record,
    aggregate_timeline_counts,
    before_date_counts,
    build_pages_branch_name,
    build_period_labels,
    classify_source_category,
    collection_day_start_hour,
    infer_profile_name_from_db,
    mined_datetime_from_note_id,
    normalize_novel_work_label,
    parse_before_date_cutoff,
    publish_reports_to_pages,
    timeline_summary,
    unique_note_records,
    validate_pages_branch_name,
)


def make_record(
    note_id: int,
    mined_at: datetime,
    work: str,
    source: str,
    *,
    card_id: int | None = None,
    day_start_hour: int = 4,
) -> Record:
    mined_date, mined_week, mined_month = build_period_labels(
        mined_at, day_start_hour
    )
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


def run_git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class TimelineTests(unittest.TestCase):
    def test_classify_source_category(self) -> None:
        self.assertEqual(
            classify_source_category(
                "週に一度クラスメイトを買う話",
                "週に一度クラスメイトを買う話",
                "Hoshi",
            ),
            "novel",
        )
        self.assertEqual(
            classify_source_category(
                "Karakai Jouzu no Takagi-san 第01話.srt (2m21s)",
                "Karakai Jouzu no Takagi-san 第01話.srt",
                "",
            ),
            "anime",
        )
        self.assertEqual(
            classify_source_category("Citrus [01].ass", "Citrus [01].ass", ""),
            "anime",
        )
        self.assertEqual(
            classify_source_category("#about＆rules", "#about＆rules", ""), "other"
        )

    def test_normalize_novel_work_label_merges_volumes(self) -> None:
        self.assertEqual(
            normalize_novel_work_label(
                "週に一度クラスメイトを買う話２ ～ふたりの時間、言い訳の五千円～"
            ),
            "週に一度クラスメイトを買う話 ～ふたりの時間、言い訳の五千円～",
        )
        self.assertEqual(
            normalize_novel_work_label("好きな子のいもうと２"),
            "好きな子のいもうと",
        )
        self.assertEqual(
            normalize_novel_work_label("クラスの姫は私のわんこ２"),
            "クラスの姫は私のわんこ",
        )

    def test_collection_day_start_hour_reads_anki_rollover(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "create table config (key text not null primary key, usn integer not null, mtime_secs integer not null, val blob not null) without rowid"
            )
            conn.execute(
                "insert into config (key, usn, mtime_secs, val) values (?, 0, 0, ?)",
                ("rollover", b"5"),
            )

            self.assertEqual(collection_day_start_hour(conn), 5)
        finally:
            conn.close()

    def test_note_id_uses_requested_timezone_and_day_start(self) -> None:
        note_time = datetime(2026, 1, 4, 17, 0, tzinfo=timezone.utc)
        note_id = int(note_time.timestamp() * 1000)

        mined_at = mined_datetime_from_note_id(note_id, ZoneInfo("Asia/Shanghai"))
        mined_date, mined_week, mined_month = build_period_labels(mined_at)

        self.assertEqual(mined_date, "2026-01-04")
        self.assertEqual(mined_week, "2026-W01")
        self.assertEqual(mined_month, "2026-01")

    def test_four_am_starts_new_mining_day(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")

        before_four = build_period_labels(datetime(2026, 6, 12, 3, 59, tzinfo=tz))
        at_four = build_period_labels(datetime(2026, 6, 12, 4, 0, tzinfo=tz))

        self.assertEqual(before_four[0], "2026-06-11")
        self.assertEqual(at_four[0], "2026-06-12")

    def test_before_date_cutoff_uses_day_start_hour(self) -> None:
        cutoff = parse_before_date_cutoff("2026-06-12", ZoneInfo("Asia/Shanghai"), 5)

        self.assertEqual(cutoff.isoformat(), "2026-06-12T05:00:00+08:00")

    def test_before_date_counts_mined_and_studied_cards(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        cutoff = parse_before_date_cutoff("2026-06-12", tz, 5)
        records = [
            make_record(1, datetime(2026, 6, 11, 10, 0, tzinfo=tz), "Work A", "A"),
            make_record(2, datetime(2026, 6, 12, 4, 59, tzinfo=tz), "Work A", "B"),
            make_record(3, datetime(2026, 6, 12, 5, 0, tzinfo=tz), "Work B", "C"),
        ]
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "create table revlog (id integer primary key, cid integer not null)"
            )
            conn.executemany(
                "insert into revlog (id, cid) values (?, ?)",
                [
                    (int(datetime(2026, 6, 11, 12, 0, tzinfo=tz).timestamp() * 1000), 1),
                    (int(datetime(2026, 6, 12, 6, 0, tzinfo=tz).timestamp() * 1000), 2),
                    (int(datetime(2026, 6, 11, 12, 0, tzinfo=tz).timestamp() * 1000) + 1, 99),
                ],
            )

            self.assertEqual(before_date_counts(conn, records, cutoff), (2, 1))
        finally:
            conn.close()

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


class PublishPagesTests(unittest.TestCase):
    def test_profile_branch_name_defaults(self) -> None:
        db_path = Path(
            "/Users/example/Library/Application Support/Anki2/JarrettYe/collection.anki2"
        )

        profile_name = infer_profile_name_from_db(db_path)
        branch_name = build_pages_branch_name(profile_name)

        self.assertEqual(profile_name, "JarrettYe")
        self.assertEqual(branch_name, "reports/JarrettYe")
        self.assertEqual(validate_pages_branch_name(branch_name), branch_name)
        with self.assertRaises(SystemExit):
            validate_pages_branch_name("reports/Bad Profile")

    def test_publish_reports_to_pages_pushes_static_branch_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote_repo = root / "remote.git"
            source_repo = root / "source"
            source_repo.mkdir()
            run_git(["init", "--bare", str(remote_repo)], root)
            run_git(["init"], source_repo)
            run_git(["checkout", "-b", "main"], source_repo)
            run_git(["config", "user.name", "Test User"], source_repo)
            run_git(["config", "user.email", "test@example.invalid"], source_repo)
            (source_repo / "README.md").write_text("source repo\n", encoding="utf-8")
            run_git(["add", "README.md"], source_repo)
            run_git(["commit", "-m", "Initial commit"], source_repo)
            run_git(["remote", "add", "origin", str(remote_repo)], source_repo)

            source_report = root / "lapis_source_report.html"
            timeline_report = root / "lapis_mining_timeline_report.html"
            source_report.write_text("<h1>source</h1>\n", encoding="utf-8")
            timeline_report.write_text("<h1>timeline</h1>\n", encoding="utf-8")

            branch = publish_reports_to_pages(
                source_report=source_report,
                timeline_report=timeline_report,
                db_path=Path("/Anki2/JarrettYe/collection.anki2"),
                remote="origin",
                repo_dir=source_repo,
            )

            self.assertEqual(branch, "reports/JarrettYe")
            self.assertEqual(run_git(["branch", "--show-current"], source_repo), "main")
            files = run_git(
                [
                    "--git-dir",
                    str(remote_repo),
                    "ls-tree",
                    "-r",
                    "--name-only",
                    "reports/JarrettYe",
                ],
                root,
            ).splitlines()
            self.assertEqual(
                files,
                [
                    "index.html",
                    "lapis_mining_timeline_report.html",
                    "lapis_source_report.html",
                ],
            )
            index_html = run_git(
                [
                    "--git-dir",
                    str(remote_repo),
                    "show",
                    "reports/JarrettYe:index.html",
                ],
                root,
            )
            self.assertIn("Timeline report", index_html)
            self.assertIn("Source report", index_html)


if __name__ == "__main__":
    unittest.main()
