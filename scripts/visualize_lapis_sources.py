#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BR_RE = re.compile(r"<br\s*/?>", flags=re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
TIMESTAMP_RE = re.compile(r"\s*\((?:\d+h)?\d+m\d+s\)\s*$")
URL_RE = re.compile(r"https?://\S+")
SUBTITLE_SOURCE_RE = re.compile(
    r"\.(?:srt|ass)(?:\s*\((?:\d+h)?\d+m\d+s\))?\s*$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class Record:
    card_id: int
    note_id: int
    mined_at: datetime
    mined_date: str
    mined_week: str
    mined_month: str
    source: str
    work: str
    domain: str
    url: str
    deck: str
    studied: bool
    source_category: str = "other"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Visualize where cards in an Anki Lapis note type come from by "
            "reading the MiscInfo field."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "Path to the Anki collection database. If omitted, the script checks "
            "ANKI_COLLECTION_PATH, then ./collection.anki2."
        ),
    )
    parser.add_argument(
        "--note-type",
        default="Lapis",
        help="Exact note type name to inspect. Default: %(default)s",
    )
    parser.add_argument(
        "--field",
        default="MiscInfo",
        help="Field name that stores source info. Default: %(default)s",
    )
    parser.add_argument(
        "--deck-contains",
        default="",
        help="Only include cards whose deck name contains this text.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="How many items to show in the summary charts. Default: %(default)s",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/lapis_source_report.html"),
        help="Output HTML report path. Default: %(default)s",
    )
    parser.add_argument(
        "--timeline-output",
        type=Path,
        default=Path("output/lapis_mining_timeline_report.html"),
        help="Output HTML timeline report path. Default: %(default)s",
    )
    parser.add_argument(
        "--timezone",
        default="",
        help=(
            "Timezone for grouping note creation dates, for example Asia/Shanghai. "
            "Defaults to the system local timezone."
        ),
    )
    parser.add_argument(
        "--day-start-hour",
        type=int,
        default=None,
        help=(
            "Override the hour when a mining day starts in the selected timezone. "
            "Defaults to Anki's collection rollover setting, falling back to 4."
        ),
    )
    return parser.parse_args()


def register_unicase(conn: sqlite3.Connection) -> None:
    def unicase(a: str, b: str) -> int:
        left = (a or "").casefold()
        right = (b or "").casefold()
        return (left > right) - (left < right)

    conn.create_collation("unicase", unicase)


def make_db_snapshot(source_db: Path) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp_dir = tempfile.TemporaryDirectory(prefix="anki-lapis-snapshot-")
    snapshot_db = Path(temp_dir.name) / source_db.name
    shutil.copy2(source_db, snapshot_db)

    for suffix in ("-wal", "-shm"):
        sidecar = source_db.with_name(source_db.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, snapshot_db.with_name(snapshot_db.name + suffix))

    return temp_dir, snapshot_db


def resolve_db_path(db_arg: Path | None) -> Path:
    if db_arg is not None:
        db_path = db_arg.expanduser().resolve()
        if not db_path.exists():
            raise SystemExit(f"Database file not found: {db_path}")
        return db_path

    env_db = os.environ.get("ANKI_COLLECTION_PATH", "").strip()
    if env_db:
        db_path = Path(env_db).expanduser().resolve()
        if not db_path.exists():
            raise SystemExit(
                "ANKI_COLLECTION_PATH is set but does not point to an existing "
                f"database: {db_path}"
            )
        return db_path

    local_db = Path("collection.anki2").resolve()
    if local_db.exists():
        return local_db

    raise SystemExit(
        "Could not find collection.anki2. Pass --db explicitly, set "
        "ANKI_COLLECTION_PATH, or run the script from a directory containing "
        "the database."
    )


def find_note_type_id(conn: sqlite3.Connection, note_type_name: str) -> int:
    rows = conn.execute("select id, name from notetypes").fetchall()
    for note_type_id, name in rows:
        if name.casefold() == note_type_name.casefold():
            return int(note_type_id)

    available = ", ".join(sorted(name for _, name in rows))
    raise SystemExit(
        f"Could not find note type {note_type_name!r}. Available note types: {available}"
    )


def find_field_ord(conn: sqlite3.Connection, note_type_id: int, field_name: str) -> int:
    rows = conn.execute(
        "select ord, name from fields where ntid=? order by ord", (note_type_id,)
    ).fetchall()
    for ord_, name in rows:
        if name.casefold() == field_name.casefold():
            return int(ord_)

    available = ", ".join(name for _, name in rows)
    raise SystemExit(
        f"Could not find field {field_name!r} on note type {note_type_id}. "
        f"Available fields: {available}"
    )


def strip_misc_html(raw: str) -> list[str]:
    text = html.unescape(raw or "")
    text = BR_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_source_label(lines: list[str]) -> str:
    if not lines:
        return "(empty MiscInfo)"
    first = TIMESTAMP_RE.sub("", lines[0]).strip()
    return normalize_spaces(first) or "(empty MiscInfo)"


def extract_url(lines: list[str]) -> str:
    for line in lines:
        match = URL_RE.search(line)
        if match:
            return match.group(0)
    return ""


def extract_domain(url: str) -> str:
    if not url:
        return "(no url)"
    domain = urlparse(url).netloc.casefold()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or "(no url)"


def normalize_spaces(text: str) -> str:
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def guess_work_label(source: str) -> str:
    work = normalize_spaces(source)

    if " | " in work:
        parts = [part.strip() for part in work.split(" | ") if part.strip()]
        if len(parts) >= 2:
            work = parts[-2]
        elif parts:
            work = parts[0]

    web_novel_match = re.match(r"^第\d+[話章回]\s*-\s*(.*?)\s*-\s*[^-]+$", work)
    if web_novel_match and web_novel_match.group(1).strip():
        work = web_novel_match.group(1).strip()

    work = re.sub(r"^\[[^\]]+\]\s*", "", work)
    work = re.sub(r"\.(srt|ass|ssa|vtt|mkv|mp4|webm|html?)$", "", work, flags=re.I)

    for pattern in (
        r"^(.*?)(?:[\s._-]+S\d+\s*E\d+\b.*)$",
        r"^(.*?)(?:\s+-\s+\d+\b.*)$",
        r"^(.*?)(?:\s*\[\d+\].*)$",
        r"^(.*?)(?:\s+第\d+[話章回].*)$",
        r"^(.*?)(?:\s+\d+\s+\(.*)$",
        r"^(.*?)(?:\s+\(\d+\).*)$",
    ):
        match = re.match(pattern, work, flags=re.I)
        if match and match.group(1).strip():
            work = match.group(1).strip(" -._")
            break

    work = re.sub(r"^第\d+[話章回]\s*[-:：]?\s*", "", work)
    work = re.sub(r"\s+\([^)]*\)$", "", work)
    work = normalize_spaces(work).strip(" -._")
    return work or source


def normalize_novel_work_label(work: str) -> str:
    work = normalize_spaces(work)
    work = re.sub(r"\s+\([^)]*\)$", "", work)
    work = re.sub(r"(?<=[^\s0-9０-９])\s*[0-9０-９]+(?=\s*(?:[～〜]|$))", "", work)
    return normalize_spaces(work).strip(" -._") or work


def has_hoshi_tag(tags: str) -> bool:
    return any(tag.casefold() == "hoshi" for tag in tags.split())


def classify_source_category(raw_source: str, cleaned_source: str, tags: str) -> str:
    if has_hoshi_tag(tags):
        return "novel"
    if TIMESTAMP_RE.search(raw_source) or SUBTITLE_SOURCE_RE.search(cleaned_source):
        return "anime"
    return "other"


def resolve_timezone(timezone_name: str):
    timezone_name = timezone_name.strip()
    if not timezone_name:
        return datetime.now().astimezone().tzinfo

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise SystemExit(f"Unknown timezone: {timezone_name}") from error


def decode_config_value(raw):
    if raw is None:
        return None
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def collection_config_value(conn: sqlite3.Connection, key: str):
    try:
        row = conn.execute("select val from config where key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        return decode_config_value(row[0])

    row = conn.execute("select conf from col limit 1").fetchone()
    if row is None:
        return None
    conf = decode_config_value(row[0])
    if isinstance(conf, dict):
        return conf.get(key)
    return None


def validate_day_start_hour(value: int) -> int:
    if not 0 <= value <= 23:
        raise SystemExit("--day-start-hour must be between 0 and 23.")
    return value


def collection_day_start_hour(conn: sqlite3.Connection, fallback: int = 4) -> int:
    value = collection_config_value(conn, "rollover")
    if value is None:
        return fallback
    try:
        return validate_day_start_hour(int(value))
    except (TypeError, ValueError) as error:
        raise SystemExit(f"Invalid Anki rollover value: {value!r}") from error


def mined_datetime_from_note_id(note_id: int, time_zone) -> datetime:
    return datetime.fromtimestamp(note_id / 1000, time_zone)


def build_period_labels(
    mined_at: datetime, day_start_hour: int = 4
) -> tuple[str, str, str]:
    mining_day_at = mined_at - timedelta(hours=day_start_hour)
    mined_day = mining_day_at.date()
    iso_year, iso_week, _ = mined_day.isocalendar()
    return (
        mined_day.isoformat(),
        f"{iso_year}-W{iso_week:02d}",
        f"{mined_day.year}-{mined_day.month:02d}",
    )


def load_records(
    conn: sqlite3.Connection,
    note_type_id: int,
    field_ord: int,
    deck_contains: str,
    time_zone,
    day_start_hour: int = 4,
) -> list[Record]:
    deck_filter = deck_contains.casefold().strip()
    records: list[Record] = []

    rows = conn.execute(
        """
        select c.id, n.id, d.name, n.flds, c.reps, n.tags
        from cards c
        join notes n on n.id = c.nid
        join decks d on d.id = c.did
        where n.mid=?
        order by n.id, c.id
        """,
        (note_type_id,),
    )

    for card_id, note_id, deck_name, flds, reps, tags in rows:
        if deck_filter and deck_filter not in deck_name.casefold():
            continue

        mined_at = mined_datetime_from_note_id(int(note_id), time_zone)
        mined_date, mined_week, mined_month = build_period_labels(
            mined_at, day_start_hour
        )
        fields = flds.split("\x1f")
        raw_misc = fields[field_ord] if len(fields) > field_ord else ""
        lines = strip_misc_html(raw_misc)
        raw_source = lines[0] if lines else ""
        source = extract_source_label(lines)
        url = extract_url(lines)
        domain = extract_domain(url)
        source_category = classify_source_category(raw_source, source, tags or "")
        work = guess_work_label(source)
        if source_category == "novel":
            work = normalize_novel_work_label(work)

        records.append(
            Record(
                card_id=int(card_id),
                note_id=int(note_id),
                mined_at=mined_at,
                mined_date=mined_date,
                mined_week=mined_week,
                mined_month=mined_month,
                source=source,
                work=work,
                domain=domain,
                url=url,
                deck=deck_name,
                studied=reps > 0,
                source_category=source_category,
            )
        )

    return records


def build_chart_rows(counter: Counter[str], top_n: int) -> list[tuple[str, int]]:
    return counter.most_common(top_n)


def build_progress_rows(
    records: list[Record], key_fn, top_n: int
) -> list[tuple[str, int, int]]:
    total_counts: Counter[str] = Counter()
    studied_counts: Counter[str] = Counter()

    for record in records:
        key = key_fn(record)
        total_counts[key] += 1
        if record.studied:
            studied_counts[key] += 1

    return [
        (label, total, studied_counts[label])
        for label, total in total_counts.most_common(top_n)
    ]


def unique_note_records(records: list[Record]) -> list[Record]:
    seen_note_ids: set[int] = set()
    unique_records: list[Record] = []
    for record in records:
        if record.note_id in seen_note_ids:
            continue
        seen_note_ids.add(record.note_id)
        unique_records.append(record)
    return unique_records


def aggregate_timeline_counts(
    records: list[Record], period: str, source_grain: str
) -> list[tuple[str, int, list[tuple[str, int]]]]:
    period_attrs = {
        "day": "mined_date",
        "week": "mined_week",
        "month": "mined_month",
    }
    source_attrs = {
        "work": "work",
        "source": "source",
    }
    period_attr = period_attrs[period]
    source_attr = source_attrs[source_grain]
    buckets: dict[str, Counter[str]] = defaultdict(Counter)

    for record in records:
        buckets[getattr(record, period_attr)][getattr(record, source_attr)] += 1

    return [
        (period_label, sum(counter.values()), counter.most_common())
        for period_label, counter in sorted(buckets.items())
    ]


def timeline_summary(records: list[Record]) -> dict[str, object]:
    if not records:
        return {
            "totalWords": 0,
            "activeDays": 0,
            "dateStart": "",
            "dateEnd": "",
            "averagePerActiveDay": 0,
            "maxDay": "",
            "maxDayWords": 0,
        }

    daily_rows = aggregate_timeline_counts(records, "day", "work")
    max_day, max_day_words, _ = max(daily_rows, key=lambda row: row[1])
    total_words = len(records)
    active_days = len(daily_rows)
    dates = [record.mined_date for record in records]
    return {
        "totalWords": total_words,
        "activeDays": active_days,
        "dateStart": min(dates),
        "dateEnd": max(dates),
        "averagePerActiveDay": total_words / active_days if active_days else 0,
        "maxDay": max_day,
        "maxDayWords": max_day_words,
    }


def build_timeline_payload(records: list[Record], time_zone_label: str) -> dict[str, object]:
    timeline_records = unique_note_records(records)
    return {
        "timezone": time_zone_label,
        "summary": timeline_summary(timeline_records),
        "records": [
            {
                "date": record.mined_date,
                "week": record.mined_week,
                "month": record.mined_month,
                "work": record.work,
                "source": record.source,
                "category": record.source_category,
            }
            for record in timeline_records
        ],
    }


def safe_json_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def timezone_display_name(time_zone) -> str:
    return str(getattr(time_zone, "key", None) or time_zone)


def render_bilingual(en: str, zh: str, *, escape_text: bool = True) -> str:
    if escape_text:
        en = html.escape(en)
        zh = html.escape(zh)
    return (
        f"<span class='lang lang-en'>{en}</span>"
        f"<span class='lang lang-zh'>{zh}</span>"
    )


def render_bar_rows(
    rows: list[tuple[str, int] | tuple[str, int, int]],
    left_header: str = "Label",
    left_header_zh: str = "标签",
    right_header: str = "Studied / mined",
    right_header_zh: str = "已学 / 挖卡",
) -> str:
    if not rows:
        return f"<p class='muted'>{render_bilingual('No data.', '没有数据。')}</p>"

    max_count = rows[0][1]
    pieces = [
        "<div class='bars'>",
        (
            "<div class='bar-row bar-row-header'>"
            f"<div class='bar-header-label'>{render_bilingual(left_header, left_header_zh)}</div>"
            f"<div class='bar-header-progress'>{render_bilingual('Progress', '进度')}</div>"
            f"<div class='bar-header-value'>{render_bilingual(right_header, right_header_zh)}</div>"
            "</div>"
        ),
    ]
    for row in rows:
        label = row[0]
        count = row[1]
        studied = row[2] if len(row) > 2 else None
        width = (count / max_count) * 100 if max_count else 0
        if studied is not None and count:
            value_text = f"{studied:,}/{count:,} ({studied / count * 100:.1f}%)"
        elif studied is not None:
            value_text = f"{studied:,}/{count:,} (0.0%)"
        else:
            value_text = f"{count:,}"
        if studied is not None and count:
            studied_width = (studied / count) * 100
            remaining_width = 100 - studied_width
            fill_html = (
                f"<div class='bar-stack' style='width:{width:.2f}%'>"
                f"<div class='bar-fill bar-fill-studied' style='width:{studied_width:.2f}%'></div>"
                f"<div class='bar-fill bar-fill-unstudied' style='width:{remaining_width:.2f}%'></div>"
                "</div>"
            )
        else:
            fill_html = (
                f"<div class='bar-stack' style='width:{width:.2f}%'>"
                "<div class='bar-fill bar-fill-total' style='width:100%'></div>"
                "</div>"
            )
        pieces.append(
            "".join(
                [
                    "<div class='bar-row'>",
                    f"<div class='bar-label' title='{html.escape(label)}'>{html.escape(label)}</div>",
                    "<div class='bar-track'>",
                    fill_html,
                    "</div>",
                    f"<div class='bar-value'>{value_text}</div>",
                    "</div>",
                ]
            )
        )
    pieces.append("</div>")
    return "\n".join(pieces)


def render_summary_card(
    label_en: str,
    label_zh: str,
    value: str,
    tip_en: str = "",
    tip_zh: str = "",
) -> str:
    attrs = ""
    if tip_en or tip_zh:
        tip_en = tip_en or tip_zh
        tip_zh = tip_zh or tip_en
        attrs = (
            f" data-tip-en='{html.escape(tip_en, quote=True)}'"
            f" data-tip-zh='{html.escape(tip_zh, quote=True)}'"
            f" title='{html.escape(f'{tip_en} / {tip_zh}', quote=True)}'"
            " tabindex='0'"
        )
    return (
        f"<div class='summary-card'{attrs}>"
        f"<div class='summary-value'>{html.escape(value)}</div>"
        f"<div class='summary-label'>{render_bilingual(label_en, label_zh)}</div>"
        "</div>"
    )


def render_table(records: list[Record], total_cards: int) -> str:
    source_counts = Counter(record.source for record in records)
    source_meta: dict[str, Record] = {}
    for record in records:
        source_meta.setdefault(record.source, record)

    rows = []
    for source, count in source_counts.most_common():
        record = source_meta[source]
        share = count / total_cards if total_cards else 0
        search_blob = " ".join(
            part.casefold() for part in (record.source, record.work)
        )
        rows.append(
            "".join(
                [
                    f"<tr data-search='{html.escape(search_blob)}'>",
                    (
                        f"<td data-label='Source' data-label-en='Source' "
                        f"data-label-zh='来源'>{html.escape(record.source)}</td>"
                    ),
                    (
                        f"<td data-label='Work / material (heuristic)' "
                        f"data-label-en='Work / material (heuristic)' "
                        f"data-label-zh='作品 / 材料（启发式）'>{html.escape(record.work)}</td>"
                    ),
                    (
                        f"<td data-label='Cards' data-label-en='Cards' "
                        f"data-label-zh='卡片'>{count}</td>"
                    ),
                    (
                        f"<td data-label='Share' data-label-en='Share' "
                        f"data-label-zh='占比'>{share * 100:.2f}%</td>"
                    ),
                    "</tr>",
                ]
            )
        )

    return "\n".join(
        [
            "<div class='table-toolbar'>",
            (
                "<input id='sourceFilter' type='search' "
                "placeholder='Filter by source or work / material' "
                "data-placeholder-en='Filter by source or work / material' "
                "data-placeholder-zh='按来源或作品 / 材料筛选'>"
            ),
            "</div>",
            "<table id='sourceTable'>",
            (
                "<thead><tr>"
                f"<th>{render_bilingual('Source', '来源')}</th>"
                f"<th>{render_bilingual('Work / material (heuristic)', '作品 / 材料（启发式）')}</th>"
                f"<th>{render_bilingual('Cards', '卡片')}</th>"
                f"<th>{render_bilingual('Share', '占比')}</th>"
                "</tr></thead>"
            ),
            "<tbody>",
            *rows,
            "</tbody>",
            "</table>",
        ]
    )


def render_timeline_html(
    records: list[Record],
    note_type_name: str,
    deck_contains: str,
    time_zone_label: str,
    day_start_hour: int,
) -> str:
    payload = build_timeline_payload(records, time_zone_label)
    summary = payload["summary"]
    total_words = int(summary["totalWords"])
    active_days = int(summary["activeDays"])
    average_per_active_day = float(summary["averagePerActiveDay"])
    max_day = str(summary["maxDay"])
    max_day_words = int(summary["maxDayWords"])
    date_start = str(summary["dateStart"])
    date_end = str(summary["dateEnd"])
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    deck_scope_en = deck_contains or "(all decks with matching note type)"
    deck_scope_zh = deck_contains or "（匹配该笔记类型的全部牌组）"
    note_type_escaped = html.escape(note_type_name)
    data_json = safe_json_script(payload)

    summary_cards = "\n".join(
        [
            render_summary_card(
                "Mined words",
                "挖词数",
                f"{total_words:,}",
                "Unique Lapis notes in the current filters. One note counts as one mined word.",
                "当前筛选范围内的唯一 Lapis 笔记数。一条笔记算一个挖词。",
            ),
            render_summary_card(
                "Active mining days",
                "挖词天数",
                f"{active_days:,}",
                "Number of mining days with at least one mined word. The day boundary follows Anki's rollover setting.",
                "至少挖过 1 个词的挖词日数量。换日边界使用 Anki 的 rollover 设置。",
            ),
            render_summary_card(
                "Average / active day",
                "活跃日均",
                f"{average_per_active_day:.1f}",
                "Mined words divided by active mining days. Days with zero mined words are excluded.",
                "挖词数除以挖词天数。完全没有挖词的日期不计入分母。",
            ),
            render_summary_card(
                "Best day",
                "最高单日",
                f"{max_day_words:,} · {html.escape(max_day)}",
                "Mining day with the highest mined-word count after timezone and rollover adjustment.",
                "按时区和换日时间调整后，挖词数最高的挖词日。",
            ),
            render_summary_card(
                "Date range",
                "日期范围",
                f"{date_start} - {date_end}",
                "Earliest to latest mining day after timezone and Anki rollover adjustment.",
                "按时区和 Anki 换日时间调整后的最早到最晚挖词日。",
            ),
        ]
    )

    css = """
    :root {
      --bg: #f5f1e9;
      --card: #fffdf8;
      --ink: #1c1f22;
      --muted: #656d72;
      --line: #d9d2c7;
      --accent: #2f7f77;
      --accent-strong: #155d59;
      --accent-soft: #d7ece7;
      --warm: #c7793f;
      --bar-bg: #ebe4d8;
    }
    html[data-lang="en"] .lang-zh {
      display: none;
    }
    html[data-lang="zh"] .lang-en {
      display: none;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Noto Serif CJK SC", serif;
      background: linear-gradient(180deg, #fbf7ee 0%, var(--bg) 42%, #eef4f2 100%);
      color: var(--ink);
    }
    .wrap {
      max-width: 1320px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }
    h1, h2 {
      margin: 0 0 12px;
      line-height: 1.1;
      font-weight: 700;
    }
    h1 {
      font-size: clamp(2rem, 3vw, 3rem);
    }
    h2 {
      font-size: 1.25rem;
    }
    p {
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.5;
    }
    .hero,
    .panel,
    .summary-card {
      background: rgba(255, 253, 248, 0.9);
      border: 1px solid var(--line);
      box-shadow: 0 12px 30px rgba(28, 31, 34, 0.05);
    }
    .hero {
      padding: 24px;
      border-radius: 18px;
    }
    .hero-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      flex-wrap: wrap;
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .link-button,
    .lang-toggle {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      background: #f1eadf;
      border-radius: 999px;
    }
    .link-button {
      min-height: 38px;
      padding: 0 14px;
      color: var(--accent-strong);
      text-decoration: none;
      font-weight: 600;
    }
    .lang-toggle {
      gap: 4px;
      padding: 4px;
    }
    .lang-button,
    .segmented-button {
      border: none;
      background: transparent;
      color: var(--muted);
      padding: 6px 10px;
      border-radius: 999px;
      font: inherit;
      cursor: pointer;
    }
    .lang-button.active,
    .segmented-button.active {
      background: var(--card);
      color: var(--ink);
      box-shadow: 0 2px 10px rgba(28, 31, 34, 0.08);
    }
    .meta {
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 0.95rem;
    }
    .chip {
      background: #f1eadf;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 12px;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin: 20px 0;
    }
    .summary-card {
      border-radius: 14px;
      padding: 18px;
      min-height: 106px;
      position: relative;
    }
    .summary-card[data-tip-en] {
      cursor: help;
    }
    .summary-card[data-tip-en]::after {
      content: attr(data-tip-en);
      position: absolute;
      left: 16px;
      right: 16px;
      top: calc(100% - 8px);
      z-index: 30;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.98);
      box-shadow: 0 10px 26px rgba(28, 31, 34, 0.14);
      color: var(--ink);
      font-size: 0.86rem;
      line-height: 1.45;
      opacity: 0;
      visibility: hidden;
      transform: translateY(-4px);
      transition: opacity 120ms ease, transform 120ms ease, visibility 120ms ease;
      pointer-events: none;
    }
    html[data-lang="zh"] .summary-card[data-tip-zh]::after {
      content: attr(data-tip-zh);
    }
    .summary-card[data-tip-en]:hover::after,
    .summary-card[data-tip-en]:focus::after {
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
    }
    .summary-card[data-tip-en]:focus {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .summary-value {
      font-size: 1.55rem;
      font-weight: 700;
      margin-bottom: 6px;
      overflow-wrap: anywhere;
    }
    .summary-label {
      color: var(--muted);
    }
    .panel {
      border-radius: 14px;
      padding: 20px;
      margin-top: 18px;
    }
    .controls {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      align-items: end;
    }
    .control-label {
      display: block;
      color: var(--muted);
      font-size: 0.84rem;
      font-weight: 600;
      margin-bottom: 6px;
    }
    .segmented {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--line);
      background: #f1eadf;
      border-radius: 999px;
    }
    input[type="search"] {
      width: 100%;
      min-height: 42px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fffdf9;
      font: inherit;
    }
    .chart-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      margin-top: 18px;
    }
    .chart-host {
      margin-top: 8px;
    }
    .axis-chart {
      --plot-height: 190px;
      --label-height: 72px;
      display: grid;
      grid-template-columns: 20px 42px minmax(0, 1fr);
      grid-template-rows: auto auto;
      column-gap: 8px;
      align-items: start;
    }
    .y-axis-title {
      grid-column: 1;
      grid-row: 1;
      height: calc(8px + var(--plot-height));
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 600;
      writing-mode: vertical-rl;
      transform: rotate(180deg);
      white-space: nowrap;
    }
    html[data-lang="zh"] .y-axis-title {
      text-orientation: upright;
      line-height: 1;
      transform: none;
    }
    .y-axis-ticks {
      grid-column: 2;
      grid-row: 1;
      height: calc(8px + var(--plot-height) + var(--label-height));
      position: relative;
      color: var(--muted);
      font-size: 0.76rem;
      font-variant-numeric: tabular-nums;
    }
    .y-tick {
      position: absolute;
      right: 0;
      transform: translateY(-50%);
      white-space: nowrap;
    }
    .y-tick-top {
      top: 8px;
    }
    .y-tick-mid {
      top: calc(8px + (var(--plot-height) / 2));
    }
    .y-tick-bottom {
      top: calc(8px + var(--plot-height));
    }
    .chart-scroll {
      grid-column: 3;
      grid-row: 1;
      overflow-x: auto;
      padding: 8px 0 4px;
    }
    .x-axis-title {
      grid-column: 3;
      grid-row: 2;
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 600;
      text-align: center;
      margin-top: 4px;
    }
    .column-chart {
      display: flex;
      align-items: stretch;
      gap: 4px;
      min-width: max(100%, calc(var(--bar-count) * 10px));
      min-height: calc(var(--plot-height) + var(--label-height));
      position: relative;
      padding: 8px 2px 0;
    }
    .column-chart::after {
      content: "";
      position: absolute;
      left: 2px;
      right: 2px;
      top: calc(8px + var(--plot-height));
      border-bottom: 1px solid var(--line);
      pointer-events: none;
    }
    .time-column {
      flex: 1 0 8px;
      min-width: 8px;
      display: grid;
      grid-template-rows: var(--plot-height) var(--label-height);
      align-items: center;
      gap: 0;
      overflow: visible;
    }
    .time-bar-slot {
      width: 100%;
      height: var(--plot-height);
      display: flex;
      align-items: flex-end;
      position: relative;
      z-index: 1;
    }
    .stack-bar {
      width: 100%;
      min-height: 2px;
      border-radius: 4px 4px 0 0;
      overflow: hidden;
    }
    .stack-bar {
      display: flex;
      flex-direction: column-reverse;
      background: var(--bar-bg);
      border: 1px solid #e0d8cb;
    }
    .stack-segment {
      width: 100%;
      min-height: 1px;
      cursor: help;
    }
    .stack-segment:hover,
    .stack-segment:focus {
      filter: brightness(1.12);
    }
    .stack-segment:focus {
      outline: 2px solid var(--ink);
      outline-offset: -2px;
    }
    .time-label {
      width: 100%;
      height: var(--label-height);
      position: relative;
      color: var(--muted);
      font-size: 0.72rem;
      overflow: visible;
      padding-top: 8px;
    }
    .time-label-text {
      display: block;
      position: absolute;
      top: 14px;
      left: 50%;
      width: max-content;
      white-space: nowrap;
      line-height: 1;
      transform: rotate(45deg);
      transform-origin: top left;
      font-variant-numeric: tabular-nums;
    }
    .chart-tooltip {
      position: fixed;
      top: 0;
      left: 0;
      z-index: 50;
      max-width: min(420px, calc(100vw - 24px));
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.98);
      box-shadow: 0 10px 26px rgba(28, 31, 34, 0.14);
      color: var(--ink);
      font-size: 0.9rem;
      line-height: 1.45;
      white-space: pre-line;
      pointer-events: none;
      opacity: 0;
      visibility: hidden;
      transform: translate(-9999px, -9999px);
    }
    .chart-tooltip.visible {
      opacity: 1;
      visibility: visible;
    }
    .table-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      color: var(--muted);
      margin-bottom: 10px;
      flex-wrap: wrap;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }
    th, td {
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      position: sticky;
      top: 0;
      background: var(--card);
      white-space: nowrap;
    }
    td:nth-child(1),
    td:nth-child(2) {
      white-space: nowrap;
    }
    td:nth-child(2) {
      font-variant-numeric: tabular-nums;
    }
    .detail-list {
      color: var(--muted);
      line-height: 1.45;
    }
    .muted {
      color: var(--muted);
    }
    @media (max-width: 960px) {
      .controls,
      .chart-grid {
        grid-template-columns: 1fr;
      }
      .axis-chart {
        grid-template-columns: 16px 36px minmax(0, 1fr);
        column-gap: 6px;
      }
      .actions {
        justify-content: flex-start;
      }
      table, thead, tbody, th, td, tr {
        display: block;
      }
      thead {
        display: none;
      }
      tr {
        padding: 12px 0;
        border-bottom: 1px solid var(--line);
      }
      td {
        border-bottom: none;
        padding: 4px 0;
      }
      td::before {
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 0.84rem;
      }
    }
    """

    js = """
    const reportData = JSON.parse(document.getElementById('timeline-data').textContent);
    const root = document.documentElement;
    const langButtons = Array.from(document.querySelectorAll('[data-set-lang]'));
    const periodButtons = Array.from(document.querySelectorAll('[data-period]'));
    const grainButtons = Array.from(document.querySelectorAll('[data-grain]'));
    const categoryButtons = Array.from(document.querySelectorAll('[data-category]'));
    const filterInput = document.getElementById('sourceFilter');
    const stackChart = document.getElementById('stackChart');
    const tableBody = document.getElementById('timelineTableBody');
    const resultCount = document.getElementById('resultCount');
    const hoverTooltip = document.createElement('div');
    hoverTooltip.className = 'chart-tooltip';
    document.body.appendChild(hoverTooltip);
    const titles = {
      en: 'Japanese Mining Timeline',
      zh: '日语挖词时间线',
    };
    const text = {
      en: {
        noData: 'No matching data.',
        periods: 'periods',
        period: 'Period',
        words: 'Words',
        details: 'Source details',
        xDate: 'Date',
        xWeek: 'Week',
        xMonth: 'Month',
        yStackedWords: 'Words by source',
      },
      zh: {
        noData: '没有匹配数据。',
        periods: '个时间段',
        period: '时间段',
        words: '词数',
        details: '来源明细',
        xDate: '日期',
        xWeek: '周',
        xMonth: '月份',
        yStackedWords: '按来源堆叠的挖词数',
      },
    };
    const colors = [
      '#2f7f77', '#c7793f', '#516da8', '#9d5a8f', '#5f8f3e', '#b8524e',
      '#3d8aa6', '#b08d32', '#7466a6', '#4f7d52', '#a75f32', '#597e9f',
      '#8e6a40', '#6f8c7b', '#a05f70',
    ];
    const state = {
      lang: 'en',
      period: 'day',
      grain: 'work',
      category: 'all',
      query: '',
    };

    function t(key) {
      return (text[state.lang] || text.en)[key] || text.en[key] || key;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function escapeAttr(value) {
      return escapeHtml(value).replaceAll('\\n', '&#10;');
    }

    function showTooltip(event, text) {
      hoverTooltip.textContent = text;
      hoverTooltip.classList.add('visible');
      moveTooltipTo(event.clientX, event.clientY);
    }

    function moveTooltip(event) {
      if (!hoverTooltip.textContent) return;
      moveTooltipTo(event.clientX, event.clientY);
    }

    function showTooltipForElement(element, text) {
      hoverTooltip.textContent = text;
      hoverTooltip.classList.add('visible');
      const rect = element.getBoundingClientRect();
      moveTooltipTo(rect.left + rect.width / 2, rect.top + rect.height / 2);
    }

    function moveTooltipTo(clientX, clientY) {
      const margin = 12;
      const rect = hoverTooltip.getBoundingClientRect();
      let left = clientX + margin;
      let top = clientY + margin;
      if (left + rect.width > window.innerWidth - margin) {
        left = clientX - rect.width - margin;
      }
      if (top + rect.height > window.innerHeight - margin) {
        top = clientY - rect.height - margin;
      }
      hoverTooltip.style.transform =
        `translate(${Math.max(margin, left)}px, ${Math.max(margin, top)}px)`;
    }

    function hideTooltip() {
      hoverTooltip.textContent = '';
      hoverTooltip.classList.remove('visible');
      hoverTooltip.style.transform = 'translate(-9999px, -9999px)';
    }

    function periodKey() {
      if (state.period === 'week') return 'week';
      if (state.period === 'month') return 'month';
      return 'date';
    }

    function sourceKey() {
      return state.grain === 'source' ? 'source' : 'work';
    }

    function filteredRecords() {
      const key = sourceKey();
      const query = state.query.trim().toLowerCase();
      return reportData.records.filter((record) => {
        if (state.category !== 'all' && record.category !== state.category) {
          return false;
        }
        if (!query) return true;
        return String(record[key]).toLowerCase().includes(query);
      });
    }

    function increment(map, key, value = 1) {
      map.set(key, (map.get(key) || 0) + value);
    }

    function buildGroups(records) {
      const pKey = periodKey();
      const sKey = sourceKey();
      const buckets = new Map();
      const sourceTotals = new Map();

      for (const record of records) {
        const period = record[pKey];
        const source = record[sKey] || '(empty)';
        if (!buckets.has(period)) buckets.set(period, new Map());
        increment(buckets.get(period), source);
        increment(sourceTotals, source);
      }

      const periods = Array.from(buckets.entries())
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([period, counter]) => {
          const total = Array.from(counter.values()).reduce((sum, value) => sum + value, 0);
          return { period, counter, total };
        });
      const sources = Array.from(sourceTotals.entries())
        .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
      return { periods, sources };
    }

    function sourceLabels(sources) {
      return sources.map(([label]) => label);
    }

    function maybePeriodLabel(index, total, label) {
      const every = Math.max(1, Math.ceil(total / 12));
      return index % every === 0 || index === total - 1 ? label : '';
    }

    function formatTick(value) {
      return Math.round(value).toLocaleString();
    }

    function xAxisLabel() {
      if (state.period === 'week') return t('xWeek');
      if (state.period === 'month') return t('xMonth');
      return t('xDate');
    }

    function renderAxisChart(max, yLabel, chartHtml) {
      const mid = max / 2;
      return `
        <div class="axis-chart">
          <div class="y-axis-title">${escapeHtml(yLabel)}</div>
          <div class="y-axis-ticks" aria-hidden="true">
            <span class="y-tick y-tick-top">${formatTick(max)}</span>
            <span class="y-tick y-tick-mid">${formatTick(mid)}</span>
            <span class="y-tick y-tick-bottom">0</span>
          </div>
          <div class="chart-scroll">${chartHtml}</div>
          <div class="x-axis-title">${escapeHtml(xAxisLabel())}</div>
        </div>
      `;
    }

    function renderStackChart(periods, sourceLabels) {
      if (!periods.length) {
        stackChart.innerHTML = `<p class="muted">${escapeHtml(t('noData'))}</p>`;
        hideTooltip();
        return;
      }
      const max = Math.max(...periods.map((period) => period.total), 1);
      const colorBySource = new Map(
        sourceLabels.map((label, index) => [label, colors[index % colors.length]])
      );
      const chartHtml = `
        <div class="column-chart" style="--bar-count:${periods.length}">
          ${periods.map((period, index) => {
            const height = Math.max((period.total / max) * 100, 2);
            const pieces = sourceLabels
              .map((label) => ({
                label,
                count: period.counter.get(label) || 0,
                color: colorBySource.get(label),
              }))
              .filter((piece) => piece.count > 0);
            const label = maybePeriodLabel(index, periods.length, period.period);
            return `
              <div class="time-column">
                <div class="time-bar-slot">
                  <div class="stack-bar" style="height:${height.toFixed(2)}%">
                    ${pieces.map((piece) => {
                      const segmentHeight = Math.max((piece.count / period.total) * 100, 1);
                      const share = ((piece.count / period.total) * 100).toFixed(1);
                      const segmentTooltip = `${period.period}\\n${piece.label}\\n${piece.count.toLocaleString()} / ${period.total.toLocaleString()} (${share}%)`;
                      return `<div class="stack-segment" data-tooltip="${escapeAttr(segmentTooltip)}" aria-label="${escapeAttr(segmentTooltip)}" tabindex="0" style="height:${segmentHeight.toFixed(2)}%; background:${piece.color}"></div>`;
                    }).join('')}
                  </div>
                </div>
                <div class="time-label"><span class="time-label-text">${escapeHtml(label)}</span></div>
              </div>
            `;
          }).join('')}
        </div>
      `;
      stackChart.innerHTML = renderAxisChart(max, t('yStackedWords'), chartHtml);
      hideTooltip();
    }

    function renderTable(periods) {
      if (!periods.length) {
        tableBody.innerHTML = `
          <tr><td colspan="3" class="muted">${escapeHtml(t('noData'))}</td></tr>
        `;
        return;
      }
      tableBody.innerHTML = periods.map((period) => {
        const sources = Array.from(period.counter.entries());
        const details = sources
          .map(([label, count]) => `${label}: ${count.toLocaleString()}`)
          .map(escapeHtml)
          .join('<br>');
        return `
          <tr>
            <td data-label="${escapeHtml(t('period'))}" data-label-en="Period" data-label-zh="时间段">${escapeHtml(period.period)}</td>
            <td data-label="${escapeHtml(t('words'))}" data-label-en="Words" data-label-zh="词数">${period.total.toLocaleString()}</td>
            <td data-label="${escapeHtml(t('details'))}" data-label-en="Source details" data-label-zh="来源明细"><span class="detail-list">${details}</span></td>
          </tr>
        `;
      }).join('');
    }

    function updateLocalizableAttrs() {
      for (const element of document.querySelectorAll('[data-label-en]')) {
        const value = element.getAttribute(`data-label-${state.lang}`);
        if (value) element.setAttribute('data-label', value);
      }
      for (const element of document.querySelectorAll('[data-placeholder-en]')) {
        const value = element.getAttribute(`data-placeholder-${state.lang}`);
        if (value) element.setAttribute('placeholder', value);
      }
    }

    function render() {
      const records = filteredRecords();
      const { periods, sources } = buildGroups(records);
      renderStackChart(periods, sourceLabels(sources));
      renderTable(periods);
      resultCount.textContent = `${periods.length.toLocaleString()} ${t('periods')}`;
      updateLocalizableAttrs();
    }

    function applyLanguage(lang) {
      state.lang = lang === 'zh' ? 'zh' : 'en';
      root.dataset.lang = state.lang;
      root.lang = state.lang;
      document.title = titles[state.lang] || titles.en;
      for (const button of langButtons) {
        const active = button.dataset.setLang === state.lang;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
      try {
        localStorage.setItem('japanese-mining-report-lang', state.lang);
      } catch (error) {
        console.debug(error);
      }
      render();
    }

    for (const button of langButtons) {
      button.addEventListener('click', () => applyLanguage(button.dataset.setLang));
    }
    for (const button of periodButtons) {
      button.addEventListener('click', () => {
        state.period = button.dataset.period;
        periodButtons.forEach((item) => item.classList.toggle('active', item === button));
        render();
      });
    }
    for (const button of grainButtons) {
      button.addEventListener('click', () => {
        state.grain = button.dataset.grain;
        grainButtons.forEach((item) => item.classList.toggle('active', item === button));
        render();
      });
    }
    for (const button of categoryButtons) {
      button.addEventListener('click', () => {
        state.category = button.dataset.category;
        categoryButtons.forEach((item) => item.classList.toggle('active', item === button));
        render();
      });
    }
    filterInput.addEventListener('input', () => {
      state.query = filterInput.value;
      render();
    });
    stackChart.addEventListener('mouseover', (event) => {
      const segment = event.target.closest('.stack-segment');
      if (!segment || !stackChart.contains(segment)) return;
      showTooltip(event, segment.dataset.tooltip || '');
    });
    stackChart.addEventListener('mousemove', (event) => {
      if (event.target.closest('.stack-segment')) {
        moveTooltip(event);
      }
    });
    stackChart.addEventListener('mouseout', (event) => {
      const segment = event.target.closest('.stack-segment');
      if (!segment || segment.contains(event.relatedTarget)) return;
      hideTooltip();
    });
    stackChart.addEventListener('focusin', (event) => {
      const segment = event.target.closest('.stack-segment');
      if (!segment || !stackChart.contains(segment)) return;
      showTooltipForElement(segment, segment.dataset.tooltip || '');
    });
    stackChart.addEventListener('focusout', (event) => {
      const segment = event.target.closest('.stack-segment');
      if (!segment) return;
      hideTooltip();
    });

    let initialLang = 'en';
    try {
      initialLang =
        localStorage.getItem('japanese-mining-report-lang') ||
        ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en');
    } catch (error) {
      console.debug(error);
    }
    applyLanguage(initialLang);
    """

    return f"""<!DOCTYPE html>
<html lang="en" data-lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Japanese Mining Timeline</title>
  <style>{css}</style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-top">
        <h1>{render_bilingual("Japanese mining timeline", "日语挖词时间线")}</h1>
        <div class="actions">
          <a class="link-button" href="lapis_source_report.html">
            {render_bilingual("Source report", "来源报告")}
          </a>
          <div class="lang-toggle" role="group" aria-label="Language switch">
            <button type="button" class="lang-button active" data-set-lang="en" aria-pressed="true">EN</button>
            <button type="button" class="lang-button" data-set-lang="zh" aria-pressed="false">中文</button>
          </div>
        </div>
      </div>
      <p>{render_bilingual(
          "This page groups mined Lapis notes by creation date and source, so one note counts as one mined word.",
          "这个页面按 Lapis 笔记创建日期和来源统计挖词情况，一条笔记算一个挖词。"
      )}</p>
      <div class="meta">
        <span class="chip">{render_bilingual(f"Note type: {note_type_escaped}", f"笔记类型：{note_type_escaped}", escape_text=False)}</span>
        <span class="chip">{render_bilingual(f"Deck filter: {html.escape(deck_scope_en)}", f"牌组筛选：{html.escape(deck_scope_zh)}", escape_text=False)}</span>
        <span class="chip">{render_bilingual(f"Timezone: {html.escape(time_zone_label)}", f"时区：{html.escape(time_zone_label)}", escape_text=False)}</span>
        <span class="chip">{render_bilingual(f"Day starts: {day_start_hour:02d}:00", f"换日时间：{day_start_hour:02d}:00", escape_text=False)}</span>
        <span class="chip">{render_bilingual(f"Generated: {html.escape(generated_at)}", f"生成时间：{html.escape(generated_at)}", escape_text=False)}</span>
      </div>
    </section>

    <section class="summary">
      {summary_cards}
    </section>

    <section class="panel">
      <h2>{render_bilingual("Controls", "控制")}</h2>
      <div class="controls">
        <div>
          <span class="control-label">{render_bilingual("Time grain", "时间粒度")}</span>
          <span class="segmented" role="group" aria-label="Time grain">
            <button type="button" class="segmented-button active" data-period="day">{render_bilingual("Day", "日")}</button>
            <button type="button" class="segmented-button" data-period="week">{render_bilingual("Week", "周")}</button>
            <button type="button" class="segmented-button" data-period="month">{render_bilingual("Month", "月")}</button>
          </span>
        </div>
        <div>
          <span class="control-label">{render_bilingual("Source grain", "来源粒度")}</span>
          <span class="segmented" role="group" aria-label="Source grain">
            <button type="button" class="segmented-button active" data-grain="work">{render_bilingual("Work / material", "作品 / 材料")}</button>
            <button type="button" class="segmented-button" data-grain="source">{render_bilingual("Exact source", "原始来源")}</button>
          </span>
        </div>
        <div>
          <span class="control-label">{render_bilingual("Source category", "来源分类")}</span>
          <span class="segmented" role="group" aria-label="Source category">
            <button type="button" class="segmented-button active" data-category="all">{render_bilingual("All", "全部")}</button>
            <button type="button" class="segmented-button" data-category="anime">{render_bilingual("Anime", "动画")}</button>
            <button type="button" class="segmented-button" data-category="novel">{render_bilingual("Novel", "小说")}</button>
          </span>
        </div>
        <label for="sourceFilter">
          <span class="control-label">{render_bilingual("Filter source", "筛选来源")}</span>
          <input id="sourceFilter" type="search"
            placeholder="Filter current source grain"
            data-placeholder-en="Filter current source grain"
            data-placeholder-zh="筛选当前来源粒度">
        </label>
      </div>
    </section>

    <section class="chart-grid">
      <article class="panel">
        <h2>{render_bilingual("Source mix over time", "来源构成随时间变化")}</h2>
        <div id="stackChart" class="chart-host"></div>
      </article>
    </section>

    <section class="panel">
      <div class="table-toolbar">
        <h2>{render_bilingual("Timeline details", "时间线明细")}</h2>
        <span id="resultCount"></span>
      </div>
      <table>
        <thead>
          <tr>
            <th>{render_bilingual("Period", "时间段")}</th>
            <th>{render_bilingual("Words", "词数")}</th>
            <th>{render_bilingual("Source details", "来源明细")}</th>
          </tr>
        </thead>
        <tbody id="timelineTableBody"></tbody>
      </table>
    </section>
  </div>
  <script id="timeline-data" type="application/json">{data_json}</script>
  <script>{js}</script>
</body>
</html>
"""


def build_html(
    records: list[Record],
    note_type_name: str,
    deck_contains: str,
    top_n: int,
) -> str:
    source_counts = Counter(record.source for record in records)
    work_counts = Counter(record.work for record in records)
    deck_counts = Counter(record.deck for record in records)
    studied_cards = sum(record.studied for record in records)
    total_cards = len(records)
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    studied_percent = (studied_cards / total_cards * 100) if total_cards else 0

    deck_scope_en = deck_contains or "(all decks with matching note type)"
    deck_scope_zh = deck_contains or "（匹配该笔记类型的全部牌组）"
    note_type_escaped = html.escape(note_type_name)
    generated_at_escaped = html.escape(generated_at)

    summary_cards = "\n".join(
        [
            render_summary_card(
                "Cards",
                "卡片数",
                f"{total_cards:,}",
                "All matching Anki cards after note type and deck filters. Multiple cards from the same note are counted separately.",
                "经过笔记类型和牌组筛选后的 Anki 卡片数。同一条笔记的多张卡片会分别计数。",
            ),
            render_summary_card(
                "Studied cards",
                "已学卡片",
                f"{studied_cards:,} ({studied_percent:.1f}%)",
                "Matching cards with at least one review. The percentage is studied cards divided by all matching cards.",
                "至少复习过一次的匹配卡片。百分比为已学卡片数除以全部匹配卡片数。",
            ),
            render_summary_card(
                "Distinct source entries",
                "来源条目数",
                f"{len(source_counts):,}",
                "Distinct exact source strings from MiscInfo after stripping timestamp suffixes such as (2m21s).",
                "从 MiscInfo 提取并去掉类似 (2m21s) 时间戳后的精确来源字符串去重数。",
            ),
            render_summary_card(
                "Distinct works / materials",
                "作品 / 材料数",
                f"{len(work_counts):,}",
                "Distinct heuristic work/material labels derived from exact source strings.",
                "由精确来源字符串启发式归并出的作品 / 材料标签去重数。",
            ),
            render_summary_card(
                "Decks",
                "牌组数",
                f"{len(deck_counts):,}",
                "Distinct Anki decks among matching cards.",
                "匹配卡片所属的 Anki 牌组去重数。",
            ),
        ]
    )

    work_chart = render_bar_rows(
        build_progress_rows(records, lambda record: record.work, top_n),
        left_header="Work / material",
        left_header_zh="作品 / 材料",
    )
    source_chart = render_bar_rows(
        build_progress_rows(records, lambda record: record.source, top_n),
        left_header="Source",
        left_header_zh="来源",
    )
    table_html = render_table(records, total_cards)
    title_html = render_bilingual("Japanese mining report", "日语挖卡报告")
    hero_intro = render_bilingual(
        (
            "This report visualizes where your "
            f"<code>{note_type_escaped}</code> cards came from while mining Japanese "
            "from different materials. It reads the <code>MiscInfo</code> field, "
            "which often stores the title, file name, timestamp, or URL of the "
            "sentence, word, or page you mined. The work/material grouping is "
            "heuristic: it tries to merge episode-level or chapter-level source "
            "names into a higher-level title, but entries with only partial names "
            "cannot always be merged perfectly."
        ),
        (
            "这个报告用来可视化你在学习日语过程中从不同材料中挖出来的 "
            f"<code>{note_type_escaped}</code> 卡片来源。它读取 "
            "<code>MiscInfo</code> 字段，该字段通常记录你挖取的句子、单词或页面"
            "的标题、文件名、时间戳或 URL。作品 / 材料分组是启发式的：它会尽量把"
            "按集或按章拆开的来源名合并到更高层级的标题，但只有部分名称的条目不一定"
            "能完全合并。"
        ),
        escape_text=False,
    )
    meta_note_type = render_bilingual(
        f"Note type: {note_type_escaped}",
        f"笔记类型：{note_type_escaped}",
        escape_text=False,
    )
    meta_deck_scope = render_bilingual(
        f"Deck filter: {html.escape(deck_scope_en)}",
        f"牌组筛选：{html.escape(deck_scope_zh)}",
        escape_text=False,
    )
    meta_generated_at = render_bilingual(
        f"Generated: {generated_at_escaped}",
        f"生成时间：{generated_at_escaped}",
        escape_text=False,
    )
    works_intro = render_bilingual(
        (
            "Best-effort grouping of mined cards into higher-level works or source "
            "materials based on the text stored in <code>MiscInfo</code>. Values are "
            "shown as studied/mined with percentages, with dark bars for studied cards "
            "and light bars for mined-but-not-yet-studied cards."
        ),
        (
            "根据 <code>MiscInfo</code> 中记录的文本，对挖卡来源做尽力而为的高层级"
            "作品 / 材料归并。右侧数值显示为 已学/挖卡（百分比）。"
        ),
        escape_text=False,
    )
    works_note = render_bilingual(
        (
            "Useful for seeing which shows, novels, manga, visual novels, readers, "
            "or websites contributed the most cards to your Japanese mining. Not exact."
        ),
        (
            "用来查看哪些动画、小说、漫画、视觉小说、阅读器或网站为你的日语"
            "挖卡贡献了最多卡片。结果并非完全精确。"
        ),
    )
    sources_intro = render_bilingual(
        (
            "Exact source strings from <code>MiscInfo</code> after stripping "
            "timestamps like <code>(2m21s)</code>. Values are shown as studied/mined "
            "with percentages, with dark bars for studied cards and light bars for "
            "mined-but-not-yet-studied cards."
        ),
        (
            "这里展示 <code>MiscInfo</code> 中的精确来源字符串，并去掉了像 "
            "<code>(2m21s)</code> 这样的时间戳。右侧数值显示为 已学/挖卡（百分比）。"
        ),
        escape_text=False,
    )
    sources_note = render_bilingual(
        (
            "Useful when you want to see which exact episode, subtitle file, chapter, "
            "reader tab, page, or other mined source produced the most cards."
        ),
        (
            "用来查看到底是哪一集、哪份字幕、哪一章、哪个阅读器标签页或哪个页面"
            "产出了最多卡片。"
        ),
    )
    table_intro = render_bilingual(
        "Filter the table below to inspect specific sources and grouped works / materials.",
        "用下面的表格筛选和查看具体来源，以及归并后的作品 / 材料。",
    )

    return f"""<!DOCTYPE html>
<html lang="en" data-lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Japanese Mining Report</title>
  <style>
    :root {{
      --bg: #f7f3ea;
      --card: #fffdf8;
      --ink: #1d1b17;
      --muted: #6e6558;
      --line: #d9cfbf;
      --accent: #c96b3b;
      --accent-soft: #efd0c1;
      --bar-bg: #eee6d7;
    }}
    html[data-lang="en"] .lang-zh {{
      display: none;
    }}
    html[data-lang="zh"] .lang-en {{
      display: none;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Noto Serif CJK SC", serif;
      background:
        radial-gradient(circle at top left, #fff8eb 0, #fff8eb 16rem, transparent 16rem),
        linear-gradient(180deg, #f3ede1 0%, var(--bg) 40%);
      color: var(--ink);
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
      line-height: 1.1;
      font-weight: 700;
    }}
    h1 {{
      font-size: clamp(2rem, 3vw, 3rem);
    }}
    h2 {{
      font-size: 1.3rem;
    }}
    p {{
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .hero {{
      background: rgba(255, 253, 248, 0.8);
      backdrop-filter: blur(6px);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 24px;
      box-shadow: 0 16px 40px rgba(67, 40, 20, 0.06);
    }}
    .hero-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      align-items: flex-start;
    }}
    .link-button {{
      display: inline-flex;
      align-items: center;
      min-height: 38px;
      padding: 0 14px;
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
      background: #f3ebdf;
      border: 1px solid var(--line);
      border-radius: 999px;
    }}
    .link-button:hover {{
      text-decoration: none;
    }}
    .lang-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px;
      background: #f3ebdf;
      border: 1px solid var(--line);
      border-radius: 999px;
    }}
    .lang-button {{
      border: none;
      background: transparent;
      color: var(--muted);
      padding: 6px 10px;
      border-radius: 999px;
      font: inherit;
      cursor: pointer;
    }}
    .lang-button.active {{
      background: var(--card);
      color: var(--ink);
      box-shadow: 0 2px 10px rgba(67, 40, 20, 0.08);
    }}
    .meta {{
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .chip {{
      background: #f3ebdf;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 12px;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin: 20px 0 28px;
    }}
    .summary-card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      min-height: 110px;
      box-shadow: 0 10px 25px rgba(67, 40, 20, 0.05);
      position: relative;
    }}
    .summary-card[data-tip-en] {{
      cursor: help;
    }}
    .summary-card[data-tip-en]::after {{
      content: attr(data-tip-en);
      position: absolute;
      left: 16px;
      right: 16px;
      top: calc(100% - 8px);
      z-index: 30;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.98);
      box-shadow: 0 10px 26px rgba(67, 40, 20, 0.14);
      color: var(--ink);
      font-size: 0.86rem;
      line-height: 1.45;
      opacity: 0;
      visibility: hidden;
      transform: translateY(-4px);
      transition: opacity 120ms ease, transform 120ms ease, visibility 120ms ease;
      pointer-events: none;
    }}
    html[data-lang="zh"] .summary-card[data-tip-zh]::after {{
      content: attr(data-tip-zh);
    }}
    .summary-card[data-tip-en]:hover::after,
    .summary-card[data-tip-en]:focus::after {{
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
    }}
    .summary-card[data-tip-en]:focus {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    .summary-value {{
      font-size: 1.8rem;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .summary-label {{
      color: var(--muted);
    }}
    .grid {{
      display: grid;
      gap: 18px;
    }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px;
      box-shadow: 0 10px 25px rgba(67, 40, 20, 0.05);
    }}
    .bars {{
      display: grid;
      gap: 10px;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(0, 1.8fr) minmax(100px, 1.7fr) 20ch;
      align-items: center;
      gap: 10px;
      font-size: 0.95rem;
    }}
    .bar-row-header {{
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      padding-bottom: 2px;
      border-bottom: 1px solid var(--line);
    }}
    .bar-header-label,
    .bar-header-value {{
      white-space: nowrap;
    }}
    .bar-header-progress {{
      text-align: left;
    }}
    .bar-label {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .bar-track {{
      height: 12px;
      background: var(--bar-bg);
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid #e2d7c6;
    }}
    .bar-stack {{
      height: 100%;
      display: flex;
      border-radius: 999px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      flex: 0 0 auto;
    }}
    .bar-fill-studied {{
      background: linear-gradient(90deg, var(--accent), #db8e45);
    }}
    .bar-fill-unstudied {{
      background: linear-gradient(90deg, #f2c991, #efddb9);
    }}
    .bar-fill-total {{
      background: linear-gradient(90deg, var(--accent), #e29b51);
    }}
    .bar-value {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      width: 20ch;
    }}
    .note {{
      margin-top: 10px;
      font-size: 0.92rem;
      color: var(--muted);
    }}
    .table-panel {{
      margin-top: 18px;
    }}
    .table-toolbar {{
      margin-bottom: 12px;
    }}
    input[type="search"] {{
      width: 100%;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fffdf9;
      font: inherit;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      position: sticky;
      top: 0;
      background: var(--card);
    }}
    td:nth-child(3), td:nth-child(4) {{
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .muted {{
      color: var(--muted);
    }}
    @media (max-width: 880px) {{
      .hero-top {{
        align-items: stretch;
      }}
      .actions {{
        justify-content: flex-start;
      }}
      .bar-row-header {{
        display: none;
      }}
      .bar-row {{
        grid-template-columns: 1fr;
      }}
      .bar-value {{
        text-align: left;
      }}
      table, thead, tbody, th, td, tr {{
        display: block;
      }}
      thead {{
        display: none;
      }}
      tr {{
        padding: 12px 0;
        border-bottom: 1px solid var(--line);
      }}
      td {{
        border-bottom: none;
        padding: 4px 0;
      }}
      td::before {{
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 0.84rem;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-top">
        <h1>{title_html}</h1>
        <div class="actions">
          <a class="link-button" href="lapis_mining_timeline_report.html">
            {render_bilingual("Timeline report", "时间线报告")}
          </a>
          <div class="lang-toggle" role="group" aria-label="Language switch">
            <button type="button" class="lang-button active" data-set-lang="en" aria-pressed="true">EN</button>
            <button type="button" class="lang-button" data-set-lang="zh" aria-pressed="false">中文</button>
          </div>
        </div>
      </div>
      <p>{hero_intro}</p>
      <div class="meta">
        <span class="chip">{meta_note_type}</span>
        <span class="chip">{meta_deck_scope}</span>
        <span class="chip">{meta_generated_at}</span>
      </div>
    </section>

    <section class="summary">
      {summary_cards}
    </section>

    <section class="grid">
      <article class="panel">
        <h2>{render_bilingual("Top works / materials", "主要作品 / 材料")}</h2>
        <p>{works_intro}</p>
        {work_chart}
        <p class="note">{works_note}</p>
      </article>

      <article class="panel">
        <h2>{render_bilingual("Top source entries", "主要来源条目")}</h2>
        <p>{sources_intro}</p>
        {source_chart}
        <p class="note">{sources_note}</p>
      </article>
    </section>

    <section class="panel table-panel">
      <h2>{render_bilingual("All source entries", "全部来源条目")}</h2>
      <p>{table_intro}</p>
      {table_html}
    </section>
  </div>
  <script>
    const root = document.documentElement;
    const input = document.getElementById('sourceFilter');
    const rows = Array.from(document.querySelectorAll('#sourceTable tbody tr'));
    const langButtons = Array.from(document.querySelectorAll('[data-set-lang]'));
    const labelElements = Array.from(document.querySelectorAll('[data-label-en]'));
    const placeholderElements = Array.from(document.querySelectorAll('[data-placeholder-en]'));
    const titles = {{
      en: 'Japanese Mining Report',
      zh: '日语挖卡报告',
    }};

    function filterRows() {{
      const query = input.value.trim().toLowerCase();
      for (const row of rows) {{
        row.style.display = row.dataset.search.includes(query) ? '' : 'none';
      }}
    }}

    function applyLanguage(lang) {{
      const nextLang = lang === 'zh' ? 'zh' : 'en';
      root.dataset.lang = nextLang;
      root.lang = nextLang;
      document.title = titles[nextLang] || titles.en;

      for (const element of placeholderElements) {{
        const value = element.getAttribute(`data-placeholder-${{nextLang}}`);
        if (value) {{
          element.setAttribute('placeholder', value);
        }}
      }}

      for (const element of labelElements) {{
        const value = element.getAttribute(`data-label-${{nextLang}}`);
        if (value) {{
          element.setAttribute('data-label', value);
        }}
      }}

      for (const button of langButtons) {{
        const active = button.dataset.setLang === nextLang;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      }}

      try {{
        localStorage.setItem('japanese-mining-report-lang', nextLang);
      }} catch (error) {{
        console.debug(error);
      }}
    }}

    for (const button of langButtons) {{
      button.addEventListener('click', () => applyLanguage(button.dataset.setLang));
    }}
    input.addEventListener('input', filterRows);

    let initialLang = 'en';
    try {{
      initialLang =
        localStorage.getItem('japanese-mining-report-lang') ||
        ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en');
    }} catch (error) {{
      console.debug(error);
    }}
    applyLanguage(initialLang);
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    db_path = resolve_db_path(args.db)
    time_zone = resolve_timezone(args.timezone)
    snapshot_dir, snapshot_db = make_db_snapshot(db_path)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(snapshot_db)
        register_unicase(conn)
        day_start_hour = (
            validate_day_start_hour(args.day_start_hour)
            if args.day_start_hour is not None
            else collection_day_start_hour(conn)
        )

        note_type_id = find_note_type_id(conn, args.note_type)
        field_ord = find_field_ord(conn, note_type_id, args.field)
        records = load_records(
            conn,
            note_type_id,
            field_ord,
            args.deck_contains,
            time_zone,
            day_start_hour,
        )

        if not records:
            raise SystemExit("No matching cards found.")

        output_path = args.output.resolve()
        timeline_output_path = args.timeline_output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            build_html(
                records=records,
                note_type_name=args.note_type,
                deck_contains=args.deck_contains,
                top_n=max(args.top, 1),
            ),
            encoding="utf-8",
        )
        timeline_output_path.parent.mkdir(parents=True, exist_ok=True)
        timeline_output_path.write_text(
            render_timeline_html(
                records=records,
                note_type_name=args.note_type,
                deck_contains=args.deck_contains,
                time_zone_label=timezone_display_name(time_zone),
                day_start_hour=day_start_hour,
            ),
            encoding="utf-8",
        )
    finally:
        if conn is not None:
            conn.close()
        snapshot_dir.cleanup()

    print(f"Wrote {len(records):,} cards to {output_path}")
    print(
        f"Wrote {len(unique_note_records(records)):,} mined notes to "
        f"{timeline_output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
