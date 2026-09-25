"""housebook-amazon CLI — Amazon order history management.

Subcommands: import, ingest, list, summary.
"""

import argparse
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

from housebook.config.settings import (
    DB_PATH,
    WORKSPACE_DIR,
)

AMAZON_DIR = str(Path(WORKSPACE_DIR) / "amazon")

ALLOWED_EXTENSIONS = (".csv", ".json", ".xml", ".txt")


# ── import (unzip) ─────────────────────────────────────────────


def cmd_import(args):
    """Unzip an Amazon data export into a profile directory.

    Deterministic import: unzip + manifest sidecar, no AI needed.
    """
    zip_path = Path(args.zip_path).expanduser().resolve()
    if not zip_path.exists():
        print(f"  File not found: {zip_path}")
        sys.exit(1)

    profile = args.profile
    if not profile:
        print("  --profile is required (e.g., --profile sterling)")
        sys.exit(1)
    if Path(profile).name != profile or profile in (".", ".."):
        print(f"  Invalid profile name: {profile!r} "
              f"(must be a plain directory name)")
        sys.exit(1)

    dest = Path(AMAZON_DIR) / profile
    dest.mkdir(parents=True, exist_ok=True)

    extracted = 0
    skipped = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            ext = os.path.splitext(member)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                skipped += 1
                continue
            # Strip the top-level zip directory if present
            parts = Path(member).parts
            if len(parts) > 1:
                rel = str(Path(*parts[1:]))
            else:
                rel = member
            target = dest / rel
            # Zip-slip guard: a member like "Export/../../x.csv" must
            # never write outside the profile directory.
            if not target.resolve().is_relative_to(dest.resolve()):
                print(f"  Error: zip member escapes the profile "
                      f"directory: {member!r}")
                sys.exit(1)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted += 1

    print(
        f"  Extracted {extracted} file(s) to amazon/{profile}/ "
        f"({skipped} non-data files skipped)."
    )

    # Write manifest sidecar
    import hashlib
    from datetime import datetime, timezone

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "1",
        "source": "amazon",
        "source_file": {
            "path": str(zip_path),
            "sha256": sha,
            "size_bytes": zip_path.stat().st_size,
            "mime_type": "application/zip",
        },
        "classified_at": datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "classified_by": "housebook-amazon-import",
        "data": {
            "profile": profile,
            "extracted_files": extracted,
            "skipped_files": skipped,
        },
    }
    manifest_path = dest / "_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"  Manifest: amazon/{profile}/_manifest.json")


# ── ingest ─────────────────────────────────────────────────────


def cmd_ingest(args):
    """Ingest Amazon CSVs from all profile directories."""
    from housebook.core.database import Database

    from .ingestor import AmazonIngestor

    db_path = args.db_path or DB_PATH
    dry_run = getattr(args, "dry_run", False)
    db = Database(
        db_path, dry_run=dry_run,
        workspace_dir=str(WORKSPACE_DIR),
    )

    from housebook.core.intelligence import Intelligence
    intel = Intelligence(db.get_rules())

    ingestor = AmazonIngestor(db, intel)
    result = ingestor.ingest_all_profiles(
        AMAZON_DIR, verbose=getattr(args, "verbose", False),
    )

    parts = []
    if result["profiles"]:
        parts.append(f"{result['profiles']} profile(s)")
    if result["rows_written"]:
        parts.append(f"{result['rows_written']} transaction(s)")
    if result["skipped"]:
        parts.append(f"{result['skipped']} unchanged")
    if result.get("duplicate_rows_skipped"):
        parts.append(
            f"{result['duplicate_rows_skipped']} duplicate row(s) skipped"
        )
    print(
        f"  Amazon ingest: "
        f"{', '.join(parts) or 'nothing to do'}."
    )
    if dry_run:
        print("  (dry run — no changes written)")


# ── list ───────────────────────────────────────────────────────


def cmd_list(args):
    """List Amazon transactions."""
    db_path = args.db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    clauses = ["source = 'Amazon'"]
    params: list = []
    if args.year:
        clauses.append("SUBSTR(date, 1, 4) = ?")
        params.append(str(args.year))
    if args.profile:
        clauses.append("profile = ?")
        params.append(args.profile)

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE {where} "
        "ORDER BY date DESC", params,
    ).fetchall()
    conn.close()

    if getattr(args, "json_output", False):
        print(json.dumps([dict(r) for r in rows], indent=2))
        return

    for r in rows:
        print(
            f"  {r['id']:>6d}  {r['date']}  "
            f"{r['amount']:>10.2f}  "
            f"{(r['profile'] or ''):>8s}  "
            f"{(r['description'] or '')[:50]}"
        )
    print(f"\n  {len(rows)} transaction(s).")


# ── summary ────────────────────────────────────────────────────


def cmd_summary(args):
    """Amazon order summary by profile."""
    db_path = args.db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    clauses = ["source = 'Amazon'"]
    params: list = []
    if args.year:
        clauses.append("SUBSTR(date, 1, 4) = ?")
        params.append(str(args.year))

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT profile, COUNT(*) AS tx_count, "
        f"ROUND(SUM(amount), 2) AS total "
        f"FROM transactions WHERE {where} "
        f"GROUP BY profile ORDER BY total DESC",
        params,
    ).fetchall()
    conn.close()

    if getattr(args, "json_output", False):
        print(json.dumps([dict(r) for r in rows], indent=2))
        return

    for r in rows:
        print(
            f"  {(r['profile'] or 'unknown'):<12s} "
            f"{r['tx_count']:>6d} orders  "
            f"${r['total']:>12,.2f}"
        )


# ── main ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Amazon order history management",
        prog="housebook-amazon",
    )
    parser.add_argument(
        "--db", dest="db_path", default=None,
        help="Override database path",
    )
    sub = parser.add_subparsers(dest="command")

    p_imp = sub.add_parser(
        "import",
        help="Unzip an Amazon data export into a profile",
    )
    p_imp.add_argument("zip_path", help="Path to the Amazon zip file")
    p_imp.add_argument(
        "--profile", required=True,
        help="Profile name (e.g., sterling, penny)",
    )

    p_ing = sub.add_parser(
        "ingest",
        help="Ingest Amazon CSVs into transactions",
    )
    p_ing.add_argument(
        "--dry-run", action="store_true",
    )
    p_ing.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print every row suppressed by the duplicate check",
    )

    p_lst = sub.add_parser("list", help="List Amazon transactions")
    p_lst.add_argument("--year", type=int)
    p_lst.add_argument("--profile", type=str)
    p_lst.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    p_sum = sub.add_parser("summary", help="Amazon order summary")
    p_sum.add_argument("--year", type=int)
    p_sum.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "import": cmd_import,
        "ingest": cmd_ingest,
        "list": cmd_list,
        "summary": cmd_summary,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
