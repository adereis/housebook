"""Credit-card statement ingestor.

Reads JSON sidecars produced by `prompts/cc/import.md`, validates
the envelope + data block, and writes one row per transaction to
the `transactions` table. Each row records full provenance back to
the source PDF and the sidecar that produced it.

The ingestor refuses to write rows from a sidecar that fails
validation (`cc.schema.validate_data_block`). This is the second
layer of the defense-in-depth that prevents classify-time bugs
from reaching the DB; the first layer is the SOP itself, the third
is `housebook-cc validate` as a manual gate.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import List

from housebook.config.settings import WORKSPACE_DIR
from housebook.core import sidecar as sidecar_mod
from housebook.core.ingestor import Ingestor
from housebook.core.models import Transaction

from .issuers import IssuerResolver
from .schema import CcSchemaError, validate_data_block


class CcIngestor(Ingestor):
    """Ingest CC sidecars into the `transactions` table."""

    @property
    def _issuers(self) -> IssuerResolver:
        """Lazily-built issuer alias resolver (one per ingestor)."""
        resolver = getattr(self, "_issuer_resolver", None)
        if resolver is None:
            resolver = self._issuer_resolver = IssuerResolver()
        return resolver

    def ingest_sidecar(self, json_path: str) -> List[Transaction]:
        """Ingest one sidecar as an all-or-nothing file transaction."""
        self._last_skipped = False
        dup_count = getattr(self, "_dup_rows_skipped", 0)
        messages: list[str] = []
        self._pending_messages = messages
        try:
            with self.db.transaction() as connection:
                result = self._ingest_sidecar(json_path, connection)
        except BaseException:
            # A rolled-back file did not suppress any durable rows.
            self._dup_rows_skipped = dup_count
            raise
        finally:
            del self._pending_messages
        for message in messages:
            print(message)
        return result

    def _ingest_sidecar(
        self, json_path: str, connection,
    ) -> List[Transaction]:
        """Validate and ingest one sidecar. Returns the rows written.

        Raises CcSchemaError if validation fails — callers (the CLI)
        decide whether to log + skip or re-raise.
        """
        json_hash = self.calculate_hash(json_path)
        if self.db.is_file_processed(
            json_path, json_hash, connection=connection,
        ):
            self._last_skipped = True
            return []

        sc = sidecar_mod.load(json_path)
        if sc.source != "cc":
            raise sidecar_mod.SidecarError(
                f"sidecar source is {sc.source!r}, expected 'cc'"
            )

        errors = validate_data_block(sc.data)
        if errors:
            raise CcSchemaError(
                f"{json_path}: {len(errors)} validation error(s):\n  - "
                + "\n  - ".join(errors)
            )

        sidecar_rel = self._relative(json_path)
        source_file_path = sc.source_file.path  # already workspace-relative
        source_file_sha256 = sc.source_file.sha256
        # Canonicalize the issuer so spelling variants across sidecars
        # ("Home-Goods" vs "Home Goods") collapse to one DB `source`
        # instead of silently fragmenting a card's history.
        issuer = self._issuers.resolve(sc.data["issuer"])

        rows_written: List[Transaction] = []
        # A single statement can legitimately list the same charge
        # more than once (two identical supercharger sessions, two
        # $1 vending purchases, etc.). Track each line's occurrence
        # within this sidecar and skip the Nth identical line only if
        # the DB already holds N copies — otherwise inserting the
        # first copy would suppress every later copy from the same
        # statement. (is_file_processed above already guards against
        # re-ingesting the whole file, so cross-file dedup is all the
        # transaction_exists check needs to provide here.)
        seen_in_sidecar: dict[tuple, int] = {}
        for t in sc.data["transactions"]:
            desc = t.get("description", "")
            amount = Decimal(str(t["amount"]))

            key = (t["date"], desc, amount)
            occurrence = seen_in_sidecar.get(key, 0) + 1
            seen_in_sidecar[key] = occurrence
            if self.db.transaction_exists(
                desc, t["date"], amount, issuer,
                max_duplicates=occurrence,
                connection=connection,
            ):
                self._dup_rows_skipped = (
                    getattr(self, "_dup_rows_skipped", 0) + 1
                )
                if getattr(self, "_verbose", False):
                    self._report(
                        f"    ~ skip duplicate row (already in DB): "
                        f"{t['date']}  {amount}  {desc[:48]}"
                    )
                continue

            tx = Transaction(
                date=t["date"],
                description=desc,
                amount=amount,
                category=t.get("category") or "Uncategorized",
                source=issuer,
                status="UNVERIFIED",
                original_file=source_file_path,
                profile=None,
                needs_review=True,
                metadata=(
                    json.dumps(t["metadata"])
                    if t.get("metadata") else None
                ),
            )
            self.db.add_transaction(
                tx,
                source_file_path=source_file_path,
                source_file_sha256=source_file_sha256,
                source_page=t.get("page"),
                sidecar_path=sidecar_rel,
                connection=connection,
            )
            rows_written.append(tx)

        self.db.mark_file_processed(
            json_path, json_hash, connection=connection,
        )
        return rows_written

    def ingest_directory(
        self,
        directory: str,
        on_error: str = "report",
        verbose: bool = False,
    ) -> dict:
        """Walk a directory tree, ingest every sidecar.

        on_error:
            "report" — append the error to the result, continue
            "raise"  — re-raise the first failure (used in tests)
        verbose:
            when True, print every transaction the dedup check
            suppresses (a non-zero duplicate count on a fresh ingest
            is a red flag worth seeing — see prompts/reingest.md).

        Returns a dict with ingested/skipped/errors counts, a
        duplicate_rows_skipped tally, and a list of
        (path, error_message) for failures.
        """
        self._verbose = verbose
        self._dup_rows_skipped = 0
        result = {
            "ingested": 0,
            "skipped": 0,
            "rows_written": 0,
            "duplicate_rows_skipped": 0,
            "empty": [],
            "errors": [],
        }
        for root, _dirs, files in os.walk(directory):
            for name in sorted(files):
                if not name.endswith(".json"):
                    continue
                jp = os.path.join(root, name)
                try:
                    rows = self.ingest_sidecar(jp)
                except (sidecar_mod.SidecarError, CcSchemaError) as e:
                    if on_error == "raise":
                        raise
                    result["errors"].append((jp, str(e)))
                    continue
                if rows:
                    result["ingested"] += 1
                    result["rows_written"] += len(rows)
                elif self._last_skipped:
                    result["skipped"] += 1
                else:
                    result["empty"].append(jp)
        result["duplicate_rows_skipped"] = self._dup_rows_skipped
        return result

    # ── internals ──────────────────────────────────────────────

    def _relative(self, abs_path: str) -> str:
        try:
            return os.path.relpath(
                abs_path, str(WORKSPACE_DIR),
            ).replace(os.sep, "/")
        except ValueError:
            return abs_path

    def _report(self, message: str) -> None:
        pending = getattr(self, "_pending_messages", None)
        if pending is None:
            print(message)
        else:
            pending.append(message)
