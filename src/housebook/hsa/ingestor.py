"""HSA medical document ingestor.

Reads JSON sidecar files produced by an AI agent that follows
prompts/hsa/import.md. Each sidecar uses the unified envelope
(see housebook.core.sidecar) wrapping an HSA-specific
`data` block.

PDFs without sidecars — and sidecars whose envelope fails validation
— are flagged as ingestion errors. The import step
(prompts/hsa/import.md) must be run first.

Sidecar convention:
  Filename: YYYY-MM-DD__Entity__DocType__Patient__Amount__Tags.pdf
  Sidecar:  same basename with .json extension
  Doc types: REC (receipt), EOB, INV (invoice), STMT, TAX, HIST, PLAN
"""

import json
import os
from decimal import Decimal
from typing import List

from housebook.config.settings import (
    WORKSPACE_DIR,
)
from housebook.core import sidecar as sidecar_mod
from housebook.core.ingestor import Ingestor
from housebook.core.models import HsaDocument, HsaExpense
from housebook.hsa.providers import ProviderResolver

_DOC_TYPE_MAP = {
    "REC": "receipt",
    "EOB": "eob",
    "INV": "invoice",
    "STMT": "statement",
    "TAX": "tax",
    "HIST": "history",
    "PLAN": "plan",
}

_EXPENSE_DOC_TYPES = {"REC", "EOB", "INV"}

_SKIP_TAGS = {"DECLINED", "VOID"}

_CATEGORY_KEYWORDS = {
    "dental": ["dental", "dentist", "orthodont", "endodont",
               "periodon", "oral surgery"],
    "vision": ["optometrist", "ophthalmol", "eye care", "vision",
               "optician", "glasses", "contacts", "lasik"],
    "pharmacy": ["pharmacy", "rx", "prescription"],
    "mental_health": ["psychiatr", "psycholog", "therapist",
                      "counseling", "mental health", "behavioral"],
    "lab": ["laboratory", "lab corp", "quest diagnostic",
            "pathology", "blood work"],
    "therapy": ["physical therapy", "occupational therapy",
                "speech therapy", "chiropractic"],
}


class HsaIngestor(Ingestor):
    def __init__(self, db, intel):
        super().__init__(db, intel)
        self.resolver = ProviderResolver()
        # Set by ingest_sidecar() each call: _last_skipped is True when
        # the file was already processed (file-level idempotency);
        # _created_count is the number of hsa_expenses rows the call
        # created (it always returns []). ingest_directory() reads both
        # to count new-vs-unchanged.
        self._last_skipped = False
        self._created_count = 0

    def resolve_provider(self, raw_name: str) -> str:
        return self.resolver.resolve(raw_name)

    # ── Sidecar ingestion ──────────────────────────────────────

    def ingest_sidecar(
        self, json_path: str
    ) -> List[HsaExpense]:
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
    ) -> List[HsaExpense]:
        """Ingest a JSON sidecar (new envelope schema_version=1)
        plus its companion source file.

        The sidecar's `data` block carries the HSA-specific metadata
        (date, entity, doc_type, patient, amount, ...). Provenance —
        source path, sha256, and the sidecar path itself — is
        recorded on every hsa_documents row that this sidecar
        produces.
        """
        self._last_skipped = False
        self._created_count = 0
        json_hash = self.calculate_hash(json_path)
        if self.db.is_file_processed(
            json_path, json_hash, connection=connection,
        ):
            self._last_skipped = True
            return []

        # Validate envelope. SidecarError propagates so callers can
        # log it and continue processing other sidecars.
        sc = sidecar_mod.load(json_path)
        if sc.source != "hsa":
            raise sidecar_mod.SidecarError(
                f"sidecar source is {sc.source!r}, expected 'hsa'"
            )

        meta = sc.data            # source-specific block
        source_file_rel = sc.source_file.path
        source_file_sha256 = sc.source_file.sha256

        # Companion source file: prefer the path declared in the
        # envelope (workspace-relative). Fall back to <json>.pdf for
        # the moment to handle in-flight migrations.
        if source_file_rel and os.path.isabs(source_file_rel):
            source_file_abs = source_file_rel
        elif source_file_rel:
            source_file_abs = os.path.join(
                str(WORKSPACE_DIR), source_file_rel,
            )
        else:
            source_file_abs = os.path.splitext(json_path)[0] + ".pdf"

        # Workspace-relative sidecar path for provenance.
        try:
            sidecar_rel = os.path.relpath(
                json_path, str(WORKSPACE_DIR),
            ).replace(os.sep, "/")
        except ValueError:
            sidecar_rel = json_path

        # We use the envelope-declared sha256 as the canonical hash;
        # this is the same value the import SOP wrote.
        pdf_path = source_file_abs
        pdf_hash = source_file_sha256

        tags = meta.get("tags", [])
        tags_upper = {t.upper() for t in tags}

        if tags_upper & _SKIP_TAGS:
            self.db.mark_file_processed(
                json_path, json_hash, connection=connection,
            )
            return []

        doc_type_raw = meta.get("doc_type", "REC")
        doc_type = _DOC_TYPE_MAP.get(doc_type_raw, "receipt")
        entity = meta.get("entity", "UNKNOWN")

        # 1. Multi-item support (one doc, many expenses)
        if "items" in meta and isinstance(meta["items"], list):
            for item in meta["items"]:
                # Item-level overrides
                i_date = item.get("date") or meta.get("date")
                i_patient = (
                    item.get("patient")
                    or meta.get("patient")
                    or "Unknown"
                ).lower()
                i_amount = item.get("amount") or 0
                i_desc_suffix = item.get("description") or ""

                i_provider = self.resolve_provider(item.get("entity") or entity)
                i_desc = f"{i_provider}"
                if i_desc_suffix:
                    i_desc += f" ({i_desc_suffix})"

                # Check for existing
                i_claim_id = item.get("claim_id") or meta.get("claim_id")
                existing_id = self._find_matching_expense(
                    i_provider, i_patient, float(i_amount), i_date,
                    doc_type, i_claim_id, connection=connection,
                )

                # Create doc metadata for this specific item
                item_doc = HsaDocument(
                    expense_id=None,
                    document_type=doc_type,
                    file_path=pdf_path if os.path.exists(pdf_path) else json_path,
                    file_hash=pdf_hash or json_hash,
                    original_filename=os.path.basename(
                        pdf_path if os.path.exists(pdf_path)
                        else json_path
                    ),
                    raw_data=json.dumps(item),
                    sidecar_path=sidecar_rel,
                )

                if existing_id is not None:
                    self._save_document_only(
                        item_doc, expense_id=existing_id,
                        connection=connection,
                    )
                    self._report(
                        f"  ~ [HSA {doc_type}] {i_date}"
                        f" {i_provider} ${i_amount}"
                        f" → linked to expense #{existing_id}"
                    )
                else:
                    # Build virtual meta for this item to reuse builder
                    item_meta = meta.copy()
                    item_meta.update(item)
                    expense = self._build_expense_from_sidecar(
                        item_meta, i_provider, i_patient,
                        i_date, doc_type, meta.get("tags", [])
                    )
                    self._save_to_db(
                        expense, item_doc, connection=connection,
                    )
                    self._report(
                        f"  + [HSA {doc_type}] {i_date}"
                        f" {i_provider}"
                        f" ${expense.patient_responsibility}"
                    )

            self.db.mark_file_processed(
                json_path, json_hash, connection=connection,
            )
            return []

        # 2. Standard single-item or account-level doc
        if doc_type_raw == "EOB" and tags:
            provider = self.resolve_provider(tags[0])
        else:
            provider = self.resolve_provider(entity)

        patient = (meta.get("patient", "Unknown") or "Unknown")
        patient = patient.lower()
        service_date = meta.get("date", "")

        raw_data_json = json.dumps(meta)

        doc = HsaDocument(
            expense_id=None,
            document_type=doc_type,
            file_path=pdf_path if os.path.exists(pdf_path)
            else json_path,
            file_hash=pdf_hash or json_hash,
            original_filename=os.path.basename(
                pdf_path if os.path.exists(pdf_path)
                else json_path
            ),
            raw_data=raw_data_json,
            sidecar_path=sidecar_rel,
        )

        if doc_type_raw in _EXPENSE_DOC_TYPES:
            financials = meta.get("financials", {})
            effective_amount = (
                financials.get("patient_responsibility")
                or meta.get("amount", 0)
            )
            if float(effective_amount) == 0:
                self._save_document_only(doc, connection=connection)
                self.db.mark_file_processed(
                    json_path, json_hash, connection=connection,
                )
                return []

            existing_id = self._find_matching_expense(
                provider, patient,
                float(effective_amount), service_date,
                doc_type, meta.get("claim_id"),
                connection=connection,
            )
            if existing_id is not None:
                self._save_document_only(
                    doc, expense_id=existing_id,
                    connection=connection,
                )
                self.db.mark_file_processed(
                    json_path, json_hash, connection=connection,
                )
                self._report(
                    f"  ~ [HSA {doc_type}] {service_date} "
                    f"{provider} ${effective_amount}"
                    f" → linked to expense #{existing_id}"
                )
                return []

            expense = self._build_expense_from_sidecar(
                meta, provider, patient, service_date,
                doc_type, tags,
            )
            self._save_to_db(expense, doc, connection=connection)
            self._report(
                f"  + [HSA {doc_type}] {service_date} "
                f"{provider} ${expense.patient_responsibility}"
            )
        else:
            self._save_document_only(doc, connection=connection)
            self._report(
                f"  + [HSA {doc_type}] {service_date} "
                f"{provider} (account-level document)"
            )

        self.db.mark_file_processed(
            json_path, json_hash, connection=connection,
        )
        return []

    def ingest_directory(self, hsa_dir: str) -> dict:
        """Walk hsa_dir and ingest every sidecar, returning counts.

        Mirrors the result-dict contract of CcIngestor /
        TaxGenericIngestor so the CLI can print an unambiguous
        new-vs-unchanged summary instead of a single "N processed"
        line. Distinguishing the two matters: an all-idempotent
        re-run (everything already ingested) should report
        "N unchanged", not "N processed" — the latter forces the
        Agent to probe further to learn nothing changed.

        Keys: ingested (files that did new work), skipped (files
        already processed), expenses (new hsa_expenses rows created),
        errors (list of (path, message)). The Reimbursements/ subtree
        is excluded, matching the prior CLI walk.
        """
        result = {
            "ingested": 0,
            "skipped": 0,
            "expenses": 0,
            "errors": [],
        }
        for root, _dirs, files in os.walk(hsa_dir):
            rel = os.path.relpath(root, hsa_dir)
            head = rel.split(os.sep)[0] if rel != "." else ""
            if head == "Reimbursements":
                continue
            for name in sorted(files):
                if not name.endswith(".json"):
                    continue
                jp = os.path.join(root, name)
                try:
                    self.ingest_sidecar(jp)
                except Exception as e:  # noqa: BLE001 — collect & continue
                    result["errors"].append((jp, str(e)))
                    continue
                if self._last_skipped:
                    result["skipped"] += 1
                else:
                    result["ingested"] += 1
                    # ingest_sidecar always returns []; expense count
                    # is tracked on the instance (see _save_to_db).
                    result["expenses"] += self._created_count
        return result

    # ── Validation ─────────────────────────────────────────────

    def validate_directory(self, hsa_dir: str) -> List[str]:
        """Check for source files without sidecars. Returns error list.

        Skips `Reimbursements/` (HSA-account-level documents). Files
        inside `_trash/` are skipped at any depth.
        """
        errors = []
        image_exts = (".pdf", ".jpg", ".jpeg", ".png")

        for root, _dirs, files in os.walk(hsa_dir):
            rel = os.path.relpath(root, hsa_dir)
            head = rel.split(os.sep)[0] if rel != "." else ""
            if head == "Reimbursements":
                continue
            if "_trash" in rel.split(os.sep):
                continue

            for f in files:
                if not f.lower().endswith(image_exts):
                    continue
                pdf_path = os.path.join(root, f)
                json_path = os.path.splitext(pdf_path)[0] \
                    + ".json"
                if not os.path.exists(json_path):
                    errors.append(pdf_path)

        return errors

    # ── Builder ────────────────────────────────────────────────

    def _build_expense_from_sidecar(
        self, meta: dict, provider: str, patient: str,
        service_date: str, doc_type: str, tags: list,
    ) -> HsaExpense:
        """Build an HsaExpense from parsed sidecar metadata."""
        amount = Decimal(str(meta.get("amount", 0)))
        financials = meta.get("financials", {})

        amount_billed = None
        insurance_paid = None
        patient_resp = amount

        if financials:
            if "billed" in financials:
                amount_billed = Decimal(
                    str(financials["billed"])
                )
            if "plan_paid" in financials:
                insurance_paid = Decimal(
                    str(financials["plan_paid"])
                )
            if "patient_responsibility" in financials:
                patient_resp = Decimal(
                    str(financials["patient_responsibility"])
                )

        entity = meta.get("entity", "")
        if doc_type == "eob" and entity:
            insurer = entity.replace("-", " ")
            description = f"{provider} (EOB via {insurer})"
        elif tags:
            tag_str = ", ".join(
                t.replace("-", " ") for t in tags
            )
            description = f"{provider} ({tag_str})"
        else:
            description = provider

        category = self._detect_category(
            description, entity
        )

        return HsaExpense(
            service_date=service_date,
            provider=provider,
            patient=patient,
            description=description,
            patient_responsibility=patient_resp,
            amount_billed=amount_billed,
            insurance_paid=insurance_paid,
            category=category,
            source=doc_type,
            status="UNREIMBURSED",
            needs_review=True,
            evidence_level="stub",
        )

    def _detect_category(
        self, text: str, filename: str
    ) -> str:
        combined = ((text or "") + " " + filename).lower()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in combined:
                    return category
        return "medical"

    def _report(self, message: str) -> None:
        pending = getattr(self, "_pending_messages", None)
        if pending is None:
            print(message)
        else:
            pending.append(message)

    # ── Database ───────────────────────────────────────────────

    _DEDUP_WINDOW_DAYS = 45

    def _find_matching_expense(
        self, provider: str, patient: str,
        amount: float, service_date: str,
        doc_type: str, claim_id: str | None = None,
        *, connection=None,
    ) -> int | None:
        """Find existing expense for the same service.

        Matches on provider (exact, post-alias), patient,
        amount (within $0.50), and date (within 45 days).
        Only matches if the expense does not already have a
        document of the same type — two receipts for the same
        amount are separate services, not duplicates.
        """
        owns_connection = connection is None
        conn = connection or self.db._get_connection()
        try:
            if claim_id:
                row = conn.execute(
                    "SELECT e.id FROM hsa_expenses e "
                    "JOIN hsa_documents d ON d.expense_id = e.id "
                    "WHERE json_extract(d.raw_data, '$.claim_id') = ? "
                    "AND e.status != 'DELETED' "
                    "LIMIT 1",
                    (claim_id,),
                ).fetchone()
                if row:
                    return row[0]

            row = conn.execute(
                "SELECT e.id FROM hsa_expenses e "
                "WHERE e.provider = ? AND e.patient = ? "
                "AND ABS(e.patient_responsibility - ?) < 0.50 "
                "AND ABS(julianday(e.service_date) "
                "        - julianday(?)) <= ? "
                "AND e.status != 'DELETED' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM hsa_documents d "
                "  WHERE d.expense_id = e.id "
                "  AND d.document_type = ?"
                ") "
                "ORDER BY e.id ASC LIMIT 1",
                (provider, patient, amount,
                 service_date, self._DEDUP_WINDOW_DAYS,
                 doc_type),
            ).fetchone()
            return row[0] if row else None
        finally:
            if owns_connection:
                conn.close()

    def _save_document_only(
        self, doc: HsaDocument, expense_id: int = None,
        *, connection=None,
    ):
        """Save an hsa_documents row, optionally linked."""
        if self.db.dry_run:
            return
        owns_connection = connection is None
        conn = connection or self.db._get_connection()
        try:
            rel_path = self.db._to_relative_path(doc.file_path)
            conn.execute(
                "INSERT INTO hsa_documents "
                "(expense_id, document_type, file_path, "
                "file_hash, original_filename, raw_data, "
                "source_page, sidecar_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    expense_id,
                    doc.document_type,
                    rel_path,
                    doc.file_hash,
                    doc.original_filename,
                    doc.raw_data,
                    doc.source_page,
                    doc.sidecar_path,
                ),
            )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def _save_to_db(
        self, expense: HsaExpense, doc: HsaDocument,
        *, connection=None,
    ):
        # Count before the dry-run guard so a dry-run still previews
        # the number of expenses that *would* be created.
        self._created_count += 1
        if self.db.dry_run:
            return
        owns_connection = connection is None
        conn = connection or self.db._get_connection()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO hsa_expenses "
                "(service_date, provider, patient, description, "
                "amount_billed, insurance_paid, "
                "patient_responsibility, category, "
                "payment_method, payment_date, transaction_id, "
                "source, status, needs_review, evidence_level, "
                "notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                (
                    expense.service_date,
                    expense.provider,
                    expense.patient,
                    expense.description,
                    float(expense.amount_billed)
                    if expense.amount_billed else None,
                    float(expense.insurance_paid)
                    if expense.insurance_paid else 0,
                    float(expense.patient_responsibility),
                    expense.category,
                    expense.payment_method,
                    expense.payment_date,
                    expense.transaction_id,
                    expense.source,
                    expense.status,
                    1 if expense.needs_review else 0,
                    expense.evidence_level,
                    expense.notes,
                ),
            )
            expense_id = c.lastrowid

            rel_path = self.db._to_relative_path(doc.file_path)
            c.execute(
                "INSERT INTO hsa_documents "
                "(expense_id, document_type, file_path, "
                "file_hash, original_filename, raw_data, "
                "source_page, sidecar_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    expense_id,
                    doc.document_type,
                    rel_path,
                    doc.file_hash,
                    doc.original_filename,
                    doc.raw_data,
                    doc.source_page,
                    doc.sidecar_path,
                ),
            )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()
