"""Convert absolute file paths to workspace-relative paths.

Stored paths in processed_files and transactions were absolute,
which broke when the workspace moved between machines or home
directories.  This migration strips the workspace prefix so
paths are portable (e.g. "input/Credit-Card-Statements/Amex/…").
"""

import os


def migrate(conn):
    from housebook.config.settings import WORKSPACE_DIR

    workspace = str(WORKSPACE_DIR)

    # Collect all known workspace prefixes from existing data
    # (handles moves between different home directories)
    rows = conn.execute(
        "SELECT DISTINCT file_path FROM processed_files"
    ).fetchall()

    prefixes = set()
    prefixes.add(workspace)
    for (path,) in rows:
        # Already relative
        if not os.path.isabs(path):
            continue
        # Try to find the "input/" or "data/" anchor
        for anchor in ("/input/", "/config/", "/data/"):
            idx = path.find(anchor)
            if idx != -1:
                prefixes.add(path[:idx])
                break

    def to_relative(path):
        if not path or not os.path.isabs(path):
            return path
        for pfx in sorted(prefixes, key=len, reverse=True):
            pfx_slash = pfx.rstrip("/") + "/"
            if path.startswith(pfx_slash):
                return path[len(pfx_slash):]
        return path

    # --- processed_files (file_path is the PRIMARY KEY) ---
    rows = conn.execute(
        "SELECT file_path, file_hash, last_processed "
        "FROM processed_files"
    ).fetchall()

    for old_path, file_hash, last_processed in rows:
        new_path = to_relative(old_path)
        if new_path != old_path:
            conn.execute(
                "INSERT OR REPLACE INTO processed_files "
                "(file_path, file_hash, last_processed) "
                "VALUES (?, ?, ?)",
                (new_path, file_hash, last_processed),
            )
            conn.execute(
                "UPDATE transactions SET original_file = ? "
                "WHERE original_file = ?",
                (new_path, old_path),
            )
            conn.execute(
                "UPDATE tax_documents SET original_file = ? "
                "WHERE original_file = ?",
                (new_path, old_path),
            )
            conn.execute(
                "UPDATE ingestion_errors SET file_path = ? "
                "WHERE file_path = ?",
                (new_path, old_path),
            )
            conn.execute(
                "DELETE FROM processed_files "
                "WHERE file_path = ?",
                (old_path,),
            )
