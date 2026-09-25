import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from glob import glob
from typing import Iterator, List

from .models import CategorizationRule, Transaction


def _decimal_to_str(d):
    return str(d)


sqlite3.register_adapter(Decimal, _decimal_to_str)


class SchemaTooNewError(Exception):
    """DB schema is newer than this version of the tool."""


def _get_file_hash(path: str) -> str:
    """Calculate SHA1 hash of a file."""
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            data = f.read(65536)
            if not data:
                break
            sha1.update(data)
    return sha1.hexdigest()


def backup_database(db_path: str, backup_dir: str = None,
                    max_backups: int = 10) -> str:
    """Create a timestamped backup of the database if it changed.

    Returns the backup file path, or empty string if no backup was
    needed or source does not exist.
    """
    if not os.path.exists(db_path):
        return ""

    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)

    base = os.path.basename(db_path)
    pattern = os.path.join(backup_dir, f"{base}.*")
    existing_backups = sorted(glob(pattern))
    latest_backup = existing_backups[-1] if existing_backups else None

    # Use a temporary file for the new backup to compare hashes
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Use SQLite online backup API for a consistent snapshot
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        dst.close()
        src.close()

        # Check if the content is different from the latest backup
        new_hash = _get_file_hash(tmp_path)
        if latest_backup and _get_file_hash(latest_backup) == new_hash:
            os.remove(tmp_path)
            return ""

        # Content changed, move to final destination
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"{base}.{timestamp}")
        shutil.move(tmp_path, backup_path)

        _rotate_backups(backup_dir, base, max_backups)
        return backup_path
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

def _rotate_backups(backup_dir: str, base: str, keep: int = 10,
                    now: datetime = None):
    """Apply tiered retention to backup files.

    Tiers (applied most-recent-first):
      - Last hour:  keep all
      - Last 7 days: keep 1 per day
      - Older:       keep 1 per week

    After tiering, the total is capped at ``keep`` as a hard limit.
    """
    pattern = os.path.join(backup_dir, f"{base}.*")
    existing = sorted(glob(pattern))
    if len(existing) <= 1:
        return

    now = now or datetime.now()

    def _parse_ts(path):
        """Extract datetime from backup filename."""
        suffix = os.path.basename(path).replace(f"{base}.", "")
        try:
            return datetime.strptime(suffix, "%Y%m%d_%H%M%S")
        except ValueError:
            return None

    # Tag each backup with its parsed timestamp
    tagged = []
    for path in existing:
        ts = _parse_ts(path)
        if ts is None:
            continue
        tagged.append((path, ts))

    if not tagged:
        return

    kept = set()
    seen_days = set()   # "YYYY-MM-DD" strings already represented
    seen_weeks = set()  # "YYYY-WW" strings already represented

    # Walk newest-first so the most recent backup per bucket wins
    for path, ts in reversed(tagged):
        age_seconds = (now - ts).total_seconds()

        if age_seconds < 3600:
            # Last hour: keep all
            kept.add(path)
        elif age_seconds < 7 * 86400:
            # Last 7 days: 1 per calendar day
            day_key = ts.strftime("%Y-%m-%d")
            if day_key not in seen_days:
                seen_days.add(day_key)
                kept.add(path)
        else:
            # Older: 1 per ISO week
            week_key = ts.strftime("%G-W%V")
            if week_key not in seen_weeks:
                seen_weeks.add(week_key)
                kept.add(path)

    # Always keep the very latest backup regardless of tier
    kept.add(tagged[-1][0])

    # Hard cap: if tiering kept more than ``keep``, trim oldest
    if len(kept) > keep:
        by_age = sorted(kept, key=lambda p: dict(tagged)[p])
        for old in by_age[:len(kept) - keep]:
            kept.discard(old)

    # Delete everything not kept
    for path, _ in tagged:
        if path not in kept:
            os.remove(path)


def checkpoint_wal(db_path: str):
    """Force WAL checkpoint so the .db file is self-contained."""
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


class Database:
    def __init__(self, db_path: str, dry_run: bool = False,
                 workspace_dir: str = None):
        self.db_path = db_path
        self.dry_run = dry_run
        self.workspace_dir = workspace_dir

    def _to_relative_path(self, path: str) -> str:
        """Convert an absolute path to a workspace-relative path."""
        if not self.workspace_dir or not path:
            return path
        if not os.path.isabs(path):
            return path.replace(os.sep, "/")
        try:
            return os.path.relpath(
                path, self.workspace_dir,
            ).replace(os.sep, "/")
        except ValueError:
            # Different drives on Windows
            return path

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Provide one serialized transaction for a logical write unit.

        ``BEGIN IMMEDIATE`` acquires SQLite's writer reservation before
        an ingestor checks ``processed_files``. Two processes racing to
        ingest the same file therefore cannot both pass the idempotency
        check and write duplicate rows. Every exception, including
        ``KeyboardInterrupt``, rolls the whole unit back.
        """
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def verify_schema_version(self):
        """Refuse to operate if the DB schema is newer than
        what this version of the code expects.

        Raises SchemaTooNewError if the database was migrated
        by a newer version of the tool.
        """
        from housebook.migrations.runner import (
            EXPECTED_SCHEMA_VERSION,
            get_schema_version,
        )

        actual = get_schema_version(self.db_path)
        if actual > EXPECTED_SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"Database schema version ({actual}) is newer "
                f"than this tool expects ({EXPECTED_SCHEMA_VERSION}). "
                f"Update the tool before operating on this database."
            )

    def get_rules(self) -> List[CategorizationRule]:
        conn = self._get_connection()
        c = conn.cursor()
        c.execute("SELECT category, keyword FROM categorization_rules")
        rules = [
            CategorizationRule(category=row[0], keyword=row[1]) for row in c.fetchall()
        ]
        conn.close()
        return rules

    def add_transaction(
        self,
        tx: Transaction,
        *,
        source_file_path: str | None = None,
        source_file_sha256: str | None = None,
        source_page: int | None = None,
        sidecar_path: str | None = None,
        connection: sqlite3.Connection | None = None,
    ):
        """Insert a transaction, optionally with full provenance.

        Supplying ``connection`` joins an outer transaction; otherwise
        this method retains its historical one-call commit behavior.
        """
        if self.dry_run:
            return
        owns_connection = connection is None
        conn = connection or self._get_connection()
        try:
            values = (
                tx.date,
                tx.description,
                str(tx.amount),
                tx.category,
                tx.source,
                tx.status,
                self._to_relative_path(tx.original_file),
                tx.trip_id,
                1 if tx.needs_review else 0,
                tx.profile,
                tx.metadata,
            )
            provenance = (
                source_file_path,
                source_file_sha256,
                source_page,
                sidecar_path,
            )
            if any(value is not None for value in provenance):
                conn.execute(
                    "INSERT INTO transactions "
                    "(date, description, amount, category, "
                    "source, status, original_file, "
                    "trip_id, needs_review, profile, metadata, "
                    "source_file_path, source_file_sha256, "
                    "source_page, sidecar_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values + (
                        self._to_relative_path(source_file_path),
                        source_file_sha256,
                        source_page,
                        self._to_relative_path(sidecar_path),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO transactions "
                    "(date, description, amount, category, "
                    "source, status, original_file, "
                    "trip_id, needs_review, profile, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def transaction_exists(
        self, description: str, date: str,
        amount: Decimal, source: str = "",
        max_duplicates: int = 1,
        profile: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Check if a matching transaction already exists.

        Uses (description, date, amount, source) as the fingerprint —
        plus profile when supplied, so two people buying the same item
        on the same day for the same price (e.g. different Amazon
        profiles) are not collapsed into a single transaction.
        max_duplicates controls how many identical transactions are
        tolerated per day (default 1 means reject the second
        occurrence).
        """
        owns_connection = connection is None
        conn = connection or self._get_connection()
        try:
            c = conn.cursor()
            profile_clause = (
                " AND profile = ?" if profile is not None else ""
            )
            profile_params = (profile,) if profile is not None else ()

            # Count existing matches with source
            c.execute(
                "SELECT COUNT(*) FROM transactions "
                "WHERE description = ? AND date = ? "
                "AND source = ? AND ("
                "  amount = ? OR ABS(amount - ?) < 0.005"
                ")" + profile_clause,
                (description, date, source,
                 str(amount), float(amount)) + profile_params,
            )
            count = c.fetchone()[0]

            if count == 0 and source:
                # Fallback: match without source for legacy data
                c.execute(
                    "SELECT COUNT(*) FROM transactions "
                    "WHERE description = ? AND date = ? "
                    "AND source = '' AND ("
                    "  amount = ? OR ABS(amount - ?) < 0.005"
                    ")" + profile_clause,
                    (description, date,
                     str(amount), float(amount)) + profile_params,
                )
                count = c.fetchone()[0]

            return count >= max_duplicates
        finally:
            if owns_connection:
                conn.close()

    def is_file_processed(
        self,
        file_path: str,
        file_hash: str = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        owns_connection = connection is None
        conn = connection or self._get_connection()
        try:
            c = conn.cursor()
            file_path = self._to_relative_path(file_path)
            c.execute(
                "SELECT file_hash FROM processed_files "
                "WHERE file_path = ?",
                (file_path,),
            )
            row = c.fetchone()
            if not row:
                return False

            stored_hash = row[0]
            # Migration case: add a missing hash while preserving the
            # processed decision that prevented duplicate ingestion.
            if not stored_hash and file_hash:
                c.execute(
                    "UPDATE processed_files SET file_hash = ? "
                    "WHERE file_path = ?",
                    (file_hash, file_path),
                )
                if owns_connection:
                    conn.commit()
                return True

            if file_hash:
                return stored_hash == file_hash
            return True
        finally:
            if owns_connection:
                conn.close()

    def mark_file_processed(
        self, file_path: str, file_hash: str = None,
        statement_start: str = None, statement_end: str = None,
        *,
        connection: sqlite3.Connection | None = None,
    ):
        if self.dry_run:
            return
        owns_connection = connection is None
        conn = connection or self._get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO processed_files "
                "(file_path, file_hash, statement_start, statement_end) "
                "VALUES (?, ?, ?, ?)",
                (self._to_relative_path(file_path), file_hash,
                 statement_start, statement_end),
            )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def log_ingestion_error(self, file_path: str, line_number: int,
                            raw_text: str, error: str, *,
                            connection: sqlite3.Connection | None = None):
        if self.dry_run:
            return
        owns_connection = connection is None
        conn = connection or self._get_connection()
        try:
            conn.execute(
                "INSERT INTO ingestion_errors "
                "(file_path, line_number, raw_text, error) "
                "VALUES (?, ?, ?, ?)",
                (self._to_relative_path(file_path), line_number,
                 raw_text[:500], error[:500]),
            )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def get_pending_review(self) -> List[Transaction]:
        conn = self._get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT date, description, amount, category, "
            "source, status, original_file, "
            "trip_id, needs_review, id, profile, metadata "
            "FROM transactions "
            "WHERE needs_review = 1 "
            "ORDER BY date DESC"
        )

        txs = []
        for row in c.fetchall():
            txs.append(
                Transaction(
                    date=row[0],
                    description=row[1],
                    amount=Decimal(str(row[2])),
                    category=row[3],
                    source=row[4],
                    status=row[5],
                    original_file=row[6],
                    trip_id=row[7],
                    needs_review=bool(row[8]),
                    id=row[9],
                    profile=row[10],
                    metadata=row[11],
                )
            )
        conn.close()
        return txs

    def backfill_metadata(
        self, transactions: List[Transaction],
    ) -> int:
        """Update metadata on existing transactions.

        Matches by (date, description, amount, source) fingerprint
        and sets the metadata column. Returns the number of rows
        updated.
        """
        if self.dry_run:
            return 0
        conn = self._get_connection()
        c = conn.cursor()
        updated = 0
        for tx in transactions:
            if not tx.metadata:
                continue
            c.execute(
                "UPDATE transactions SET metadata = ? "
                "WHERE date = ? AND description = ? "
                "AND source = ? "
                "AND ABS(amount - ?) < 0.005 "
                "AND metadata IS NULL",
                (
                    tx.metadata,
                    tx.date,
                    tx.description,
                    tx.source,
                    float(tx.amount),
                ),
            )
            updated += c.rowcount
        conn.commit()
        conn.close()
        return updated

    def bulk_update_transactions(self, updates: List[dict]):
        """Batch-update transaction categories and review status.

        updates: list of dicts with keys id, category,
        trip_id, needs_review.
        """
        if self.dry_run:
            return
        conn = self._get_connection()
        c = conn.cursor()
        for up in updates:
            c.execute(
                "UPDATE transactions "
                "SET category = ?, trip_id = ?, "
                "needs_review = ?, status = 'AGENT_VERIFIED' "
                "WHERE id = ?",
                (
                    up["category"],
                    up.get("trip_id"),
                    up.get("needs_review", 0),
                    up["id"],
                ),
            )
        conn.commit()
        conn.close()
