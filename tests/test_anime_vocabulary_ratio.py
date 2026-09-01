from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from scripts.analyze_anime_vocabulary_ratio import (
    CueMatcher,
    SubtitleCue,
    normalize_subtitle_text,
    parse_subtitle_cues,
)
from scripts.generate_anniversary_diary import MiningRecord, TIME_ZONE


def mining_record(sentence: str, position: int | None) -> MiningRecord:
    return MiningRecord(
        note_id=1,
        mined_at=datetime(2026, 3, 13, 12, tzinfo=TIME_ZONE),
        expression="表达",
        sentence=sentence,
        source="Example.srt",
        source_display="Example",
        work="Example",
        category="anime",
        jlpt="未标注",
        position_seconds=position,
    )


class SubtitleParserTests(unittest.TestCase):
    def test_srt_parser_keeps_text_after_blank_line(self) -> None:
        source = """1
00:00:01,000 --> 00:00:03,000

空行の後の字幕

2
00:00:04,000 --> 00:00:05,000
次の字幕
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_text(source, encoding="utf-8")
            cues = parse_subtitle_cues(path)

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "空行の後の字幕")
        self.assertEqual(cues[1].text, "次の字幕")

    def test_ass_parser_deduplicates_exact_dialogue_events(self) -> None:
        source = """[Events]
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}字幕
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}字幕
Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ass"
            path.write_text(source, encoding="utf-8")
            cues = parse_subtitle_cues(path)

        self.assertEqual(len(cues), 1)
        self.assertEqual(normalize_subtitle_text(cues[0].text), "字幕")


class CueMatcherTests(unittest.TestCase):
    def test_exact_text_wins_over_an_adjacent_timestamp(self) -> None:
        cues = [
            SubtitleCue(10.0, 12.0, "最初の文", (1,)),
            SubtitleCue(12.1, 14.0, "含有 / 生詞", (2,)),
        ]
        match = CueMatcher(cues).match(mining_record("含有 生詞", 12))

        self.assertEqual(match.cue_index, 1)
        self.assertEqual(match.method, "exact_text")

    def test_unique_text_can_match_without_a_timestamp(self) -> None:
        cues = [SubtitleCue(10.0, 12.0, "唯一の文", (1,))]
        match = CueMatcher(cues).match(mining_record("唯一の文", None))

        self.assertEqual(match.cue_index, 0)
        self.assertEqual(match.method, "exact_text_without_position")


if __name__ == "__main__":
    unittest.main()
