"""Workspace sync via rclone, safe across several machines.

Each machine keeps two anchors from its last sync, and neither is
synced:

- the DB anchor: SQLite's file header change counter (bytes 24-28),
  stored beside the DB and compared with the local and remote
  counters;
- the file anchor: a manifest of every other synced file (size,
  mtime, MD5), compared with fresh listings of both trees.

Together they pin each difference on the side that made it. A pull
applies only the remote's changes and a push only this machine's, so
neither can undo work done on the other side, and the bare command
does both. A file (or the DB) changed on both sides is a conflict and
stops the sync. --force mirrors one side over the other instead: the
way to settle a conflict, and to anchor a machine that has no
manifest yet.

Nothing is lost silently. A file a pull replaces or deletes here moves
to data/backups/displaced-<time>/, since rclone's local deletions are
permanent. A push asks before deleting remote content that exists
nowhere here. Backups reach the remote additively, so no machine can
delete another's.
"""

import argparse
import contextlib
import json
import os
import platform
import posixpath
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from housebook.config.settings import (
    BACKUP_DIR,
    DB_PATH,
    PROJECT_ROOT,
    RCLONE_REMOTE,
    WORKSPACE_DIR,
)
from housebook.core.database import backup_database, checkpoint_wal

PROG = "housebook-sync"
WORKSPACE_ENV = "HOUSEBOOK_WORKSPACE_DIR"
REMOTE_ENV = "HOUSEBOOK_RCLONE_REMOTE"

# Never moved by a mirror or a per-file copy. The DB, the journal and
# the backups each travel by their own rules below; the anchor and
# the manifest are this machine's private state.
RCLONE_EXCLUDES = [
    "--exclude", "*.db-wal",
    "--exclude", "*.db-shm",
    "--exclude", "*.sync_anchor",
    "--exclude", "*.sync_manifest*",
    "--exclude", "*.sync_journal",
    "--exclude", ".DS_Store",
    "--exclude", "__pycache__/**",
    "--exclude", "data/backups/**",
]

# rclone's exit codes for a directory or file that does not exist.
RCLONE_NOT_FOUND = (3, 4)


class SyncError(Exception):
    """A sync step failed in a way the user must see and resolve."""


# ── SQLite change counter helpers ────────────────────────────────

def get_file_change_counter(db_path) -> int:
    """Read the 32-bit change counter from the SQLite file header."""
    with open(db_path, "rb") as f:
        f.seek(24)
        raw = f.read(4)
    if len(raw) < 4:
        return 0
    return struct.unpack(">I", raw)[0]


def get_remote_change_counter(remote_db_path: str) -> int | None:
    """Read 4 bytes at offset 24 from the remote DB via rclone cat."""
    result = subprocess.run(
        ["rclone", "cat", remote_db_path,
         "--offset", "24", "--count", "4"],
        capture_output=True, check=False,
    )
    if result.returncode != 0 or len(result.stdout) < 4:
        return None
    return struct.unpack(">I", result.stdout)[0]


def _anchor_path(db_path) -> str:
    """Return the path to the sidecar sync-anchor file."""
    base = os.path.splitext(db_path)[0]
    return base + ".sync_anchor"


def save_pull_counter(db_path, counter: int):
    """Store the change counter in a sidecar file next to the DB.

    Uses a plain file instead of a DB table so the write doesn't
    bump the DB's own change counter (which would cause false
    positives in the unpushed-changes check).
    """
    with open(_anchor_path(db_path), "w") as f:
        f.write(str(counter))


def load_pull_counter(db_path) -> int | None:
    """Read the stored change counter from the sidecar file.

    Falls back to sync_metadata table for migration from the
    previous in-DB storage.
    """
    anchor = _anchor_path(db_path)
    if os.path.exists(anchor):
        try:
            with open(anchor) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            return None

    # Migration fallback: read from sync_metadata table
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT value FROM sync_metadata "
            "WHERE key = 'remote_change_counter'"
        ).fetchone()
        conn.close()
        if row:
            counter = int(row[0])
            save_pull_counter(db_path, counter)
            return counter
        return None
    except (sqlite3.OperationalError, TypeError):
        return None


def _local_counter() -> int | None:
    if not os.path.exists(DB_PATH):
        return None
    checkpoint_wal(DB_PATH)
    return get_file_change_counter(DB_PATH)


# ── sync journal ────────────────────────────────────────────────

MAX_JOURNAL_ENTRIES = 20


def _journal_path(db_path) -> str:
    """Return the path to the sync journal file (synced to remote)."""
    base = os.path.splitext(db_path)[0]
    return base + ".sync_journal"


def _get_hostname() -> str:
    return platform.node()


def load_journal(db_path) -> list[dict]:
    """Read the sync journal. Returns [] if missing or corrupt."""
    path = _journal_path(db_path)
    try:
        with open(path) as f:
            entries = json.load(f)
        if isinstance(entries, list):
            return entries
        return []
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return []


def save_journal(db_path, entries: list[dict]):
    """Write journal entries, capping at MAX_JOURNAL_ENTRIES."""
    path = _journal_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(entries[-MAX_JOURNAL_ENTRIES:], f, indent=2)


def append_journal(db_path, direction: str,
                   anchor: int | None, files: int):
    """Append a sync operation to the journal."""
    entries = load_journal(db_path)
    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "direction": direction,
        "hostname": _get_hostname(),
        "anchor": anchor,
        "files": files,
    })
    save_journal(db_path, entries)


def merge_journals(*journals: list[dict]) -> list[dict]:
    """Every entry from every copy, oldest first, without duplicates.

    Each machine appends to its own copy, so neither copy alone holds
    the whole history. Copying one over the other would drop entries.
    """
    merged = {}
    for journal in journals:
        for entry in journal:
            if isinstance(entry, dict):
                key = (entry.get("timestamp", ""),
                       entry.get("hostname", ""),
                       entry.get("direction", ""),
                       entry.get("anchor"))
                merged.setdefault(key, entry)
    return sorted(merged.values(), key=lambda e: e.get("timestamp", ""))


# ── rclone helpers ───────────────────────────────────────────────

def workspace_contains_checkout(workspace, project_root) -> bool:
    """True if the code checkout lies at or below the workspace.

    A mirror makes its destination identical to its source, so a pull
    into such a workspace deletes the checkout (src/, .git/) and a
    push from it uploads the repository, .env included. An explicit
    workspace can still point at the repo (`.`) or an ancestor such
    as $HOME.
    """
    ws = os.path.realpath(workspace)
    root = os.path.realpath(project_root)
    return root == ws or root.startswith(ws.rstrip(os.sep) + os.sep)


def _check_workspace(workspace: str):
    if workspace_contains_checkout(workspace, str(PROJECT_ROOT)):
        print(
            f"Error: workspace {workspace} contains the code checkout "
            f"({PROJECT_ROOT}). Refusing to sync: a pull would delete "
            "the checkout and a push would upload it, .env included. "
            f"Set {WORKSPACE_ENV} to a directory outside the "
            "repository."
        )
        sys.exit(1)


def _check_rclone():
    if not shutil.which("rclone"):
        print("Error: rclone is not installed or not in PATH.")
        sys.exit(1)


def _check_remote(remote_path: str):
    remote_name = remote_path.split(":")[0]
    result = subprocess.run(
        ["rclone", "listremotes"],
        capture_output=True, text=True, check=False,
    )
    if f"{remote_name}:" not in result.stdout:
        print(
            f"Error: rclone remote '{remote_name}:' is not "
            f"configured. Run 'rclone config' to set it up."
        )
        sys.exit(1)


def _rel(workspace, path) -> str:
    """A workspace file's path as rclone lists it."""
    return os.path.relpath(path, workspace).replace(os.sep, "/")


def _remote_file(remote_path: str, rel: str) -> str:
    return f"{remote_path}/{rel}"


def _run_rclone_logged(args: list,
                       filters: bool = True) -> tuple[int, int]:
    """Run rclone with a log file, then print a transfer summary.

    Keeps --progress for the live terminal UI while capturing
    verbose output to a temp file.  After rclone exits, the log
    is parsed for Copied/Deleted actions and printed as a
    persistent summary. `filters=False` drops RCLONE_EXCLUDES, which
    rclone refuses next to --files-from-raw; a file list comes from
    filtered listings anyway.

    Returns (copied_count, deleted_count).
    """
    fd, log_path = tempfile.mkstemp(suffix=".log", prefix="rclone-")
    os.close(fd)
    try:
        subprocess.run(
            ["rclone"] + args + (RCLONE_EXCLUDES if filters else [])
            + ["--log-file", log_path, "--log-level", "INFO"],
            check=True,
        )
        return _print_transfer_summary(log_path)
    except subprocess.CalledProcessError:
        with open(log_path) as f:
            log_contents = f.read()
        if log_contents.strip():
            print("\n--- rclone log ---", file=sys.stderr)
            print(log_contents.rstrip(), file=sys.stderr)
            print("--- end log ---\n", file=sys.stderr)
        raise
    finally:
        os.unlink(log_path)


def _print_transfer_summary(log_path: str) -> tuple[int, int]:
    """Parse an rclone log and print transferred/deleted files.

    Returns (copied_count, deleted_count).
    """
    # Match lines like:
    #   INFO  : data/finance.db: Copied (replaced existing)
    #   INFO  : data/backups/old.db: Deleted
    action_re = re.compile(
        r"(?:INFO|NOTICE)\s+:\s+(.+?):\s+(Copied|Deleted|Moved)"
    )
    actions = []
    with open(log_path) as f:
        for line in f:
            if "modification time" in line:
                continue
            m = action_re.search(line)
            if m:
                actions.append((m.group(2).lower(), m.group(1)))

    if not actions:
        return (0, 0)

    copied = [p for v, p in actions if v == "copied"]
    deleted = [p for v, p in actions if v == "deleted"]

    print("\n  Transfer summary:")
    if copied:
        print(f"  Copied ({len(copied)}):")
        for path in copied:
            print(f"    + {path}")
    if deleted:
        print(f"  Deleted ({len(deleted)}):")
        for path in deleted:
            print(f"    - {path}")

    return (len(copied), len(deleted))


@contextlib.contextmanager
def _file_list(paths):
    """A temporary --files-from-raw list (one path per line)."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="sync-files-")
    with os.fdopen(fd, "w") as f:
        for p in sorted(paths):
            f.write(p + "\n")
    try:
        yield path
    finally:
        os.unlink(path)


# ── file anchor ──────────────────────────────────────────────────
#
# To a mirror, a file only the destination has is junk to delete,
# whether it is a statement imported here an hour ago or one another
# machine removed last week. The manifest tells the two apart: it
# records the tree both sides shared after this machine's last sync,
# so each difference can be pinned on the side that made it.

MANIFEST_VERSION = 1

_MODTIME_RE = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$"
)


def _manifest_path(db_path) -> str:
    """Return the path to the per-machine file manifest (not synced)."""
    base = os.path.splitext(db_path)[0]
    return base + ".sync_manifest"


def save_manifest(db_path, remote_path: str, files: dict):
    """Record the file tree both sides shared after a sync.

    Written under a temporary name and renamed, so an interrupted
    write leaves the previous manifest instead of a truncated one.
    """
    path = _manifest_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "version": MANIFEST_VERSION,
        "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "remote": remote_path,
        "files": files,
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_manifest(db_path, remote_path: str) -> dict | None:
    """Return the manifest anchoring this remote, or None.

    A manifest recorded against another remote describes a different
    tree, so it anchors nothing here. An unreadable one is reported
    and ignored. Without a manifest only identical files count as
    synced, which errs toward refusing.
    """
    path = _manifest_path(db_path)
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"Warning: ignoring unreadable sync manifest {path}: {e}",
              file=sys.stderr)
        return None
    if (not isinstance(data, dict)
            or data.get("version") != MANIFEST_VERSION
            or data.get("remote") != remote_path
            or not isinstance(data.get("files"), dict)):
        return None
    return data


def _modtime_second(value: str) -> str:
    """Normalize an rclone ModTime to whole UTC seconds.

    Local listings carry nanoseconds and the local offset, Drive
    carries milliseconds in UTC. Truncated to the second in UTC, one
    instant reads the same from both sides.
    """
    m = _MODTIME_RE.match(value)
    if not m:
        raise SyncError(f"unrecognized rclone ModTime: {value!r}")
    offset = "+00:00" if m.group(2) == "Z" else m.group(2)
    ts = datetime.fromisoformat(m.group(1) + offset)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_tree(root: str) -> dict[str, dict]:
    """List every synced file under root with its size, mtime and MD5.

    Uses the transfers' own filters, so it lists exactly the files a
    sync can touch. MD5 is None where the backend keeps none (an
    rclone crypt remote, for one).
    """
    result = subprocess.run(
        ["rclone", "lsjson", "-R", "--files-only", "--fast-list",
         "--hash", "--hash-type", "md5", root] + RCLONE_EXCLUDES,
        capture_output=True, text=True, check=False,
    )
    if result.returncode in RCLONE_NOT_FOUND:
        return {}  # a remote never pushed to
    if result.returncode != 0:
        raise SyncError(
            f"rclone could not list {root}:\n{result.stderr.strip()}"
        )
    tree = {}
    for item in json.loads(result.stdout):
        tree[item["Path"]] = {
            "size": item["Size"],
            "mtime": _modtime_second(item["ModTime"]),
            "md5": (item.get("Hashes") or {}).get("md5") or None,
        }
    return tree


def same_file(a: dict | None, b: dict | None) -> bool:
    """True if two listing entries describe the same content.

    MD5 decides when both sides have one. Otherwise size and
    modification time stand in, the evidence rclone's own sync uses
    by default. Two absent entries are the same (nothing either side).
    """
    if a is None or b is None:
        return a is None and b is None
    if a["size"] != b["size"]:
        return False
    if a.get("md5") and b.get("md5"):
        return a["md5"] == b["md5"]
    return a["mtime"] == b["mtime"]


@dataclass
class TreeChanges:
    """Differences between the trees, each pinned on the side that made it.

    `local` and `remote` map a path to "added", "modified" or
    "deleted". `conflicts` lists paths both sides changed differently.
    """
    local: dict[str, str] = field(default_factory=dict)
    remote: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def _change_kind(before: dict | None, after: dict | None) -> str:
    if before is None:
        return "added"
    if after is None:
        return "deleted"
    return "modified"


def synthesize_anchor(local: dict, remote: dict) -> dict:
    """The anchor a machine without a manifest can assume.

    Only files identical on both sides count as synced. Everything
    else is unattributed, which `SyncPlan.needs_side` turns into a
    refusal unless one side is empty.
    """
    return {p: e for p, e in local.items() if same_file(e, remote.get(p))}


def diff_trees(anchor: dict, local: dict, remote: dict) -> TreeChanges:
    """Pin every difference between local and remote on one side.

    A path that moved away from the anchor on one side only is that
    side's change, safe to carry to the other. A path both sides moved
    to different content is a conflict; to the same content, nothing
    is left to do.
    """
    changes = TreeChanges()
    for path in sorted(set(anchor) | set(local) | set(remote)):
        was, here, there = anchor.get(path), local.get(path), remote.get(path)
        local_changed = not same_file(here, was)
        remote_changed = not same_file(there, was)
        if local_changed and remote_changed:
            if not same_file(here, there):
                changes.conflicts.append(path)
        elif local_changed:
            changes.local[path] = _change_kind(was, here)
        elif remote_changed:
            changes.remote[path] = _change_kind(was, there)
    return changes


def apply_changes(tree: dict, changes: dict[str, str],
                  source: dict) -> dict:
    """The tree after one side's changes were carried onto it."""
    result = dict(tree)
    for path, kind in changes.items():
        if kind == "deleted":
            result.pop(path, None)
        else:
            result[path] = source[path]
    return result


def reanchor(anchor: dict, local: dict, remote: dict) -> dict:
    """The anchor after a sync: what both sides now share.

    A path identical on both sides is synced at that content. A path
    that still differs keeps its old anchor entry, so the side that
    moved away from it still shows as the side that changed.
    """
    new = {}
    for path in set(anchor) | set(local) | set(remote):
        here, there = local.get(path), remote.get(path)
        if same_file(here, there):
            if here is not None:
                new[path] = here
        elif path in anchor:
            new[path] = anchor[path]
    return new


def pair_renames(deleted: list[str], added: list[str],
                 before: dict, after: dict) -> dict[str, str]:
    """Match deleted paths to added ones that hold the same content.

    A rename reaches the other side as one deletion plus one addition,
    and pairing them shows that nothing is lost. A sidecar rewritten
    along with its renamed document has new content, so it is paired
    by name instead: x.json follows x.pdf when x.pdf became y.pdf and
    y.json was added.
    """
    unpaired = sorted(added)
    renames = {}
    for path in sorted(deleted):
        match = next((a for a in unpaired
                      if same_file(before[path], after[a])), None)
        if match is not None:
            renames[path] = match
            unpaired.remove(match)

    new_stem = {posixpath.splitext(old)[0]: posixpath.splitext(new)[0]
                for old, new in renames.items()}
    for path in sorted(deleted):
        if path in renames:
            continue
        stem, ext = posixpath.splitext(path)
        target = new_stem.get(stem)
        if target and target + ext in unpaired:
            renames[path] = target + ext
            unpaired.remove(target + ext)
    return renames


def lost_deletions(deleted: list[str], gone: dict, kept: dict,
                   added: list[str]) -> list[str]:
    """Deleted paths whose content survives nowhere in `kept`.

    A rename, a move into _trash/, or a duplicate deleted while its
    twin stays all keep the content, so deleting the old path loses
    nothing. A sidecar that followed its renamed document counts as
    kept too.
    """
    renames = pair_renames(deleted, added, gone, kept)
    by_size: dict[int, list[dict]] = {}
    for entry in kept.values():
        by_size.setdefault(entry["size"], []).append(entry)
    return [path for path in sorted(deleted)
            if path not in renames
            and not any(same_file(gone[path], e)
                        for e in by_size.get(gone[path]["size"], []))]


def _mirror_changes(source: dict, dest: dict) -> dict[str, str]:
    """What mirroring source onto dest changes on dest."""
    return {p: _change_kind(dest.get(p), source.get(p))
            for p in sorted(set(source) | set(dest))
            if not same_file(source.get(p), dest.get(p))}


# ── backups ──────────────────────────────────────────────────────
#
# Backups only ever reach the remote by an additive copy, and both
# mirrors exclude them, so no machine's push can delete another's
# backups. Local rotation therefore does not propagate: the remote
# keeps every backup (and every displaced-* folder) until pruned by
# hand.

def _backups_pending(workspace: str, remote_path: str) -> bool:
    """True if a local backup is missing from the remote."""
    if not os.path.isdir(BACKUP_DIR) or not os.listdir(BACKUP_DIR):
        return False
    remote = _remote_file(remote_path, _rel(workspace, BACKUP_DIR))
    result = subprocess.run(
        ["rclone", "check", BACKUP_DIR, remote, "--one-way", "--quiet"],
        capture_output=True, check=False,
    )
    return result.returncode != 0


def _push_backups(workspace: str, remote_path: str):
    """Copy local backups to the remote additively (never deletes)."""
    remote = _remote_file(remote_path, _rel(workspace, BACKUP_DIR))
    _run_rclone_logged(["copy", os.fspath(BACKUP_DIR), remote,
                        "--progress"])


# ── sync plan ────────────────────────────────────────────────────

@dataclass
class SyncPlan:
    """Both trees, both anchors, and what each side changed since.

    The DB is kept out of `files`: SQLite files cannot be merged, and
    its change counter (not the manifest) is the anchor that says
    which side wrote to it.
    """
    local_tree: dict
    remote_tree: dict
    anchor: dict
    manifest: dict | None
    files: TreeChanges
    local_db: dict | None
    remote_db: dict | None
    local_counter: int | None
    anchor_counter: int | None
    remote_counter: int | None

    @property
    def db_differs(self) -> bool:
        return not same_file(self.local_db, self.remote_db)

    @property
    def db_local_changed(self) -> bool:
        if not self.db_differs or self.local_db is None:
            return False
        if self.anchor_counter is None:
            return True
        return self.local_counter != self.anchor_counter

    @property
    def db_remote_changed(self) -> bool:
        if not self.db_differs or self.remote_db is None:
            return False
        if self.anchor_counter is None:
            return True
        return self.remote_counter != self.anchor_counter

    @property
    def db_conflict(self) -> bool:
        """The DBs differ and the counters cannot say which side wrote.

        Either both did, or (an anomaly) neither counter moved. One
        side has to win explicitly.
        """
        return self.db_differs and (
            self.db_local_changed == self.db_remote_changed)

    @property
    def local_dirty(self) -> bool:
        return bool(self.files.local) or self.db_local_changed

    @property
    def remote_dirty(self) -> bool:
        return bool(self.files.remote) or self.db_remote_changed

    @property
    def conflicted(self) -> bool:
        return bool(self.files.conflicts) or self.db_conflict

    @property
    def has_differences(self) -> bool:
        return self.local_dirty or self.remote_dirty or self.conflicted

    @property
    def local_empty(self) -> bool:
        return not self.local_tree and self.local_db is None

    @property
    def remote_empty(self) -> bool:
        return not self.remote_tree and self.remote_db is None

    @property
    def needs_side(self) -> bool:
        """No manifest, so differences cannot be pinned on a side.

        A file only one side has may be new there or deleted on the
        other; merging would resurrect deletions. One --force run
        picks a side and anchors this machine. When one side is empty
        there is nothing to choose.
        """
        return (self.manifest is None and self.has_differences
                and not self.local_empty and not self.remote_empty)

    def local_deletions(self) -> tuple[list[str], list[str]]:
        """(deleted, added) paths among this machine's changes."""
        deleted = [p for p, k in self.files.local.items() if k == "deleted"]
        added = [p for p, k in self.files.local.items() if k == "added"]
        return deleted, added


def build_plan(remote_path: str, workspace: str) -> SyncPlan:
    """List both trees and attribute every difference to a side."""
    local_counter = anchor_counter = None
    if os.path.exists(DB_PATH):
        local_counter = _local_counter()
        anchor_counter = load_pull_counter(DB_PATH)
    db_rel = _rel(workspace, DB_PATH)
    remote_counter = get_remote_change_counter(
        _remote_file(remote_path, db_rel))

    local_tree = list_tree(workspace)
    remote_tree = list_tree(remote_path)
    local_db = local_tree.pop(db_rel, None)
    remote_db = remote_tree.pop(db_rel, None)

    manifest = load_manifest(DB_PATH, remote_path)
    anchor = (manifest["files"] if manifest
              else synthesize_anchor(local_tree, remote_tree))
    return SyncPlan(
        local_tree=local_tree, remote_tree=remote_tree,
        anchor=anchor, manifest=manifest,
        files=diff_trees(anchor, local_tree, remote_tree),
        local_db=local_db, remote_db=remote_db,
        local_counter=local_counter, anchor_counter=anchor_counter,
        remote_counter=remote_counter,
    )


def _record_after(plan: SyncPlan, remote_path: str, local_after: dict,
                  remote_after: dict, db_shared: bool):
    """Move this machine's anchors to the state both sides now share."""
    save_manifest(DB_PATH, remote_path,
                  reanchor(plan.anchor, local_after, remote_after))
    if db_shared and os.path.exists(DB_PATH):
        save_pull_counter(DB_PATH, get_file_change_counter(DB_PATH))


# ── presentation ─────────────────────────────────────────────────

def _recommendation(plan: SyncPlan) -> tuple[str, str]:
    """Return (symbol, message) for the status dashboard."""
    if plan.remote_empty and not plan.local_empty:
        return ("*", "Remote is empty. Safe to push (initial sync)")
    if plan.local_empty and not plan.remote_empty:
        return ("*", "Workspace is empty. Safe to pull (initial sync)")
    if plan.needs_side:
        return ("!", ("No sync anchor here yet. Review with status "
                      "--verbose, then pick a side once with --force"))
    if plan.conflicted:
        return ("!", ("Changed on both sides. Review with status "
                      "--verbose, then keep one version"))
    if plan.local_dirty and plan.remote_dirty:
        return ("<>", ("Both sides changed different files. Safe to "
                       f"sync ({PROG} merges them)"))
    if plan.local_dirty:
        return (">", "Only this machine changed. Safe to push")
    if plan.remote_dirty:
        return ("<", "Only the remote changed. Safe to pull")
    return ("=", "In sync. Nothing to do.")


_MARKS = {"added": "+", "modified": "~", "deleted": "-"}


def _change_lines(changes: dict[str, str], before: dict,
                  after: dict) -> list[str]:
    """Render one side's changes, with renames paired up."""
    renames = pair_renames(
        [p for p, k in changes.items() if k == "deleted"],
        [p for p, k in changes.items() if k == "added"],
        before, after,
    )
    lines = [f"    > {old}\n        → {new}"
             for old, new in sorted(renames.items())]
    shown = set(renames) | set(renames.values())
    lines += [f"    {_MARKS[kind]} {path}"
              for path, kind in sorted(changes.items())
              if path not in shown]
    return lines


def _change_summary(changes: dict[str, str], before: dict,
                    after: dict) -> str:
    """One line of counts, e.g. "3 added, 1 deleted (1 of them renames)"."""
    counts = {k: sum(1 for v in changes.values() if v == k)
              for k in ("added", "modified", "deleted")}
    parts = [f"{n} {k}" for k, n in counts.items() if n]
    renames = pair_renames(
        [p for p, k in changes.items() if k == "deleted"],
        [p for p, k in changes.items() if k == "added"],
        before, after,
    )
    summary = ", ".join(parts) or "none"
    if renames:
        summary += f" ({len(renames)} of them renames)"
    return summary


def _print_side(title: str, changes: dict[str, str], before: dict,
                after: dict):
    if not changes:
        return
    print(f"\n  {title} ({len(changes)}):")
    for line in _change_lines(changes, before, after):
        print(line)


def _print_db_state(plan: SyncPlan):
    if not plan.db_differs:
        return
    if plan.db_conflict:
        where = "changed on both sides, or the counters cannot tell"
    elif plan.db_local_changed:
        where = "changed here"
    else:
        where = "changed on the remote"
    print(f"\n  Database: {where} (local {plan.local_counter}, "
          f"anchor {plan.anchor_counter}, remote {plan.remote_counter})")


def _print_conflicts(plan: SyncPlan):
    if plan.files.conflicts:
        print(f"\n  Changed differently on both sides "
              f"({len(plan.files.conflicts)}):")
        for path in plan.files.conflicts:
            print(f"    ! {path}")


_DISPLACED = "data/backups/displaced-<time>/"


def _refuse(operation: str, plan: SyncPlan):
    """Explain why an operation would lose work, then exit."""
    verb = operation.upper()
    if plan.needs_side:
        print(f"\n  REFUSING TO {verb}: this machine has no sync anchor "
              "for this remote yet,\n  so it cannot tell its own "
              "changes from stale copies.")
        _print_side("Only here, or different here", plan.files.local,
                    plan.remote_tree, plan.local_tree)
        _print_side("Only on the remote, or different there",
                    plan.files.remote, plan.local_tree, plan.remote_tree)
        _print_conflicts(plan)
        _print_db_state(plan)
        options = [
            (f"{PROG} status -v", "Review both sides"),
            (f"{PROG} pull --force", ("Take the remote; this machine's "
                                      f"versions move to {_DISPLACED}")),
            (f"{PROG} push --force", "Make the remote match this machine"),
        ]
    else:
        print(f"\n  REFUSING TO {verb}: these changed on both sides "
              "since this machine's last\n  sync, so either version "
              "would overwrite the other.")
        _print_conflicts(plan)
        _print_db_state(plan)
        options = [
            (f"{PROG} status -v", "Review both sides"),
            ("(keep one version)", ("Copy it over the other, e.g. with "
                                    "rclone copyto, then sync again")),
            (f"{PROG} pull --force", ("Take the remote for everything; "
                                      "this machine's versions move to "
                                      f"{_DISPLACED}")),
            (f"{PROG} push --force", "Take this machine for everything"),
        ]
    context = _format_conflict(load_journal(DB_PATH), _get_hostname())
    if context:
        print(f"\n{context}")
    print("\n  Options:")
    for command, meaning in options:
        print(f"    {command:<29} {meaning}")
    sys.exit(1)


def _format_journal_entry(entry: dict) -> str:
    """Format one journal entry as a status line."""
    try:
        ts = datetime.fromisoformat(entry["timestamp"])
        ts_local = ts.astimezone()
        ts_str = ts_local.strftime("%b %d %H:%M")
    except (KeyError, ValueError):
        ts_str = "????"
    direction = entry.get("direction", "?")
    hostname = entry.get("hostname", "?")
    files = entry.get("files", 0)
    return f"  {ts_str}  {direction:<5} {hostname:<14} {files} files"


def _format_conflict(journal: list[dict],
                     hostname: str) -> str:
    """Build context lines from journal for conflict messages."""
    lines = []
    last_sync = next(
        (e for e in reversed(journal)
         if e.get("hostname") == hostname),
        None,
    )
    last_remote = next(
        (e for e in reversed(journal)
         if e.get("direction") == "push"
         and e.get("hostname") != hostname),
        None,
    )
    if last_sync:
        try:
            ts = datetime.fromisoformat(
                last_sync["timestamp"]
            ).astimezone().strftime("%b %d %H:%M")
        except (KeyError, ValueError):
            ts = "????"
        lines.append(
            f"  Your last sync:    {ts}  "
            f"{last_sync['direction']} on "
            f"{last_sync['hostname']}"
        )
    if last_remote:
        try:
            ts = datetime.fromisoformat(
                last_remote["timestamp"]
            ).astimezone().strftime("%b %d %H:%M")
        except (KeyError, ValueError):
            ts = "????"
        files = last_remote.get("files", "?")
        lines.append(
            f"  Remote modified:   {ts}  "
            f"push from {last_remote['hostname']}  "
            f"({files} files)"
        )
    return "\n".join(lines)


def _print_pull_plan(plan: SyncPlan, mirror: bool, fetch_db: bool):
    if mirror:
        print("  --force: this machine will end identical to the "
              f"remote; its own versions move to {_DISPLACED}")
        _print_side("To change here",
                    _mirror_changes(plan.remote_tree, plan.local_tree),
                    plan.local_tree, plan.remote_tree)
    else:
        _print_side("Remote changes to apply here", plan.files.remote,
                    plan.local_tree, plan.remote_tree)
    if fetch_db:
        print(f"\n  Database: take the remote copy (counter "
              f"{plan.remote_counter}), after a local backup")


def _print_push_plan(plan: SyncPlan, mirror: bool, send_db: bool,
                     lost: list[str], backups: bool):
    if mirror:
        print("  --force: the remote will end identical to this "
              "machine.")
        _print_side("To change on the remote",
                    _mirror_changes(plan.local_tree, plan.remote_tree),
                    plan.remote_tree, plan.local_tree)
    else:
        _print_side("Changes here to apply on the remote",
                    plan.files.local, plan.remote_tree, plan.local_tree)
    if send_db:
        print(f"\n  Database: send this machine's copy (counter "
              f"{plan.local_counter})")
    if lost:
        print(f"\n  Deleting these needs confirmation; their content "
              f"exists nowhere here ({len(lost)}):")
        for path in lost:
            print(f"    - {path}")
    if backups:
        print("\n  Local backups not yet on the remote will be copied "
              "(never deleted there).")


# ── transfers ────────────────────────────────────────────────────

def _displaced_dir() -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return os.path.join(BACKUP_DIR, f"displaced-{stamp}")


def _inside(root, rel: str) -> str:
    """Join a listed path onto root, refusing anything that escapes."""
    parts = rel.split("/")
    if rel.startswith("/") or ".." in parts or "" in parts:
        raise SyncError(f"refusing unsafe path from a listing: {rel!r}")
    return os.path.join(root, *parts)


def _displace_local(workspace: str, paths: list[str], displaced: str):
    """Move local files aside instead of deleting them."""
    print(f"  Moving {len(paths)} file(s) the remote deleted to "
          f"{displaced}:")
    for rel in sorted(paths):
        dst = _inside(displaced, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(_inside(workspace, rel), dst)
        print(f"    - {rel}")


def _remote_journal(remote_path: str, workspace: str) -> list[dict]:
    """The remote's copy of the journal; [] if it has none yet."""
    rel = _rel(workspace, _journal_path(DB_PATH))
    result = subprocess.run(
        ["rclone", "cat", _remote_file(remote_path, rel)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode in RCLONE_NOT_FOUND:
        return []
    if result.returncode != 0:
        raise SyncError("rclone could not read the remote sync "
                        f"journal:\n{result.stderr.strip()}")
    try:
        entries = json.loads(result.stdout)
    except ValueError:
        print("Warning: ignoring an unreadable remote sync journal",
              file=sys.stderr)
        return []
    return entries if isinstance(entries, list) else []


def _copy_listed(src: str, dst: str, paths: list[str],
                 extra: list[str] = ()) -> tuple[int, int]:
    """Copy exactly the listed paths, even where size and time match.

    The plan already decided these differ (by MD5 where it can), so
    rclone's quick size-and-time check must not skip any of them.
    """
    with _file_list(paths) as listing:
        return _run_rclone_logged(
            ["copy", src, dst, "--files-from-raw", listing,
             "--ignore-times", "--progress", *extra], filters=False)


def _confirm_deletions(paths: list[str], allowed: bool) -> bool:
    """Ask before a push deletes remote content that exists nowhere here."""
    print(f"\n  This push would DELETE {len(paths)} remote file(s) "
          "whose content exists nowhere here:")
    for path in paths[:20]:
        print(f"    - {path}")
    if len(paths) > 20:
        print(f"    ... and {len(paths) - 20} more")
    if allowed:
        print("  --allow-deletions: deleting them.")
        return True
    if not _stdin_is_tty():
        print("  Non-interactive session: refusing. Once the deletion "
              "is confirmed,\n  re-run with --allow-deletions.")
        return False
    answer = input("  Proceed with deletions? [y/N] ").strip().lower()
    return answer == "y"


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


# ── commands ─────────────────────────────────────────────────────

def cmd_status(remote_path: str, workspace: str,
               verbose: bool = False):
    """Show a sync dashboard: anchors, changes per side, history."""
    plan = build_plan(remote_path, workspace)
    sym, rec_msg = _recommendation(plan)

    local = plan.local_counter
    anchor = plan.anchor_counter
    remote = plan.remote_counter

    # Header
    print("Sync Status")
    print("─" * 47)
    print(f"  Machine:  {_get_hostname()}")
    print(f"  Remote:   {remote_path}")
    print()

    # Database section
    print("── Database " + "─" * 37)
    print(f"  Local counter:   "
          f"{local if local is not None else '(no DB)'}")
    print(f"  Anchor:          "
          f"{anchor if anchor is not None else '(none)'}")
    print(f"  Remote counter:  "
          f"{remote if remote is not None else '(unreachable)'}")
    if plan.db_conflict:
        print("  ! Differs, and the counters cannot tell which side "
              "wrote")
    elif plan.db_local_changed:
        print("  > Local has unpushed changes")
    elif plan.db_remote_changed:
        print("  < Remote was modified")
    else:
        print("  = Clean")
    print()

    # Files section
    print("── Files " + "─" * 40)
    if plan.manifest:
        try:
            saved = datetime.fromisoformat(
                plan.manifest["saved"]
            ).astimezone().strftime("%b %d %H:%M")
        except (KeyError, ValueError):
            saved = "????"
        print(f"  Anchor:             {saved} "
              f"({len(plan.manifest['files'])} files)")
    else:
        print("  Anchor:             (none for this remote: only "
              "identical files count as synced)")
    sides = (
        ("Changed here:     ", plan.files.local,
         plan.remote_tree, plan.local_tree),
        ("Changed on remote:", plan.files.remote,
         plan.local_tree, plan.remote_tree),
    )
    for label, changes, before, after in sides:
        print(f"  {label}  {_change_summary(changes, before, after)}")
        if verbose:
            for line in _change_lines(changes, before, after):
                print(line)
    print(f"  Conflicts:          {len(plan.files.conflicts)}")
    if verbose:
        for path in plan.files.conflicts:
            print(f"    ! {path}")
    if not verbose and plan.has_differences:
        print("  (--verbose lists the files)")
    print()

    # Recent syncs section
    journal = load_journal(DB_PATH)
    print("── Recent Syncs " + "─" * 33)
    if journal:
        for entry in journal[-5:]:
            print(_format_journal_entry(entry))
    else:
        print("  (no sync history)")
    print()

    # Recommendation
    print("── Recommendation " + "─" * 31)
    print(f"  {sym} {rec_msg}")


def cmd_pull(remote_path: str, workspace: str,
             dry_run: bool = False, force: bool = False,
             plan: SyncPlan | None = None):
    """Apply the remote's changes here, leaving this machine's alone.

    With --force, mirror the remote instead: this machine ends
    identical to it. Either way, a file the pull replaces or deletes
    here moves under data/backups/displaced-<time>/.
    """
    if plan is None:
        plan = build_plan(remote_path, workspace)
    if not force and (plan.conflicted or plan.needs_side):
        _refuse("pull", plan)

    mirror = force
    if mirror:
        fetch_db = plan.db_differs and plan.remote_db is not None
        changes = _mirror_changes(plan.remote_tree, plan.local_tree)
    else:
        fetch_db = plan.db_remote_changed
        changes = plan.files.remote
    if not changes and not fetch_db:
        if not dry_run and not plan.has_differences:
            # Both sides match: anchor here, bootstrapping a machine
            # that has no manifest yet.
            _record_after(plan, remote_path, plan.local_tree,
                          plan.remote_tree, db_shared=True)
        note = (" This machine has changes to push."
                if plan.local_dirty else "")
        print(f"Nothing to pull.{note}")
        return

    if dry_run:
        print(f"Performing DRY RUN pull from {remote_path}...")
        _print_pull_plan(plan, mirror, fetch_db)
        return

    displaced = _displaced_dir()
    if fetch_db:
        bk = backup_database(DB_PATH, BACKUP_DIR)
        if bk:
            print(f"  Pre-pull backup: {bk}")
        else:
            print("  Database unchanged, skipping pre-pull backup.")

    print(f"Pulling from {remote_path}...")
    if mirror:
        _run_rclone_logged(
            ["sync", remote_path, workspace,
             "--exclude", "/" + _rel(workspace, DB_PATH),
             "--backup-dir", displaced, "--progress"])
        local_after = dict(plan.remote_tree)
    else:
        fetch = [p for p, k in changes.items() if k != "deleted"]
        gone = [p for p, k in changes.items() if k == "deleted"]
        if fetch:
            _copy_listed(remote_path, workspace, fetch,
                         ["--backup-dir", displaced])
        if gone:
            _displace_local(workspace, gone, displaced)
        local_after = apply_changes(plan.local_tree, changes,
                                    plan.remote_tree)
    if fetch_db:
        subprocess.run(
            ["rclone", "copyto",
             _remote_file(remote_path, _rel(workspace, DB_PATH)),
             DB_PATH],
            check=True,
        )
    elif mirror and plan.db_differs:
        print("  The remote has no database; the local one is left "
              "as it is.")
    print("Pull complete.")
    if os.path.isdir(displaced):
        print(f"  Files the pull replaced or deleted here were moved "
              f"to {displaced}")

    save_journal(DB_PATH, merge_journals(
        load_journal(DB_PATH), _remote_journal(remote_path, workspace)))
    counter = _local_counter()
    append_journal(DB_PATH, "pull", counter,
                   len(changes) + int(fetch_db))
    _record_after(plan, remote_path, local_after, plan.remote_tree,
                  db_shared=fetch_db or not plan.db_differs)
    if counter is not None:
        print(f"  Sync anchor: change counter {counter}")


def cmd_push(remote_path: str, workspace: str,
             dry_run: bool = False, force: bool = False,
             allow_deletions: bool = False,
             plan: SyncPlan | None = None,
             confirmed: frozenset = frozenset()):
    """Apply this machine's changes to the remote, leaving its own alone.

    Only the planned paths are touched, so a file another machine
    adds meanwhile is never deleted. With --force, mirror this machine
    instead: the remote ends identical to it. Either way, deleting
    remote content that exists nowhere here needs confirmation (a
    prompt, or --allow-deletions); `confirmed` lists paths a caller
    already confirmed.
    """
    if plan is None:
        plan = build_plan(remote_path, workspace)
    if not force and (plan.conflicted or plan.needs_side):
        _refuse("push", plan)

    mirror = force
    if mirror:
        send_db = plan.db_differs and plan.local_db is not None
        changes = _mirror_changes(plan.local_tree, plan.remote_tree)
    else:
        send_db = plan.db_local_changed
        changes = plan.files.local
    backups = _backups_pending(workspace, remote_path)
    if not changes and not send_db and not backups:
        if not dry_run and not plan.has_differences:
            _record_after(plan, remote_path, plan.local_tree,
                          plan.remote_tree, db_shared=True)
        note = (" The remote has changes to pull."
                if plan.remote_dirty else "")
        print(f"Nothing to push.{note}")
        return

    deleted = [p for p, k in changes.items() if k == "deleted"]
    added = [p for p, k in changes.items() if k == "added"]
    lost = lost_deletions(deleted, plan.remote_tree, plan.local_tree,
                          added)
    if dry_run:
        print(f"Performing DRY RUN push to {remote_path}...")
        _print_push_plan(plan, mirror, send_db, lost, backups)
        return
    unconfirmed = [p for p in lost if p not in confirmed]
    if unconfirmed and not _confirm_deletions(unconfirmed,
                                              allow_deletions):
        sys.exit(1)

    if changes or send_db:
        counter = _local_counter()
        # The journal travels last, so it is merged and appended
        # first. A failed push takes the entry back out: the journal
        # never records a push that did not happen.
        journal_before = load_journal(DB_PATH)
        save_journal(DB_PATH, merge_journals(
            journal_before, _remote_journal(remote_path, workspace)))
        append_journal(DB_PATH, "push", counter,
                       len(changes) + int(send_db))
        print(f"Pushing to {remote_path}...")
        try:
            if mirror:
                _run_rclone_logged(
                    ["sync", workspace, remote_path,
                     "--exclude", "/" + _rel(workspace, DB_PATH),
                     "--progress"])
            else:
                send = [p for p, k in changes.items() if k != "deleted"]
                if send:
                    _copy_listed(workspace, remote_path, send)
                if deleted:
                    with _file_list(deleted) as listing:
                        _run_rclone_logged(
                            ["delete", remote_path,
                             "--files-from-raw", listing],
                            filters=False)
            if send_db:
                subprocess.run(
                    ["rclone", "copyto", DB_PATH,
                     _remote_file(remote_path, _rel(workspace, DB_PATH))],
                    check=True,
                )
            journal = _journal_path(DB_PATH)
            subprocess.run(
                ["rclone", "copyto", journal,
                 _remote_file(remote_path, _rel(workspace, journal))],
                check=True,
            )
        except subprocess.CalledProcessError:
            save_journal(DB_PATH, journal_before)
            raise
        remote_after = (dict(plan.local_tree) if mirror else
                        apply_changes(plan.remote_tree, changes,
                                      plan.local_tree))
        _record_after(plan, remote_path, plan.local_tree, remote_after,
                      db_shared=send_db or not plan.db_differs)
    if backups:
        _push_backups(workspace, remote_path)
    print("Push complete.")


def cmd_sync(remote_path: str, workspace: str, dry_run: bool = False,
             allow_deletions: bool = False):
    """Apply each side's changes to the other: pull, then push.

    Stops when a file or the DB changed on both sides, or when this
    machine has no anchor yet; then one --force run picks a side.
    """
    plan = build_plan(remote_path, workspace)
    if plan.conflicted or plan.needs_side:
        _refuse("sync", plan)
    deleted, added = plan.local_deletions()
    lost = lost_deletions(deleted, plan.remote_tree, plan.local_tree,
                          added)
    if dry_run:
        print(f"Performing DRY RUN sync with {remote_path}...")
        _print_pull_plan(plan, mirror=False,
                         fetch_db=plan.db_remote_changed)
        _print_push_plan(plan, mirror=False,
                         send_db=plan.db_local_changed, lost=lost,
                         backups=_backups_pending(workspace, remote_path))
        if not plan.has_differences:
            print("  In sync. Nothing to do.")
        return
    # Ask before changing anything. The pull never touches the paths
    # this machine deleted, so the answer still holds for the push.
    if lost and not _confirm_deletions(lost, allow_deletions):
        sys.exit(1)
    if plan.remote_dirty:
        cmd_pull(remote_path, workspace, plan=plan)
        plan = build_plan(remote_path, workspace)
    cmd_push(remote_path, workspace, plan=plan, confirmed=frozenset(lost))


def main():
    parser = argparse.ArgumentParser(
        description="Sync workspace with remote storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  (none)    Pull the remote's changes, then push this machine's;
            stop if the same file or the DB changed on both sides
  status    Show what changed on each side, without changing anything
  pull      Apply the remote's changes here
  push      Apply this machine's changes to the remote
""",
    )
    parser.add_argument(
        "command", nargs="?", default=None,
        choices=["status", "pull", "push"],
        help="Sync direction (default: both)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview sync without making changes",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Mirror one side over the other: push makes the remote "
             "match this machine, pull makes this machine match the "
             "remote (its versions kept under data/backups/displaced-*)",
    )
    parser.add_argument(
        "--allow-deletions", action="store_true",
        help="Let a push delete remote files whose content exists "
             "nowhere here, without asking",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="List the changed files in status output",
    )
    parser.add_argument(
        "--remote",
        help=f"Override rclone remote path (default: {REMOTE_ENV} env)",
    )
    args = parser.parse_args()

    if args.force and args.command not in ("pull", "push"):
        print(f"Error: --force needs a direction. Use `{PROG} pull "
              f"--force` (the remote wins) or `{PROG} push --force` "
              "(this machine wins).")
        sys.exit(1)
    if args.allow_deletions and args.command in ("pull", "status"):
        print("Error: --allow-deletions only applies to a push (or a "
              "bare sync).")
        sys.exit(1)

    remote_path = args.remote or RCLONE_REMOTE
    if not remote_path:
        print(
            f"Error: No rclone remote configured. Set {REMOTE_ENV} "
            "in .env or pass --remote."
        )
        sys.exit(1)

    workspace = str(WORKSPACE_DIR)

    _check_workspace(workspace)
    _check_rclone()
    _check_remote(remote_path)

    try:
        if args.command == "status":
            cmd_status(remote_path, workspace, verbose=args.verbose)
        elif args.command == "pull":
            cmd_pull(remote_path, workspace, args.dry_run, args.force)
        elif args.command == "push":
            cmd_push(remote_path, workspace, args.dry_run, args.force,
                     args.allow_deletions)
        else:
            cmd_sync(remote_path, workspace, args.dry_run,
                     args.allow_deletions)
    except SyncError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
