from datetime import date, timedelta
import unittest

from scripts.generate_learning_time_data import SERIES, summarize_daily


class StudyTimeAggregationTests(unittest.TestCase):
    def test_partial_week_uses_only_its_actual_days(self):
        start = date(2026, 9, 1)
        rows = [
            {"d": (start + timedelta(days=i)).isoformat(),
             "a": 60.0 if i < 7 else 120.0, "j": 0.0, "n": 0.0, "r": 0.0, "b": 0.0}
            for i in range(10)
        ]
        result = summarize_daily(rows)
        self.assertEqual([row["days"] for row in result["weeklyMinutesPerDay"]], [7, 3])
        self.assertEqual([row["a"] for row in result["weeklyMinutesPerDay"]], [1.0, 2.0])
        self.assertEqual(result["totalsSeconds"]["a"], 780.0)
        self.assertAlmostEqual(result["cumulativeHours"][-1]["a"], 780 / 3600)

    def test_recent_28_days_and_cumulative_preserve_daily_seconds(self):
        start = date(2026, 8, 1)
        rows = [
            {"d": (start + timedelta(days=i)).isoformat(),
             "a": i + 0.12345, "j": 0.0, "n": 19.23456, "r": 5.98765, "b": 0.987654}
            for i in range(35)
        ]
        result = summarize_daily(rows)
        self.assertEqual(result["recent28Days"]["start"], "2026-08-08")
        self.assertEqual(result["recent28Days"]["days"], 28)
        expected = sum(row[key] for row in rows[-28:] for key in SERIES) / 28 / 60
        self.assertAlmostEqual(result["recent28Days"]["meanMinutesPerDay"], expected)
        self.assertEqual(len(result["cumulativeHours"]), 36)
        for key in SERIES:
            for i in range(1, len(result["cumulativeHours"])):
                increment = result["cumulativeHours"][i][key] - result["cumulativeHours"][i - 1][key]
                self.assertAlmostEqual(increment * 3600, rows[i - 1][key])


if __name__ == "__main__":
    unittest.main()
