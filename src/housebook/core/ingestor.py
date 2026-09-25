import hashlib
from typing import List

from .database import Database
from .intelligence import Intelligence
from .models import Transaction


class Ingestor:
    def __init__(self, db: Database, intel: Intelligence):
        self.db = db
        self.intel = intel

    def ingest(self, source_path: str) -> List[Transaction]:
        raise NotImplementedError("Ingestor must implement ingest method")

    def calculate_hash(self, file_path: str) -> str:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                byte_block = f.read(4096)
                if not byte_block:
                    break
                if isinstance(byte_block, str):
                    byte_block = byte_block.encode("utf-8")
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
