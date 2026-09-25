"""Credit-card statements module.

Public API:
    CcIngestor — ingests CC sidecars (envelope-wrapped) into transactions
    validate_data_block — structural checks on CC sidecars
"""

from .ingestor import CcIngestor
from .schema import validate_data_block

__all__ = ["CcIngestor", "validate_data_block"]
