"""housebook-hsa CLI -- HSA Shoebox management.

Subcommands: summary, list, check, scan, candidates, plan, verify,
             merge, link-doc, providers.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

from housebook.config.settings import (
    DB_PATH,
    HSA_PROVIDERS_JSON,
    WORKSPACE_DIR,
)
from housebook.core import sidecar as sidecar_mod
from housebook.hsa.matching import find_candidate_matches
from housebook.hsa.math_proof import calculate_payment_math
from housebook.hsa.providers import ProviderResolver

LEVEL_STUB = "stub"
LEVEL_WEAK = "weak"
LEVEL_READY = "ready"
LEVEL_STRONG = "strong"
VALID_LEVELS = (LEVEL_STUB, LEVEL_WEAK, LEVEL_READY, LEVEL_STRONG)
REIMBURSABLE_LEVELS = (LEVEL_READY, LEVEL_STRONG)

LEVEL_BADGES = {
    LEVEL_STUB: "[ ]",
    LEVEL_WEAK: "[?]",
    LEVEL_READY: "[✓]",
    LEVEL_STRONG: "[✓+]",
}

VALID_EXCLUSION_REASONS = (
    "fsa_paid", "hra_paid", "trivial_amount",
    "missing_receipt", "non_qualified",
)


def _connect(db_path=None):
    """Return a WAL-mode connection with Row factory."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _log_change(conn, record_id, field, old_val, new_val,
                changed_by="agent", table_name="hsa_expenses"):
    """Write an entry to hsa_audit_log."""
    conn.execute(
        "INSERT INTO hsa_audit_log "
        "(table_name, record_id, field_name, "
        "old_value, new_value, changed_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            table_name,
            record_id,
            field,
            str(old_val) if old_val is not None else None,
            str(new_val) if new_val is not None else None,
            changed_by,
        ),
    )


# ── summary ────────────────────────────────────────────────────


def cmd_summary(args):
    """Show HSA shoebox totals by year, patient, and category."""
    conn = _connect(args.db_path)

    where = "WHERE status != 'DELETED' AND (plan_role IS NULL OR plan_role != 'master')"
    params = []
    if args.year:
        where += " AND strftime('%Y', service_date) = ?"
        params.append(str(args.year))
    if args.patient:
        where += " AND patient = ?"
        params.append(args.patient)

    rows = conn.execute(
        f"""SELECT
                strftime('%Y', service_date) AS year,
                patient,
                category,
                status,
                COUNT(*) AS count,
                SUM(patient_responsibility) AS total
            FROM hsa_expenses
            {where}
            GROUP BY year, patient, category, status
            ORDER BY year, patient, category""",
        params,
    ).fetchall()

    totals = conn.execute(
        f"""SELECT
                status,
                COUNT(*) AS count,
                SUM(patient_responsibility) AS total
            FROM hsa_expenses {where}
            GROUP BY status""",
        params,
    ).fetchall()

    pending = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM hsa_expenses {where} AND needs_review = 1",
        params,
    ).fetchone()["cnt"]

    reimbursable_ph = ",".join("?" for _ in REIMBURSABLE_LEVELS)
    reimbursable_total = conn.execute(
        f"SELECT COALESCE(SUM(patient_responsibility), 0) "
        f"AS total FROM hsa_expenses "
        f"{where} AND status = 'UNREIMBURSED' "
        f"AND exclusion_reason IS NULL "
        f"AND evidence_level IN ({reimbursable_ph})",
        params + list(REIMBURSABLE_LEVELS),
    ).fetchone()["total"]

    excluded = conn.execute(
        f"SELECT COUNT(*) AS count, "
        f"COALESCE(SUM(patient_responsibility), 0) AS total "
        f"FROM hsa_expenses {where} "
        f"AND exclusion_reason IS NOT NULL",
        params,
    ).fetchone()

    evidence_totals = conn.execute(
        f"SELECT evidence_level, COUNT(*) AS count, "
        f"COALESCE(SUM(patient_responsibility), 0) AS total "
        f"FROM hsa_expenses {where} AND status = 'UNREIMBURSED' "
        f"GROUP BY evidence_level ORDER BY evidence_level",
        params,
    ).fetchall()

    conn.close()

    if args.json_output:
        out = {
            "by_group": [dict(r) for r in rows],
            "totals": [dict(r) for r in totals],
            "pending_review": pending,
            "reimbursable_total": reimbursable_total,
            "excluded_count": excluded["count"],
            "excluded_total": excluded["total"],
            "evidence_totals": [dict(r) for r in evidence_totals],
        }
        print(json.dumps(out, indent=2, default=str))
        return

    if not rows:
        print("  No HSA expenses found.")
        return

    grand_total = 0.0
    unreimbursed = 0.0

    print("\n  HSA Shoebox Summary")
    print(f"  {'=' * 60}")

    current_year = None
    for r in rows:
        if r["year"] != current_year:
            current_year = r["year"]
            print(f"\n  -- {current_year} --")

        status_mark = ""
        if r["status"] == "REIMBURSED":
            status_mark = " [reimbursed]"
        elif r["status"] == "PENDING":
            status_mark = " [pending]"

        print(
            f"    {r['patient']:<10s}  {r['category']:<15s}  "
            f"{r['count']:3d} items  "
            f"${r['total']:>10,.2f}{status_mark}"
        )
        grand_total += r["total"] or 0
        if r["status"] == "UNREIMBURSED":
            unreimbursed += r["total"] or 0

    print(f"\n  {'─' * 60}")
    for t in totals:
        print(
            f"    {t['status']:<20s}  {t['count']:3d} items  "
            f"${t['total']:>10,.2f}"
        )

    if evidence_totals:
        print("\n  Readiness Breakdown (unreimbursed):")
        for et in evidence_totals:
            badge = LEVEL_BADGES.get(et["evidence_level"], "[ ]")
            print(
                f"    {badge:5s}  {et['evidence_level'] or 'unknown':<8s}  "
                f"{et['count']:3d} items  ${et['total']:>10,.2f}"
            )

    print(f"\n  Total medical spending:     ${grand_total:>10,.2f}")
    print(f"  Total unreimbursed:        ${unreimbursed:>10,.2f}")
    print(f"  Reimbursable (IRS-proof):  ${reimbursable_total:>10,.2f}")
    if excluded["count"]:
        n = excluded["count"]
        t = excluded["total"]
        print(f"  Excluded ({n:d} items):       ${t:>10,.2f}")
    if pending:
        print(f"  Pending review:            {pending:>10d}")
    print()


# ── list ───────────────────────────────────────────────────────


def cmd_list(args):
    """Flat list of HSA expenses."""
    conn = _connect(args.db_path)

    where = "WHERE e.status != 'DELETED'"
    params = []
    if args.year:
        where += " AND strftime('%Y', e.service_date) = ?"
        params.append(str(args.year))
    if args.status:
        where += " AND e.status = ?"
        params.append(args.status.upper())
    if args.patient:
        where += " AND e.patient = ?"
        params.append(args.patient)
    if args.needs_review:
        where += " AND e.needs_review = 1"
    if getattr(args, "excluded", False):
        where += " AND e.exclusion_reason IS NOT NULL"

    rows = conn.execute(
        f"""SELECT e.id, e.service_date, e.provider, e.patient,
                   e.description, e.patient_responsibility,
                   e.category, e.source, e.status,
                   e.needs_review, e.transaction_id,
                   e.evidence_level, e.exclusion_reason,
                   COUNT(d.id) AS doc_count
            FROM hsa_expenses e
            LEFT JOIN hsa_documents d ON d.expense_id = e.id
            {where}
            GROUP BY e.id
            ORDER BY e.service_date DESC, e.id DESC""",
        params,
    ).fetchall()
    conn.close()

    if args.json_output:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        print("  No HSA expenses found.")
        return

    print(
        f"  {'ID':>4s}  {'Date':10s}  {'Provider':<25s}  "
        f"{'Patient':<8s}  {'Amount':>10s}  "
        f"{'Evidence':<13s}  {'Docs':>4s}"
    )
    print(
        f"  {'─' * 4}  {'─' * 10}  {'─' * 25}  {'─' * 8}  "
        f"{'─' * 10}  {'─' * 13}  {'─' * 4}"
    )

    for r in rows:
        level = r["evidence_level"] or LEVEL_STUB
        badge = LEVEL_BADGES.get(level, "[ ]")
        evidence = f"{badge} {level}"
        if r["needs_review"]:
            evidence += " *"
        if r["exclusion_reason"]:
            evidence = f"[X] {r['exclusion_reason']}"

        provider = (r["provider"] or "")[:25]
        print(
            f"  {r['id']:4d}  {r['service_date'] or '          ':10s}  "
            f"{provider:<25s}  {r['patient'] or '':<8s}  "
            f"${r['patient_responsibility']:>9,.2f}  "
            f"{evidence:<13s}  {r['doc_count']:4d}"
        )

    print(f"\n  {len(rows)} expense(s)  (* = needs review, [X] = excluded)")


# ── check ──────────────────────────────────────────────────────


def cmd_check(args):
    """Data quality checks for HSA expenses."""
    conn = _connect(args.db_path)
    issues = []

    # Duplicates by service_date + patient + amount
    dupes = conn.execute("""
        SELECT service_date, patient, patient_responsibility,
               COUNT(*) AS cnt, GROUP_CONCAT(id) AS ids,
               GROUP_CONCAT(provider) AS providers
        FROM hsa_expenses
        WHERE status != 'DELETED'
        GROUP BY service_date, patient, patient_responsibility
        HAVING cnt > 1
    """).fetchall()

    for d in dupes:
        issues.append(
            {
                "type": "duplicate",
                "severity": "warning",
                "message": (
                    f"Possible duplicate: providers '{d['providers']}' on "
                    f"{d['service_date']} for patient '{d['patient']}' "
                    f"${d['patient_responsibility']:.2f} - ids {d['ids']}"
                ),
            }
        )

    # Duplicates by claim_id
    duplicate_claims = conn.execute("""
        SELECT json_extract(d.raw_data, '$.claim_id') AS claim_id,
               GROUP_CONCAT(e.id) AS ids,
               GROUP_CONCAT(e.status) AS statuses
        FROM hsa_documents d
        JOIN hsa_expenses e ON e.id = d.expense_id
        WHERE json_extract(d.raw_data, '$.claim_id') IS NOT NULL
        GROUP BY json_extract(d.raw_data, '$.claim_id')
        HAVING COUNT(DISTINCT e.id) > 1
    """).fetchall()

    for dc in duplicate_claims:
        # Ignore if all but one are DELETED, but flag otherwise
        active = [s for s in dc['statuses'].split(',') if s != 'DELETED']
        if len(active) > 1:
            issues.append(
                {
                    "type": "duplicate_claim",
                    "severity": "error",
                    "message": (
                        f"Duplicate claim ID {dc['claim_id']} "
                        f"found across expenses: {dc['ids']}"
                    )
                }
            )

    # Expenses with no documents
    no_docs = conn.execute("""
        SELECT e.id, e.service_date, e.provider, e.source
        FROM hsa_expenses e
        LEFT JOIN hsa_documents d ON d.expense_id = e.id
        WHERE d.id IS NULL AND e.source != 'cc_stub'
    """).fetchall()

    for n in no_docs:
        issues.append(
            {
                "type": "missing_document",
                "severity": "warning",
                "message": (
                    f"No documents: id={n['id']} {n['provider']} on {n['service_date']}"
                ),
            }
        )

    # CC stubs without receipts (the whole point of stubs)
    stubs_no_docs = conn.execute("""
        SELECT COUNT(*) AS cnt FROM hsa_expenses
        WHERE source = 'cc_stub' AND needs_review = 1
    """).fetchone()["cnt"]

    # Document integrity
    integrity = conn.execute("""
        SELECT d.id, d.file_path, d.file_hash
        FROM hsa_documents d
    """).fetchall()

    integrity_issues = 0

    for doc in integrity:
        path = doc["file_path"]
        # Resolve relative paths
        if not os.path.isabs(path):
            abs_path = os.path.join(WORKSPACE_DIR, path)
        else:
            abs_path = path

        if not os.path.exists(abs_path):
            issues.append(
                {
                    "type": "missing_file",
                    "severity": "error",
                    "message": (f"File missing: doc_id={doc['id']} {path}"),
                }
            )
            integrity_issues += 1
            continue

        # Actually verify the stored hash. Existence alone is not
        # integrity: a corrupted or swapped receipt is precisely the
        # tampering an IRS-audit-proof ledger must detect, and it used
        # to pass `check` clean. Re-hashing is IO-bound, so it is
        # opt-in via --verify-hashes.
        if getattr(args, "verify_hashes", False) and doc["file_hash"]:
            actual = sidecar_mod.sha256_file(abs_path)
            if actual != doc["file_hash"]:
                issues.append(
                    {
                        "type": "hash_mismatch",
                        "severity": "error",
                        "message": (
                            f"Content changed since ingest: "
                            f"doc_id={doc['id']} {path} "
                            f"(recorded {doc['file_hash'][:12]}…, "
                            f"found {actual[:12]}…)"
                        ),
                    }
                )
                integrity_issues += 1

    # Math proof: consolidated payments must sum correctly. Use the
    # same projection as verify so audit diagnostics and promotion
    # guards cannot disagree.
    linked_transactions = conn.execute(
        "SELECT DISTINCT transaction_id FROM hsa_expenses "
        "WHERE transaction_id IS NOT NULL AND status != 'DELETED'"
    ).fetchall()
    for linked in linked_transactions:
        proof = calculate_payment_math(conn, linked["transaction_id"])
        if not proof.transaction_exists:
            issues.append({
                "type": "missing_payment_transaction",
                "severity": "error",
                "message": (
                    f"Expenses {list(proof.expense_ids)} reference "
                    f"missing transaction {proof.transaction_id}"
                ),
            })
        elif not proof.balanced:
            issues.append({
                "type": "math_proof_mismatch",
                "severity": "error",
                "message": (
                    f"Consolidated payment mismatch: "
                    f"txn {proof.transaction_id} "
                    f"(${proof.transaction_amount:.2f}) != "
                    f"expenses {list(proof.expense_ids)} "
                    f"(${proof.expense_total:.2f})"
                ),
            })

    # Expenses marked ready but missing receipt document
    ready_no_receipt = conn.execute("""
        SELECT e.id, e.provider, e.service_date
        FROM hsa_expenses e
        WHERE e.evidence_level = ?
          AND e.status != 'DELETED'
          AND NOT EXISTS (
              SELECT 1 FROM hsa_documents d
              WHERE d.expense_id = e.id
                AND d.document_type = 'receipt'
          )
    """, (LEVEL_READY,)).fetchall()

    for r in ready_no_receipt:
        issues.append({
            "type": "ready_without_receipt",
            "severity": "info",
            "message": (
                f"Ready but no receipt: id={r['id']} "
                f"{r['provider']} on {r['service_date']}"
            ),
        })

    # Weak expenses without transaction link
    orphan_weak = conn.execute("""
        SELECT id, provider, service_date,
               patient_responsibility
        FROM hsa_expenses
        WHERE evidence_level = ?
          AND transaction_id IS NULL
          AND status != 'DELETED'
    """, (LEVEL_WEAK,)).fetchall()

    for o in orphan_weak:
        issues.append({
            "type": "weak_without_link",
            "severity": "warning",
            "message": (
                f"Weak evidence without payment link: "
                f"id={o['id']} {o['provider']} "
                f"${o['patient_responsibility']:.2f}"
            ),
        })

    # Payment plan integrity checks
    orphan_plan_links = conn.execute("""
        SELECT e.id, e.payment_plan_id
        FROM hsa_expenses e
        LEFT JOIN hsa_payment_plans p ON e.payment_plan_id = p.id
        WHERE e.payment_plan_id IS NOT NULL AND p.id IS NULL
    """).fetchall()
    for o in orphan_plan_links:
        issues.append({
            "type": "orphaned_plan_link",
            "severity": "error",
            "message": (
                f"Expense id={o['id']} references "
                f"non-existent plan #{o['payment_plan_id']}"
            ),
        })

    bad_master_level = conn.execute("""
        SELECT id, evidence_level
        FROM hsa_expenses
        WHERE plan_role = 'master'
          AND evidence_level IN (?, ?)
          AND status != 'DELETED'
    """, REIMBURSABLE_LEVELS).fetchall()
    for b in bad_master_level:
        issues.append({
            "type": "master_reimbursable",
            "severity": "error",
            "message": (
                f"Plan master id={b['id']} has reimbursable "
                f"evidence_level '{b['evidence_level']}' "
                f"— masters must stay stub"
            ),
        })

    plan_sums = conn.execute("""
        SELECT p.id, p.name, p.total_liability,
               COALESCE(SUM(e.patient_responsibility), 0) AS installment_total,
               COUNT(e.id) AS installment_count
        FROM hsa_payment_plans p
        LEFT JOIN hsa_expenses e
            ON e.payment_plan_id = p.id
            AND e.plan_role = 'installment'
            AND e.status != 'DELETED'
        GROUP BY p.id
    """).fetchall()
    for ps in plan_sums:
        if ps["installment_count"] == 0:
            issues.append({
                "type": "plan_no_installments",
                "severity": "warning",
                "message": (
                    f"Plan #{ps['id']} '{ps['name']}' has "
                    f"no installments"
                ),
            })

    pending = conn.execute(
        "SELECT COUNT(*) AS cnt FROM hsa_expenses WHERE needs_review = 1"
    ).fetchone()["cnt"]

    conn.close()

    if args.json_output:
        print(
            json.dumps(
                {
                    "issues": issues,
                    "pending_review": pending,
                    "stubs_pending": stubs_no_docs,
                },
                indent=2,
            )
        )
        return

    if not issues and pending == 0:
        print("  All clear - no issues found.")
        return

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for i in errors:
            print(f"    x {i['message']}")

    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for i in warnings:
            print(f"    ! {i['message']}")

    infos = [i for i in issues if i["severity"] == "info"]
    if infos:
        print(f"\n  AUDIT QUALITY ({len(infos)}):")
        for i in infos:
            print(f"    i {i['message']}")

    print(f"\n  {pending} expense(s) pending review.")
    if stubs_no_docs:
        print(f"  {stubs_no_docs} CC stub(s) awaiting receipt collection.")


# ── scan ───────────────────────────────────────────────────────


def cmd_scan(args):
    """Scan CC transactions for medical expenses."""
    from housebook.hsa.scanner import (
        scan_cc_transactions,
    )

    stubs = scan_cc_transactions(
        db_path=args.db_path,
        dry_run=args.dry_run,
    )

    if args.json_output:
        print(json.dumps(stubs, indent=2, default=str))
        return

    if not stubs:
        print("  No new medical transactions found.")
        return

    prefix = "Would create" if args.dry_run else "Created"
    print(f"\n  {prefix} {len(stubs)} HSA stub(s):\n")

    for s in stubs:
        provider = (s["provider"] or "")[:40]
        print(
            f"    {s['service_date']}  ${s['amount']:>9,.2f}  "
            f"{s['category']:<12s}  {provider}"
        )

    total = sum(s["amount"] for s in stubs)
    print(f"\n  Total: ${total:,.2f}")

    if args.dry_run:
        print("\n  (dry run - no changes written)")


# ── verify ─────────────────────────────────────────────────────


def cmd_verify(args):
    """Mark expenses as agent-verified."""
    conn = _connect(args.db_path)

    ids = args.ids
    placeholders = ",".join("?" for _ in ids)

    rows = conn.execute(
        f"SELECT id, category, patient, provider, "
        f"needs_review, evidence_level, transaction_id, "
        f"notes, payment_method, payment_date, plan_role, "
        f"exclusion_reason, patient_responsibility "
        f"FROM hsa_expenses "
        f"WHERE id IN ({placeholders})",
        ids,
    ).fetchall()

    if not rows:
        print("  No matching expenses found.")
        conn.close()
        return

    verified = 0
    for row in rows:
        # ── Guards run before ANY write. A blocked row must produce
        # nothing — previously the category/patient/provider audit-log
        # INSERTs had already happened when a guard hit `continue`, so
        # the IRS-defense log recorded changes that were never applied.
        if args.evidence_level:
            if (row["plan_role"] == "master"
                    and args.evidence_level in REIMBURSABLE_LEVELS):
                print(
                    f"  Blocked: id={row['id']} is a plan master "
                    f"— masters cannot be set to "
                    f"'{args.evidence_level}'. Only installments "
                    f"are directly reimbursable."
                )
                continue

            # The link being created in this same call counts too:
            # checking only the pre-update transaction_id let
            # `--transaction-id X --evidence-level ready` on a
            # previously-unlinked row bypass the math proof entirely.
            effective_txn = args.transaction_id or row["transaction_id"]
            if (args.evidence_level in REIMBURSABLE_LEVELS
                    and effective_txn):
                proof = calculate_payment_math(
                    conn,
                    effective_txn,
                    proposed_expense=(
                        row["id"], row["patient_responsibility"] or 0,
                    ),
                )
                if not proof.transaction_exists:
                    print(
                        f"  Blocked: id={row['id']} links to "
                        f"transaction {effective_txn}, which does "
                        f"not exist."
                    )
                    continue
                if not proof.balanced:
                    print(
                        f"  Blocked: id={row['id']} math proof "
                        f"fails — CC charge "
                        f"${proof.transaction_amount:.2f} != "
                        f"expenses ${proof.expense_total:.2f}. "
                        f"Fix amounts or link missing expenses."
                    )
                    continue

        updates = []
        params = []

        if args.category:
            _log_change(conn, row["id"], "category", row["category"], args.category)
            updates.append("category = ?")
            params.append(args.category)

        if args.patient:
            _log_change(conn, row["id"], "patient", row["patient"], args.patient)
            updates.append("patient = ?")
            params.append(args.patient)

        if args.provider:
            _log_change(conn, row["id"], "provider", row["provider"], args.provider)
            updates.append("provider = ?")
            params.append(args.provider)

        if args.evidence_level:
            _log_change(
                conn,
                row["id"],
                "evidence_level",
                row["evidence_level"],
                args.evidence_level,
            )
            updates.append("evidence_level = ?")
            params.append(args.evidence_level)

        if args.transaction_id:
            _log_change(
                conn,
                row["id"],
                "transaction_id",
                row["transaction_id"],
                args.transaction_id,
            )
            updates.append("transaction_id = ?")
            params.append(args.transaction_id)

        if args.notes:
            _log_change(conn, row["id"], "notes", row["notes"], args.notes)
            updates.append("notes = ?")
            params.append(args.notes)

        if args.payment_method:
            _log_change(
                conn,
                row["id"],
                "payment_method",
                row["payment_method"],
                args.payment_method,
            )
            updates.append("payment_method = ?")
            params.append(args.payment_method)

        if args.payment_date:
            _log_change(
                conn, row["id"], "payment_date", row["payment_date"], args.payment_date
            )
            updates.append("payment_date = ?")
            params.append(args.payment_date)

        if getattr(args, "exclude", None):
            _log_change(
                conn, row["id"], "exclusion_reason",
                row["exclusion_reason"], args.exclude,
            )
            updates.append("exclusion_reason = ?")
            params.append(args.exclude)
        elif getattr(args, "include", False):
            if row["exclusion_reason"]:
                _log_change(
                    conn, row["id"], "exclusion_reason",
                    row["exclusion_reason"], None,
                )
                updates.append("exclusion_reason = NULL")

        if row["needs_review"]:
            _log_change(conn, row["id"], "needs_review", "1", "0")
            updates.append("needs_review = 0")

        _log_change(conn, row["id"], "updated_at", None, datetime.now().isoformat())
        updates.append("updated_at = CURRENT_TIMESTAMP")

        if updates:
            # Safe: updates contains only hardcoded literals like "category = ?"
            sql = f"UPDATE hsa_expenses SET {', '.join(updates)} WHERE id = ?"
            params.append(row["id"])
            conn.execute(sql, params)
            verified += 1

    conn.commit()
    conn.close()
    if verified == len(rows):
        print(f"  Verified {verified} expense(s).")
    else:
        print(f"  Verified {verified} of {len(rows)} expense(s) "
              f"(blocked rows unchanged).")


# ── link-doc ───────────────────────────────────────────────────


def cmd_link_doc(args):
    """Attach a document to an existing expense."""
    conn = _connect(args.db_path)

    # Verify expense exists
    row = conn.execute(
        "SELECT id FROM hsa_expenses WHERE id = ?",
        (args.expense_id,),
    ).fetchone()
    if not row:
        print(f"  Expense {args.expense_id} not found.")
        conn.close()
        return

    if not os.path.isfile(args.file_path):
        print(f"  File not found: {args.file_path}")
        conn.close()
        return

    # Normalize path: convert to relative if within workspace
    file_path = args.file_path
    if WORKSPACE_DIR and os.path.isabs(file_path):
        try:
            rel = os.path.relpath(file_path, WORKSPACE_DIR)
            if not rel.startswith(".."):
                file_path = rel
        except ValueError:
            pass

    # Calculate hash
    import hashlib

    sha256 = hashlib.sha256()
    with open(args.file_path, "rb") as f:
        while True:
            data = f.read(4096)
            if not data:
                break
            sha256.update(data)
    file_hash = sha256.hexdigest()

    # Detect doc type from parent dir or extension
    parent = os.path.basename(os.path.dirname(args.file_path)).lower()
    doc_type = "other"
    if parent in ("receipts", "receipt"):
        doc_type = "receipt"
    elif parent in ("eobs", "eob"):
        doc_type = "eob"
    elif parent in ("statements", "statement"):
        doc_type = "statement"

    conn.execute(
        "INSERT INTO hsa_documents "
        "(expense_id, document_type, file_path, file_hash, "
        "original_filename) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            args.expense_id,
            doc_type,
            file_path,
            file_hash,
            os.path.basename(args.file_path),
        ),
    )
    conn.commit()
    conn.close()
    print(
        f"  Linked {os.path.basename(args.file_path)} "
        f"to expense {args.expense_id} as '{doc_type}'."
    )


# ── candidates ─────────────────────────────────────────────────


def cmd_candidates(args):
    """Find potential matches between expenses and stubs."""
    conn = _connect(args.db_path)
    resolver = ProviderResolver()

    # 1. Get pending expenses (receipts/eobs/invoices)
    expenses = conn.execute(
        "SELECT * FROM hsa_expenses "
        "WHERE status = 'UNREIMBURSED' "
        "AND source IN ('receipt', 'eob', 'invoice') "
        "AND needs_review = 1"
    ).fetchall()

    # 2. Get available stubs
    stubs = conn.execute(
        "SELECT * FROM hsa_expenses "
        "WHERE status = 'UNREIMBURSED' "
        "AND source = 'cc_stub' "
        "AND needs_review = 1"
    ).fetchall()

    # Build CC provenance lookup: transaction_id → source statement
    stub_tx_ids = [
        s["transaction_id"] for s in stubs
        if s["transaction_id"]
    ]
    cc_provenance: dict[int, str] = {}
    if stub_tx_ids:
        placeholders = ",".join("?" for _ in stub_tx_ids)
        prov_rows = conn.execute(
            f"SELECT id, source_file_path FROM transactions "
            f"WHERE id IN ({placeholders})",
            stub_tx_ids,
        ).fetchall()
        for pr in prov_rows:
            if pr["source_file_path"]:
                cc_provenance[pr["id"]] = pr["source_file_path"]

    matches, installment_patterns = find_candidate_matches(
        expenses,
        stubs,
        resolver=resolver,
        cc_provenance=cc_provenance,
    )

    conn.close()

    if args.json_output:
        print(json.dumps(
            {"exact_matches": matches, "installment_patterns": installment_patterns},
            indent=2, default=str,
        ))
        return

    if not matches and not installment_patterns:
        print("  No obvious candidates found.")
        return

    if matches:
        print(f"\n  Found {len(matches)} exact match candidate(s):\n")
        print(
            f"  {'Score':>5s}  {'Expense (ID)':<30s}  {'Stub (ID)':<30s}  "
            f"{'Lag':>4s}  {'Amount':>10s}  CC Statement"
        )
        print(
            f"  {'─' * 5}  {'─' * 30}  {'─' * 30}  "
            f"{'─' * 4}  {'─' * 10}  {'─' * 30}"
        )

        for m in matches:
            e = m["expense"]
            s = m["stub"]
            e_desc = f"{e['service_date']} {e['provider'][:18]} ({e['id']})"
            s_desc = f"{s['service_date']} {s['provider'][:18]} ({s['id']})"
            cc_stmt = os.path.basename(
                m.get("cc_statement", "")
            ) if m.get("cc_statement") else ""
            print(
                f"  {m['score']:5d}  {e_desc:<30s}  {s_desc:<30s}  "
                f"{m['delta_days']:3d}d  "
                f"${e['patient_responsibility']:>9,.2f}  {cc_stmt}"
            )

    if installment_patterns:
        print(f"\n  Found {len(installment_patterns)} potential installment "
              f"pattern(s):\n")
        for p in installment_patterns:
            stub_ids_str = ",".join(str(i) for i in p["stub_ids"])
            print(f"  POTENTIAL_INSTALLMENT — {p['provider']}")
            total_paid = p['installment_amount'] * p['installment_count']
            print(f"    Amount:      ${p['installment_amount']:,.2f}/payment  "
                  f"× {p['installment_count']} payments "
                  f"= ${total_paid:,.2f} total paid")
            if p["master_candidates"]:
                for m in p["master_candidates"]:
                    print(f"    Master bill: #{m['id']}  "
                          f"${m['patient_responsibility']:,.2f}  "
                          f"[{m['source']}]  {m['service_date']}")
                master_ids = ",".join(str(m["id"]) for m in p["master_candidates"])
                print(f"    Suggest:     housebook-hsa plan "
                      f"--master {master_ids} "
                      f"--installments {stub_ids_str} "
                      f'--name "{p["provider"]} Payment Plan"')
            else:
                print("    No master bill found — file an INV sidecar, then:")
                print(f"    Suggest:     housebook-hsa plan "
                      f"--master <ingest_id> "
                      f"--installments {stub_ids_str} "
                      f'--name "{p["provider"]} Payment Plan"')
            print()
    print()


# ── plan ───────────────────────────────────────────────────────


def _print_plan(plan, masters, installments):
    total_paid = sum(i["patient_responsibility"] or 0 for i in installments)
    pct = (total_paid / plan["total_liability"] * 100) if plan["total_liability"] else 0
    print(f"\n  Plan #{plan['id']}: {plan['name']}")
    print(f"  Total liability:   ${plan['total_liability']:>10,.2f}")
    print(f"  Total paid:        ${total_paid:>10,.2f}  ({pct:.1f}%)")
    remaining = plan["total_liability"] - total_paid
    print(f"  Remaining:         ${remaining:>10,.2f}")
    if plan["notes"]:
        print(f"  Notes: {plan['notes']}")
    print("\n  Master records (not directly reimbursable):")
    for m in masters:
        badge = LEVEL_BADGES.get(m["evidence_level"], "   ")
        print(f"    {badge} #{m['id']:>4d}  {m['service_date']}  "
              f"${m['patient_responsibility']:>9,.2f}  {m['provider']}")
    print(f"\n  Installments ({len(installments)} payments):")
    for i in installments:
        badge = LEVEL_BADGES.get(i["evidence_level"], "   ")
        print(f"    {badge} #{i['id']:>4d}  {i['service_date']}  "
              f"${i['patient_responsibility']:>9,.2f}  {i['provider']}")
    print()


def cmd_plan(args):
    """Create or display a payment plan."""
    conn = _connect(args.db_path)

    # --list: show all plans
    if args.list_plans:
        plans = conn.execute(
            "SELECT p.*, "
            " (SELECT COUNT(*) FROM hsa_expenses "
            "  WHERE payment_plan_id = p.id AND plan_role = 'installment') "
            "  AS installment_count "
            "FROM hsa_payment_plans p ORDER BY p.created_at"
        ).fetchall()
        if args.json_output:
            print(json.dumps([dict(p) for p in plans], indent=2, default=str))
            conn.close()
            return
        if not plans:
            print("  No payment plans found.")
            conn.close()
            return
        for p in plans:
            print(f"  #{p['id']}  {p['name']}  "
                  f"${p['total_liability']:,.2f}  "
                  f"({p['installment_count']} installments)")
        conn.close()
        return

    # --show: display a single plan
    if args.show_id is not None:
        plan = conn.execute(
            "SELECT * FROM hsa_payment_plans WHERE id = ?", (args.show_id,)
        ).fetchone()
        if not plan:
            print(f"  Plan #{args.show_id} not found.")
            conn.close()
            return
        masters = conn.execute(
            "SELECT * FROM hsa_expenses "
            "WHERE payment_plan_id = ? AND plan_role = 'master'",
            (args.show_id,),
        ).fetchall()
        installments = conn.execute(
            "SELECT * FROM hsa_expenses "
            "WHERE payment_plan_id = ? AND plan_role = 'installment' "
            "ORDER BY service_date",
            (args.show_id,),
        ).fetchall()
        if args.json_output:
            print(json.dumps({
                "plan": dict(plan),
                "masters": [dict(m) for m in masters],
                "installments": [dict(i) for i in installments],
            }, indent=2, default=str))
        else:
            _print_plan(dict(plan), [dict(m) for m in masters],
                        [dict(i) for i in installments])
        conn.close()
        return

    # Create a new plan
    if not args.master_ids or not args.installment_ids:
        print("  --master and --installments are required to create a plan.")
        conn.close()
        return

    master_ids = args.master_ids
    installment_ids = args.installment_ids

    # Validate all IDs exist and are not already linked
    all_ids = master_ids + installment_ids
    rows = conn.execute(
        f"SELECT id, patient_responsibility, source, payment_plan_id "
        f"FROM hsa_expenses "
        f"WHERE id IN ({','.join('?' * len(all_ids))})",
        all_ids,
    ).fetchall()
    found = {r["id"] for r in rows}
    missing = set(all_ids) - found
    if missing:
        print(f"  Expense IDs not found: {sorted(missing)}")
        conn.close()
        return

    already_linked = [r for r in rows if r["payment_plan_id"]]
    if already_linked:
        for r in already_linked:
            print(f"  ID {r['id']} already linked to plan "
                  f"#{r['payment_plan_id']}.")
        conn.close()
        return

    # Derive total_liability from master records
    master_rows = [r for r in rows if r["id"] in set(master_ids)]
    total_liability = sum(r["patient_responsibility"] or 0 for r in master_rows)

    name = args.name or f"Payment Plan ({', '.join(str(i) for i in master_ids)})"

    cur = conn.execute(
        "INSERT INTO hsa_payment_plans (name, total_liability, notes) VALUES (?, ?, ?)",
        (name, total_liability, args.notes),
    )
    plan_id = cur.lastrowid

    # Mark masters — keep evidence_level as stub (not directly reimbursable)
    for mid in master_ids:
        conn.execute(
            "UPDATE hsa_expenses SET payment_plan_id = ?, plan_role = 'master', "
            "evidence_level = 'stub', "
            "notes = CASE WHEN notes IS NULL THEN ? "
            "             ELSE notes || '; ' || ? END, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (plan_id,
             f"Plan master #{plan_id} — not directly reimbursable",
             f"Plan master #{plan_id} — not directly reimbursable",
             mid),
        )
        _log_change(conn, mid, "payment_plan_id", None, plan_id)
        _log_change(conn, mid, "plan_role", None, "master")

    # Mark installments
    for iid in installment_ids:
        conn.execute(
            "UPDATE hsa_expenses SET payment_plan_id = ?, plan_role = 'installment', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (plan_id, iid),
        )
        _log_change(conn, iid, "payment_plan_id", None, plan_id)
        _log_change(conn, iid, "plan_role", None, "installment")

    conn.commit()

    masters = conn.execute(
        "SELECT * FROM hsa_expenses WHERE payment_plan_id = ? AND plan_role = 'master'",
        (plan_id,),
    ).fetchall()
    installments = conn.execute(
        "SELECT * FROM hsa_expenses "
        "WHERE payment_plan_id = ? AND plan_role = 'installment' "
        "ORDER BY service_date",
        (plan_id,),
    ).fetchall()
    conn.close()

    if args.json_output:
        print(json.dumps({
            "plan_id": plan_id,
            "name": name,
            "total_liability": total_liability,
            "master_ids": master_ids,
            "installment_ids": installment_ids,
        }, indent=2))
        return

    plan_dict = {"id": plan_id, "name": name,
                 "total_liability": total_liability, "notes": args.notes}
    _print_plan(plan_dict, [dict(m) for m in masters],
                [dict(i) for i in installments])


# ── delete ─────────────────────────────────────────────────────


def cmd_delete(args):
    """Soft-delete one or more expenses."""
    conn = _connect(args.db_path)
    deleted = []
    skipped = []

    for eid in args.ids:
        row = conn.execute(
            "SELECT id, status, notes FROM hsa_expenses WHERE id = ?",
            (eid,),
        ).fetchone()
        if not row:
            skipped.append({"id": eid, "reason": "not found"})
            continue
        if row["status"] == "DELETED":
            skipped.append({"id": eid, "reason": "already deleted"})
            continue

        _log_change(conn, eid, "status", row["status"], "DELETED")
        if args.reason:
            old_notes = row["notes"] or ""
            new_notes = (old_notes + "\n" + args.reason).strip()
            _log_change(conn, eid, "notes", old_notes, new_notes)
            conn.execute(
                "UPDATE hsa_expenses SET status = 'DELETED', "
                "needs_review = 0, notes = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_notes, eid),
            )
        else:
            conn.execute(
                "UPDATE hsa_expenses SET status = 'DELETED', "
                "needs_review = 0, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (eid,),
            )
        deleted.append(eid)

    conn.commit()
    conn.close()

    if getattr(args, "json_output", False):
        print(json.dumps({"deleted": deleted, "skipped": skipped}))
    else:
        if deleted:
            print(f"  Deleted {len(deleted)} expense(s).")
        for s in skipped:
            print(f"  Skipped {s['id']}: {s['reason']}.")


# ── merge ──────────────────────────────────────────────────────


_MERGE_TRANSFER_FIELDS = (
    "transaction_id", "payment_method", "payment_date",
)


def _merge_expenses(
    conn, source, targets, *, reason=None, dry_run=False,
):
    """Apply the canonical one-to-one or one-to-many merge."""
    docs = conn.execute(
        "SELECT id, file_path FROM hsa_documents WHERE expense_id = ?",
        (source["id"],),
    ).fetchall()
    first_target_id = targets[0]["id"]
    target_ids = [target["id"] for target in targets]

    cc_statement = ""
    if source["transaction_id"]:
        cc_row = conn.execute(
            "SELECT source_file_path FROM transactions WHERE id = ?",
            (source["transaction_id"],),
        ).fetchone()
        if cc_row and cc_row["source_file_path"]:
            cc_statement = (
                f" (CC statement: {cc_row['source_file_path']})"
            )
    resolved_reason = reason or (
        f"Merged from stub {source['id']}{cc_statement}"
    )
    result = {
        "source_id": source["id"],
        "target_ids": target_ids,
        "fields_transferred": [],
        "docs_transferred": len(docs),
        "docs_target_id": first_target_id,
    }
    if dry_run:
        return result

    transferred = set()
    for target in targets:
        # Field names come only from the hard-coded tuple above.
        for field in _MERGE_TRANSFER_FIELDS:
            if source[field] and not target[field]:
                _log_change(
                    conn, target["id"], field,
                    target[field], source[field],
                )
                conn.execute(
                    f"UPDATE hsa_expenses SET {field} = ? WHERE id = ?",
                    (source[field], target["id"]),
                )
                transferred.add(field)

        if target["needs_review"]:
            _log_change(
                conn, target["id"], "needs_review", "1", "0",
            )
            conn.execute(
                "UPDATE hsa_expenses SET needs_review = 0 WHERE id = ?",
                (target["id"],),
            )

        note_lines = [resolved_reason]
        if docs and target["id"] != first_target_id:
            note_lines.append(
                f"Stub docs transferred to expense #{first_target_id}"
            )
        current_notes = target["notes"] or ""
        new_notes = (
            current_notes + "\n" + "\n".join(note_lines)
        ).strip()
        _log_change(
            conn, target["id"], "notes", current_notes, new_notes,
        )
        conn.execute(
            "UPDATE hsa_expenses SET notes = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_notes, target["id"]),
        )

    for document in docs:
        _log_change(
            conn, document["id"], "expense_id",
            source["id"], first_target_id,
            changed_by="agent", table_name="hsa_documents",
        )
        conn.execute(
            "UPDATE hsa_documents SET expense_id = ? WHERE id = ?",
            (first_target_id, document["id"]),
        )

    _log_change(
        conn, source["id"], "status", source["status"], "DELETED",
    )
    conn.execute(
        "UPDATE hsa_expenses SET status = 'DELETED', "
        "needs_review = 0 WHERE id = ?",
        (source["id"],),
    )
    result["fields_transferred"] = [
        field for field in _MERGE_TRANSFER_FIELDS if field in transferred
    ]
    return result


def cmd_merge(args):
    """Merge one expense into another (docs reassigned, source deleted)."""
    if args.target_id == args.source_id:
        # Without this guard both fetches return the same live row and
        # the "source" gets soft-deleted — a typo'd `merge 5 5` would
        # disappear expense 5.
        print("  Target and source must be different expenses.")
        sys.exit(1)

    conn = _connect(args.db_path)

    target = conn.execute(
        "SELECT * FROM hsa_expenses WHERE id = ?", (args.target_id,)
    ).fetchone()

    source = conn.execute(
        "SELECT * FROM hsa_expenses WHERE id = ?", (args.source_id,)
    ).fetchone()

    if not target or not source:
        print("  Target or Source ID not found.")
        conn.close()
        return

    if source["status"] == "DELETED":
        print(f"  Source {args.source_id} is already deleted.")
        conn.close()
        return

    if target["status"] == "DELETED":
        print(f"  Target {args.target_id} is deleted.")
        conn.close()
        return

    result = _merge_expenses(
        conn, source, [target], reason=args.reason,
    )

    conn.commit()
    conn.close()

    if getattr(args, "json_output", False):
        print(json.dumps({
            "target_id": target["id"],
            "source_id": source["id"],
            "fields_transferred": result["fields_transferred"],
            "docs_transferred": result["docs_transferred"],
        }))
    else:
        print(f"  Successfully merged {source['id']} into {target['id']}.")


def cmd_merge_many(args):
    """Consolidate a CC stub into multiple service records."""
    if args.source_id in args.target_ids:
        print("  Source cannot also be a merge target.")
        sys.exit(1)

    conn = _connect(args.db_path)
    dry_run = getattr(args, "dry_run", False)

    source = conn.execute(
        "SELECT * FROM hsa_expenses WHERE id = ?", (args.source_id,)
    ).fetchone()

    if not source:
        print(f"  Source ID {args.source_id} not found.")
        conn.close()
        return

    if source["status"] == "DELETED":
        print(f"  Source {args.source_id} is already deleted.")
        conn.close()
        return

    if source["source"] != "cc_stub":
        print(f"  Source {args.source_id} is not a CC stub "
              f"(source={source['source']}).")
        conn.close()
        return

    targets = []
    for tid in args.target_ids:
        t = conn.execute(
            "SELECT * FROM hsa_expenses WHERE id = ?", (tid,)
        ).fetchone()
        if not t:
            print(f"  Target ID {tid} not found.")
            conn.close()
            return
        if t["status"] == "DELETED":
            print(f"  Target {tid} is deleted.")
            conn.close()
            return
        if t["source"] not in ("receipt", "eob", "invoice"):
            print(f"  Target {tid} is not a service record "
                  f"(source={t['source']}).")
            conn.close()
            return
        targets.append(t)

    result = _merge_expenses(
        conn,
        source,
        targets,
        reason=args.reason,
        dry_run=dry_run,
    )
    first_target_id = result["docs_target_id"]
    target_ids = [t["id"] for t in targets]

    if dry_run:
        conn.close()
        if getattr(args, "json_output", False):
            print(json.dumps(result))
        else:
            print(f"  Would merge stub {source['id']} into "
                  f"{len(targets)} targets: {target_ids}")
            print(f"  Would transfer {result['docs_transferred']} doc(s) to "
                  f"expense #{first_target_id}")
            print(f"  Would delete stub {source['id']}")
        return

    conn.commit()
    conn.close()

    if getattr(args, "json_output", False):
        print(json.dumps(result))
    else:
        print(f"  Successfully merged {source['id']} into {len(targets)} targets.")


def cmd_list_docs(args):
    """List HSA documents."""
    conn = _connect(args.db_path)

    query = (
        "SELECT id, expense_id, document_type, file_path, "
        "sidecar_path, ingested_at FROM hsa_documents"
    )
    rows = conn.execute(query).fetchall()
    conn.close()

    if getattr(args, "json_output", False):
        out = [dict(r) for r in rows]
        print(json.dumps(out, indent=2))
    else:
        for r in rows:
            print(f"{r['id']:<4}  {r['document_type']:<10}  {r['file_path']}")


# ── providers ──────────────────────────────────────────────────


def cmd_providers(args):
    """List known HSA providers."""
    conn = _connect(args.db_path)

    rows = conn.execute(
        "SELECT id, canonical_name, category, aliases "
        "FROM hsa_providers ORDER BY canonical_name"
    ).fetchall()
    conn.close()

    # Also show config-based providers
    config_providers = []
    if os.path.exists(HSA_PROVIDERS_JSON):
        with open(HSA_PROVIDERS_JSON) as f:
            data = json.load(f)
        config_providers = data.get("providers", [])

    if args.json_output:
        out = {
            "db_providers": [dict(r) for r in rows],
            "config_providers": config_providers,
        }
        print(json.dumps(out, indent=2))
        return

    if not rows and not config_providers:
        print("  No providers configured.")
        print("  Add providers to config/hsa/providers.json")
        return

    if config_providers:
        print("\n  Config providers:")
        for p in config_providers:
            aliases = ", ".join(p.get("aliases", []))
            print(
                f"    {p['canonical_name']:<30s}  "
                f"{p.get('category', ''):<12s}  "
                f"aliases: {aliases}"
            )

    if rows:
        print("\n  Learned providers (DB):")
        for r in rows:
            aliases = r["aliases"] or "[]"
            print(
                f"    {r['canonical_name']:<30s}  "
                f"{r['category'] or '':<12s}  "
                f"aliases: {aliases}"
            )


def cmd_ingest(args):
    """Ingest classified HSA sidecars into hsa_expenses."""
    from housebook.config.settings import HSA_DIR
    from housebook.core.database import Database
    from housebook.hsa.ingestor import HsaIngestor

    db_path = args.db_path or DB_PATH
    dry_run = getattr(args, "dry_run", False)
    db = Database(
        db_path, dry_run=dry_run,
        workspace_dir=str(WORKSPACE_DIR),
    )
    ingestor = HsaIngestor(db, None)

    if not os.path.isdir(HSA_DIR):
        print("  hsa/ directory not found.")
        sys.exit(1)

    orphans = ingestor.validate_directory(HSA_DIR)
    for orphan in orphans:
        short = os.path.relpath(orphan, HSA_DIR)
        print(f"  ! Missing sidecar: {short}")

    result = ingestor.ingest_directory(HSA_DIR)

    parts = []
    if result["ingested"]:
        parts.append(f"{result['ingested']} sidecar(s)")
    if result["expenses"]:
        parts.append(f"{result['expenses']} expense(s)")
    if result["skipped"]:
        parts.append(f"{result['skipped']} unchanged")
    if result["errors"]:
        parts.append(f"{len(result['errors'])} error(s)")
    print(f"  HSA ingest: {', '.join(parts) or 'nothing to do'}.")
    for path, err in result["errors"]:
        print(f"  ! {os.path.relpath(path, HSA_DIR)}: {err}")
    if dry_run:
        print("  (dry run — no changes written)")


# ── main ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="HSA Shoebox - medical expense ledger",
        prog="housebook-hsa",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="Override database path",
    )
    sub = parser.add_subparsers(dest="command")

    # summary
    p_sum = sub.add_parser(
        "summary",
        help="Shoebox totals by year/patient/category",
    )
    p_sum.add_argument("--year", type=int)
    p_sum.add_argument("--patient", type=str)
    p_sum.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )

    # list
    p_lst = sub.add_parser(
        "list",
        help="Flat list of all HSA expenses",
    )
    p_lst.add_argument("--year", type=int)
    p_lst.add_argument("--status", type=str)
    p_lst.add_argument("--patient", type=str)
    p_lst.add_argument(
        "--needs-review",
        action="store_true",
        help="Show only expenses needing review",
    )
    p_lst.add_argument(
        "--excluded",
        action="store_true",
        help="Show only excluded (non-eligible) expenses",
    )
    p_lst.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )

    # check
    p_chk = sub.add_parser(
        "check",
        help="Data quality and integrity checks",
    )
    p_chk.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )
    p_chk.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Re-hash every source file and compare against the hash "
             "recorded at ingest (slow; detects altered documents)",
    )

    # ingest
    p_ing = sub.add_parser(
        "ingest",
        help="Ingest classified HSA sidecars into hsa_expenses",
    )
    p_ing.add_argument(
        "--dry-run",
        action="store_true",
    )

    # scan
    p_scan = sub.add_parser(
        "scan",
        help="Scan CC transactions for medical expenses",
    )
    p_scan.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing",
    )
    p_scan.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )

    # candidates
    p_cand = sub.add_parser(
        "candidates",
        help="Find potential matches between expenses and stubs",
    )
    p_cand.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )

    # plan
    p_plan = sub.add_parser(
        "plan",
        help="Create or display a payment plan",
    )
    p_plan.add_argument(
        "--master",
        dest="master_ids",
        type=lambda s: [int(x) for x in s.split(",")],
        metavar="IDS",
        help="Comma-separated IDs of master liability records (EOB/INV)",
    )
    p_plan.add_argument(
        "--installments",
        dest="installment_ids",
        type=lambda s: [int(x) for x in s.split(",")],
        metavar="IDS",
        help="Comma-separated IDs of installment CC stubs",
    )
    p_plan.add_argument("--name", type=str, help="Plan name")
    p_plan.add_argument("--notes", type=str, help="Optional notes")
    p_plan.add_argument(
        "--list",
        dest="list_plans",
        action="store_true",
        help="List all payment plans",
    )
    p_plan.add_argument(
        "--show",
        dest="show_id",
        type=int,
        metavar="PLAN_ID",
        help="Show details of a specific plan",
    )
    p_plan.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )

    # list-docs
    p_list_docs = sub.add_parser(
        "list-docs",
        help="List HSA documents",
    )
    p_list_docs.add_argument(
        "--json", action="store_true", default=False,
        dest="json_output",
    )

    # merge
    p_merge = sub.add_parser(
        "merge",
        help="Merge one expense into another",
    )
    p_merge.add_argument("target_id", type=int, help="Expense to keep")
    p_merge.add_argument("source_id", type=int, help="Expense to merge in")
    p_merge.add_argument("--reason", type=str, help="Note for the merge")
    p_merge.add_argument(
        "--json", action="store_true", default=False,
        dest="json_output",
    )

    # merge-many
    p_merge_many = sub.add_parser(
        "merge-many",
        help="Consolidate a CC stub into multiple service records",
    )
    p_merge_many.add_argument("source_id", type=int, help="Stub record ID")
    p_merge_many.add_argument(
        "target_ids", nargs="+", type=int, help="Verified record IDs"
    )
    p_merge_many.add_argument("--reason", type=str, help="Note for the merge")
    p_merge_many.add_argument(
        "--dry-run", action="store_true", default=False,
        dest="dry_run", help="Preview without writing",
    )
    p_merge_many.add_argument(
        "--json", action="store_true", default=False,
        dest="json_output",
    )

    # delete
    p_del = sub.add_parser(
        "delete",
        help="Soft-delete expenses",
    )
    p_del.add_argument(
        "ids", nargs="+", type=int, help="Expense IDs to delete",
    )
    p_del.add_argument("--reason", type=str, help="Reason for deletion")
    p_del.add_argument(
        "--json", action="store_true", default=False,
        dest="json_output",
    )

    # verify
    p_ver = sub.add_parser(
        "verify",
        help="Mark expenses as reviewed",
    )
    p_ver.add_argument(
        "ids",
        nargs="+",
        type=int,
        help="Expense IDs to verify",
    )
    p_ver.add_argument("--category", type=str)
    p_ver.add_argument("--patient", type=str)
    p_ver.add_argument("--provider", type=str)
    p_ver.add_argument(
        "--evidence-level",
        type=str,
        choices=list(VALID_LEVELS),
        help="Set evidence level (ready/strong = reimbursable)",
    )
    p_ver.add_argument("--transaction-id", type=int)
    p_ver.add_argument("--notes", type=str)
    p_ver.add_argument("--payment-method", type=str)
    p_ver.add_argument("--payment-date", type=str)
    excl_group = p_ver.add_mutually_exclusive_group()
    excl_group.add_argument(
        "--exclude", type=str, metavar="REASON",
        choices=VALID_EXCLUSION_REASONS,
        help="Mark as non-eligible "
             f"({', '.join(VALID_EXCLUSION_REASONS)})",
    )
    excl_group.add_argument(
        "--include", action="store_true", default=False,
        help="Clear exclusion (re-include for reimbursement)",
    )

    # link-doc
    p_link = sub.add_parser(
        "link-doc",
        help="Attach a document to an expense",
    )
    p_link.add_argument(
        "expense_id",
        type=int,
        help="Expense ID",
    )
    p_link.add_argument(
        "file_path",
        type=str,
        help="Path to document file",
    )

    # providers
    p_prov = sub.add_parser(
        "providers",
        help="List known providers",
    )
    p_prov.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "summary": cmd_summary,
        "list": cmd_list,
        "check": cmd_check,
        "ingest": cmd_ingest,
        "scan": cmd_scan,
        "candidates": cmd_candidates,
        "plan": cmd_plan,
        "merge": cmd_merge,
        "merge-many": cmd_merge_many,
        "delete": cmd_delete,
        "list-docs": cmd_list_docs,
        "verify": cmd_verify,
        "link-doc": cmd_link_doc,
        "providers": cmd_providers,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
