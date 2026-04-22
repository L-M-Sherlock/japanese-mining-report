#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


BR_RE = re.compile(r"<br\s*/?>", flags=re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
TIMESTAMP_RE = re.compile(r"\s*\((?:\d+h)?\d+m\d+s\)\s*$")
URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Record:
    source: str
    work: str
    domain: str
    url: str
    deck: str
    studied: bool


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


def load_records(
    conn: sqlite3.Connection,
    note_type_id: int,
    field_ord: int,
    deck_contains: str,
) -> list[Record]:
    deck_filter = deck_contains.casefold().strip()
    records: list[Record] = []

    rows = conn.execute(
        """
        select c.id, d.name, n.flds, c.reps
        from cards c
        join notes n on n.id = c.nid
        join decks d on d.id = c.did
        where n.mid=?
        """,
        (note_type_id,),
    )

    for _, deck_name, flds, reps in rows:
        if deck_filter and deck_filter not in deck_name.casefold():
            continue

        fields = flds.split("\x1f")
        raw_misc = fields[field_ord] if len(fields) > field_ord else ""
        lines = strip_misc_html(raw_misc)
        source = extract_source_label(lines)
        url = extract_url(lines)
        domain = extract_domain(url)

        records.append(
            Record(
                source=source,
                work=guess_work_label(source),
                domain=domain,
                url=url,
                deck=deck_name,
                studied=reps > 0,
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


def render_bar_rows(
    rows: list[tuple[str, int] | tuple[str, int, int]],
    left_header: str = "Label",
    right_header: str = "Studied / mined",
) -> str:
    if not rows:
        return "<p class='muted'>No data.</p>"

    max_count = rows[0][1]
    pieces = [
        "<div class='bars'>",
        (
            "<div class='bar-row bar-row-header'>"
            f"<div class='bar-header-label'>{html.escape(left_header)}</div>"
            "<div class='bar-header-progress'>Progress</div>"
            f"<div class='bar-header-value'>{html.escape(right_header)}</div>"
            "</div>"
        ),
    ]
    for row in rows:
        label = row[0]
        count = row[1]
        studied = row[2] if len(row) > 2 else None
        width = (count / max_count) * 100 if max_count else 0
        value_text = f"{studied:,}/{count:,}" if studied is not None else f"{count:,}"
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


def render_summary_card(label: str, value: str) -> str:
    return (
        "<div class='summary-card'>"
        f"<div class='summary-value'>{html.escape(value)}</div>"
        f"<div class='summary-label'>{html.escape(label)}</div>"
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
            part.casefold() for part in (record.source, record.work, record.domain, record.deck)
        )
        rows.append(
            "".join(
                [
                    f"<tr data-search='{html.escape(search_blob)}'>",
                    f"<td data-label='Source'>{html.escape(record.source)}</td>",
                    f"<td data-label='Work / material (heuristic)'>{html.escape(record.work)}</td>",
                    f"<td data-label='Cards'>{count}</td>",
                    f"<td data-label='Share'>{share * 100:.2f}%</td>",
                    "</tr>",
                ]
            )
        )

    return "\n".join(
        [
            "<div class='table-toolbar'>",
            "<input id='sourceFilter' type='search' placeholder='Filter by source or work / material'>",
            "</div>",
            "<table id='sourceTable'>",
            "<thead><tr><th>Source</th><th>Work / material (heuristic)</th><th>Cards</th><th>Share</th></tr></thead>",
            "<tbody>",
            *rows,
            "</tbody>",
            "</table>",
        ]
    )


def build_html(
    records: list[Record],
    note_type_name: str,
    deck_contains: str,
    top_n: int,
) -> str:
    source_counts = Counter(record.source for record in records)
    work_counts = Counter(record.work for record in records)
    domain_counts = Counter(record.domain for record in records)
    deck_counts = Counter(record.deck for record in records)
    studied_cards = sum(record.studied for record in records)
    total_cards = len(records)
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    deck_scope = deck_contains or "(all decks with matching note type)"

    summary_cards = "\n".join(
        [
            render_summary_card("Cards", f"{total_cards:,}"),
            render_summary_card("Studied cards", f"{studied_cards:,}"),
            render_summary_card("Distinct source entries", f"{len(source_counts):,}"),
            render_summary_card("Distinct works / materials", f"{len(work_counts):,}"),
            render_summary_card("Decks", f"{len(deck_counts):,}"),
        ]
    )

    work_chart = render_bar_rows(
        build_progress_rows(records, lambda record: record.work, top_n),
        left_header="Work / material",
    )
    source_chart = render_bar_rows(
        build_progress_rows(records, lambda record: record.source, top_n),
        left_header="Source",
    )
    table_html = render_table(records, total_cards)

    return f"""<!DOCTYPE html>
<html lang="en">
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
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
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
      grid-template-columns: minmax(0, 1.8fr) minmax(100px, 1.7fr) 12ch;
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
      width: 12ch;
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
    td:nth-child(4), td:nth-child(5) {{
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
      <h1>Japanese mining report</h1>
      <p>This report visualizes where your <code>{html.escape(note_type_name)}</code> cards came from while mining Japanese from different materials. It reads the <code>MiscInfo</code> field, which often stores the title, file name, timestamp, or URL of the sentence, word, or page you mined. The work/material grouping is heuristic: it tries to merge episode-level or chapter-level source names into a higher-level title, but entries with only partial names cannot always be merged perfectly.</p>
      <div class="meta">
        <span class="chip">Note type: {html.escape(note_type_name)}</span>
        <span class="chip">Deck filter: {html.escape(deck_scope)}</span>
        <span class="chip">Generated: {html.escape(generated_at)}</span>
      </div>
    </section>

    <section class="summary">
      {summary_cards}
    </section>

    <section class="grid">
      <article class="panel">
        <h2>Top works / materials</h2>
        <p>Best-effort grouping of mined cards into higher-level works or source materials based on the text stored in <code>MiscInfo</code>. Values are shown as studied/mined, with dark bars for studied cards and light bars for mined-but-not-yet-studied cards.</p>
        {work_chart}
        <p class="note">Useful for seeing which shows, novels, manga, visual novels, readers, or websites contributed the most cards to your Japanese mining. Not exact.</p>
      </article>

      <article class="panel">
        <h2>Top source entries</h2>
        <p>Exact source strings from <code>MiscInfo</code> after stripping timestamps like <code>(2m21s)</code>. Values are shown as studied/mined, with dark bars for studied cards and light bars for mined-but-not-yet-studied cards.</p>
        {source_chart}
        <p class="note">Useful when you want to see which exact episode, subtitle file, chapter, reader tab, page, or other mined source produced the most cards.</p>
      </article>
    </section>

    <section class="panel table-panel">
      <h2>All source entries</h2>
      <p>Filter the table below to inspect mined sources, grouped works/materials, and any stored site/domain metadata.</p>
      {table_html}
    </section>
  </div>
  <script>
    const input = document.getElementById('sourceFilter');
    const rows = Array.from(document.querySelectorAll('#sourceTable tbody tr'));

    function filterRows() {{
      const query = input.value.trim().toLowerCase();
      for (const row of rows) {{
        row.style.display = row.dataset.search.includes(query) ? '' : 'none';
      }}
    }}

    input.addEventListener('input', filterRows);
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    db_path = resolve_db_path(args.db)
    snapshot_dir, snapshot_db = make_db_snapshot(db_path)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(snapshot_db)
        register_unicase(conn)

        note_type_id = find_note_type_id(conn, args.note_type)
        field_ord = find_field_ord(conn, note_type_id, args.field)
        records = load_records(conn, note_type_id, field_ord, args.deck_contains)

        if not records:
            raise SystemExit("No matching cards found.")

        output_path = args.output.resolve()
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
    finally:
        if conn is not None:
            conn.close()
        snapshot_dir.cleanup()

    print(f"Wrote {len(records):,} cards to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
