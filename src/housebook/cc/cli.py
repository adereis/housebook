"""housebook-cc CLI — Credit-card statement management.

Subcommands: ingest, validate, list, summary, check.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from housebook.config.settings import (
    DB_PATH,
    WORKSPACE_DIR,
)

CC_DIR = str(Path(WORKSPACE_DIR) / "cc")


# ── validate ───────────────────────────────────────────────────


def cmd_validate(args):
    """Validate all CC sidecars under cc/. Reports all problems."""
    from housebook.core import sidecar as sidecar_mod

    from .issuers import IssuerResolver
    from .schema import validate_data_block

    cc_root = Path(CC_DIR)
    if not cc_root.is_dir():
        print("  cc/ directory not found.")
        sys.exit(1)

    resolver = IssuerResolver()

    total = 0
    passed = 0
    failed = 0
    all_errors: list[tuple[str, list[str]]] = []

    for jp in sorted(cc_root.rglob("*.json")):
        rel = jp.relative_to(cc_root)
        total += 1
        try:
            sc = sidecar_mod.load(str(jp))
        except sidecar_mod.SidecarError as e:
            all_errors.append((str(rel), [f"envelope: {e}"]))
            failed += 1
            continue
        if sc.source != "cc":
            all_errors.append(
                (str(rel), [f"source is {sc.source!r}, expected 'cc'"])
            )
            failed += 1
            continue
        errs = validate_data_block(sc.data, issuer_resolver=resolver)
        if errs:
            all_errors.append((str(rel), errs))
            failed += 1
        else:
            passed += 1

    if getattr(args, "json_output", False):
        out = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": [
                {"sidecar": p, "errors": e} for p, e in all_errors
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"  Validated {total} sidecar(s): "
              f"{passed} passed, {failed} failed.")
        for path, errs in all_errors:
            print(f"\n  {path}:")
            for e in errs:
                print(f"    - {e}")

    sys.exit(1 if failed else 0)


# ── ingest ─────────────────────────────────────────────────────


def cmd_ingest(args):
    """Ingest classified CC sidecars into the transactions table."""
    from housebook.core.database import Database

    from .ingestor import CcIngestor

    db_path = args.db_path or DB_PATH
    dry_run = getattr(args, "dry_run", False)
    db = Database(
        db_path, dry_run=dry_run,
        workspace_dir=str(WORKSPACE_DIR),
    )
    ingestor = CcIngestor(db, None)

    if not os.path.isdir(CC_DIR):
        print("  cc/ directory not found.")
        sys.exit(1)

    result = ingestor.ingest_directory(
        CC_DIR, verbose=getattr(args, "verbose", False),
    )

    parts = []
    if result["ingested"]:
        parts.append(f"{result['ingested']} statement(s)")
    if result["rows_written"]:
        parts.append(f"{result['rows_written']} transaction(s)")
    if result["skipped"]:
        parts.append(f"{result['skipped']} unchanged")
    if result.get("duplicate_rows_skipped"):
        parts.append(
            f"{result['duplicate_rows_skipped']} duplicate row(s) skipped"
        )
    if result.get("empty"):
        parts.append(f"{len(result['empty'])} wrote NO rows")
    if result["errors"]:
        parts.append(f"{len(result['errors'])} error(s)")

    print(f"  CC ingest: {', '.join(parts) or 'nothing to do'}.")

    # A new sidecar that produced nothing is a red flag, not a no-op:
    # it is now marked processed and will never be retried.
    for path in result.get("empty", []):
        short = os.path.relpath(path, CC_DIR)
        print(f"  ⚠ {short}: newly processed but wrote 0 transactions "
              f"— check the sidecar's transactions[] array.")

    for path, err in result["errors"]:
        short = os.path.relpath(path, CC_DIR)
        print(f"  ! {short}: {err}")

    if dry_run:
        print("  (dry run — no changes written)")

    # A partially failed ingest must not look like success to
    # automation (validate and check already exit non-zero).
    if result["errors"]:
        sys.exit(1)


# ── list ───────────────────────────────────────────────────────


def cmd_list(args):
    """Flat list of CC transactions from the DB."""
    db_path = args.db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    clauses = [
        # NULL-safe: `category != 'CC Payment'` alone is NULL (false)
        # for NULL categories, silently dropping those rows.
        "(category IS NULL OR category != 'CC Payment')",
        "linked_transaction_id IS NULL",
        "status != 'RECONCILED'",
    ]
    params: list = []

    if args.year:
        clauses.append("SUBSTR(date, 1, 4) = ?")
        params.append(str(args.year))
    if args.source:
        clauses.append("source = ?")
        params.append(args.source)

    where = " AND ".join(clauses)
    sql = (
        f"SELECT * FROM transactions WHERE {where} "
        "ORDER BY date DESC"
    )
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if getattr(args, "json_output", False):
        print(json.dumps([dict(r) for r in rows], indent=2))
        return

    for r in rows:
        print(
            f"  {r['id']:>6d}  {r['date']}  "
            f"{r['amount']:>10.2f}  {r['source']:<12s}  "
            f"{(r['category'] or ''):<20s}  "
            f"{(r['description'] or '')[:40]}"
        )
    print(f"\n  {len(rows)} transaction(s).")


# ── summary ────────────────────────────────────────────────────


def cmd_summary(args):
    """Aggregate spending summary by source/category."""
    db_path = args.db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    clauses = [
        # NULL-safe: `category != 'CC Payment'` alone is NULL (false)
        # for NULL categories, silently dropping those rows.
        "(category IS NULL OR category != 'CC Payment')",
        "linked_transaction_id IS NULL",
        "status != 'RECONCILED'",
    ]
    params: list = []
    if args.year:
        clauses.append("SUBSTR(date, 1, 4) = ?")
        params.append(str(args.year))

    where = " AND ".join(clauses)
    sql = (
        f"SELECT source, COUNT(*) AS tx_count, "
        f"ROUND(SUM(amount), 2) AS total "
        f"FROM transactions WHERE {where} "
        f"GROUP BY source ORDER BY total DESC"
    )
    rows = conn.execute(sql, params).fetchall()

    grand_total_row = conn.execute(
        f"SELECT COUNT(*) AS tx_count, ROUND(SUM(amount), 2) AS total "
        f"FROM transactions WHERE {where}",
        params,
    ).fetchone()
    conn.close()

    if getattr(args, "json_output", False):
        out = {
            "by_source": [dict(r) for r in rows],
            "grand_total": dict(grand_total_row) if grand_total_row else {},
        }
        print(json.dumps(out, indent=2))
        return

    print(f"  {'Source':<15s} {'Count':>6s} {'Total':>12s}")
    print("  " + "-" * 35)
    for r in rows:
        print(
            f"  {r['source']:<15s} {r['tx_count']:>6d} "
            f"${r['total']:>10.2f}"
        )
    if grand_total_row:
        print("  " + "-" * 35)
        print(
            f"  {'TOTAL':<15s} {grand_total_row['tx_count']:>6d} "
            f"${grand_total_row['total']:>10.2f}"
        )


# ── check ──────────────────────────────────────────────────────


def cmd_check(args):
    """Data quality checks: orphan PDFs, sidecar mismatches, etc."""
    issues: list[str] = []

    # 1. Validate all sidecars
    from housebook.core import sidecar as sidecar_mod

    from .issuers import IssuerResolver
    from .schema import validate_data_block

    resolver = IssuerResolver()
    cc_root = Path(CC_DIR)
    if cc_root.is_dir():
        for jp in sorted(cc_root.rglob("*.json")):
            rel = jp.relative_to(cc_root)
            try:
                sc = sidecar_mod.load(str(jp))
                if sc.source == "cc":
                    errs = validate_data_block(
                        sc.data, issuer_resolver=resolver
                    )
                    for e in errs:
                        issues.append(f"sidecar {rel}: {e}")
            except Exception as e:
                issues.append(f"sidecar {rel}: {e}")

    # 2. Check for source files without sidecars
    if cc_root.is_dir():
        source_exts = {".pdf", ".jpg", ".jpeg", ".png"}
        for fp in sorted(cc_root.rglob("*")):
            if fp.suffix.lower() not in source_exts:
                continue
            rel = fp.relative_to(cc_root)
            sidecar = fp.with_suffix(".json")
            if not sidecar.exists():
                issues.append(f"orphan source file (no sidecar): {rel}")

    # 3. Check DB provenance
    db_path = args.db_path or DB_PATH
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        stale = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE original_file LIKE 'input/Credit-Card-Statements/%'"
        ).fetchone()[0]
        conn.close()
        if stale:
            issues.append(
                f"{stale} transaction(s) still reference old "
                "input/Credit-Card-Statements/ path — run "
                "housebook-init-db to apply migration 017"
            )

    if getattr(args, "json_output", False):
        print(json.dumps({"issues": issues}, indent=2))
    else:
        if issues:
            for iss in issues:
                print(f"  ! {iss}")
            print(f"\n  {len(issues)} issue(s) found.")
        else:
            print("  All checks passed.")

    sys.exit(1 if issues else 0)


# ── main ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Credit-card statement management",
        prog="housebook-cc",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="Override database path",
    )
    sub = parser.add_subparsers(dest="command")

    # validate
    p_val = sub.add_parser(
        "validate",
        help="Validate all CC sidecars",
    )
    p_val.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    # ingest
    p_ing = sub.add_parser(
        "ingest",
        help="Ingest classified CC sidecars into transactions",
    )
    p_ing.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing to DB",
    )
    p_ing.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print every row suppressed by the duplicate check",
    )

    # list
    p_lst = sub.add_parser(
        "list",
        help="List CC transactions",
    )
    p_lst.add_argument("--year", type=int)
    p_lst.add_argument("--source", type=str)
    p_lst.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    # summary
    p_sum = sub.add_parser(
        "summary",
        help="Spending summary by source/category",
    )
    p_sum.add_argument("--year", type=int)
    p_sum.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    # check
    p_chk = sub.add_parser(
        "check",
        help="Data quality checks",
    )
    p_chk.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "validate": cmd_validate,
        "ingest": cmd_ingest,
        "list": cmd_list,
        "summary": cmd_summary,
        "check": cmd_check,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
