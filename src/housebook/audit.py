"""Agent-facing audit CLI for reviewing and verifying transactions.

Replaces the raw SQL workflow in the monthly audit SOP with
structured subcommands that enforce the transaction status
lifecycle (UNVERIFIED → AGENT_VERIFIED) and provide both
human-readable and JSON output for Agent consumption.
"""

import argparse
import json
import os
import sqlite3
import sys

from housebook.config.settings import (
    BACKUP_DIR,
    DB_PATH,
)
from housebook.core.database import backup_database
from housebook.core.spending import spend_filter


def _connect(db_path=None):
    """Return a WAL-mode connection with Row factory."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _backup(db_path=None):
    """Back up the target DB, isolating non-default DBs from prod backups.

    The production ``BACKUP_DIR`` is only used when the target really is
    the live ``DB_PATH``. For an overridden ``--db-path`` (tests, scratch
    DBs), ``backup_dir`` is left ``None`` so ``backup_database`` defaults
    to a ``<db_dir>/backups`` folder beside the throwaway DB — otherwise
    its backup is named after the temp file's basename and pollutes the
    live workspace backups, which then get mirrored to the remote on the
    next ``housebook-sync push``.
    """
    resolved = db_path or DB_PATH
    backup_dir = (
        BACKUP_DIR
        if os.path.abspath(resolved) == os.path.abspath(DB_PATH)
        else None
    )
    return backup_database(resolved, backup_dir)


# ── pending ──────────────────────────────────────────────────────

def cmd_pending(args):
    """List transactions awaiting review."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    query = """
        SELECT t.id, t.date, t.amount, t.description, t.category,
               t.source, t.metadata, t.original_file, t.profile,
               t.trip_id, tr.name AS trip_name
        FROM transactions t
        LEFT JOIN trips tr ON t.trip_id = tr.id
        WHERE t.needs_review = 1 AND t.status != 'RECONCILED'
    """
    params = []
    if args.source:
        query += " AND t.source = ?"
        params.append(args.source)
    query += " ORDER BY t.source, t.date"

    rows = cur.execute(query, params).fetchall()
    conn.close()

    if args.json_output:
        out = []
        for r in rows:
            item = dict(r)
            if item.get("metadata"):
                try:
                    item["metadata"] = json.loads(item["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(item)
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_pending(rows)


def _print_pending(rows):
    print(f"\n{'=' * 70}")
    print(f"PENDING REVIEW: {len(rows)} transaction(s)")
    print(f"{'=' * 70}")

    if not rows:
        print("  Nothing to review.")
        return

    current_source = None
    for r in rows:
        if r["source"] != current_source:
            current_source = r["source"]
            print(f"\n── {current_source} ──")

        sign = "+" if r["amount"] > 0 else " "
        meta_hint = ""
        if r["metadata"]:
            try:
                m = json.loads(r["metadata"])
                if "raw_lines" in m:
                    meta_hint = f'  [{m["raw_lines"][0][:60]}]'
            except (json.JSONDecodeError, TypeError):
                pass

        print(
            f"  [{r['id']:>5}] {r['date']}  "
            f"{sign}{r['amount']:>10.2f}  "
            f"{r['category']:<28} "
            f"{r['description'][:55]}"
            f"{meta_hint}"
        )

    print()


# ── calibrate ────────────────────────────────────────────────────

def cmd_calibrate(args):
    """Show category distribution of verified transactions."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT category, COUNT(*) as cnt,
            GROUP_CONCAT(substr(description, 1, 60), ' | ') as samples
        FROM transactions
        WHERE needs_review = 0
        GROUP BY category
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()

    if args.json_output:
        out = [{"category": r["category"], "count": r["cnt"],
                "samples": r["samples"]} for r in rows]
        print(json.dumps(out, indent=2))
    else:
        print(f"\n{'=' * 70}")
        print("CATEGORY CALIBRATION (verified transactions)")
        print(f"{'=' * 70}\n")
        for r in rows:
            samples = r["samples"] or ""
            # Deduplicate and limit samples
            unique = list(dict.fromkeys(samples.split(" | ")))[:4]
            print(f"  {r['cnt']:>5}  {r['category']:<30}  {' | '.join(unique)[:80]}")
        print()


# ── trips ────────────────────────────────────────────────────────

def _trip_overlap_filter(args):
    """Build a WHERE fragment + params for --year/--since/--until.

    A trip overlaps a [lo, hi] range when its start_date <= hi AND its
    end_date >= lo. --year is sugar for the calendar-year range; --since
    and --until set an open-ended lower/upper bound respectively.
    """
    lo = hi = None
    if getattr(args, "year", None):
        lo = f"{args.year}-01-01"
        hi = f"{args.year}-12-31"
    if getattr(args, "since", None):
        lo = args.since
    if getattr(args, "until", None):
        hi = args.until

    clauses, params = [], []
    if hi is not None:
        clauses.append("t.start_date <= ?")
        params.append(hi)
    if lo is not None:
        clauses.append("t.end_date >= ?")
        params.append(lo)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    filtered = bool(clauses)
    return where, params, filtered


def cmd_trips(args):
    """List trips for assignment context."""
    if getattr(args, "all", False):
        args.limit = 0

    conn = _connect(args.db_path)
    cur = conn.cursor()

    where, params, filtered = _trip_overlap_filter(args)

    # Total matching the same filter (no cap) — the "of N" in the footer.
    total = cur.execute(
        f"SELECT COUNT(*) FROM trips t{where}", params
    ).fetchone()[0]

    query = f"""
        SELECT t.id, t.name, t.start_date, t.end_date,
               t.type, t.location, t.status,
               COUNT(tx.id) as tx_count,
               COALESCE(SUM(ABS(tx.amount)), 0) as total_spend
        FROM trips t
        LEFT JOIN transactions tx ON tx.trip_id = t.id
        {where}
        GROUP BY t.id
        ORDER BY t.start_date DESC
    """
    # --limit 0 (or --all, which sets limit to 0) disables the cap.
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    rows = cur.execute(query, params).fetchall()
    conn.close()

    if args.json_output:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    else:
        print(f"\n{'=' * 70}")
        print("TRIPS")
        print(f"{'=' * 70}\n")
        for r in rows:
            print(
                f"  [{r['id']:>3}] {r['name']:<40} "
                f"{r['start_date']} → {r['end_date']}"
            )
            print(
                f"        {r['type']:<12} {r['location'] or 'N/A':<25} "
                f"{r['tx_count']} txns  ${r['total_spend']:.2f}"
            )

        scope = " matching filter" if filtered else ""
        if len(rows) < total:
            print(
                f"\n  Showing {len(rows)} of {total} trips{scope} "
                f"(newest first). Use --limit 0 (or --all) for all, "
                f"or --year/--since/--until to filter."
            )
        else:
            print(f"\n  Showing all {total} trips{scope}.")
        print()


# ── create-trip ──────────────────────────────────────────────────

def cmd_create_trip(args):
    """Create a new trip record."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    cur.execute(
        """INSERT INTO trips (name, start_date, end_date, type,
                              location, status, created_by)
           VALUES (?, ?, ?, ?, ?, 'confirmed', 'agent')""",
        (args.name, args.start, args.end, args.type, args.location),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()

    if args.json_output:
        print(json.dumps({"id": trip_id, "name": args.name}))
    else:
        print(f"Created trip [{trip_id}]: {args.name} "
              f"({args.start} → {args.end}, {args.type})")


# ── verify ───────────────────────────────────────────────────────

def _parse_id_ranges(spec: str) -> list[int]:
    """Parse '7087,7094-7108,7110' into a list of ints."""
    ids = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ids.extend(range(int(lo), int(hi) + 1))
        else:
            ids.append(int(part))
    return ids


def cmd_verify(args):
    """Batch-verify transactions: set category, status, trip."""
    ids = _parse_id_ranges(args.ids)

    if not ids:
        print("Error: no transaction IDs provided.")
        sys.exit(1)

    conn = _connect(args.db_path)
    cur = conn.cursor()

    # Validate IDs exist and are unverified
    placeholders = ",".join("?" * len(ids))
    existing = cur.execute(
        f"SELECT id, status, category FROM transactions "
        f"WHERE id IN ({placeholders})", ids,
    ).fetchall()

    found_ids = {r["id"] for r in existing}
    missing = set(ids) - found_ids
    if missing:
        print(f"Error: transaction IDs not found: {sorted(missing)}")
        conn.close()
        sys.exit(1)

    # SQLite FK enforcement is off, so a typo'd id would otherwise
    # create a dangling reference (same guard as cmd_assign).
    if args.trip is not None and not cur.execute(
            "SELECT 1 FROM trips WHERE id = ?", (args.trip,)).fetchone():
        print(f"Error: no trip with id {args.trip}")
        conn.close()
        sys.exit(1)
    if args.project is not None and not cur.execute(
            "SELECT 1 FROM projects WHERE id = ?", (args.project,)).fetchone():
        print(f"Error: no project with id {args.project}")
        conn.close()
        sys.exit(1)

    already_verified = [r for r in existing if r["status"] != "UNVERIFIED"]
    if already_verified and not args.force:
        av_ids = [r["id"] for r in already_verified]
        print(f"Warning: {len(av_ids)} transaction(s) already verified: "
              f"{av_ids}")
        print("Use --force to re-verify.")
        conn.close()
        sys.exit(1)

    # Build the update. Never DOWNGRADE a USER_VERIFIED row (the user's
    # manual confirmation outranks an agent re-verify) — preserve it.
    set_clauses = [
        "status = CASE WHEN status = 'USER_VERIFIED' "
        "THEN 'USER_VERIFIED' ELSE 'AGENT_VERIFIED' END",
        "needs_review = 0",
    ]
    params = []

    if args.category:
        set_clauses.append("category = ?")
        params.append(args.category)

    if args.trip is not None:
        set_clauses.append("trip_id = ?")
        params.append(args.trip)

    if args.project is not None:
        set_clauses.append("project_id = ?")
        params.append(args.project)

    params.extend(ids)

    # Backup before writing
    _backup(args.db_path)

    cur.execute(
        f"UPDATE transactions SET {', '.join(set_clauses)} "
        f"WHERE id IN ({placeholders})",
        params,
    )
    updated = cur.rowcount
    conn.commit()

    # Report what changed
    result = {
        "updated": updated,
        "ids": sorted(ids),
        "category": args.category,
        "trip_id": args.trip,
        "project_id": args.project,
    }

    if args.json_output:
        print(json.dumps(result))
    else:
        parts = [f"Verified {updated} transaction(s)"]
        if args.category:
            parts.append(f"category={args.category}")
        if args.trip is not None:
            parts.append(f"trip={args.trip}")
        if args.project is not None:
            parts.append(f"project={args.project}")
        print("  ".join(parts))

    conn.close()


# ── link ────────────────────────────────────────────────────────

def cmd_link(args):
    """Link a purchase to its refund/cancellation (bidirectional)."""
    purchase_id = args.purchase_id
    refund_id = args.refund_id

    if purchase_id == refund_id:
        print("Error: cannot link a transaction to itself.")
        sys.exit(1)

    conn = _connect(args.db_path)
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT id, date, description, amount, category, "
        "linked_transaction_id "
        "FROM transactions WHERE id IN (?, ?)",
        (purchase_id, refund_id),
    ).fetchall()

    found = {r["id"]: r for r in rows}
    missing = {purchase_id, refund_id} - found.keys()
    if missing:
        print(f"Error: transaction IDs not found: {sorted(missing)}")
        conn.close()
        sys.exit(1)

    for tid, row in found.items():
        if row["linked_transaction_id"] is not None and not args.force:
            print(f"Error: transaction {tid} is already linked to "
                  f"{row['linked_transaction_id']}. Use --force to re-link.")
            conn.close()
            sys.exit(1)

    _backup(args.db_path)

    cur.execute(
        "UPDATE transactions SET linked_transaction_id = ? WHERE id = ?",
        (refund_id, purchase_id),
    )
    cur.execute(
        "UPDATE transactions SET linked_transaction_id = ? WHERE id = ?",
        (purchase_id, refund_id),
    )
    conn.commit()

    p = found[purchase_id]
    r = found[refund_id]
    result = {
        "purchase": {"id": purchase_id, "description": p["description"],
                     "amount": p["amount"], "date": p["date"]},
        "refund": {"id": refund_id, "description": r["description"],
                   "amount": r["amount"], "date": r["date"]},
    }

    if args.json_output:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Linked: [{purchase_id}] {p['description'][:50]} "
              f"${p['amount']:.2f}")
        print(f"    ↔   [{refund_id}] {r['description'][:50]} "
              f"${r['amount']:.2f}")

    conn.close()


def cmd_link_amazon_refunds(args):
    """Deterministically link Amazon refund rows to their original
    purchase by Order ID.

    Scope: only 1:1 cases where exactly one unlinked purchase row and
    exactly one unlinked refund row share an Order ID, and the two
    amounts cancel to within $0.01. Multi-line purchase orders and
    multi-refund orders are reported but skipped — the single-FK
    schema can't safely express them, and AGENTS.md's link policy is
    "only full refunds where amounts cancel exactly."

    Handles both physical refunds (csv='refunds') and digital
    refunds (csv='digital_refunds') uniformly via the Order ID
    metadata persisted at ingest time.
    """
    conn = _connect(args.db_path)
    cur = conn.cursor()

    from decimal import Decimal

    refunds = cur.execute("""
        SELECT id, date, description, amount,
               json_extract(metadata, '$.amazon_order_id') AS oid,
               json_extract(metadata, '$.csv') AS csv
        FROM transactions
        WHERE source = 'Amazon'
          AND linked_transaction_id IS NULL
          AND json_extract(metadata, '$.csv')
              IN ('refunds', 'digital_refunds')
          AND json_extract(metadata, '$.amazon_order_id') IS NOT NULL
        ORDER BY date
    """).fetchall()

    linked = []
    skipped = []
    # Within-run consumption tracking: prevents two refunds for the
    # same Order ID from both claiming the only purchase row (the
    # second link would overwrite the first asymmetrically since
    # linked_transaction_id is single-FK).
    consumed_purchase_ids: set = set()

    for r in refunds:
        purchases = cur.execute("""
            SELECT id, date, description, amount
            FROM transactions
            WHERE source = 'Amazon'
              AND linked_transaction_id IS NULL
              AND json_extract(metadata, '$.amazon_order_id') = ?
              AND json_extract(metadata, '$.csv')
                  IN ('orders', 'digital')
        """, (r["oid"],)).fetchall()
        # Exclude purchases consumed earlier in this run.
        purchases = [
            p for p in purchases if p["id"] not in consumed_purchase_ids
        ]

        reason = None
        if len(purchases) == 0:
            reason = "no unlinked purchase row for Order ID"
        elif len(purchases) > 1:
            reason = f"{len(purchases)} unlinked purchase rows (multi-line)"
        else:
            p = purchases[0]
            # Decimal avoids float roundoff on borderline cases like
            # $59.99 + (-$59.98) = $0.01 (a real partial refund).
            net = Decimal(str(p["amount"])) + Decimal(str(r["amount"]))
            if abs(net) > Decimal("0.005"):
                reason = (
                    f"amounts don't cancel: purchase ${p['amount']:.2f} "
                    f"+ refund ${r['amount']:.2f} = ${float(net):.2f}"
                )

        if reason:
            skipped.append({
                "refund_id": r["id"],
                "refund_date": r["date"],
                "refund_amount": float(r["amount"]),
                "refund_desc": r["description"],
                "order_id": r["oid"],
                "reason": reason,
            })
            continue

        p = purchases[0]
        consumed_purchase_ids.add(p["id"])
        linked.append({
            "purchase_id": p["id"],
            "purchase_date": p["date"],
            "purchase_amount": float(p["amount"]),
            "purchase_desc": p["description"],
            "refund_id": r["id"],
            "refund_date": r["date"],
            "refund_amount": float(r["amount"]),
            "refund_desc": r["description"],
            "order_id": r["oid"],
        })

    if not args.dry_run and linked:
        _backup(args.db_path)
        for pair in linked:
            cur.execute(
                "UPDATE transactions SET linked_transaction_id = ? "
                "WHERE id = ?",
                (pair["refund_id"], pair["purchase_id"]),
            )
            cur.execute(
                "UPDATE transactions SET linked_transaction_id = ? "
                "WHERE id = ?",
                (pair["purchase_id"], pair["refund_id"]),
            )
        conn.commit()

    result = {
        "linked": linked,
        "skipped": skipped,
        "linked_count": len(linked),
        "skipped_count": len(skipped),
        "dry_run": bool(args.dry_run),
    }

    if args.json_output:
        print(json.dumps(result, indent=2, default=str))
    else:
        prefix = "(dry-run) would link" if args.dry_run else "Linked"
        print(f"{prefix} {len(linked)} purchase↔refund pair(s):")
        for pair in linked:
            print(
                f"  [{pair['purchase_id']}] {pair['purchase_date']} "
                f"${pair['purchase_amount']:>8.2f}  "
                f"{pair['purchase_desc'][:40]}"
            )
            print(
                f"  ↔ [{pair['refund_id']}] {pair['refund_date']} "
                f"${pair['refund_amount']:>8.2f}  "
                f"{pair['refund_desc'][:40]}"
            )
        if skipped:
            print()
            print(
                f"Skipped {len(skipped)} refund row(s) needing user review:"
            )
            from collections import Counter
            reasons = Counter(s["reason"].split(":")[0] for s in skipped)
            for reason, cnt in reasons.most_common():
                print(f"  {cnt:>4}  {reason}")
            print()
            print("Use --json for the full list with Order IDs.")

    conn.close()


def cmd_unlink(args):
    """Remove the link between two paired transactions."""
    tx_id = args.id

    conn = _connect(args.db_path)
    cur = conn.cursor()

    row = cur.execute(
        "SELECT id, linked_transaction_id FROM transactions WHERE id = ?",
        (tx_id,),
    ).fetchone()

    if not row:
        print(f"Error: transaction {tx_id} not found.")
        conn.close()
        sys.exit(1)

    partner_id = row["linked_transaction_id"]
    if partner_id is None:
        print(f"Error: transaction {tx_id} is not linked.")
        conn.close()
        sys.exit(1)

    _backup(args.db_path)

    cur.execute(
        "UPDATE transactions SET linked_transaction_id = NULL "
        "WHERE id IN (?, ?)",
        (tx_id, partner_id),
    )
    conn.commit()

    result = {"unlinked": [tx_id, partner_id]}
    if args.json_output:
        print(json.dumps(result))
    else:
        print(f"Unlinked transactions {tx_id} ↔ {partner_id}")

    conn.close()


# ── linked ──────────────────────────────────────────────────────

def cmd_linked(args):
    """List all linked transaction pairs."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT a.id AS purchase_id, a.date AS purchase_date,
               a.description AS purchase_desc, a.amount AS purchase_amount,
               a.category AS purchase_category, a.source AS purchase_source,
               b.id AS refund_id, b.date AS refund_date,
               b.description AS refund_desc, b.amount AS refund_amount,
               b.category AS refund_category, b.source AS refund_source
        FROM transactions a
        JOIN transactions b ON a.linked_transaction_id = b.id
        WHERE a.id < b.id
        ORDER BY a.date DESC
    """).fetchall()
    conn.close()

    if args.json_output:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    else:
        print(f"\n{'=' * 70}")
        print(f"LINKED PAIRS: {len(rows)} pair(s)")
        print(f"{'=' * 70}")

        if not rows:
            print("  No linked transactions.")
            return

        for r in rows:
            net = r["purchase_amount"] + r["refund_amount"]
            print(f"\n  [{r['purchase_id']:>5}] {r['purchase_date']}  "
                  f"+${r['purchase_amount']:>10.2f}  "
                  f"{r['purchase_desc'][:45]}")
            print(f"  [{r['refund_id']:>5}] {r['refund_date']}  "
                  f" ${r['refund_amount']:>10.2f}  "
                  f"{r['refund_desc'][:45]}")
            print(f"         net: ${net:.2f}  "
                  f"({r['purchase_source']}/{r['refund_source']})")

        print()


# ── summary ──────────────────────────────────────────────────────

def cmd_summary(args):
    """Post-audit summary: what was recently verified."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    # Recently verified (AGENT_VERIFIED) transactions
    verified = cur.execute("""
        SELECT t.id, t.date, t.amount, t.description, t.category,
               t.source, t.trip_id, tr.name AS trip_name
        FROM transactions t
        LEFT JOIN trips tr ON t.trip_id = tr.id
        WHERE t.status = 'AGENT_VERIFIED' AND t.needs_review = 0
        ORDER BY t.source, t.date
    """).fetchall()

    # Remaining unreviewed
    remaining = cur.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE needs_review = 1 AND status = 'UNVERIFIED'"
    ).fetchone()[0]

    conn.close()

    if args.json_output:
        out = {
            "verified_count": len(verified),
            "remaining_unreviewed": remaining,
            "by_category": {},
            "by_trip": {},
        }
        for r in verified:
            cat = r["category"]
            out["by_category"][cat] = out["by_category"].get(cat, 0) + 1
            if r["trip_name"]:
                tn = r["trip_name"]
                out["by_trip"][tn] = out["by_trip"].get(tn, 0) + 1
        print(json.dumps(out, indent=2))
    else:
        print(f"\n{'=' * 70}")
        print("AUDIT SUMMARY")
        print(f"{'=' * 70}\n")

        # Group by category
        by_cat = {}
        by_trip = {}
        total_amount = 0.0
        for r in verified:
            cat = r["category"]
            by_cat.setdefault(cat, []).append(r)
            total_amount += abs(r["amount"])
            if r["trip_name"]:
                by_trip.setdefault(r["trip_name"], []).append(r)

        print(f"  Agent-verified: {len(verified)} transactions "
              f"(${total_amount:,.2f} total)")
        print(f"  Still pending:  {remaining}\n")

        if by_cat:
            print("  By category:")
            for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
                txs = by_cat[cat]
                cat_total = sum(abs(r["amount"]) for r in txs)
                print(f"    {len(txs):>4}  {cat:<30}  ${cat_total:>10,.2f}")

        if by_trip:
            print("\n  By trip:")
            for tn, txs in by_trip.items():
                trip_total = sum(abs(r["amount"]) for r in txs)
                print(f"    {len(txs):>4}  {tn:<40}  ${trip_total:>10,.2f}")

        print()


# ── apply-rules ─────────────────────────────────────────────────


def cmd_apply_rules(args):
    """Apply rules.json category guesses to pending transactions."""
    import re
    from datetime import datetime, timedelta

    from housebook.config.settings import RULES_JSON

    db_path = getattr(args, "db_path", None) or DB_PATH
    if not os.path.exists(RULES_JSON):
        print(f"  Error: {RULES_JSON} not found.")
        return

    bk = _backup(getattr(args, "db_path", None))
    if bk:
        print(f"  Pre-audit backup: {bk}")

    with open(RULES_JSON, "r") as f:
        mapping = json.load(f)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    # Determine the date floor. Default is the last 365 days (correct for a
    # monthly audit); --since overrides it; --all removes it entirely (full
    # backfill pass). The floor is echoed below so its scope is never silent.
    if getattr(args, "all", False):
        floor = None
    elif getattr(args, "since", None):
        floor = args.since
    else:
        floor = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    if floor is None:
        pending = conn.execute(
            "SELECT id, description, category FROM transactions "
            "WHERE needs_review = 1"
        ).fetchall()
        skipped_older = 0
    else:
        pending = conn.execute(
            "SELECT id, description, category FROM transactions "
            "WHERE needs_review = 1 AND date >= ?",
            (floor,),
        ).fetchall()
        skipped_older = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE needs_review = 1 AND date < ?",
            (floor,),
        ).fetchone()[0]

    # Only override generic/default categories. If the ingestor or
    # a prior audit already assigned a specific category, respect
    # it — rules are a fallback, not an override.
    overridable = {
        None, "", "Uncategorized", "Miscellaneous",
        "Shopping & Retail",
    }

    suggestions = 0
    for tx in pending:
        if tx["category"] not in overridable:
            continue
        desc = tx["description"].upper()
        found_cat = None
        for cat, keywords in mapping.items():
            for kw in keywords:
                pattern = r"\b" + re.escape(kw.upper()) + r"\b"
                if re.search(pattern, desc):
                    found_cat = cat
                    break
            if found_cat:
                break
        if found_cat and found_cat != tx["category"]:
            conn.execute(
                "UPDATE transactions SET category = ? WHERE id = ?",
                (found_cat, tx["id"]),
            )
            suggestions += 1

    conn.commit()
    if floor is None:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE needs_review = 1"
        ).fetchone()[0]
    else:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE needs_review = 1 AND date >= ?",
            (floor,),
        ).fetchone()[0]
    conn.close()

    # Make the scope explicit: what was examined, and what was skipped.
    if floor is None:
        print(f"  Examined {len(pending)} pending row(s) (all dates; --all).")
    else:
        print(
            f"  Examined {len(pending)} pending row(s) dated >= {floor}."
        )
        if skipped_older:
            print(
                f"  {skipped_older} older row(s) NOT considered — "
                f"use --since YYYY-MM-DD or --all for a backfill."
            )
    print(
        f"  Applied {suggestions} category guess(es). "
        f"{remaining} still pending review."
    )


# ── detect-trips ────────────────────────────────────────────────


def cmd_detect_trips(args):
    """Detect trip candidates from transaction clusters."""
    from datetime import date, timedelta

    from housebook.core.trip_detector import detect_trips

    result = detect_trips(
        db_path=args.db_path or DB_PATH,
        months=args.months,
        min_transactions=args.min_transactions,
        gap_days=args.gap_days,
    )

    if getattr(args, "json_output", False):
        print(json.dumps(result, indent=2, default=str))
        return

    # Echo the effective look-back so the window is never silent. This must
    # mirror trip_detector's own cutoff (months * 30 days) exactly.
    since = (date.today() - timedelta(days=args.months * 30)).isoformat()
    print(
        f"  Scanning the last {args.months} months (since {since}). "
        f"Use --months N to widen."
    )

    candidates = result["candidates"]
    advances = result["advance_payments"]
    print(
        f"  Found {len(candidates)} candidate trip(s), "
        f"{len(advances)} advance payment(s)."
    )
    for i, c in enumerate(candidates):
        print(
            f"  #{i + 1}: {c['start_date']} to {c['end_date']}  "
            f"{c['location'] or 'Unknown'}  "
            f"{c['transaction_count']} tx  "
            f"${abs(c['total_spend']):.2f}"
        )


# ── projects ─────────────────────────────────────────────────────
#
# Projects are the user-initiated, supervised-retrieval counterpart to
# trips: the user declares one, its matching criteria live on the row,
# and the matcher scores existing transactions against it. Projects
# GROUP spending (reporting + budget) — they are NOT a spending-view
# exclusion filter. See prompts/projects.md.

# Spending-view predicate: one definition in core/spending.py, shared
# with the UI. Net spend sums SIGNED amounts (charges +, refunds −) over
# the visible set so linked returns that remain visible still net
# correctly. Aliased where cmd_projects JOINs projects (which also has a
# `status` column) so the references stay unambiguous.


def _csv_to_json_list(raw):
    """Turn a comma-separated CLI value into a JSON array string."""
    if not raw:
        return "[]"
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return json.dumps(items)


def cmd_create_project(args):
    """Create a new project record (status='open')."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    cur.execute(
        """INSERT INTO projects
               (name, description, type, location, start_date, end_date,
                status, match_keywords, match_categories, budget,
                created_by)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 'agent')""",
        (
            args.name,
            args.description,
            args.type,
            args.location,
            args.start,
            args.end,
            _csv_to_json_list(args.keywords),
            _csv_to_json_list(args.categories),
            args.budget,
        ),
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()

    if args.json_output:
        print(json.dumps({"id": project_id, "name": args.name}))
    else:
        window = f"{args.start or '?'} → {args.end or 'ongoing'}"
        print(f"Created project [{project_id}]: {args.name} "
              f"({args.type}, {window})")


def cmd_match_project(args):
    """Score candidate transactions for one project, or all open ones.

    With no project id, sweeps every status='open' project — the natural
    monthly-audit step ("show me all project candidates").
    """
    from housebook.core.project_matcher import match_project

    db_path = args.db_path or DB_PATH
    conn = _connect(args.db_path)
    if args.project_id is not None:
        ids = [args.project_id]
    else:
        ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM projects WHERE status = 'open' "
                "ORDER BY start_date DESC"
            ).fetchall()
        ]
    conn.close()

    results = [match_project(db_path, pid, min_score=args.min_score)
               for pid in ids]

    if args.json_output:
        print(json.dumps({"projects": results}, indent=2, default=str))
        return

    if not ids:
        print("  No open projects to match. "
              "Create one with: housebook-audit create-project ...")
        return

    for res in results:
        p = res["project"]
        cands = res["candidates"]
        end = p["end_date"] or "ongoing"
        print(f"\n  Project [{p['id']}] \"{p['name']}\"  "
              f"window {p['start_date']} → {end}")
        print(f"    keywords: {', '.join(p['keywords']) or '(none)'}")
        print(f"    categories: {', '.join(p['categories']) or '(none)'}")
        print(f"    {len(cands)} candidate(s) at score ≥ {args.min_score} "
              f"(necessary, not sufficient — confirm before verifying):")
        for c in cands:
            sign = "+" if c["amount"] > 0 else " "
            print(
                f"      [{c['id']:>5}] {c['date']}  "
                f"{sign}{c['amount']:>10.2f}  "
                f"score {c['score']}  "
                f"{c['description'][:45]:<45} "
                f"{{{', '.join(c['signals'])}}}"
            )
        if cands:
            id_list = ",".join(str(c["id"]) for c in cands)
            print(f"    → review, then: housebook-audit verify <ids> "
                  f"--project {p['id']}   (candidate ids: {id_list})")
    print()


def cmd_projects(args):
    """List projects with transaction count, net spend, and budget."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    where = ""
    params = []
    if args.status != "all":
        where = "WHERE p.status = ?"
        params.append(args.status)

    # Correlated subqueries (not JOINs) so transaction and manual-expense
    # sums don't multiply each other via a cartesian product.
    rows = cur.execute(
        f"""
        SELECT p.id, p.name, p.type, p.location, p.status,
               p.start_date, p.end_date, p.budget,
               (SELECT COUNT(*) FROM transactions tx
                 WHERE tx.project_id = p.id) AS tx_count,
               (SELECT COALESCE(SUM(tx.amount), 0) FROM transactions tx
                 WHERE tx.project_id = p.id AND {spend_filter('tx')})
               + (SELECT COALESCE(SUM(m.amount), 0) FROM manual_expenses m
                   WHERE m.project_id = p.id) AS net_spend
        FROM projects p
        {where}
        ORDER BY p.status, p.start_date DESC
        """,
        params,
    ).fetchall()
    conn.close()

    if args.json_output:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return

    print(f"\n{'=' * 70}")
    print(f"PROJECTS ({args.status})")
    print(f"{'=' * 70}\n")
    if not rows:
        print("  No projects.\n")
        return
    for r in rows:
        end = r["end_date"] or "ongoing"
        print(f"  [{r['id']:>3}] {r['name']:<38} [{r['status']}] "
              f"{r['start_date']} → {end}")
        line = (f"        {r['type']:<12} {r['location'] or 'N/A':<22} "
                f"{r['tx_count']} txns  net ${r['net_spend']:,.2f}")
        if r["budget"] is not None:
            pct = (r["net_spend"] / r["budget"] * 100) if r["budget"] else 0
            line += f"  / ${r['budget']:,.2f} budget ({pct:.0f}%)"
        print(line)
    print()


def cmd_close_project(args):
    """Mark a project closed; optionally freeze end_date to last tx."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    proj = cur.execute(
        "SELECT id, name, status, end_date FROM projects WHERE id = ?",
        (args.project_id,),
    ).fetchone()
    if proj is None:
        print(f"Error: no project with id {args.project_id}")
        conn.close()
        sys.exit(1)
    if proj["status"] == "closed" and not args.force:
        print(f"Project [{proj['id']}] is already closed. "
              f"Use --force to re-close.")
        conn.close()
        sys.exit(1)

    set_clauses = ["status = 'closed'", "closed_at = CURRENT_TIMESTAMP"]
    if args.freeze_end:
        last = cur.execute(
            "SELECT MAX(date) FROM transactions WHERE project_id = ?",
            (args.project_id,),
        ).fetchone()[0]
        if last:
            set_clauses.append("end_date = ?")
            cur.execute(
                f"UPDATE projects SET {', '.join(set_clauses)} WHERE id = ?",
                (last, args.project_id),
            )
        else:
            cur.execute(
                f"UPDATE projects SET {', '.join(set_clauses)} WHERE id = ?",
                (args.project_id,),
            )
    else:
        cur.execute(
            f"UPDATE projects SET {', '.join(set_clauses)} WHERE id = ?",
            (args.project_id,),
        )
    conn.commit()
    conn.close()

    if args.json_output:
        print(json.dumps({"id": args.project_id, "status": "closed"}))
    else:
        print(f"Closed project [{args.project_id}]: {proj['name']}")


def cmd_project_summary(args):
    """Per-project report: net spend, budget, by-category, tx list."""
    conn = _connect(args.db_path)
    cur = conn.cursor()

    proj = cur.execute(
        "SELECT * FROM projects WHERE id = ?", (args.project_id,)
    ).fetchone()
    if proj is None:
        print(f"Error: no project with id {args.project_id}")
        conn.close()
        sys.exit(1)

    txs = cur.execute(
        f"""
        SELECT id, date, amount, description, category, source
        FROM transactions
        WHERE project_id = ? AND {spend_filter()}
        ORDER BY date ASC
        """,
        (args.project_id,),
    ).fetchall()
    # Project-linked manual expenses (off-ledger costs: craftsmanship, lump
    # material totals). Treated as one-time lump amounts — see add-manual.
    manual = cur.execute(
        """
        SELECT id, start_date AS date, amount, description, category
        FROM manual_expenses
        WHERE project_id = ?
        ORDER BY start_date ASC
        """,
        (args.project_id,),
    ).fetchall()
    conn.close()

    net = sum(r["amount"] for r in txs) + sum(r["amount"] for r in manual)
    by_cat = {}
    for r in list(txs) + list(manual):
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + r["amount"]

    if args.json_output:
        print(json.dumps({
            "project": dict(proj),
            "net_spend": net,
            "transaction_count": len(txs),
            "manual_count": len(manual),
            "by_category": by_cat,
            "budget": proj["budget"],
            "remaining": (proj["budget"] - net
                          if proj["budget"] is not None else None),
            "transactions": [dict(r) for r in txs],
            "manual_expenses": [dict(r) for r in manual],
        }, indent=2, default=str))
        return

    end = proj["end_date"] or "ongoing"
    print(f"\n{'=' * 70}")
    print(f"PROJECT [{proj['id']}]  {proj['name']}  [{proj['status']}]")
    print(f"{'=' * 70}")
    print(f"  {proj['type']}  {proj['location'] or ''}  "
          f"{proj['start_date']} → {end}")
    count_note = f"{len(txs)} transactions"
    if manual:
        count_note += f" + {len(manual)} manual"
    print(f"\n  Net spend: ${net:,.2f}   ({count_note})")
    if proj["budget"] is not None:
        remaining = proj["budget"] - net
        pct = (net / proj["budget"] * 100) if proj["budget"] else 0
        label = "remaining" if remaining >= 0 else "OVER by"
        print(f"  Budget:    ${proj['budget']:,.2f}   "
              f"({pct:.0f}% used, ${abs(remaining):,.2f} {label})")
    if by_cat:
        print("\n  By category:")
        for cat in sorted(by_cat, key=lambda c: -abs(by_cat[c])):
            print(f"    {cat:<30}  ${by_cat[cat]:>10,.2f}")
    if manual:
        print("\n  Manual (off-ledger) entries:")
        for r in manual:
            print(f"    {r['date']}  ${r['amount']:>10,.2f}  "
                  f"{r['description'][:40]}")
    print()


def cmd_add_manual(args):
    """Create a manual expense, optionally linked to a project.

    Manual expenses are off-ledger costs (cash/check craftsmanship, lump
    material totals) kept out of the immutable source ledger. Defaults to
    a one-time entry; pass --project to roll it into a project's total.
    """
    if args.project is not None:
        conn = _connect(args.db_path)
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (args.project,)
        ).fetchone()
        conn.close()
        if not exists:
            print(f"Error: no project with id {args.project}")
            sys.exit(1)

    _backup(args.db_path)
    conn = _connect(args.db_path)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO manual_expenses
               (description, amount, category, start_date, end_date,
                frequency, project_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (args.description, args.amount, args.category, args.date,
         args.end, args.frequency, args.project),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()

    if args.json_output:
        print(json.dumps({"id": mid, "amount": args.amount,
                          "project_id": args.project}))
    else:
        tail = f" → project {args.project}" if args.project else ""
        print(f"Added manual expense [{mid}]: {args.description} "
              f"${args.amount:,.2f} ({args.category}){tail}")


def cmd_assign(args):
    """Tag transactions with a project and/or trip — a PURE grouping op.

    Unlike `verify`, this never rewrites status or needs_review and never
    requires --force, so it is safe on already-audited rows (it will not
    downgrade a USER_VERIFIED row). Use it to group existing, verified
    transactions into a project/trip; use `verify` when you are auditing a
    transaction and assigning it in the same step.
    """
    if args.project is None and args.trip is None:
        print("Error: provide --project and/or --trip.")
        sys.exit(1)
    ids = _parse_id_ranges(args.ids)
    if not ids:
        print("Error: no transaction IDs provided.")
        sys.exit(1)

    conn = _connect(args.db_path)
    cur = conn.cursor()
    placeholders = ",".join("?" * len(ids))
    found = {r["id"] for r in cur.execute(
        f"SELECT id FROM transactions WHERE id IN ({placeholders})", ids)}
    missing = set(ids) - found
    if missing:
        print(f"Error: transaction IDs not found: {sorted(missing)}")
        conn.close()
        sys.exit(1)

    if args.project is not None and not cur.execute(
            "SELECT 1 FROM projects WHERE id = ?", (args.project,)).fetchone():
        print(f"Error: no project with id {args.project}")
        conn.close()
        sys.exit(1)
    if args.trip is not None and not cur.execute(
            "SELECT 1 FROM trips WHERE id = ?", (args.trip,)).fetchone():
        print(f"Error: no trip with id {args.trip}")
        conn.close()
        sys.exit(1)

    set_clauses, params = [], []
    if args.project is not None:
        set_clauses.append("project_id = ?")
        params.append(args.project)
    if args.trip is not None:
        set_clauses.append("trip_id = ?")
        params.append(args.trip)
    params.extend(ids)

    _backup(args.db_path)
    cur.execute(
        f"UPDATE transactions SET {', '.join(set_clauses)} "
        f"WHERE id IN ({placeholders})", params)
    updated = cur.rowcount
    conn.commit()
    conn.close()

    if args.json_output:
        print(json.dumps({"updated": updated, "ids": sorted(ids),
                          "project_id": args.project, "trip_id": args.trip}))
    else:
        parts = [f"Tagged {updated} transaction(s)"]
        if args.project is not None:
            parts.append(f"project={args.project}")
        if args.trip is not None:
            parts.append(f"trip={args.trip}")
        print("  ".join(parts) + "  (status unchanged)")


def cmd_edit_project(args):
    """Amend an existing project — only the fields you pass are changed."""
    conn = _connect(args.db_path)
    cur = conn.cursor()
    if cur.execute("SELECT 1 FROM projects WHERE id = ?",
                   (args.project_id,)).fetchone() is None:
        print(f"Error: no project with id {args.project_id}")
        conn.close()
        sys.exit(1)

    fields, params = [], []

    def setf(col, val):
        fields.append(f"{col} = ?")
        params.append(val)

    if args.name is not None:
        setf("name", args.name)
    if args.start is not None:
        setf("start_date", args.start)
    if args.end is not None:
        setf("end_date", args.end)
    if args.location is not None:
        setf("location", args.location)
    if args.description is not None:
        setf("description", args.description)
    if args.budget is not None:
        setf("budget", args.budget)
    if args.status is not None:
        setf("status", args.status)
    if args.keywords is not None:
        setf("match_keywords", _csv_to_json_list(args.keywords))
    if args.categories is not None:
        setf("match_categories", _csv_to_json_list(args.categories))

    if not fields:
        print("Error: nothing to update — pass at least one field "
              "(--name/--start/--end/--budget/...).")
        conn.close()
        sys.exit(1)

    _backup(args.db_path)
    params.append(args.project_id)
    cur.execute(
        f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    conn.close()

    changed = [f.split(" =")[0] for f in fields]
    if args.json_output:
        print(json.dumps({"id": args.project_id, "updated_fields": changed}))
    else:
        print(f"Updated project [{args.project_id}]: {', '.join(changed)}")


# ── main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Agent-facing audit CLI for transaction review",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""subcommands:
  pending       List unreviewed transactions
  calibrate     Show verified category distribution
  trips         List trips for assignment context
  create-trip   Create a new trip record
  verify        Batch-verify transactions (audit + optional assign)
  assign        Tag transactions with a project/trip (status unchanged)
  link          Link a purchase to its refund/cancellation
  link-amazon-refunds
                Auto-link Amazon refund↔purchase pairs by Order ID (1:1)
  unlink        Remove a transaction link
  linked        List all linked transaction pairs
  summary       Post-audit summary of verified transactions
  create-project   Create a user-initiated project (renovation, ...)
  match-project    Score candidate transactions (all open projects)
  projects         List projects with spend/budget
  close-project    Mark a project closed
  edit-project     Amend a project's window/budget/criteria
  project-summary  Per-project spend/budget report
  add-manual       Create a manual (off-ledger) expense, optionally
                   linked to a project (craftsmanship, lump totals)

examples:
  housebook-audit pending
  housebook-audit pending --source Amex --json
  housebook-audit calibrate
  housebook-audit trips --limit 5
  housebook-audit trips --year 2024          # trips overlapping 2024
  housebook-audit trips --all                # no cap (backfill)
  housebook-audit apply-rules --all          # categorize all dates (backfill)
  housebook-audit create-trip "Italy Vacation" --start 2026-04-09 \\
      --end 2026-04-28 --type personal --location "Italy, France"
  housebook-audit verify 7087,7094-7108 --category "Local Transit" --trip 18
  housebook-audit create-project "Master Bath Reno" --type renovation \\
      --start 2025-09-01 --budget 12000 \\
      --keywords "HOME DEPOT,TILE,PLUMB" --categories "Home Improvement"
  housebook-audit match-project              # sweep every open project
  housebook-audit verify 8120,8125 --project 3
  housebook-audit project-summary 3
  housebook-audit link 7087 7110
  housebook-audit link-amazon-refunds --dry-run
  housebook-audit link-amazon-refunds
  housebook-audit linked
  housebook-audit summary
""",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Machine-readable JSON output",
    )
    common.add_argument(
        "--db", dest="db_path", default=None,
        help="Override database path (default: from settings)",
    )

    sub = parser.add_subparsers(dest="command")

    # pending
    p_pending = sub.add_parser("pending", parents=[common],
                               help="List unreviewed transactions")
    p_pending.add_argument(
        "--source", help="Filter by source (e.g. Amex, BoA)",
    )

    # calibrate
    sub.add_parser("calibrate", parents=[common],
                   help="Show verified category distribution")

    # trips
    p_trips = sub.add_parser("trips", parents=[common],
                             help="List trips")
    p_trips.add_argument(
        "--limit", type=int, default=10,
        help="Max trips to show (default: 10; 0 = all)",
    )
    p_trips.add_argument(
        "--all", action="store_true",
        help="Show all trips (no cap; same as --limit 0)",
    )
    p_trips.add_argument(
        "--year", type=int, default=None,
        help="Only trips overlapping calendar year YYYY",
    )
    p_trips.add_argument(
        "--since", default=None,
        help="Only trips overlapping on/after YYYY-MM-DD",
    )
    p_trips.add_argument(
        "--until", default=None,
        help="Only trips overlapping on/before YYYY-MM-DD",
    )

    # create-trip
    p_ct = sub.add_parser("create-trip", parents=[common],
                          help="Create a new trip")
    p_ct.add_argument("name", help="Trip name")
    p_ct.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    p_ct.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    p_ct.add_argument(
        "--type", default="personal",
        choices=["personal", "work"],
        help="Trip type (default: personal)",
    )
    p_ct.add_argument("--location", default=None, help="Trip location")

    # verify
    p_verify = sub.add_parser("verify", parents=[common],
                              help="Batch-verify transactions")
    p_verify.add_argument(
        "ids",
        help="Transaction IDs: 7087,7094-7108,7110",
    )
    p_verify.add_argument("--category", help="Set category for all IDs")
    p_verify.add_argument(
        "--trip", type=int, default=None,
        help="Assign trip ID",
    )
    p_verify.add_argument(
        "--project", type=int, default=None,
        help="Assign project ID",
    )
    p_verify.add_argument(
        "--force", action="store_true",
        help="Re-verify already-verified transactions",
    )

    # assign (pure project/trip tagging — no status change, no --force)
    p_assign = sub.add_parser(
        "assign", parents=[common],
        help="Tag transactions with a project/trip (status unchanged)")
    p_assign.add_argument("ids", help="Transaction IDs: 7087,7094-7108")
    p_assign.add_argument("--project", type=int, default=None,
                          help="Project ID to tag")
    p_assign.add_argument("--trip", type=int, default=None,
                          help="Trip ID to tag")

    # link
    p_link = sub.add_parser("link", parents=[common],
                            help="Link purchase to refund/cancellation")
    p_link.add_argument("purchase_id", type=int, help="Purchase transaction ID")
    p_link.add_argument("refund_id", type=int, help="Refund transaction ID")
    p_link.add_argument(
        "--force", action="store_true",
        help="Re-link already-linked transactions",
    )

    # link-amazon-refunds
    p_lar = sub.add_parser(
        "link-amazon-refunds", parents=[common],
        help="Auto-link Amazon refund↔purchase pairs by Order ID (1:1 only)",
    )
    p_lar.add_argument(
        "--dry-run", action="store_true",
        help="Preview links without writing",
    )

    # unlink
    p_unlink = sub.add_parser("unlink", parents=[common],
                              help="Remove a transaction link")
    p_unlink.add_argument("id", type=int, help="Either transaction ID in pair")

    # linked
    sub.add_parser("linked", parents=[common], help="List all linked pairs")

    # summary
    sub.add_parser("summary", parents=[common], help="Post-audit summary")

    # apply-rules
    p_ar = sub.add_parser("apply-rules", parents=[common],
                          help="Apply rules.json category guesses")
    p_ar.add_argument(
        "--since", default=None,
        help="Override the 365-day floor; examine rows on/after YYYY-MM-DD",
    )
    p_ar.add_argument(
        "--all", action="store_true",
        help="No date floor — categorize all pending rows (backfill pass)",
    )

    # detect-trips
    p_dt = sub.add_parser("detect-trips", parents=[common],
                          help="Detect trip candidates from transactions")
    p_dt.add_argument(
        "--months", type=int, default=12,
        help="Look-back window in months (default: 12)",
    )
    p_dt.add_argument(
        "--min-transactions", type=int, default=3,
        help="Min anchor transactions per cluster (default: 3)",
    )
    p_dt.add_argument(
        "--gap-days", type=int, default=3,
        help="Max days between transactions in a cluster (default: 3)",
    )

    # create-project
    p_cp = sub.add_parser("create-project", parents=[common],
                          help="Create a new project")
    p_cp.add_argument("name", help="Project name")
    p_cp.add_argument("--type", default="renovation",
                      help="Project type (renovation, event, ...)")
    p_cp.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    p_cp.add_argument(
        "--end", default=None,
        help="End date YYYY-MM-DD (omit for an open/ongoing project)",
    )
    p_cp.add_argument("--location", default=None, help="Project location")
    p_cp.add_argument("--description", default=None,
                      help="Free-text description (the user's framing)")
    p_cp.add_argument("--budget", type=float, default=None,
                      help="Planned budget")
    p_cp.add_argument("--keywords", default=None,
                      help="Comma-separated merchant/description keywords")
    p_cp.add_argument("--categories", default=None,
                      help="Comma-separated categories to match")

    # match-project
    p_mp = sub.add_parser("match-project", parents=[common],
                          help="Score candidates for a project (all open)")
    p_mp.add_argument(
        "project_id", type=int, nargs="?", default=None,
        help="Project ID (omit to sweep every open project)",
    )
    p_mp.add_argument(
        "--min-score", type=int, default=2,
        help="Minimum candidate score to surface (default: 2)",
    )

    # projects
    p_pl = sub.add_parser("projects", parents=[common],
                          help="List projects with spend/budget")
    p_pl.add_argument(
        "--status", default="open", choices=["open", "closed", "all"],
        help="Filter by status (default: open)",
    )

    # close-project
    p_clp = sub.add_parser("close-project", parents=[common],
                           help="Mark a project closed")
    p_clp.add_argument("project_id", type=int, help="Project ID")
    p_clp.add_argument(
        "--freeze-end", action="store_true",
        help="Set end_date to the last assigned transaction's date",
    )
    p_clp.add_argument(
        "--force", action="store_true", help="Re-close an already-closed project",
    )

    # project-summary
    p_ps = sub.add_parser("project-summary", parents=[common],
                          help="Per-project spend/budget report")
    p_ps.add_argument("project_id", type=int, help="Project ID")

    # edit-project
    p_ep = sub.add_parser("edit-project", parents=[common],
                          help="Amend an existing project's fields")
    p_ep.add_argument("project_id", type=int, help="Project ID")
    p_ep.add_argument("--name", default=None)
    p_ep.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    p_ep.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    p_ep.add_argument("--location", default=None)
    p_ep.add_argument("--description", default=None)
    p_ep.add_argument("--budget", type=float, default=None)
    p_ep.add_argument("--status", default=None, choices=["open", "closed"])
    p_ep.add_argument("--keywords", default=None,
                      help="Comma-separated keywords (replaces existing)")
    p_ep.add_argument("--categories", default=None,
                      help="Comma-separated categories (replaces existing)")

    # add-manual
    p_am = sub.add_parser("add-manual", parents=[common],
                          help="Create a manual (off-ledger) expense")
    p_am.add_argument("description", help="Expense description")
    p_am.add_argument("--amount", type=float, required=True,
                      help="Amount (positive for a cost)")
    p_am.add_argument("--category", required=True, help="Category")
    p_am.add_argument("--date", required=True,
                      help="Date YYYY-MM-DD (start_date)")
    p_am.add_argument("--end", default=None,
                      help="End date YYYY-MM-DD (recurring only)")
    p_am.add_argument("--frequency", default="one-time",
                      help="one-time (default), monthly, yearly")
    p_am.add_argument("--project", type=int, default=None,
                      help="Link to project ID (rolls into its total)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "pending": cmd_pending,
        "calibrate": cmd_calibrate,
        "trips": cmd_trips,
        "create-trip": cmd_create_trip,
        "verify": cmd_verify,
        "link": cmd_link,
        "link-amazon-refunds": cmd_link_amazon_refunds,
        "unlink": cmd_unlink,
        "linked": cmd_linked,
        "summary": cmd_summary,
        "apply-rules": cmd_apply_rules,
        "detect-trips": cmd_detect_trips,
        "create-project": cmd_create_project,
        "match-project": cmd_match_project,
        "projects": cmd_projects,
        "close-project": cmd_close_project,
        "project-summary": cmd_project_summary,
        "edit-project": cmd_edit_project,
        "add-manual": cmd_add_manual,
        "assign": cmd_assign,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
