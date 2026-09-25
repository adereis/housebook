"""HSA Shoebox module — medical expense ledger and receipt tracking.

Public API:
    HsaIngestor — ingests JSON sidecars (AI-pre-processed documents)
    scan_cc_transactions — scans CC transactions for medical expenses
"""

from .ingestor import HsaIngestor
from .scanner import scan_cc_transactions

__all__ = ["HsaIngestor", "scan_cc_transactions"]
