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

Each export is cumulative, so most of its rows are already stored.
A row is recognized by Amazon's identity — Order ID, date and amount
within its CSV and profile — never by its description, which Amazon
rewords between exports. See `_sync_rows`.
"""

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from housebook.config.settings import WORKSPACE_DIR
from housebook.core.ingestor import Ingestor
from housebook.core.models import Transaction

# Label per metadata.csv value, for progress and conflict messages.
KIND_LABELS = {
    "orders": "orders",
    "digital": "digital",
    "digital_refunds": "digital refunds",
    "refunds": "refunds",
}


class AmazonIdentityError(Exception):
    """A profile holds Amazon rows that carry no Order ID.

    Such a row can never match an export row, so ingesting would
    store its purchase or refund a second time.
    """


@dataclass
class _ExportRow:
    """One prospective DB row, parsed from an export CSV."""

    order_id: str
    date: str
    amount: Decimal
    description: str
    category_text: str  # text handed to Intelligence for a category
    line: int  # CSV line, for ingestion_errors


@dataclass
class _StoredRow:
    id: int
    date: str
    amount: Decimal
    description: str
    linked_transaction_id: Optional[int]


def _cents(value) -> Decimal:
    """Compare amounts at cent precision; the DB column is REAL."""
    return Decimal(str(value)).quantize(Decimal("0.01"))


class AmazonIngestor(Ingestor):
    def __init__(self, db, intel):
        super().__init__(db, intel)
        # Running totals across every file this instance ingests.
        self.counts = self._zero_counts()

    def ingest_profile(
        self,
        profile_dir: str,
        profile: str | None = None,
    ) -> List[Transaction]:
        """Ingest one profile's four export CSVs; return new rows."""
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

        verbose: print every export row found already stored.
        """
        self._verbose = verbose
        self.counts = self._zero_counts()
        result = {"profiles": 0, "rows_written": 0, "skipped": 0}
        if not os.path.isdir(amazon_dir):
            return {**result, **self.counts}
        for entry in sorted(os.listdir(amazon_dir)):
            subdir = os.path.join(amazon_dir, entry)
            if not os.path.isdir(subdir) or entry in (
                "__pycache__", ".DS_Store",
            ):
                continue
            restated_before = self.counts["restated"]
            txs = self.ingest_profile(subdir, profile=entry)
            if txs or self.counts["restated"] > restated_before:
                result["profiles"] += 1
                result["rows_written"] += len(txs)
            else:
                result["skipped"] += 1
        return {**result, **self.counts}

    @staticmethod
    def _zero_counts() -> dict:
        return {"already_present": 0, "restated": 0, "conflicts": 0}

    def _count(self, name: str, n: int = 1) -> None:
        self.counts[name] += n

    def _ingest_file(self, path: str, ingest) -> Optional[List[Transaction]]:
        """Run one CSV and its processed marker in one transaction.

        Returns ``None`` when the file was already processed, and a list
        (possibly empty) for a newly processed file.
        """
        file_hash = self.calculate_hash(path)
        counts = dict(self.counts)
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
            self.counts = counts
            raise
        finally:
            del self._pending_messages
        for message in messages:
            print(message)
        return result

    # ── Matching export rows to stored rows ────────────────────

    def _sync_rows(
        self, kind: str, rows: List[_ExportRow], path: str,
        profile: str, *, connection,
    ) -> List[Transaction]:
        """Store what one CSV adds or restates; return inserted rows.

        A row's identity is (Order ID, date, amount) within its CSV
        kind and profile. The description is deliberately left out.
        Amazon renames products between exports (a subscription
        add-on renamed on every past charge). A refund's description
        names whichever item of a multi-item order the export lists
        last. Either way a description key re-inserts rows the DB
        already holds, which is how earlier exports duplicated refunds.

        Per Order ID, export rows that match a stored row exactly are
        already present. The leftovers are settled as follows:

        - Only export rows remain: they are new and get inserted. A
          new order, or a later refund on an old order, looks like this.
        - One stored and one export row remain on the same date, or one
          of each overall: Amazon restated the row. A pre-order that
          was authorized at one price and charged at a lower one looks
          like this. The stored row takes the export's date and amount
          and is flagged for review again.
        - Anything else remains, such as a stored row the export no
          longer lists: it is reported and logged to ingestion_errors,
          and nothing is written for that order. Guessing would either
          double-count or rewrite an audited row on a hunch.
        """
        rel_path = self._ws_relative(path)
        file_sha = self.calculate_hash(path)
        label = f"Amazon {KIND_LABELS[kind]} ({profile})"

        anonymous = connection.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE source = 'Amazon' AND profile = ? AND ("
            " json_extract(metadata, '$.amazon_order_id') IS NULL"
            " OR json_extract(metadata, '$.csv') IS NULL)",
            (profile,),
        ).fetchone()[0]
        if anonymous:
            raise AmazonIdentityError(
                f"{anonymous} Amazon row(s) of profile {profile!r} carry "
                f"no Order ID in metadata, so no export row can match "
                f"them and ingest would store them twice. Give each "
                f"its amazon_order_id and csv, or remove it if it "
                f"duplicates a row that has them, then ingest again."
            )

        stored: dict[str, list[_StoredRow]] = defaultdict(list)
        for tx_id, oid, date, amount, desc, linked in connection.execute(
            "SELECT id, json_extract(metadata, '$.amazon_order_id'),"
            " date, amount, description, linked_transaction_id "
            "FROM transactions WHERE source = 'Amazon' AND profile = ?"
            " AND json_extract(metadata, '$.csv') = ? ORDER BY id",
            (profile, kind),
        ):
            stored[oid].append(
                _StoredRow(tx_id, date, _cents(amount), desc, linked),
            )
        exported: dict[str, list[_ExportRow]] = defaultdict(list)
        for row in rows:
            exported[row.order_id].append(row)

        txs: List[Transaction] = []
        order_ids = list(exported) + [o for o in stored if o not in exported]
        for oid in order_ids:
            new = list(exported.get(oid, []))
            gone = list(stored.get(oid, []))
            for row in list(new):
                match = next(
                    (s for s in gone
                     if s.date == row.date and s.amount == _cents(row.amount)),
                    None,
                )
                if match is not None:
                    gone.remove(match)
                    new.remove(row)
                    self._note_present(kind, row)
            for row, old in self._pair_restated(new, gone):
                self.db.restate_transaction(
                    old.id, date=row.date, amount=row.amount,
                    source_file_path=rel_path,
                    source_file_sha256=file_sha,
                    connection=connection,
                )
                self._count("restated")
                link = (
                    f"; linked to #{old.linked_transaction_id}, so "
                    f"re-check that pair"
                    if old.linked_transaction_id else ""
                )
                self._report(
                    f"  ~ {label}: #{old.id} restated "
                    f"{old.date} {old.amount} -> {row.date} "
                    f"{_cents(row.amount)}, flagged for review{link}: "
                    f"{old.description[:60]}"
                )
            if gone:
                self._report_conflict(
                    label, path, oid, new, gone, connection=connection,
                )
                continue
            for row in new:
                txs.append(self._insert(
                    kind, row, profile, rel_path, file_sha,
                    connection=connection,
                ))
        if txs:
            self._report(f"  + {label}: {len(txs)} new")
        return txs

    @staticmethod
    def _pair_restated(
        new: List[_ExportRow], gone: List[_StoredRow],
    ) -> list[tuple[_ExportRow, _StoredRow]]:
        """Pair leftover export and stored rows of one Order ID.

        A pair is unambiguous when one row of each side shares a date,
        or when exactly one row of each side is left. Paired rows are
        removed from both lists in place.
        """
        pairs = []
        for date in sorted({row.date for row in new}):
            on_new = [row for row in new if row.date == date]
            on_gone = [s for s in gone if s.date == date]
            if len(on_new) == 1 and len(on_gone) == 1:
                pairs.append((on_new[0], on_gone[0]))
                new.remove(on_new[0])
                gone.remove(on_gone[0])
        if len(new) == 1 and len(gone) == 1:
            pairs.append((new.pop(), gone.pop()))
        return pairs

    def _report_conflict(
        self, label: str, path: str, oid: str,
        new: List[_ExportRow], gone: List[_StoredRow], *, connection,
    ) -> None:
        """Log an order whose rows cannot be settled automatically."""
        for s in gone:
            self.db.log_ingestion_error(
                path, 0, f"{oid} #{s.id} {s.date} {s.amount}",
                f"{label}: stored row #{s.id} is not in this export",
                connection=connection,
            )
            self._report(
                f"  ! {label}: order {oid}: stored #{s.id} "
                f"{s.date} {s.amount} is not in this export: "
                f"{s.description[:60]}"
            )
        for row in new:
            self.db.log_ingestion_error(
                path, row.line, f"{oid} {row.date} {row.amount}",
                f"{label}: not inserted; order {oid} has stored rows "
                f"this export no longer lists",
                connection=connection,
            )
            self._report(
                f"  ! {label}: order {oid}: export row {row.date} "
                f"{_cents(row.amount)} not inserted, because the order "
                f"has unmatched stored rows"
            )
        self._count("conflicts", len(gone) + len(new))

    def _insert(
        self, kind: str, row: _ExportRow, profile: str,
        rel_path: str, file_sha: str, *, connection,
    ) -> Transaction:
        cat = "Shopping & Retail"
        if self.intel:
            cat, _ = self.intel.get_category(row.category_text, row.amount)
            if cat == "Miscellaneous":
                cat = "Shopping & Retail"
        tx = Transaction(
            date=row.date,
            description=row.description,
            amount=row.amount,
            category=cat,
            source="Amazon",
            status="UNVERIFIED",
            original_file=rel_path,
            profile=profile,
            needs_review=True,
            metadata=json.dumps({
                "amazon_order_id": row.order_id,
                "csv": kind,
            }),
        )
        self.db.add_transaction(
            tx,
            source_file_path=rel_path,
            source_file_sha256=file_sha,
            sidecar_path=None,
            connection=connection,
        )
        return tx

    # ── Orders ─────────────────────────────────────────────────

    def _ingest_orders(
        self, path: str, profile: str,
        orders_map: dict[str, str],
        *, connection,
    ) -> List[Transaction]:
        """One row per shipment line; an order may span several.

        Two identical lines (the same item bought twice) are two rows;
        `_sync_rows` compares rows as a multiset, so both land.
        """
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = list(csv.DictReader(f))
        rows: List[_ExportRow] = []
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
                desc = f"Amazon: {product[:100]}"
                rows.append(_ExportRow(
                    order_id, row["Order Date"].split("T")[0],
                    amount, desc, desc, i + 2,
                ))
            except (InvalidOperation, KeyError, ValueError) as e:
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Order parse error: {e}",
                    connection=connection,
                )
        return self._sync_rows(
            "orders", rows, path, profile, connection=connection,
        )

    # ── Digital Content Orders ──────────────────────────────────

    def _ingest_digital_content(
        self, path: str, profile: str,
        *, connection,
    ) -> List[Transaction]:
        """Aggregate Digital Content rows by Order ID into one row
        per order at the net Transaction Amount.

        Skips orders with non-USD currency, $0 net (free downloads,
        gift redemptions), or invalid amounts.
        """
        with open(path, "r", encoding="utf-8-sig") as f:
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

        # Second pass: one row per qualifying order.
        rows: List[_ExportRow] = []
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
            rows.append(_ExportRow(
                oid, o["date"], net, desc, desc, o["first_line"],
            ))
        return self._sync_rows(
            "digital", rows, path, profile, connection=connection,
        )

    # ── Digital Returns ────────────────────────────────────────

    def _ingest_digital_returns(
        self, path: str, profile: str,
        *, connection,
    ) -> List[Transaction]:
        """Aggregate Digital Returns rows by Order ID into one
        refund row per order at the negated net amount.

        Mirrors `_ingest_digital_content`'s multi-row aggregation
        shape (Price Amount + Tax + optional Coupon/Promotion rows),
        but the per-row Transaction Amount values sum to the gross
        refund credit (positive in the CSV). The DB amount is stored
        as the negation, matching how physical refunds appear.

        Skips orders with non-USD currency, $0 net, missing date, or
        invalid amounts.
        """
        with open(path, "r", encoding="utf-8-sig") as f:
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

        rows: List[_ExportRow] = []
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
            rows.append(_ExportRow(
                oid, o["date"], amount, desc, desc, o["first_line"],
            ))
        return self._sync_rows(
            "digital_refunds", rows, path, profile,
            connection=connection,
        )

    # ── Refunds ────────────────────────────────────────────────

    def _ingest_refunds(
        self, path: str, profile: str,
        orders_map: dict[str, str],
        *, connection,
    ) -> List[Transaction]:
        """One row per refund event; an order may have several.

        The description borrows a product name from Order History, so
        it is cosmetic (see AGENTS.md "Refund-row schema quirks").
        """
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = list(csv.DictReader(f))
        rows: List[_ExportRow] = []
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
                amount = -Decimal(amt_str)
                product = orders_map.get(order_id, "Unknown Product")
                rows.append(_ExportRow(
                    order_id, row["Refund Date"].split("T")[0], amount,
                    f"Amazon REFUND: {product[:100]}",
                    f"Amazon: {product}", i + 2,
                ))
            except (InvalidOperation, KeyError, ValueError) as e:
                self.db.log_ingestion_error(
                    path, i + 2, str(row)[:200],
                    f"Refund parse error: {e}",
                    connection=connection,
                )
        return self._sync_rows(
            "refunds", rows, path, profile, connection=connection,
        )

    # ── Helpers ─────────────────────────────────────────────────

    def _note_present(self, kind: str, row: _ExportRow) -> None:
        """Count (and optionally print) an export row already stored."""
        self._count("already_present")
        if getattr(self, "_verbose", False):
            self._report(
                f"    = already stored ({kind}): {row.date}  "
                f"{_cents(row.amount)}  {row.description[:48]}"
            )

    def _load_orders_map(
        self, path: str, orders_map: dict[str, str],
    ) -> None:
        with open(path, "r", encoding="utf-8-sig") as f:
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
