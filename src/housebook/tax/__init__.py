"""Tax module — sidecar-driven ingestion and estimation.

Public API:
    compute_tax_estimate(db_path, year) — federal + state estimate
    TaxGenericIngestor — ingests tax sidecars (envelope-wrapped)
"""

from .estimate import compute_tax_estimate
from .ingestor import TaxGenericIngestor

__all__ = ["compute_tax_estimate", "TaxGenericIngestor"]
