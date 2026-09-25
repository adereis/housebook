from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

CATEGORY_TRANSFERS_REFUNDS = "Transfers & Refunds"
CATEGORY_CC_PAYMENT = "CC Payment"


@dataclass
class Transaction:
    date: str
    description: str
    amount: Decimal
    category: str
    source: str
    status: str
    original_file: str
    profile: Optional[str] = None
    needs_review: bool = True
    id: Optional[int] = None
    trip_id: Optional[int] = None
    metadata: Optional[str] = None


@dataclass
class CategorizationRule:
    category: str
    keyword: str
    id: Optional[int] = None


@dataclass
class TaxDocument:
    tax_year: int
    document_type: str
    issuer: str
    category: str
    # None when the source document has no amount (or extraction
    # failed). Per AGENTS.md missing data is NULL, never a fabricated
    # 0 — a $0.00 reads as "the form says zero".
    amount: Optional[Decimal]
    original_file: str
    currency: str = "USD"
    status: str = "UNVERIFIED"
    needs_review: bool = True
    raw_data: Optional[str] = None
    id: Optional[int] = None


@dataclass
class HsaExpense:
    service_date: str
    provider: str
    patient: str
    description: str
    patient_responsibility: Decimal
    category: str
    source: str
    amount_billed: Optional[Decimal] = None
    insurance_paid: Optional[Decimal] = None
    payment_method: Optional[str] = None
    payment_date: Optional[str] = None
    transaction_id: Optional[int] = None
    status: str = "UNREIMBURSED"
    needs_review: bool = True
    evidence_level: str = "stub"
    notes: Optional[str] = None
    exclusion_reason: Optional[str] = None
    id: Optional[int] = None


@dataclass
class HsaDocument:
    expense_id: Optional[int]
    document_type: str
    file_path: str
    file_hash: str
    original_filename: str
    raw_data: Optional[str] = None
    # Provenance (added in migration 016):
    # source_page is the 1-indexed page where this row appeared in the
    # source file (null for whole-document sidecars; populated by CC
    # ingest in Phase 2). sidecar_path is the workspace-relative path
    # to the JSON sidecar that produced this row.
    source_page: Optional[int] = None
    sidecar_path: Optional[str] = None
    id: Optional[int] = None
