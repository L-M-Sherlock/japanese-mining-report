"""Identify books associated with Sasayaki audio, without inferring listening time."""

from __future__ import annotations

import json
import math
import re
import unicodedata
import warnings
from collections import defaultdict
from pathlib import Path


def normalized_book_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (OSError, UnicodeError, ValueError) as error:
        warnings.warn(f"Cannot inspect book metadata {path}: {error}", stacklevel=2)
        return {}


def audiobook_evidence(book_dir: Path) -> dict:
    playback_path = book_dir / "sasayaki_playback.json"
    match_path = book_dir / "sasayaki_match.json"
    playback = read_object(playback_path)
    alignment = read_object(match_path)
    bookmark = playback.get("audioBookmark")
    audio_bound = isinstance(bookmark, str) and bool(bookmark.strip())
    matches = alignment.get("matches", [])
    valid_count = 0
    for item in matches if isinstance(matches, list) else []:
        if not isinstance(item, dict):
            continue
        start, end = item.get("startTime"), item.get("endTime")
        if (type(start) in (int, float) and type(end) in (int, float)
                and math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
            valid_count += 1
    status = "confirmed" if audio_bound and valid_count else (
        "incomplete" if playback_path.exists() or match_path.exists() else "no_markers"
    )
    return {
        "category": "audiobook" if status == "confirmed" else "novel",
        "status": status,
        "audioBound": audio_bound,
        "validMatches": valid_count,
    }


def load_audiobook_names(books_dir: Path) -> frozenset[str]:
    """Exact book-title/folder aliases; reject names shared with a text-only book."""
    candidates: dict[str, set[bool]] = defaultdict(set)
    for path in books_dir.expanduser().glob("*/metadata.json"):
        metadata = read_object(path)
        evidence = audiobook_evidence(path.parent)
        is_audio = evidence["category"] == "audiobook"
        for name in (metadata.get("title"), metadata.get("renamedTitle"), path.parent.name):
            if isinstance(name, str) and name.strip():
                candidates[normalized_book_name(name)].add(is_audio)
    return frozenset(name for name, states in candidates.items() if states == {True})


def is_audiobook_source(source: str, sentence_audio: str, names: frozenset[str]) -> bool:
    if re.search(r"\[sound:hoshi_sasayaki_[^\]]+\]", sentence_audio, re.IGNORECASE):
        return True
    return normalized_book_name(source) in names
