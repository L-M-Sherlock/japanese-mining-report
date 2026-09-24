from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.book_media import audiobook_evidence, load_audiobook_names
from scripts.generate_anniversary_diary import load_reading_days
from scripts.visualize_lapis_sources import classify_source_category


def write_json(folder: Path, filename: str, data) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def bind_audio(folder: Path) -> None:
    write_json(folder, "sasayaki_playback.json", {"audioBookmark": "test-bookmark", "lastPosition": 99999})
    write_json(folder, "sasayaki_match.json", {"matches": [{"startTime": 1.5, "endTime": 3.0}]})


class BookMediaTests(unittest.TestCase):
    def test_requires_binding_and_valid_alignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(audiobook_evidence(folder)["status"], "no_markers")
            write_json(folder, "sasayaki_playback.json", {"audioBookmark": "test"})
            self.assertEqual(audiobook_evidence(folder)["status"], "incomplete")
            for start, end in [(3, 1), (True, 4), (0, float("inf"))]:
                write_json(folder, "sasayaki_match.json", {"matches": [{"startTime": start, "endTime": end}]})
                self.assertEqual(audiobook_evidence(folder)["category"], "novel")
            bind_audio(folder)
            self.assertEqual(audiobook_evidence(folder)["category"], "audiobook")

    def test_book_binding_classifies_generic_pasted_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = root / "ある物語"
            write_json(book, "metadata.json", {"id": "audio", "title": "ある物語"})
            bind_audio(book)
            names = load_audiobook_names(root)
            self.assertEqual(classify_source_category(
                "ある物語", "ある物語", " Hoshi ",
                sentence_audio="[sound:paste-generic.mp3]", audiobook_names=names,
            ), "audiobook")
            self.assertEqual(classify_source_category(
                "ある物語 第01話.srt", "ある物語 第01話.srt", "", audiobook_names=names,
            ), "anime")
            self.assertEqual(classify_source_category(
                "別の本", "別の本", " Hoshi ", sentence_audio="[sound:paste-other.mp3]",
            ), "novel")
            self.assertEqual(classify_source_category(
                "音源 (2m3s)", "音源", "", sentence_audio="[sound:hoshi_sasayaki_example.m4a]",
            ), "audiobook")

    def test_conflicting_titles_are_not_automatically_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for folder in (root / "audio-edition", root / "text-edition"):
                write_json(folder, "metadata.json", {"id": folder.name, "title": "同じ題名"})
            bind_audio(root / "audio-edition")
            names = load_audiobook_names(root)
            self.assertNotIn("同じ題名", names)
            self.assertIn("audio-edition", names)

    def test_time_split_conserves_total_and_ignores_playback_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio, novel = root / "audio", root / "novel"
            bind_audio(audio)
            novel.mkdir()
            books = [SimpleNamespace(id="a", folder_path=audio), SimpleNamespace(id="n", folder_path=novel)]
            stats = [SimpleNamespace(book_id="a", title="音声作品", date_key="2026-09-13", reading_time_seconds=180.5, characters_read=1000),
                     SimpleNamespace(book_id="n", title="文字作品", date_key="2026-09-13", reading_time_seconds=120.5, characters_read=500)]
            library = SimpleNamespace(books=books, stats=stats)
            module = SimpleNamespace(load_library=lambda *_: library)
            with patch("scripts.generate_anniversary_diary.import_reading_module", return_value=module):
                days, summary = load_reading_days(root, Path("unused.py"), date(2026, 9, 13), date(2026, 9, 13))
            self.assertEqual(summary["seconds"], 301.0)
            self.assertEqual(summary["categories"]["audiobook"]["seconds"], 180.5)
            self.assertEqual(summary["categories"]["novel"]["seconds"], 120.5)
            self.assertEqual(len(days[date(2026, 9, 13)]), 2)


if __name__ == "__main__":
    unittest.main()
