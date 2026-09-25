"""Tax document ingestor.

Reads JSON sidecars produced by `prompts/tax/import.md`. Each
sidecar uses the v1 envelope wrapping a tax-specific data block.
Validates via `tax.schema.validate_data_block`.
"""

import json
import os
from decimal import Decimal
from typing import List, Optional

from housebook.config.settings import WORKSPACE_DIR
from housebook.core import sidecar as sidecar_mod
from housebook.core.ingestor import Ingestor
from housebook.core.models import TaxDocument

from .schema import TaxSchemaError, validate_data_block


class TaxGenericIngestor(Ingestor):

    def ingest_sidecar(
        self, json_path: str,
    ) -> List[TaxDocument]:
        """Ingest one sidecar as an all-or-nothing file transaction."""
        messages: list[str] = []
        self._pending_messages = messages
        try:
            with self.db.transaction() as connection:
                result = self._ingest_sidecar(json_path, connection)
        finally:
            del self._pending_messages
        for message in messages:
            print(message)
        return result

    def _ingest_sidecar(
        self, json_path: str, connection,
    ) -> List[TaxDocument]:
        """Ingest one envelope-wrapped tax sidecar."""
        json_hash = self.calculate_hash(json_path)
        if self.db.is_file_processed(
            json_path, json_hash, connection=connection,
        ):
            return []

        sc = sidecar_mod.load(json_path)
        if sc.source != "tax":
            raise sidecar_mod.SidecarError(
                f"sidecar source is {sc.source!r}, expected 'tax'"
            )
        errors = validate_data_block(sc.data)
        if errors:
            raise TaxSchemaError(
                f"{json_path}: {len(errors)} error(s):\n  - "
                + "\n  - ".join(errors)
            )

        sidecar_rel = self._ws_relative(json_path)
        source_path = sc.source_file.path
        source_sha = sc.source_file.sha256
        tax_year = sc.data["tax_year"]

        docs_data = sc.data.get("documents") or [sc.data]
        result: List[TaxDocument] = []
        for entry in docs_data:
            # The schema explicitly permits a null amount; preserve it
            # as NULL. Writing 0 turned "we don't know" into "the form
            # says zero", which then flowed into the tax estimate as a
            # real figure. Note `or 0` also swallowed a genuine 0.
            raw_amount = entry.get("amount")
            doc = TaxDocument(
                tax_year=tax_year,
                document_type=entry.get("document_type", "UNKNOWN"),
                issuer=entry.get("issuer", "UNKNOWN"),
                category=entry.get("category", "Other"),
                amount=(None if raw_amount is None
                        else Decimal(str(raw_amount))),
                currency=entry.get("currency", "USD"),
                original_file=source_path,
                status="UNVERIFIED",
                needs_review=True,
                raw_data=json.dumps(entry.get("form_data") or {}),
            )
            written = self._save_to_db_with_provenance(
                doc,
                source_file_path=source_path,
                source_file_sha256=source_sha,
                sidecar_path=sidecar_rel,
                connection=connection,
            )
            if written:
                result.append(doc)
                shown = "(no amount)" if doc.amount is None \
                    else f"${doc.amount}"
                self._report(
                    f"  + [TAX {doc.document_type}] {tax_year} "
                    f"{doc.issuer} {shown}"
                )
            else:
                # The (year, type, issuer, amount) dedup dropped this
                # row. Two genuinely distinct documents can collide on
                # those four fields (e.g. two $0 1099-HCs from one
                # issuer), so say so rather than reporting it written.
                shown = "(no amount)" if doc.amount is None \
                    else f"${doc.amount}"
                self._report(
                    f"  ~ [TAX {doc.document_type}] {tax_year} "
                    f"{doc.issuer} {shown} — already present, skipped"
                )

        self.db.mark_file_processed(
            json_path, json_hash, connection=connection,
        )
        return result

    def ingest_sidecar_directory(
        self, directory: str, on_error: str = "report",
    ) -> dict:
        """Walk a directory tree and ingest every tax sidecar."""
        result = {
            "ingested": 0, "skipped": 0,
            "rows_written": 0, "errors": [],
        }
        for root, _dirs, files in os.walk(directory):
            for name in sorted(files):
                if not name.endswith(".json"):
                    continue
                jp = os.path.join(root, name)
                try:
                    rows = self.ingest_sidecar(jp)
                except (sidecar_mod.SidecarError, TaxSchemaError) as e:
                    if on_error == "raise":
                        raise
                    result["errors"].append((jp, str(e)))
                    continue
                if rows:
                    result["ingested"] += 1
                    result["rows_written"] += len(rows)
                else:
                    result["skipped"] += 1
        return result

    def _ws_relative(self, path: str) -> str:
        try:
            return os.path.relpath(
                path, str(WORKSPACE_DIR),
            ).replace(os.sep, "/")
        except ValueError:
            return path

    def _report(self, message: str) -> None:
        pending = getattr(self, "_pending_messages", None)
        if pending is None:
            print(message)
        else:
            pending.append(message)

    def _save_to_db_with_provenance(
        self, doc: TaxDocument,
        source_file_path: str,
        source_file_sha256: str,
        sidecar_path: str,
        source_page: Optional[int] = None,
        connection=None,
    ) -> bool:
        """Insert the row. Returns False if dedup skipped it."""
        if self.db.dry_run:
            return True
        owns_connection = connection is None
        conn = connection or self.db._get_connection()
        try:
            c = conn.cursor()
            if doc.amount is None:
                # `ABS(amount - NULL)` is NULL, so an unknown-amount
                # row would never dedup against its own re-ingest.
                c.execute(
                    "SELECT COUNT(*) FROM tax_documents "
                    "WHERE tax_year = ? AND document_type = ? "
                    "AND issuer = ? AND amount IS NULL",
                    (doc.tax_year, doc.document_type, doc.issuer),
                )
            else:
                c.execute(
                    "SELECT COUNT(*) FROM tax_documents "
                    "WHERE tax_year = ? AND document_type = ? "
                    "AND issuer = ? AND ABS(amount - ?) < 0.005",
                    (doc.tax_year, doc.document_type,
                     doc.issuer, float(doc.amount)),
                )
            if c.fetchone()[0] > 0:
                return False
            c.execute(
                "INSERT INTO tax_documents "
                "(tax_year, document_type, issuer, category, "
                "amount, currency, original_file, "
                "status, needs_review, raw_data, "
                "source_file_path, source_file_sha256, "
                "source_page, sidecar_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.tax_year, doc.document_type,
                    doc.issuer, doc.category,
                    None if doc.amount is None else float(doc.amount),
                    doc.currency,
                    doc.original_file,
                    doc.status,
                    1 if doc.needs_review else 0,
                    doc.raw_data,
                    source_file_path, source_file_sha256,
                    source_page, sidecar_path,
                ),
            )
            if owns_connection:
                conn.commit()
            return True
        finally:
            if owns_connection:
                conn.close()
