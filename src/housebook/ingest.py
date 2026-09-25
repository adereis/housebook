"""housebook-ingest CLI — cross-cutting ingestion status.

Subcommands: list.
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from housebook.config.settings import DB_PATH

# ── list subcommand ────────────────────────────────────────────


def _connect(db_path=None):
    """Return a WAL-mode connection with Row factory."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _cmd_list(args):
    """Show ingested files grouped by source with date ranges."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    query = """
        SELECT
            source,
            original_file AS file_path,
            MIN(date) AS first_date,
            MAX(date) AS last_date,
            COUNT(id) AS tx_count
        FROM transactions
        WHERE source IS NOT NULL
    """
    params: list = []
    if args.source:
        query += " AND source = ?"
        params.append(args.source)

    query += " GROUP BY original_file"
    query += " ORDER BY MAX(date)"

    rows = cur.execute(query, params).fetchall()
    conn.close()

    if not rows:
        if args.source:
            print(f"No ingested files found for source '{args.source}'.")
        else:
            print("No ingested files found.")
        return

    if args.latest:
        rows = _pick_latest(rows)

    if args.json_output:
        _list_json(rows)
    else:
        _list_table(rows)


def _source_for(row):
    """Return the source for a row."""
    return row["source"] or "Unknown"


def _pick_latest(rows):
    """Return only the row with the latest end date per source."""
    latest: dict = {}
    for r in rows:
        src = _source_for(r)
        end = r["last_date"] or ""
        prev = latest.get(src)
        if not prev:
            latest[src] = dict(r)
        elif end > (prev["last_date"] or ""):
            latest[src] = dict(r)
    return list(latest.values())


def _list_json(rows):
    """Emit JSON output with per-source grouping and gap info."""
    by_source: dict = defaultdict(list)
    for r in rows:
        source = _source_for(r)
        by_source[source].append({
            "file": r["file_path"],
            "first_date": r["first_date"],
            "last_date": r["last_date"],
            "tx_count": r["tx_count"],
        })

    out = {}
    for source, files in by_source.items():
        gaps = _detect_gaps(files) if source != "Amazon" else []
        out[source] = {
            "files": files,
            "total_files": len(files),
            "gaps": gaps,
        }

    print(json.dumps(out, indent=2, default=str))


def _list_table(rows):
    """Render a human-readable table grouped by source."""
    by_source: dict = defaultdict(list)
    for r in rows:
        d = dict(r)
        d["_source"] = _source_for(r)
        by_source[d["_source"]].append(d)

    total_files = 0
    for source in sorted(by_source):
        files = by_source[source]
        total_files += len(files)
        print(f"\n  Source: {source} ({len(files)} file(s))")
        print(f"  {'─' * 70}")

        for r in files:
            fname = r["file_path"]
            parts = fname.replace("\\", "/").split("/")
            if len(parts) > 2:
                fname = "/".join(parts[-2:])

            start = r["first_date"] or "          "
            end = r["last_date"] or "          "
            tx_count = r["tx_count"]
            warn = "⚠ " if tx_count == 0 else ""
            print(
                f"    {start} → {end}"
                f"  ({tx_count:3d} txns)"
                f"  {warn}{fname}"
            )

        if source != "Amazon":
            gaps = _detect_gaps(files)
            for g in gaps:
                print(
                    f"    ⚠ Gap: no statement covering "
                    f"~{g['after']} → ~{g['before']}"
                )

    print(f"\n  {total_files} file(s) total")


def _detect_gaps(files):
    """Find gaps > 5 days between consecutive statement date ranges."""
    gaps = []
    sorted_files = sorted(
        files,
        key=lambda f: f.get("first_date") or "",
    )

    for i in range(1, len(sorted_files)):
        prev_end = sorted_files[i - 1].get("last_date")
        curr_start = sorted_files[i].get("first_date")
        try:
            d_prev = datetime.strptime(prev_end, "%Y-%m-%d")
            d_curr = datetime.strptime(curr_start, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if (d_curr - d_prev) > timedelta(days=5):
            gaps.append({
                "after": prev_end,
                "before": curr_start,
            })
    return gaps


# ── main ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Cross-cutting ingestion status",
        prog="housebook-ingest",
    )
    parser.add_argument(
        "--db", dest="db_path", default=None,
        help="Override database path",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser(
        "list",
        help="List ingested statements with date ranges and gaps",
    )
    p_list.add_argument(
        "--source", type=str, default=None,
        help="Filter by source (e.g. Amex, BoA, Amazon)",
    )
    p_list.add_argument(
        "--latest", action="store_true",
        help="Show only the most recent file per source",
    )
    p_list.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Machine-readable JSON output",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "list": _cmd_list,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
