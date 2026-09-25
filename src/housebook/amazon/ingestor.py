"""Amazon order/refund CSV ingestor.

Reads four CSVs from a profile directory (already extracted from
the Amazon zip export):

  Your Amazon Orders/Order History.csv         — physical orders
  Your Amazon Orders/Digital Content Orders.csv — Prime/Kindle/Audible
  Your Amazon Orders/Digital Returns.csv       — digital refunds (D01-)
  Your Returns & Refunds/Refund Details.csv    — physical refunds

CSV files are the source of truth directly — no JSON sidecars are
written for individual rows, since the data is already structured.
A single manifest sidecar per profile records what was imported.

Digital Content Orders carries one *order* across multiple CSV rows
(typically one `Price Amount` row + one `Tax` row, sometimes with
extra rows whose `Offer Type Code` is `Promotion` or `Coupon` to
encode discounts; the negative `Transaction Amount` on a `Coupon`
row offsets the positive `Price Amount` row). Net amount per order
= sum of `Transaction Amount` across all rows for one `Order ID`.

Digital Returns has the same multi-row-per-order shape as Digital
Content Orders; the per-row Transaction Amount values sum to the
gross refund credit (positive in the CSV) — the ingestor stores it
as a negative DB amount so it reduces spending the same way physical
refunds do. Discriminator metadata.csv = 'digital_refunds'.

Replacement Orders.csv is intentionally **not** ingested: its rows
are just (Order ID, Replacement Order ID) mappings; the replacement
Order ID itself already appears in Order History at $0 and is
correctly skipped by the zero-amount filter. Monthly Payment Balance
/ Plans (BNPL) are also not ingested as new transactions — the
gross Order History row already counts the full sale price, and
splitting it into installments is a reconciler-side concern (see
AGENTS.md "Amazon ↔ bank reconciliation").
"""

import csv
import json
import os
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from housebook.config.settings import WORKSPACE_DIR
from housebook.core.ingestor import Ingestor
from housebook.core.models import Transaction


class AmazonIngestor(Ingestor):
    def ingest_profile(
        self,
        profile_dir: str,
        profile: str | None = None,
    ) -> List[Transaction]:
        """Ingest Order History + Refund Details for one profile."""
        transactions: List[Transaction] = []
        profile = profile or os.path.basename(profile_dir)
        orders_path = os.path.join(
            profile_dir, "Your Amazon Orders", "Order History.csv",
        )
        digital_path = os.path.join(
            profile_dir, "Your Amazon Orders",
            "Digital Content Orders.csv",
        )
        digital_returns_path = os.path.join(
            profile_dir, "Your Amazon Orders",
            "Digital Returns.csv",
        )
        refunds_path = os.path.join(
            profile_dir, "Your Returns & Refunds", "Refund Details.csv",
        )

        orders_map: dict[str, str] = {}

        if os.path.exists(orders_path):
            rows = self._ingest_file(
                orders_path,
                lambda connection: self._ingest_orders(
                    orders_path, profile, orders_map,
                    connection=connection,
                ),
            )
            if rows is None:
                self._load_orders_map(orders_path, orders_map)
            else:
                transactions.extend(rows)

        if os.path.exists(digital_path):
            rows = self._ingest_file(
                digital_path,
                lambda connection: self._ingest_digital_content(
                    digital_path, profile, connection=connection,
                ),
            )
            transactions.extend(rows or [])

        if os.path.exists(digital_returns_path):
            rows = self._ingest_file(
                digital_returns_path,
                lambda connection: self._ingest_digital_returns(
                    digital_returns_path, profile,
                    connection=connection,
                ),
            )
            transactions.extend(rows or [])

        if os.path.exists(refunds_path):
            rows = self._ingest_file(
                refunds_path,
                lambda connection: self._ingest_refunds(
                    refunds_path, profile, orders_map,
                    connection=connection,
                ),
            )
            transactions.extend(rows or [])

        return transactions

    def ingest_all_profiles(
        self, amazon_dir: str, verbose: bool = False,
    ) -> dict:
        """Walk amazon/<profile>/ directories, ingest each.

        verbose: print every row suppressed by the duplicate check.
        """
        self._verbose = verbose
        self._dup_rows_skipped = 0
        result = {
            "profiles": 0, "rows_written": 0,
            "skipped": 0, "duplicate_rows_skipped": 0,
        }
        if not os.path.isdir(amazon_dir):
            return result
        for entry in sorted(os.listdir(amazon_dir)):
            subdir = os.path.join(amazon_dir, entry)
            if not os.path.isdir(subdir) or entry in (
                "__pycache__", ".DS_Store",
            ):
                continue
            txs = self.ingest_profile(subdir, profile=entry)
            if txs:
                result["profiles"] += 1
                result["rows_written"] += len(txs)
            else:
                result["skipped"] += 1
        result["duplicate_rows_skipped"] = self._dup_rows_skipped
        return result

    def _ingest_file(self, path: str, ingest) -> Optional[List[Transaction]]:
        """Run one CSV and its processed marker in one transaction.

        Returns ``None`` when the file was already processed, and a list
        (possibly empty) for a newly processed file.
        """
        file_hash = self.calculate_hash(path)
        dup_count = getattr(self, "_dup_rows_skipped", 0)
        messages: list[str] = []
        self._pending_messages = messages
        try:
            with self.db.transaction() as connection:
                if self.db.is_file_processed(
                    path, file_hash, connection=connection,
                ):
                    result = None
                else:
                    result = ingest(connection)
                    self.db.mark_file_processed(
                        path, file_hash, connection=connection,
                    )
        except BaseException:
            self._dup_rows_skipped = dup_count
            raise
        finally:
            del self._pending_messages
        for message in messages:
            print(message)
        return result

    # ── Orders ─────────────────────────────────────────────────

    def _ingest_orders(
        self, path: str, profile: str,
        orders_map: dict[str, str],
        *, connection,
    ) -> List[Transaction]:
        rel_path = self._ws_relative(path)
        file_sha = self.calculate_hash(path)
        txs: List[Transaction] = []
        with open(path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        # See cc/ingestor.py: a CSV can legitimately contain the same
        # product/date/amount more than once (two separate orders of
        # the same item). Track occurrences within this file so the
        # Nth identical row is skipped only when the DB already holds
        # N copies. (is_file_processed in ingest_profile guards
        # re-ingesting the whole file.)
        seen_in_file: dict[tuple, int] = {}
        for i, row in enumerate(reader):
            try:
                order_id = row["Order ID"]
                product = row["Product Name"]
                orders_map[order_id] = product
                currency = row.get("Currency", "USD")
                if currency != "USD":
                    continue
                amt_str = (
                    row["Total Amount"]
                    .replace("$", "").replace(",", "")
                )
                if amt_str.lower() == "not applicable" or not amt_str:
                    continue
                amount = Decimal(amt_str)
                if amount == 0:
                    continue
                date_raw = row["Order Date"].split("T")[0]
                desc = f"Amazon: {product[:100]}"
                key = (date_raw, desc, amount)
                occurrence = seen_in_file.get(key, 0) + 1
                seen_in_file[key] = occurrence
                if not self.db.transaction_exists(
                    desc, date_raw, amount, "Amazon",
                    max_duplicates=occurrence, profile=profile,
                    connection=connection,
                ):
                    cat = "Shopping & Retail"
                    if self.intel:
                        cat, _ = self.intel.get_category(desc, amount)
                        if cat == "Miscellaneous":
                            cat = "Shopping & Retail"
                    tx = Transaction(
                        date=date_raw,
                        description=desc,
                        amount=amount,
                        category=cat,
                        source="Amazon",
                        status="UNVERIFIED",
                        original_file=rel_path,
                        profile=profile,
                        needs_review=True,
                        metadata=json.dumps({
                            "amazon_order_id": order_id,
                            "csv": "orders",
                        }),
                    )
                    self.db.add_transaction(
                        tx,
                        source_file_path=rel_path,
                        source_file_sha256=file_sha,
                        sidecar_path=None,
                        connection=connection,
                    )
                    txs.append(tx)
                else:
                    self._note_dup_skip("order", date_raw, amount, desc)
            except (InvalidOperation, KeyError, ValueError) as e:
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Order parse error: {e}",
                    connection=connection,
                )
        if txs:
            self._report(
                f"  + Amazon orders ({profile}): {len(txs)} new"
            )
        return txs

    # ── Digital Content Orders ──────────────────────────────────

    def _ingest_digital_content(
        self, path: str, profile: str,
        *, connection,
    ) -> List[Transaction]:
        """Aggregate Digital Content rows by Order ID; write one
        transaction per order at the net Transaction Amount.

        Skips orders with non-USD currency, $0 net (free downloads,
        gift redemptions), or invalid amounts.
        """
        rel_path = self._ws_relative(path)
        file_sha = self.calculate_hash(path)
        txs: List[Transaction] = []

        with open(path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))

        # First pass: bucket rows by Order ID, accumulating the net
        # amount and capturing the first-seen product/date.
        orders: dict[str, dict] = defaultdict(
            lambda: {
                "net": Decimal("0"),
                "product": "",
                "date": "",
                "currency": "USD",
                "skip": False,  # set True on bad data or non-USD
                "first_line": None,
            }
        )
        for i, row in enumerate(reader):
            try:
                oid = row["Order ID"]
            except KeyError as e:
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Digital parse error: missing key {e}",
                    connection=connection,
                )
                continue
            o = orders[oid]
            if o["first_line"] is None:
                o["first_line"] = i + 2
                o["product"] = row.get("Product Name", "") or ""
                o["date"] = (
                    (row.get("Order Date", "") or "").split("T")[0]
                )
                o["currency"] = (
                    row.get("Price Currency Code", "USD") or "USD"
                ).strip() or "USD"
            else:
                # If any row in the order has a different currency,
                # treat the whole order as mixed/unsafe and skip.
                row_ccy = (
                    row.get("Price Currency Code", "USD") or "USD"
                ).strip() or "USD"
                if row_ccy != o["currency"]:
                    o["skip"] = True

            amt_raw = (row.get("Transaction Amount") or "").strip()
            if not amt_raw or amt_raw.lower() == "not applicable":
                continue
            try:
                o["net"] += Decimal(amt_raw)
            except InvalidOperation as e:
                o["skip"] = True
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Digital parse error: bad Transaction Amount "
                    f"({amt_raw!r}): {e}",
                    connection=connection,
                )

        # Second pass: emit one transaction per qualifying order.
        # Multiplicity is tracked the same way as Order History so two
        # legitimately-identical (date, desc, amount) orders both land.
        seen_in_file: dict[tuple, int] = {}
        for oid, o in orders.items():
            if o["skip"]:
                continue
            if o["currency"] != "USD":
                continue
            net = o["net"]
            if net == 0:
                continue
            if not o["date"]:
                self.db.log_ingestion_error(
                    path, o["first_line"] or 0, oid,
                    "Digital parse error: missing Order Date",
                    connection=connection,
                )
                continue
            desc = f"Amazon Digital: {o['product'][:100]}"
            key = (o["date"], desc, net)
            occurrence = seen_in_file.get(key, 0) + 1
            seen_in_file[key] = occurrence
            if self.db.transaction_exists(
                desc, o["date"], net, "Amazon",
                max_duplicates=occurrence, profile=profile,
                connection=connection,
            ):
                self._note_dup_skip("digital", o["date"], net, desc)
                continue
            cat = "Shopping & Retail"
            if self.intel:
                cat, _ = self.intel.get_category(desc, net)
                if cat == "Miscellaneous":
                    cat = "Shopping & Retail"
            tx = Transaction(
                date=o["date"],
                description=desc,
                amount=net,
                category=cat,
                source="Amazon",
                status="UNVERIFIED",
                original_file=rel_path,
                profile=profile,
                needs_review=True,
                metadata=json.dumps({
                    "amazon_order_id": oid,
                    "csv": "digital",
                }),
            )
            self.db.add_transaction(
                tx,
                source_file_path=rel_path,
                source_file_sha256=file_sha,
                sidecar_path=None,
                connection=connection,
            )
            txs.append(tx)

        if txs:
            self._report(
                f"  + Amazon digital ({profile}): {len(txs)} new"
            )
        return txs

    # ── Digital Returns ────────────────────────────────────────

    def _ingest_digital_returns(
        self, path: str, profile: str,
        *, connection,
    ) -> List[Transaction]:
        """Aggregate Digital Returns rows by Order ID; write one
        refund transaction per order at the negated net amount.

        Mirrors `_ingest_digital_content`'s multi-row aggregation
        shape (Price Amount + Tax + optional Coupon/Promotion rows),
        but the per-row Transaction Amount values sum to the gross
        refund credit (positive in the CSV). The DB amount is stored
        as the negation, matching how physical refunds appear.

        Skips orders with non-USD currency, $0 net, missing date, or
        invalid amounts.
        """
        rel_path = self._ws_relative(path)
        file_sha = self.calculate_hash(path)
        txs: List[Transaction] = []

        with open(path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))

        orders: dict[str, dict] = defaultdict(
            lambda: {
                "net": Decimal("0"),
                "product": "",
                "date": "",
                "currency": "USD",
                "skip": False,
                "first_line": None,
            }
        )
        for i, row in enumerate(reader):
            try:
                oid = row["Order ID"]
            except KeyError as e:
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Digital return parse error: missing key {e}",
                    connection=connection,
                )
                continue
            o = orders[oid]
            if o["first_line"] is None:
                o["first_line"] = i + 2
                o["product"] = row.get("Product Name", "") or ""
                o["date"] = (
                    (row.get("Return Date", "") or "").split("T")[0]
                )
                o["currency"] = (
                    row.get("Base Currency", "USD") or "USD"
                ).strip() or "USD"
            else:
                row_ccy = (
                    row.get("Base Currency", "USD") or "USD"
                ).strip() or "USD"
                if row_ccy != o["currency"]:
                    o["skip"] = True

            amt_raw = (row.get("Transaction Amount") or "").strip()
            if not amt_raw or amt_raw.lower() == "not applicable":
                continue
            try:
                o["net"] += Decimal(amt_raw)
            except InvalidOperation as e:
                o["skip"] = True
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Digital return parse error: bad "
                    f"Transaction Amount ({amt_raw!r}): {e}",
                    connection=connection,
                )

        seen_in_file: dict[tuple, int] = {}
        for oid, o in orders.items():
            if o["skip"]:
                continue
            if o["currency"] != "USD":
                continue
            net = o["net"]
            if net == 0:
                continue
            if not o["date"]:
                self.db.log_ingestion_error(
                    path, o["first_line"] or 0, oid,
                    "Digital return parse error: missing Return Date",
                    connection=connection,
                )
                continue
            amount = -net  # refund credit ⇒ negative DB amount
            desc = f"Amazon Digital Refund: {o['product'][:100]}"
            key = (o["date"], desc, amount)
            occurrence = seen_in_file.get(key, 0) + 1
            seen_in_file[key] = occurrence
            if self.db.transaction_exists(
                desc, o["date"], amount, "Amazon",
                max_duplicates=occurrence, profile=profile,
                connection=connection,
            ):
                self._note_dup_skip(
                    "digital_refund", o["date"], amount, desc,
                )
                continue
            cat = "Shopping & Retail"
            if self.intel:
                cat, _ = self.intel.get_category(desc, amount)
                if cat == "Miscellaneous":
                    cat = "Shopping & Retail"
            tx = Transaction(
                date=o["date"],
                description=desc,
                amount=amount,
                category=cat,
                source="Amazon",
                status="UNVERIFIED",
                original_file=rel_path,
                profile=profile,
                needs_review=True,
                metadata=json.dumps({
                    "amazon_order_id": oid,
                    "csv": "digital_refunds",
                }),
            )
            self.db.add_transaction(
                tx,
                source_file_path=rel_path,
                source_file_sha256=file_sha,
                sidecar_path=None,
                connection=connection,
            )
            txs.append(tx)

        if txs:
            self._report(
                f"  + Amazon digital refunds ({profile}): "
                f"{len(txs)} new"
            )
        return txs

    # ── Refunds ────────────────────────────────────────────────

    def _ingest_refunds(
        self, path: str, profile: str,
        orders_map: dict[str, str],
        *, connection,
    ) -> List[Transaction]:
        rel_path = self._ws_relative(path)
        file_sha = self.calculate_hash(path)
        txs: List[Transaction] = []
        with open(path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        # Same multiplicity handling as orders: allow a CSV's own
        # repeated refund rows while still deduping against the DB.
        seen_in_file: dict[tuple, int] = {}
        for i, row in enumerate(reader):
            try:
                order_id = row["Order ID"]
                currency = row.get("Currency", "USD")
                if currency != "USD":
                    continue
                amt_str = (
                    row["Refund Amount"]
                    .replace("$", "").replace(",", "")
                )
                if amt_str.lower() == "not applicable" or not amt_str:
                    continue
                amount = Decimal(amt_str)
                date_raw = row["Refund Date"].split("T")[0]
                product = orders_map.get(order_id, "Unknown Product")
                desc = f"Amazon REFUND: {product[:100]}"
                key = (date_raw, desc, -amount)
                occurrence = seen_in_file.get(key, 0) + 1
                seen_in_file[key] = occurrence
                if not self.db.transaction_exists(
                    desc, date_raw, -amount, "Amazon",
                    max_duplicates=occurrence, profile=profile,
                    connection=connection,
                ):
                    cat = "Shopping & Retail"
                    if self.intel:
                        cat, _ = self.intel.get_category(
                            f"Amazon: {product}", -amount,
                        )
                        if cat == "Miscellaneous":
                            cat = "Shopping & Retail"
                    tx = Transaction(
                        date=date_raw,
                        description=desc,
                        amount=-amount,
                        category=cat,
                        source="Amazon",
                        status="UNVERIFIED",
                        original_file=rel_path,
                        profile=profile,
                        needs_review=True,
                        metadata=json.dumps({
                            "amazon_order_id": order_id,
                            "csv": "refunds",
                        }),
                    )
                    self.db.add_transaction(
                        tx,
                        source_file_path=rel_path,
                        source_file_sha256=file_sha,
                        sidecar_path=None,
                        connection=connection,
                    )
                    txs.append(tx)
                else:
                    self._note_dup_skip("refund", date_raw, -amount, desc)
            except (InvalidOperation, KeyError, ValueError) as e:
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Refund parse error: {e}",
                    connection=connection,
                )
        if txs:
            self._report(
                f"  + Amazon refunds ({profile}): {len(txs)} new"
            )
        return txs

    # ── Helpers ─────────────────────────────────────────────────

    def _note_dup_skip(self, kind, date_raw, amount, desc) -> None:
        """Record (and optionally print) a row suppressed by the
        duplicate check. A non-zero count on a fresh ingest is worth
        seeing — see prompts/reingest.md."""
        self._dup_rows_skipped = getattr(self, "_dup_rows_skipped", 0) + 1
        if getattr(self, "_verbose", False):
            self._report(
                f"    ~ skip duplicate {kind} (already in DB): "
                f"{date_raw}  {amount}  {desc[:48]}"
            )

    def _load_orders_map(
        self, path: str, orders_map: dict[str, str],
    ) -> None:
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                orders_map[row["Order ID"]] = row["Product Name"]

    def _report(self, message: str) -> None:
        pending = getattr(self, "_pending_messages", None)
        if pending is None:
            print(message)
        else:
            pending.append(message)

    def _ws_relative(self, path: str) -> str:
        try:
            return os.path.relpath(
                path, str(WORKSPACE_DIR),
            ).replace(os.sep, "/")
        except ValueError:
            return path
