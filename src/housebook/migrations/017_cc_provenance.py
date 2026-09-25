"""Migration 017 — Phase 2: CC provenance columns + path rewrite.

Adds the four provenance columns to `transactions` so every CC row
can answer "which page of which file, classified by which sidecar":

    source_file_path     TEXT    — workspace-relative path to source PDF
    source_file_sha256   TEXT    — sha256 of that PDF
    source_page          INTEGER — 1-indexed page where row appeared (NULL ok)
    sidecar_path         TEXT    — workspace-relative path to JSON sidecar

For existing rows whose `original_file` points at the old
`input/Credit-Card-Statements/...` location: rewrite `original_file`
to the new `cc/<YYYY>/<canonical>.pdf` path AND populate the new
provenance columns.

The old→new mapping isn't recorded anywhere structured. We
reconstruct it by matching each old PDF's transaction set against
each new sidecar's transaction set. Since the cheaper-agent
classify pulled transactions verbatim out of the DB, the tuples
match exactly. We use (date, description, amount) tuples sorted as
a fingerprint per file.

If a fingerprint doesn't uniquely identify a sidecar, we leave the
row alone and let `housebook-cc check` report it later.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path


def _workspace_root(conn: sqlite3.Connection) -> Path:
    """Resolve the workspace root from the env var or fall back to
    the directory holding the SQLite DB.

    Migration 016 demonstrates this pattern — the DB is always at
    <workspace>/data/finance.db.
    """
    env = os.environ.get("HOUSEBOOK_WORKSPACE_DIR")
    if env:
        return Path(env).resolve()
    cur = conn.execute("PRAGMA database_list")
    for _, name, file in cur.fetchall():
        if name == "main" and file:
            return Path(file).resolve().parent.parent
    return Path.cwd()


def _columns_present(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _add_provenance_columns(conn: sqlite3.Connection) -> None:
    cols = _columns_present(conn, "transactions")
    if "source_file_path" not in cols:
        conn.execute(
            "ALTER TABLE transactions "
            "ADD COLUMN source_file_path TEXT"
        )
    if "source_file_sha256" not in cols:
        conn.execute(
            "ALTER TABLE transactions "
            "ADD COLUMN source_file_sha256 TEXT"
        )
    if "source_page" not in cols:
        conn.execute(
            "ALTER TABLE transactions "
            "ADD COLUMN source_page INTEGER"
        )
    if "sidecar_path" not in cols:
        conn.execute(
            "ALTER TABLE transactions "
            "ADD COLUMN sidecar_path TEXT"
        )
    conn.commit()


def _fingerprint_for_old_pdf(
    conn: sqlite3.Connection, original_file: str,
) -> tuple:
    """Build a stable fingerprint of the rows attributed to an old
    PDF path: a sorted tuple of (date, description, amount) with
    amounts rounded to cents."""
    rows = conn.execute(
        "SELECT date, description, ROUND(amount, 2) "
        "FROM transactions WHERE original_file = ? "
        "ORDER BY date, description, amount",
        (original_file,),
    ).fetchall()
    return tuple((d, desc, float(amt)) for (d, desc, amt) in rows)


def _fingerprint_for_sidecar(sidecar_path: Path) -> tuple:
    with sidecar_path.open() as f:
        sc = json.load(f)
    txs = sc.get("data", {}).get("transactions", [])
    items = sorted(
        ((t.get("date"), t.get("description", ""),
          round(float(t.get("amount", 0)), 2))
         for t in txs),
        key=lambda x: (x[0] or "", x[1] or "", x[2]),
    )
    return tuple(items)


def _build_old_to_new_map(
    conn: sqlite3.Connection, workspace: Path,
) -> dict[str, dict]:
    """Return {old_original_file: {new_path, sha256, sidecar_path}}.

    Sidecars whose fingerprint doesn't match exactly one old PDF
    are skipped — `housebook-cc check` surfaces those for the user.
    """
    old_paths = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT original_file FROM transactions "
            "WHERE original_file LIKE 'input/Credit-Card-Statements/%'"
        )
    ]
    old_fingerprints = {p: _fingerprint_for_old_pdf(conn, p) for p in old_paths}

    fp_to_old = defaultdict(list)
    for p, fp in old_fingerprints.items():
        fp_to_old[fp].append(p)

    cc_root = workspace / "cc"
    if not cc_root.is_dir():
        return {}

    mapping: dict[str, dict] = {}
    for sidecar in sorted(cc_root.rglob("*.json")):
        with sidecar.open() as f:
            sc = json.load(f)
        if sc.get("source") != "cc":
            continue
        sf = sc.get("source_file", {})
        new_path = sf.get("path")
        sha256 = sf.get("sha256")
        if not new_path or not sha256:
            continue

        sidecar_rel = str(sidecar.relative_to(workspace)).replace(os.sep, "/")

        fp = _fingerprint_for_sidecar(sidecar)
        candidates = fp_to_old.get(fp, [])
        if len(candidates) != 1:
            # Either no old row matches, or the fingerprint is
            # ambiguous. Either way we can't safely rewrite — leave
            # the old row alone and let `housebook-cc check` flag it.
            continue
        old_path = candidates[0]
        if old_path in mapping:
            # Two sidecars claim the same old PDF — also ambiguous.
            mapping.pop(old_path, None)
            continue
        mapping[old_path] = {
            "new_path": new_path,
            "sha256": sha256,
            "sidecar_path": sidecar_rel,
        }
    return mapping


def _rewrite_transactions(
    conn: sqlite3.Connection, mapping: dict[str, dict],
) -> int:
    """Apply the mapping. Returns rows updated."""
    total = 0
    for old, info in mapping.items():
        cur = conn.execute(
            "UPDATE transactions SET "
            "  original_file      = ?, "
            "  source_file_path   = ?, "
            "  source_file_sha256 = ?, "
            "  sidecar_path       = ? "
            "WHERE original_file = ?",
            (
                info["new_path"],
                info["new_path"],
                info["sha256"],
                info["sidecar_path"],
                old,
            ),
        )
        total += cur.rowcount
    conn.commit()
    return total


def migrate(conn: sqlite3.Connection) -> None:
    _add_provenance_columns(conn)
    workspace = _workspace_root(conn)
    mapping = _build_old_to_new_map(conn, workspace)
    if not mapping:
        # Either the workspace has no cc/ tree yet, or no match
        # could be made. The columns are added; the path rewrite
        # is a no-op until the user runs the bulk classify.
        return
    rewritten = _rewrite_transactions(conn, mapping)
    print(
        f"  migration 017: rewrote {rewritten} row(s) across "
        f"{len(mapping)} statement(s) to new cc/ paths"
    )
