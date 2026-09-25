"""housebook-tax CLI — tax document review and reporting.

Subcommands: estimate, summary, check, list, validate.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from housebook.config.settings import DB_PATH, WORKSPACE_DIR

from .estimate import compute_tax_estimate

TAX_DIR = str(Path(WORKSPACE_DIR) / "tax")


def _connect(db_path=None):
    """Return a WAL-mode connection with Row factory."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_amount(amount) -> str:
    """Render an amount, distinguishing NULL from a real $0.00.

    A missing amount is stored as NULL (never fabricated as 0), and
    `f"${None:,.2f}"` raises — so every display path goes through here.
    """
    if amount is None:
        return "—"
    return f"${amount:,.2f}"


# ── estimate ────────────────────────────────────────────────────

def cmd_estimate(args):
    """Show federal + state tax estimate."""
    result = compute_tax_estimate(args.db_path, args.year)

    if "error" in result:
        print(f"  {result['error']}")
        return

    if args.json_output:
        print(json.dumps(result, indent=2))
        return

    inc = result["income"]
    ded = result["deductions"]
    fed = result["federal"]
    st = result["state"]

    print(f"\n  {result['year']} Tax Estimate "
          f"({result['filing_status']}, {result['filing_state']})")
    print(f"  {'=' * 50}")

    unknown = result.get("unknown_amounts") or []
    if unknown:
        print(f"\n  ⚠ INCOMPLETE — {len(unknown)} document(s) have no "
              f"amount and contributed nothing:")
        for u in unknown:
            print(f"      {u['document_type']} from {u['issuer']}")
        print("    Extract those amounts before trusting this estimate.")

    print("\n  INCOME")
    print(f"    Wages:                    ${inc['wages']:>12,.2f}")
    if inc["ordinary_dividends"]:
        print(f"    Ordinary dividends:       "
              f"${inc['ordinary_dividends']:>12,.2f}")
    if inc["qualified_dividends"]:
        print(f"    Qualified dividends:      "
              f"${inc['qualified_dividends']:>12,.2f}  (15%)")
    if inc["short_term_gains"]:
        print(f"    ST capital gains:         "
              f"${inc['short_term_gains']:>12,.2f}")
    if inc["long_term_gains"]:
        print(f"    LT capital gains:         "
              f"${inc['long_term_gains']:>12,.2f}  (15%)")
    if inc["loss_carryover_applied"]:
        print(f"    Loss carryover applied:   "
              f"${-inc['loss_carryover_applied']:>12,.2f}")
    if inc["brazilian_income"]:
        print(f"    Brazilian income:         "
              f"${inc['brazilian_income']:>12,.2f}")
    if inc["rollover_nontaxable"]:
        print(f"    Rollover (non-taxable):   "
              f"${inc['rollover_nontaxable']:>12,.2f}")
    print(f"    AGI:                      ${result['agi']:>12,.2f}")

    print(f"\n  DEDUCTIONS ({ded['type']})")
    print(f"    Deduction used:           ${ded['amount']:>12,.2f}")
    if ded["qbi_deduction"]:
        print(f"    QBI (Sec 199A):           "
              f"${ded['qbi_deduction']:>12,.2f}")

    ti = result["taxable_income"]
    print("\n  TAXABLE INCOME")
    print(f"    Ordinary:                 ${ti['ordinary']:>12,.2f}")
    print(f"    Preferential (QD+LT CG): ${ti['preferential']:>12,.2f}")
    print(f"    Total:                    ${ti['total']:>12,.2f}")

    print("\n  FEDERAL TAX")
    print(f"    On ordinary income:       ${fed['ordinary_tax']:>12,.2f}")
    pref_rates = "/".join(
        f"{b['rate']:.0%}" for b in result["preferential_brackets"]
    ) or "0%"
    print(f"    On QD + LT CG ({pref_rates}):".ljust(30)
          + f"${fed['preferential_tax']:>12,.2f}")
    if fed["niit"]:
        print(f"    NIIT (3.8%):              ${fed['niit']:>12,.2f}")
    print(f"    Foreign tax credit:       "
          f"${fed['foreign_tax_credit']:>12,.2f}")
    if fed["child_tax_credit"]:
        print(f"    Child tax credit:         "
              f"${fed['child_tax_credit']:>12,.2f}")
    elif fed["ctc_phaseout"]:
        print("    Child tax credit:         "
              "$        0.00  (phased out)")
    print(f"    Tax after credits:        "
          f"${fed['tax_after_credits']:>12,.2f}")
    print(f"    Withheld:                 ${fed['withheld']:>12,.2f}")
    bal = fed["balance"]
    if bal > 0:
        print(f"    >>> BALANCE DUE:          ${bal:>12,.2f}")
    else:
        print(f"    >>> REFUND:               ${abs(bal):>12,.2f}")

    print(f"\n  {st['name']} STATE TAX ({st['rate']*100:.0f}% flat)")
    print(f"    State tax:                ${st['tax']:>12,.2f}")
    if st["plan_529_deduction"]:
        print(f"    529 deduction:            "
              f"${st['plan_529_deduction']:>12,.2f}")
    print(f"    Withheld:                 ${st['withheld']:>12,.2f}")
    sbal = st["balance"]
    if sbal > 0:
        print(f"    >>> BALANCE DUE:          ${sbal:>12,.2f}")
    else:
        print(f"    >>> REFUND:               ${abs(sbal):>12,.2f}")

    print()


# ── summary ─────────────────────────────────────────────────────

def cmd_summary(args):
    """Show tax documents grouped by category with totals."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    where = "WHERE 1=1"
    params = []
    if args.year:
        where += " AND tax_year = ?"
        params.append(args.year)

    rows = cur.execute(
        f"""SELECT id, tax_year, document_type, issuer,
                   category, amount, currency, status,
                   needs_review, original_file, raw_data
            FROM tax_documents {where}
            ORDER BY tax_year, category, document_type, issuer""",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        print("No tax documents found.")
        return

    if args.json_output:
        out = []
        for r in rows:
            item = dict(r)
            if item.get("raw_data"):
                try:
                    item["raw_data"] = json.loads(item["raw_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(item)
        print(json.dumps(out, indent=2, default=str))
        return

    current_cat = None
    cat_total = 0.0
    grand_total = 0.0

    for r in rows:
        if r["category"] != current_cat:
            if current_cat is not None:
                print(f"  {'':50s}  {'SUBTOTAL':>12s}  "
                      f"${cat_total:>12,.2f}")
                print()
            current_cat = r["category"]
            cat_total = 0.0
            print(f"  ── {current_cat} ──")

        status_flag = ""
        if r["needs_review"]:
            status_flag = " [needs review]"
        elif r["status"] == "AGENT_VERIFIED":
            status_flag = " [verified]"

        label = f"{r['document_type']} ({r['issuer']})"
        print(f"    {r['tax_year']}  {label:<40s}  "
              f"{_fmt_amount(r['amount']):>13s}{status_flag}")

        cat_total += r["amount"] or 0
        grand_total += r["amount"] or 0

    if current_cat is not None:
        print(f"  {'':50s}  {'SUBTOTAL':>12s}  "
              f"${cat_total:>12,.2f}")

    print()
    print(f"  {'':50s}  {'TOTAL':>12s}  "
          f"${grand_total:>12,.2f}")
    print(f"\n  {len(rows)} document(s)")


# ── check ───────────────────────────────────────────────────────

def cmd_check(args):
    """Detect duplicates, missing data, and quality issues."""
    conn = _connect(args.db_path)
    cur = conn.cursor()
    issues = []

    dupes = cur.execute("""
        SELECT tax_year, document_type, issuer, amount,
               COUNT(*) as cnt, GROUP_CONCAT(id) as ids
        FROM tax_documents
        GROUP BY tax_year, document_type, issuer, amount
        HAVING cnt > 1
    """).fetchall()

    for d in dupes:
        issues.append({
            "type": "duplicate",
            "severity": "error",
            "message": (
                f"Duplicate: {d['document_type']} from "
                f"{d['issuer']} year {d['tax_year']} "
                f"{_fmt_amount(d['amount'])} — ids {d['ids']}"
            ),
            "ids": d["ids"],
        })

    # NULL and 0 mean different things now that the ingestor preserves
    # NULL: NULL is "unknown" (an error — it silently drops out of the
    # estimate), 0 is "the form really says zero" (worth a look, but
    # legitimate for e.g. a fully-offset 1099).
    missing = cur.execute("""
        SELECT id, tax_year, document_type, issuer, original_file
        FROM tax_documents
        WHERE amount IS NULL
          AND document_type NOT IN ('1099-HC', '1095-C', 'UNKNOWN')
    """).fetchall()

    for m in missing:
        issues.append({
            "type": "missing_amount",
            "severity": "error",
            "message": (
                f"Missing amount: id={m['id']} {m['document_type']} "
                f"from {m['issuer']} year {m['tax_year']} — "
                f"extraction failed; excluded from the tax estimate"
            ),
            "ids": str(m["id"]),
        })

    zeros = cur.execute("""
        SELECT id, tax_year, document_type, issuer, original_file
        FROM tax_documents
        WHERE amount = 0
          AND document_type NOT IN ('1099-HC', '1095-C', 'UNKNOWN')
    """).fetchall()

    for z in zeros:
        issues.append({
            "type": "zero_amount",
            "severity": "warning",
            "message": (
                f"Zero amount: id={z['id']} {z['document_type']} "
                f"from {z['issuer']} year {z['tax_year']} — "
                f"verify the form really reports $0"
            ),
            "ids": str(z["id"]),
        })

    pending = cur.execute("""
        SELECT COUNT(*) as cnt FROM tax_documents
        WHERE needs_review = 1
    """).fetchone()["cnt"]

    path_dupes = cur.execute("""
        SELECT pf1.file_path, pf2.file_path, pf1.file_hash
        FROM processed_files pf1
        JOIN processed_files pf2
          ON pf1.file_hash = pf2.file_hash
         AND pf1.file_path < pf2.file_path
        WHERE pf1.file_path LIKE '%Tax-Documents%'
    """).fetchall()

    for p in path_dupes:
        issues.append({
            "type": "path_duplicate",
            "severity": "warning",
            "message": (
                f"Same file under two paths:\n"
                f"      {p['file_path']}\n"
                f"      {p[1]}"
            ),
            "ids": None,
        })

    conn.close()

    if args.json_output:
        print(json.dumps({
            "issues": issues,
            "pending_review": pending,
        }, indent=2))
        return

    if not issues and pending == 0:
        print("  All clear — no issues found.")
        return

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for i in errors:
            print(f"    ✗ {i['message']}")
        print()

    if warnings:
        print(f"  WARNINGS ({len(warnings)}):")
        for i in warnings:
            print(f"    ! {i['message']}")
        print()

    print(f"  {pending} document(s) pending review.")


# ── list ────────────────────────────────────────────────────────

def cmd_list(args):
    """Simple flat list of all tax documents."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    where = "WHERE 1=1"
    params = []
    if args.year:
        where += " AND tax_year = ?"
        params.append(args.year)

    rows = cur.execute(
        f"""SELECT id, tax_year, document_type, issuer,
                   amount, currency, status, needs_review,
                   original_file
            FROM tax_documents {where}
            ORDER BY id""",
        params,
    ).fetchall()
    conn.close()

    if args.json_output:
        print(json.dumps([dict(r) for r in rows], indent=2,
                         default=str))
        return

    if not rows:
        print("No tax documents found.")
        return

    print(f"  {'ID':>4s}  {'Year':4s}  {'Type':<15s}  "
          f"{'Issuer':<20s}  {'Amount':>12s}  {'Status':<16s}")
    print(f"  {'─'*4}  {'─'*4}  {'─'*15}  "
          f"{'─'*20}  {'─'*12}  {'─'*16}")

    for r in rows:
        status = r["status"]
        if r["needs_review"]:
            status += " *"
        print(f"  {r['id']:4d}  {r['tax_year']}  "
              f"{r['document_type']:<15s}  "
              f"{(r['issuer'] or ''):<20s}  "
              f"{_fmt_amount(r['amount']):>12s}  {status}")

    print(f"\n  {len(rows)} document(s)  "
          f"(* = needs review)")


# ── ingest ──────────────────────────────────────────────────────


def cmd_ingest(args):
    """Ingest classified tax sidecars into tax_documents."""
    from housebook.core.database import Database

    from .ingestor import TaxGenericIngestor

    db_path = args.db_path or DB_PATH
    dry_run = getattr(args, "dry_run", False)
    db = Database(
        db_path, dry_run=dry_run,
        workspace_dir=str(WORKSPACE_DIR),
    )
    ingestor = TaxGenericIngestor(db, None)

    if not os.path.isdir(TAX_DIR):
        print("  tax/ directory not found.")
        sys.exit(1)

    result = ingestor.ingest_sidecar_directory(TAX_DIR)

    parts = []
    if result["ingested"]:
        parts.append(f"{result['ingested']} document(s)")
    if result["rows_written"]:
        parts.append(f"{result['rows_written']} row(s)")
    if result["skipped"]:
        parts.append(f"{result['skipped']} unchanged")
    if result["errors"]:
        parts.append(f"{len(result['errors'])} error(s)")
    print(f"  Tax ingest: {', '.join(parts) or 'nothing to do'}.")
    for path, err in result["errors"]:
        print(f"  ! {os.path.relpath(path, TAX_DIR)}: {err}")
    if dry_run:
        print("  (dry run — no changes written)")


# ── validate ────────────────────────────────────────────────────


def cmd_validate(args):
    """Validate all tax sidecars under tax/."""
    from housebook.core import sidecar as sidecar_mod

    from .schema import validate_data_block

    tax_root = Path(TAX_DIR)
    if not tax_root.is_dir():
        print("  tax/ directory not found.")
        sys.exit(1)

    total = 0
    passed = 0
    failed = 0
    all_errors: list[tuple[str, list[str]]] = []

    for jp in sorted(tax_root.rglob("*.json")):
        rel = jp.relative_to(tax_root)
        total += 1
        try:
            sc = sidecar_mod.load(str(jp))
        except sidecar_mod.SidecarError as e:
            all_errors.append((str(rel), [f"envelope: {e}"]))
            failed += 1
            continue
        if sc.source != "tax":
            all_errors.append(
                (str(rel), [f"source is {sc.source!r}"])
            )
            failed += 1
            continue
        errs = validate_data_block(sc.data)
        if errs:
            all_errors.append((str(rel), errs))
            failed += 1
        else:
            passed += 1

    if getattr(args, "json_output", False):
        out = {"total": total, "passed": passed,
               "failed": failed,
               "errors": [{"sidecar": p, "errors": e}
                          for p, e in all_errors]}
        print(json.dumps(out, indent=2))
    else:
        print(f"  Validated {total} sidecar(s): "
              f"{passed} passed, {failed} failed.")
        for path, errs in all_errors:
            print(f"\n  {path}:")
            for e in errs:
                print(f"    - {e}")

    sys.exit(1 if failed else 0)


# ── main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Tax document review and reporting",
        prog="housebook-tax",
    )
    parser.add_argument(
        "--db", dest="db_path", default=None,
        help="Override database path",
    )
    sub = parser.add_subparsers(dest="command")

    p_est = sub.add_parser(
        "estimate", help="Federal + state tax estimate",
    )
    p_est.add_argument(
        "--year", type=int, default=2025,
        help="Tax year (default: 2025)",
    )
    p_est.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    p_sum = sub.add_parser(
        "summary",
        help="Tax documents grouped by category with totals",
    )
    p_sum.add_argument("--year", type=int)
    p_sum.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    p_chk = sub.add_parser(
        "check", help="Detect duplicates and data quality issues",
    )
    p_chk.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    p_lst = sub.add_parser(
        "list", help="Simple flat list of all tax documents",
    )
    p_lst.add_argument("--year", type=int)
    p_lst.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    p_ing = sub.add_parser(
        "ingest",
        help="Ingest classified tax sidecars",
    )
    p_ing.add_argument(
        "--dry-run", action="store_true",
    )



    p_val = sub.add_parser(
        "validate",
        help="Validate all tax sidecars",
    )
    p_val.add_argument(
        "--json", dest="json_output", action="store_true",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "estimate": cmd_estimate,
        "summary": cmd_summary,
        "check": cmd_check,
        "list": cmd_list,
        "ingest": cmd_ingest,
        "validate": cmd_validate,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
