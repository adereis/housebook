import json
import os
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from housebook.config.settings import (
    DB_PATH,
    RECONCILER_CONFIG_JSON,
    WORKSPACE_DIR,
)

from .database import Database
from .models import CATEGORY_TRANSFERS_REFUNDS


def _parse_date(value) -> Optional[datetime]:
    """Parse an ISO date, or None if it is missing/malformed.

    A single legacy row with a NULL or non-ISO date used to abort the
    entire reconcile with a raw traceback; an unparseable date simply
    can't participate in date-window matching.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


class Reconciler:
    def __init__(self, db: Database):
        self.db = db
        self.config = self._load_json(
            RECONCILER_CONFIG_JSON,
            {
                "amazon_keywords": ["AMZN", "AMAZON"],
                "amazon_exclusion_patterns": [],
                "date_window_days": 3,
            },
        )

    def _load_json(self, path: str, default):
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return default

    def reconcile_amazon(
        self,
        date_window_days: Optional[int] = None,
        dry_run: bool = False,
    ) -> Tuple[int, int]:
        """Hide bank-side Amazon-merchant rows that duplicate the CSV.

        The Amazon CSV is the **source of truth** for Amazon purchases.
        When a card statement has been ingested and contains
        Amazon-merchant rows (the exceptional case — see
        ``AGENTS.md`` "Amazon ↔ bank reconciliation"), those rows
        double-count with the CSV and must be hidden.

        Matching runs in three passes:

        **Pass 0 — BNPL.** Amazon CSV rows tagged
        ``metadata.is_bnpl=true`` (with ``installment_count`` and
        ``downpayment``, derived from Monthly Payment Plans.csv)
        describe an installment plan. The pass finds bank-side Amazon
        rows whose amount matches the downpayment, the regular
        installment, or the final installment (which absorbs the
        rounding remainder) and whose date falls inside the plan's
        expected schedule, then marks each as RECONCILED. Partial
        coverage is allowed (only the first N of M installments may
        have posted). Finding *more* matching rows than the plan can
        hold means an unrelated charge shares the installment amount,
        so the plan is refused rather than guessing which rows to
        hide — those rows surface as orphans instead.

        **Pass 1 — aggregate-by-Order-ID.** Amazon CSV rows are grouped
        by ``metadata.amazon_order_id`` (set during ingest); a bank
        charge of \\$26.00 matches an Order whose CSV rows sum to
        \\$26.00. This closes multi-line physical orders that the
        fuzzy per-row matcher cannot handle (a bank charge of \\$26.00
        is one row, but its CSV counterpart can be 2+ shipment-line
        rows summing to \\$26.00). Only purchase rows
        (``csv='orders'`` or ``csv='digital'``) are aggregated;
        refund rows (``csv='refunds'`` / ``csv='digital_refunds'``)
        are excluded since they carry the original order's Order ID
        with opposite sign.

        **Pass 2 — fuzzy per-row fallback.** For bank rows that did
        not match an aggregate, the historical per-row matcher runs:
        match a single CSV row by amount (within 1¢) and date
        (within ``date_window_days``). Handles single-row orders and
        rows where the CSV side has no Order ID metadata (e.g. the
        14 §6c excess refunds).

        For each match, the **bank-side** row is marked
        ``status='RECONCILED'`` and recategorized to Transfers &
        Refunds. The Amazon CSV row is **never modified** — it remains
        visible as the counted truth.

        Both passes share ``consumed_amazon_ids``, so one CSV row
        cannot be claimed by multiple bank charges (and an aggregate
        with any consumed row is skipped). Bank rows that find no
        CSV match are recorded on ``self.last_orphans`` so the caller
        can flag them — they are the failure mode (likely missing
        from the CSV export, or outside its coverage window) and
        should be surfaced to the user, not silently left visible.

        Each ``last_matches`` entry carries a ``"matcher"`` field
        (``"aggregate"`` or ``"fuzzy"``); aggregate matches also
        include ``"amazon_order_id"`` and ``"n_lines"``.

        When ``dry_run`` is True, no UPDATEs are issued and nothing is
        committed; ``self.last_matches`` and ``self.last_orphans`` are
        still populated so a caller can preview.

        Returns ``(matches_found, orphan_count)``.
        """
        if date_window_days is None:
            date_window_days = self.config.get("date_window_days", 3)
        self.last_matches = []
        self.last_orphans = []

        conn = sqlite3.connect(self.db.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Keywords come from workspace config (synced JSON) — bind them
        # as parameters, never interpolate into the SQL text.
        keywords = self.config.get("amazon_keywords", ["AMZN", "AMAZON"])
        if not keywords:
            raise ValueError(
                "amazon_keywords is empty in reconciler config — "
                "no bank row could ever match"
            )
        keyword_clauses = " OR ".join(
            ["description LIKE ?" for _ in keywords]
        )

        # Bank-side Amazon-merchant rows not yet reconciled. Filtering
        # on status as well as category protects against legacy rows
        # that were RECONCILED without category being rewritten.
        c.execute(
            f"""
            SELECT * FROM transactions
            WHERE source != 'Amazon'
            AND status != 'RECONCILED'
            AND category != ?
            AND ({keyword_clauses})
            """,
            (CATEGORY_TRANSFERS_REFUNDS,
             *[f"%{kw}%" for kw in keywords]),
        )
        bank_txs = c.fetchall()

        # Post-filter: drop bank rows whose description matches an
        # exclusion pattern. These rows have an Amazon keyword in the
        # description but are NOT Amazon.com purchases — e.g. the
        # Amazon office cafeteria ("AMAZON XYZ01 CAFE ANYTOWN") is
        # a meal-spend merchant that shouldn't appear in the orphan
        # list at all. Substring match, case-insensitive.
        exclusions = self.config.get(
            "amazon_exclusion_patterns", [],
        )
        if exclusions:
            ex_lower = [p.lower() for p in exclusions]
            bank_txs = [
                b for b in bank_txs
                if not any(
                    p in (b["description"] or "").lower()
                    for p in ex_lower
                )
            ]

        # Amazon CSV rows — source of truth, never modified.
        c.execute("SELECT * FROM transactions WHERE source = 'Amazon'")
        amazon_txs = c.fetchall()

        aggregates = self._build_amazon_aggregates(amazon_txs)
        consumed_amazon_ids: set = set()

        # Pass 0: BNPL plan-based matching. Bank rows matching a
        # BNPL plan's downpayment/installment schedule are paired as
        # a group against the single gross Order History row tagged
        # `metadata.is_bnpl=true`. Runs first because the plan tag
        # is opt-in and has higher specificity than the amount-based
        # aggregate match — we know exactly which CSV row owns these
        # installments.
        matched_bnpl_ids = self._try_bnpl_pass(
            bank_txs, amazon_txs, consumed_amazon_ids, dry_run, c,
        )
        if matched_bnpl_ids:
            bank_txs = [
                b for b in bank_txs if b["id"] not in matched_bnpl_ids
            ]

        # Pass 1: aggregate-by-Order-ID
        unmatched_after_pass1 = []
        for b_tx in bank_txs:
            if not self._try_aggregate_match(
                b_tx, aggregates, consumed_amazon_ids,
                date_window_days, dry_run, c,
            ):
                unmatched_after_pass1.append(b_tx)

        # Pass 2: fuzzy per-row fallback
        for b_tx in unmatched_after_pass1:
            if not self._try_fuzzy_match(
                b_tx, amazon_txs, consumed_amazon_ids,
                date_window_days, dry_run, c,
            ):
                self.last_orphans.append({
                    "bank_id": b_tx["id"],
                    "bank_date": b_tx["date"],
                    "bank_desc": b_tx["description"],
                    "bank_source": b_tx["source"],
                    "amount": float(Decimal(str(b_tx["amount"]))),
                })

        if not dry_run:
            conn.commit()
        conn.close()
        return len(self.last_matches), len(self.last_orphans)

    # ── Pass 0 helpers (BNPL) ──────────────────────────────────

    def _try_bnpl_pass(
        self, bank_txs, amazon_txs, consumed, dry_run, c,
    ):
        """Pair bank installment charges against the single gross
        Order History row of a BNPL order.

        Returns the set of bank tx IDs reconciled by this pass, so
        the main loop can skip them in passes 1/2.

        A BNPL plan is an Amazon CSV row whose metadata carries
        ``is_bnpl=true``, ``installment_count``, and ``downpayment``.
        For each plan, the bank-side schedule is one charge at
        ``downpayment`` plus ``(installment_count - 1)`` regular
        charges at ``(gross - downpayment) / (installment_count - 1)``.

        Partial coverage is allowed — if only the first 4 of 5
        installments have hit the card so far, those 4 still match
        (and the gross CSV row stays counted). A later reconcile
        run picks up the 5th when it lands.

        Safety check: candidate total must not exceed gross + 1¢.
        That guards against amount-collision pulling in unrelated
        bank rows whose amounts happen to equal the installment.
        """
        matched: set = set()
        plans = self._build_bnpl_plans(amazon_txs)
        if not plans:
            return matched

        for plan in plans:
            if plan["row_id"] in consumed:
                continue
            candidates = self._find_bnpl_candidates(
                bank_txs, plan, matched,
            )
            # None = ambiguous (more matching rows than the plan can
            # hold); [] = nothing posted yet. Both mean "don't claim".
            if not candidates:
                continue
            total = sum(
                Decimal(str(b["amount"])) for b in candidates
            )
            if total > plan["gross"] + Decimal("0.01"):
                continue  # Safety: don't over-match

            for b_tx in candidates:
                matched.add(b_tx["id"])
                self.last_matches.append({
                    "bank_id": b_tx["id"],
                    "bank_date": b_tx["date"],
                    "bank_desc": b_tx["description"],
                    "bank_source": b_tx["source"],
                    "amazon_id": plan["row_id"],
                    "amazon_date": plan["date"],
                    "amount": float(Decimal(str(b_tx["amount"]))),
                    "matcher": "bnpl",
                    "amazon_order_id": plan["order_id"],
                    "n_lines": 1,
                })
                if not dry_run:
                    self._mark_bank_reconciled(c, b_tx["id"])
            consumed.add(plan["row_id"])
        return matched

    def _build_bnpl_plans(self, amazon_txs):
        """Return BNPL plan dicts for every Amazon CSV row tagged
        ``metadata.is_bnpl=true``. Rows missing the required
        installment_count / downpayment fields are skipped."""
        plans = []
        for a_tx in amazon_txs:
            meta = a_tx["metadata"]
            if not meta:
                continue
            try:
                m = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                continue
            if not m.get("is_bnpl"):
                continue
            try:
                count = int(m["installment_count"])
                down = Decimal(str(m["downpayment"]))
            except (KeyError, ValueError, TypeError, InvalidOperation):
                # InvalidOperation is an ArithmeticError, not a
                # ValueError — a garbage downpayment string used to
                # abort the whole reconcile instead of skipping the tag.
                continue
            if count < 2:
                continue
            gross = Decimal(str(a_tx["amount"]))
            regular = (
                (gross - down) / (count - 1)
            ).quantize(Decimal("0.01"))
            # When (gross - down) doesn't divide evenly the final
            # installment absorbs the remainder — e.g. $100 over 3
            # gives 33.33, 33.33, 33.34. Matching only `regular` left
            # that last cent-off charge a permanent orphan.
            final = (gross - down) - regular * (count - 2)
            plans.append({
                "row_id": a_tx["id"],
                "order_id": m.get("amazon_order_id", ""),
                "date": a_tx["date"],
                "gross": gross,
                "count": count,
                "downpayment": down,
                "regular": regular,
                "final": final,
            })
        return plans

    def _find_bnpl_candidates(self, bank_txs, plan, already_matched):
        """Return bank rows whose amount and date match a BNPL
        plan's installment schedule.

        Date window: from ``order_date - 7`` (downpayment can post
        slightly before the recorded order date if posted on a
        weekend) through ``order_date + 32 × (count + 1)`` days
        (one extra month of slack for late installments).

        A plan holds at most one downpayment and ``count - 1``
        installments. Finding MORE matching rows than that means an
        unrelated Amazon charge shares the installment amount inside
        the window, and nothing distinguishes it from a real
        installment — so the plan is refused (returns None) rather
        than guessing which rows to hide. Silently hiding a real
        expense from spending views is the worse error; an unmatched
        installment merely surfaces as an orphan for the agent.
        """
        p_date = _parse_date(plan["date"])
        if p_date is None:
            return None
        earliest = p_date - timedelta(days=7)
        latest = p_date + timedelta(days=32 * (plan["count"] + 1))

        downs, regulars = [], []
        for b in bank_txs:
            if b["id"] in already_matched:
                continue
            b_date = _parse_date(b["date"])
            if b_date is None:
                continue
            if not (earliest <= b_date <= latest):
                continue
            amount = Decimal(str(b["amount"]))
            if abs(amount - plan["downpayment"]) < Decimal("0.01"):
                downs.append((b_date, b))
            elif (
                abs(amount - plan["regular"]) < Decimal("0.01")
                # The final installment absorbs the rounding remainder.
                or abs(amount - plan["final"]) < Decimal("0.01")
            ):
                regulars.append((b_date, b))

        if abs(plan["downpayment"] - plan["regular"]) < Decimal("0.01"):
            # Indistinguishable amounts: one pool of at most `count`.
            if len(downs) > plan["count"]:
                return None
            found = downs
        else:
            if len(downs) > 1 or len(regulars) > plan["count"] - 1:
                return None
            found = downs + regulars

        found.sort(key=lambda pair: pair[0])
        return [b for _, b in found]

    # ── Pass 1 helpers ─────────────────────────────────────────

    def _build_amazon_aggregates(self, amazon_txs):
        """Group Amazon CSV rows by ``metadata.amazon_order_id``.

        Only purchase rows (``csv='orders'`` / ``csv='digital'``) are
        included; refund rows (``csv='refunds'`` /
        ``csv='digital_refunds'``) carry the original order's Order ID
        with opposite sign and would zero out a paid order's aggregate.
        Rows with NULL or non-purchase metadata are silently skipped
        (they fall through to the pass-2 fuzzy matcher).
        """
        by_id: dict = {}
        for a_tx in amazon_txs:
            meta = a_tx["metadata"]
            if not meta:
                continue
            try:
                m = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                continue
            oid = m.get("amazon_order_id")
            csv_tag = m.get("csv")
            if not oid or csv_tag not in ("orders", "digital"):
                continue
            by_id.setdefault(oid, []).append(a_tx)

        aggregates = []
        for oid, rows in by_id.items():
            total = sum(Decimal(str(r["amount"])) for r in rows)
            # All rows of a multi-line order carry the same Order Date
            # in the source CSV; using MIN() is just a defensive choice
            # in case a future Amazon export changes that property.
            agg_date = min(r["date"] for r in rows)
            aggregates.append({
                "order_id": oid,
                "date": agg_date,
                "total": total,
                "row_ids": [r["id"] for r in rows],
                "n_lines": len(rows),
            })
        return aggregates

    def _try_aggregate_match(
        self, b_tx, aggregates, consumed, window, dry_run, c,
    ):
        b_date = _parse_date(b_tx["date"])
        if b_date is None:
            return False
        b_amount = Decimal(str(b_tx["amount"]))
        for agg in aggregates:
            # An aggregate with ANY consumed row is no longer
            # available at its full total. Skip rather than match
            # against a stale total.
            if any(rid in consumed for rid in agg["row_ids"]):
                continue
            if abs(b_amount - agg["total"]) >= Decimal("0.01"):
                continue
            a_date = _parse_date(agg["date"])
            if a_date is None or abs((b_date - a_date).days) > window:
                continue
            consumed.update(agg["row_ids"])
            self.last_matches.append({
                "bank_id": b_tx["id"],
                "bank_date": b_tx["date"],
                "bank_desc": b_tx["description"],
                "bank_source": b_tx["source"],
                "amazon_id": agg["row_ids"][0],
                "amazon_date": agg["date"],
                "amount": float(b_amount),
                "matcher": "aggregate",
                "amazon_order_id": agg["order_id"],
                "n_lines": agg["n_lines"],
            })
            if not dry_run:
                self._mark_bank_reconciled(c, b_tx["id"])
            return True
        return False

    # ── Pass 2 helpers ─────────────────────────────────────────

    def _try_fuzzy_match(
        self, b_tx, amazon_txs, consumed, window, dry_run, c,
    ):
        b_date = _parse_date(b_tx["date"])
        if b_date is None:
            return False
        b_amount = Decimal(str(b_tx["amount"]))
        for a_tx in amazon_txs:
            if a_tx["id"] in consumed:
                continue
            a_amount = Decimal(str(a_tx["amount"]))
            if abs(b_amount - a_amount) >= Decimal("0.01"):
                continue
            a_date = _parse_date(a_tx["date"])
            if a_date is None or abs((b_date - a_date).days) > window:
                continue
            consumed.add(a_tx["id"])
            self.last_matches.append({
                "bank_id": b_tx["id"],
                "bank_date": b_tx["date"],
                "bank_desc": b_tx["description"],
                "bank_source": b_tx["source"],
                "amazon_id": a_tx["id"],
                "amazon_date": a_tx["date"],
                "amount": float(b_amount),
                "matcher": "fuzzy",
            })
            if not dry_run:
                self._mark_bank_reconciled(c, b_tx["id"])
            return True
        return False

    def _mark_bank_reconciled(self, c, bank_id):
        """Bank side only: hide and recategorize. The Amazon CSV row
        is intentionally NOT modified — it stays visible as the source
        of truth."""
        c.execute(
            f"""
            UPDATE transactions
            SET category = '{CATEGORY_TRANSFERS_REFUNDS}',
                status = 'RECONCILED',
                needs_review = 0
            WHERE id = ?
            """,
            (bank_id,),
        )


def main(argv=None):
    """Console entrypoint: `housebook-reconcile`.

    Hides bank/CC Amazon-merchant rows that duplicate the Amazon CSV
    (source of truth). Matches by amount (within 1¢) and date (within
    the configured window, default 3 days), then marks the **bank-side**
    row RECONCILED + Transfers & Refunds. The Amazon CSV row is never
    modified.

    This is an exceptional operation — Amazon-Chase statements are
    normally not ingested. See AGENTS.md "Amazon ↔ bank reconciliation"
    for when this runbook applies. Run with --dry-run first: the
    matcher is fuzzy (amount+date), so two unrelated same-amount
    charges within the window can mis-pair on a high-volume card.

    Orphan bank-Amazon rows (no CSV match) are reported separately
    and warrant a conversation with the user.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="housebook-reconcile",
        description="Hide bank/CC Amazon-merchant rows that duplicate "
                    "the Amazon CSV (CSV stays as source of truth).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview matches without writing to the database.",
    )
    parser.add_argument(
        "--db", dest="db_path", default=None,
        help="Override the database path.",
    )
    parser.add_argument(
        "--date-window", dest="date_window", type=int, default=None,
        help="Max days between bank and Amazon dates (default: 3).",
    )
    args = parser.parse_args(argv)

    db = Database(args.db_path or DB_PATH, workspace_dir=str(WORKSPACE_DIR))
    rec = Reconciler(db)
    mode = "dry run — no changes written" if args.dry_run else "writing changes"
    print(f"  Amazon reconciliation ({mode})...")
    matches, orphans = rec.reconcile_amazon(
        date_window_days=args.date_window, dry_run=args.dry_run,
    )
    for m in rec.last_matches:
        # Aggregate matches: append "(N lines)" when an order has
        # more than one CSV row — that is the cue this match used
        # the Order-ID-aware path rather than fuzzy per-row.
        # BNPL matches: append "(bnpl, <order-id>)" so the user can
        # spot installment groups in the output.
        extra = ""
        if m.get("matcher") == "aggregate" and m.get("n_lines", 1) > 1:
            extra = f"  ({m['n_lines']} lines, {m['amazon_order_id']})"
        elif m.get("matcher") == "bnpl":
            extra = f"  (bnpl, {m['amazon_order_id']})"
        print(
            f"    {m['bank_source']:>12} #{m['bank_id']} "
            f"{m['bank_date']}  ${m['amount']:>9,.2f}  "
            f"{m['bank_desc'][:40]:40}  ↔  Amazon #{m['amazon_id']} "
            f"{m['amazon_date']}{extra}"
        )
    n_bnpl = sum(
        1 for m in rec.last_matches if m.get("matcher") == "bnpl"
    )
    n_agg = sum(
        1 for m in rec.last_matches if m.get("matcher") == "aggregate"
    )
    n_fuzzy = sum(
        1 for m in rec.last_matches if m.get("matcher") == "fuzzy"
    )
    print(
        f"  Reconciliation complete. {matches} match(es) "
        f"({n_bnpl} bnpl, {n_agg} aggregate, {n_fuzzy} fuzzy)."
    )
    if orphans:
        print(
            f"\n  ⚠  {orphans} bank-Amazon row(s) found NO CSV match — "
            "review with the user (either the CSV is incomplete, or "
            "the row predates the CSV coverage window):"
        )
        for o in rec.last_orphans:
            print(
                f"    {o['bank_source']:>12} #{o['bank_id']} "
                f"{o['bank_date']}  ${o['amount']:>9,.2f}  "
                f"{o['bank_desc'][:60]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
