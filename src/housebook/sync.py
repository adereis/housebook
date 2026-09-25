"""Workspace sync via rclone with database safety guards.

Uses SQLite's file header change counter (bytes 24-28) for
optimistic concurrency control: on pull the remote counter is
stored in the DB; on push it is compared to the current remote
counter to detect conflicts from other machines.
"""

import argparse
import json
import os
import platform
import shutil
import sqlite3
import struct
import subprocess
import sys
from datetime import datetime, timezone

from housebook.config.settings import (
    BACKUP_DIR,
    DB_PATH,
    PROJECT_ROOT,
    RCLONE_REMOTE,
    WORKSPACE_DIR,
)
from housebook.core.database import backup_database, checkpoint_wal

RCLONE_EXCLUDES = [
    "--exclude", "*.db-wal",
    "--exclude", "*.db-shm",
    "--exclude", "*.sync_anchor",
    "--exclude", ".DS_Store",
    "--exclude", "__pycache__/**",
    "--exclude", "data/backups/**",
]

# Diff/status excludes: also hide DB-tier metadata from file counts.
# These files still sync normally — they're just not user-facing.
RCLONE_DIFF_EXCLUDES = RCLONE_EXCLUDES + [
    "--exclude", "*.sync_journal",
]


# ── SQLite change counter helpers ────────────────────────────────

def get_file_change_counter(db_path: str) -> int:
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
        capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) < 4:
        return None
    return struct.unpack(">I", result.stdout)[0]


def _anchor_path(db_path: str) -> str:
    """Return the path to the sidecar sync-anchor file."""
    base = os.path.splitext(db_path)[0]
    return base + ".sync_anchor"


def save_pull_counter(db_path: str, counter: int):
    """Store the change counter in a sidecar file next to the DB.

    Uses a plain file instead of a DB table so the write doesn't
    bump the DB's own change counter (which would cause false
    positives in the unpushed-changes check).
    """
    with open(_anchor_path(db_path), "w") as f:
        f.write(str(counter))


def load_pull_counter(db_path: str) -> int | None:
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


# ── sync journal ────────────────────────────────────────────────

MAX_JOURNAL_ENTRIES = 20


def _journal_path(db_path: str) -> str:
    """Return the path to the sync journal file (synced to remote)."""
    base = os.path.splitext(db_path)[0]
    return base + ".sync_journal"


def _get_hostname() -> str:
    return platform.node()


def load_journal(db_path: str) -> list[dict]:
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


def save_journal(db_path: str, entries: list[dict]):
    """Write journal entries, capping at MAX_JOURNAL_ENTRIES."""
    path = _journal_path(db_path)
    with open(path, "w") as f:
        json.dump(entries[-MAX_JOURNAL_ENTRIES:], f, indent=2)


def append_journal(db_path: str, direction: str,
                   anchor: int, files: int):
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


# ── rclone helpers ───────────────────────────────────────────────

def workspace_contains_checkout(workspace: str, project_root: str) -> bool:
    """True if the code checkout lies at or below the workspace.

    `rclone sync` makes its destination identical to its source, so a
    pull into such a workspace deletes the checkout (src/, .git/) and
    a push from it uploads the repository, .env included. Settings
    refuses an unset workspace, but an explicit one can still point at
    the repo (`.`) or an ancestor such as $HOME.
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
            "Set HOUSEBOOK_WORKSPACE_DIR to a directory "
            "outside the repository."
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
        capture_output=True, text=True,
    )
    if f"{remote_name}:" not in result.stdout:
        print(
            f"Error: rclone remote '{remote_name}:' is not "
            f"configured. Run 'rclone config' to set it up."
        )
        sys.exit(1)


def _run_rclone(args: list):
    subprocess.run(["rclone"] + args + RCLONE_EXCLUDES, check=True)


def _run_rclone_logged(args: list):
    """Run rclone with a log file, then print a transfer summary.

    Keeps --progress for the live terminal UI while capturing
    verbose output to a temp file.  After rclone exits, the log
    is parsed for Copied/Deleted actions and printed as a
    persistent summary.
    """
    import tempfile

    fd, log_path = tempfile.mkstemp(suffix=".log", prefix="rclone-")
    os.close(fd)
    try:
        subprocess.run(
            ["rclone"] + args + RCLONE_EXCLUDES
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
    import re

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


def _has_diff(src: str, dst: str, one_way: bool = True) -> bool:
    """Return True if src and dst have differences."""
    cmd = ["rclone", "check", src, dst, "--quiet"] + RCLONE_DIFF_EXCLUDES
    if one_way:
        cmd.append("--one-way")
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode != 0


def _list_diff(src: str, dst: str) -> list[str]:
    """List files in src that are missing or different in dst."""
    result = subprocess.run(
        ["rclone", "check", src, dst, "--one-way",
         "--combined", "-"] + RCLONE_DIFF_EXCLUDES,
        capture_output=True, text=True,
    )
    if result.returncode == 0 and not result.stdout.strip():
        return []
    return [line[2:] for line in result.stdout.splitlines()
            if line and line[0] in "+*"]


def _count_diff(src: str, dst: str) -> int:
    """Count files in src that are missing or different in dst."""
    return len(_list_diff(src, dst))


def _push_backups(workspace: str, remote_path: str,
                  dry_run: bool = False):
    """Push data/backups to remote (DB tier, synced separately)."""
    local_backups = os.path.join(workspace, "data", "backups")
    remote_backups = f"{remote_path}/data/backups"
    if not os.path.isdir(local_backups):
        return
    args = ["sync", local_backups, remote_backups]
    if dry_run:
        args += ["--dry-run", "-v"]
        _run_rclone(args)
    else:
        args.append("--progress")
        _run_rclone_logged(args)


def _backups_have_diff(workspace: str, remote_path: str) -> bool:
    """Return True if the backups tier differs from remote (either way).

    The main file-tier diff (`_count_diff`) excludes `data/backups/**`,
    so a backups-only change (e.g. rotation or a cleanup of stray test
    backups) is invisible to it. `cmd_push` consults this so such a
    change still triggers `_push_backups` instead of early-returning
    "Nothing to push". Checks both directions so pending *deletions*
    (present on remote, gone locally) count too.
    """
    local_backups = os.path.join(workspace, "data", "backups")
    remote_backups = f"{remote_path}/data/backups"
    if not os.path.isdir(local_backups):
        return False
    result = subprocess.run(
        ["rclone", "check", local_backups, remote_backups, "--quiet"],
        capture_output=True,
    )
    return result.returncode != 0


def _remote_db_path(remote_path: str) -> str:
    """Build the rclone path to the remote finance.db."""
    return f"{remote_path}/data/finance.db"


# ── sync state & recommendation ─────────────────────────────────

def _compute_sync_state(remote_path: str, workspace: str,
                        verbose: bool = False) -> dict:
    """Gather all state needed for the status dashboard."""
    remote_db = _remote_db_path(remote_path)

    local_counter = None
    anchor = None
    if os.path.exists(DB_PATH):
        checkpoint_wal(DB_PATH)
        local_counter = get_file_change_counter(DB_PATH)
        anchor = load_pull_counter(DB_PATH)

    remote_counter = get_remote_change_counter(remote_db)

    push_all = None
    pull_all = None
    try:
        push_all = _list_diff(workspace, remote_path)
    except (subprocess.CalledProcessError, OSError):
        pass
    try:
        pull_all = _list_diff(remote_path, workspace)
    except (subprocess.CalledProcessError, OSError):
        pass

    # Files that differ appear in both directions; only show them
    # on the push side since local changes take precedence.
    if push_all is not None and pull_all is not None:
        push_set = set(push_all)
        pull_all = [f for f in pull_all if f not in push_set]

    push_count = len(push_all) if push_all is not None else None
    pull_count = len(pull_all) if pull_all is not None else None

    return {
        "hostname": _get_hostname(),
        "remote_path": remote_path,
        "local_counter": local_counter,
        "anchor": anchor,
        "remote_counter": remote_counter,
        "push_count": push_count,
        "push_files": push_all if verbose else None,
        "pull_count": pull_count,
        "pull_files": pull_all if verbose else None,
        "journal": load_journal(DB_PATH),
    }


def _recommendation(state: dict) -> tuple[str, str]:
    """Return (symbol, message) based on counters and file diffs."""
    local = state["local_counter"]
    anchor = state["anchor"]
    remote = state["remote_counter"]
    push_n = state.get("push_count") or 0
    pull_n = state.get("pull_count") or 0
    has_file_diff = push_n > 0 or pull_n > 0

    if remote is None:
        return ("*", "Remote DB not found. Safe to push (initial sync)")
    if anchor is None:
        return ("*", "No sync history. Run housebook-sync to initialize")

    db_clean = (local == anchor == remote)

    if db_clean and not has_file_diff:
        return ("=", "In sync. Nothing to do.")
    if db_clean and has_file_diff:
        if push_n and pull_n:
            return (">", "DB clean, files pending both ways. "
                    "Run housebook-sync")
        if push_n:
            return (">", "DB clean, files to push")
        return ("<", "DB clean, files to pull")
    if local != anchor and remote == anchor:
        return (">", "Safe to push")
    if local == anchor and remote != anchor:
        return ("<", "Remote has changes. Safe to pull")
    return ("!", "Both sides changed. Pull first, review, then push")


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


# ── commands ─────────────────────────────────────────────────────

def cmd_status(remote_path: str, workspace: str,
               verbose: bool = False):
    """Show a sync dashboard with counters, history, recommendation."""
    state = _compute_sync_state(remote_path, workspace, verbose)
    sym, rec_msg = _recommendation(state)

    local = state["local_counter"]
    anchor = state["anchor"]
    remote = state["remote_counter"]

    # Header
    print("Sync Status")
    print("─" * 47)
    print(f"  Machine:  {state['hostname']}")
    print(f"  Remote:   {state['remote_path']}")
    print()

    # Database section
    print("── Database " + "─" * 37)
    print(f"  Local counter:   "
          f"{local if local is not None else '(no DB)'}")
    print(f"  Anchor:          "
          f"{anchor if anchor is not None else '(none)'}")
    print(f"  Remote counter:  "
          f"{remote if remote is not None else '(unreachable)'}")

    if anchor is not None and local is not None:
        if local != anchor and (remote is None or remote == anchor):
            print("  > Local has unpushed changes")
        elif local == anchor and remote is not None and remote != anchor:
            print("  > Remote was modified")
        elif (local != anchor
              and remote is not None and remote != anchor):
            print("  > Both sides changed since last sync")
        else:
            print("  > Clean")
    print()

    # Pending section
    print("── Pending " + "─" * 38)
    push_str = (str(state["push_count"])
                if state["push_count"] is not None else "?")
    pull_str = (str(state["pull_count"])
                if state["pull_count"] is not None else "?")
    hint = "" if verbose else "   (--verbose for details)"
    print(f"  Would push:  {push_str} files{hint}")
    if state.get("push_files"):
        for path in state["push_files"]:
            print(f"    {path}")
    print(f"  Would pull:  {pull_str} files")
    if state.get("pull_files"):
        for path in state["pull_files"]:
            print(f"    {path}")
    print()

    # Recent syncs section
    journal = state["journal"]
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
             dry_run: bool = False, force: bool = False):
    if dry_run:
        print(f"Performing DRY RUN pull from {remote_path}...")
        _run_rclone(
            ["sync", remote_path, workspace, "--update",
             "--exclude", "data/*.db",
             "--dry-run", "-v"]
        )
        print("  (database is always pulled separately)")
        return

    if not force and os.path.exists(DB_PATH):
        checkpoint_wal(DB_PATH)
        local_counter = get_file_change_counter(DB_PATH)
        stored_counter = load_pull_counter(DB_PATH)

        if (stored_counter is not None
                and local_counter != stored_counter):
            journal = load_journal(DB_PATH)
            context = _format_conflict(
                journal, _get_hostname()
            )
            print("\n  UNPUSHED LOCAL CHANGES DETECTED")
            if context:
                print(context)
            print(
                "\n  Options:\n"
                "    housebook-sync push          "
                "Push your changes first\n"
                "    housebook-sync pull --force   "
                "Overwrite local (backup created "
                "automatically)\n"
                "    housebook-sync status         "
                "Review the full state"
            )
            sys.exit(1)

    if not force:
        print(f"Checking for updates from {remote_path}...")
        if not _has_diff(remote_path, workspace, one_way=True):
            print("Local workspace is up-to-date. Nothing to pull.")
            return

    bk = backup_database(DB_PATH, BACKUP_DIR)
    if bk:
        print(f"  Pre-pull backup: {bk}")
    else:
        print("  Database unchanged, skipping pre-pull backup.")

    print(f"Pulling latest data from {remote_path}...")
    # The DB is excluded here and pulled separately so the WAL
    # checkpoint mtime doesn't cause rclone to skip a fresher remote DB.
    counts = _run_rclone_logged(
        ["sync", remote_path, workspace, "--update", "--progress",
         "--exclude", "data/*.db"]
    )
    remote_db = _remote_db_path(remote_path)
    subprocess.run(["rclone", "copyto", remote_db, DB_PATH], check=True)
    print("Pull complete.")

    if os.path.exists(DB_PATH):
        counter = get_file_change_counter(DB_PATH)
        save_pull_counter(DB_PATH, counter)
        files = (counts[0] if counts else 0) + 1
        append_journal(DB_PATH, "pull", counter, files)
        print(f"  Sync anchor: change counter {counter}")


def cmd_push(remote_path: str, workspace: str,
             dry_run: bool = False, force: bool = False):
    checkpoint_wal(DB_PATH)

    push_count = 0
    if not dry_run:
        push_count = _count_diff(workspace, remote_path)
        # The file-tier diff excludes data/backups/**, so check the
        # backups tier separately — otherwise a backups-only change
        # (e.g. rotation or test-backup cleanup) is skipped and never
        # reaches _push_backups.
        backups_diff = _backups_have_diff(workspace, remote_path)
        if not force and not push_count and not backups_diff:
            print("Remote is up-to-date. Nothing to push.")
            return

    # Conflict detection: compare remote counter with last-pull
    if not dry_run and not force:
        remote_db = _remote_db_path(remote_path)
        remote_counter = get_remote_change_counter(remote_db)
        stored_counter = load_pull_counter(DB_PATH)

        if (remote_counter is not None
                and stored_counter is not None
                and remote_counter != stored_counter):
            journal = load_journal(DB_PATH)
            context = _format_conflict(
                journal, _get_hostname()
            )
            print("\n  CONFLICT: Remote was modified since "
                  "your last sync")
            if context:
                print(context)
            print(
                "\n  Options:\n"
                "    housebook-sync pull          "
                "Get remote changes first\n"
                "    housebook-sync push --force   "
                "Overwrite remote\n"
                "    housebook-sync status         "
                "Review the full state"
            )
            sys.exit(1)

    if dry_run:
        print(f"Performing DRY RUN push to {remote_path}...")
        _run_rclone(
            ["sync", workspace, remote_path,
             "--dry-run", "-v"]
        )
        _push_backups(workspace, remote_path, dry_run=True)
        return
    else:
        counter = get_file_change_counter(DB_PATH)
        # Journal appended BEFORE rclone so it's included in the
        # push payload — keeps local and remote journal in sync.
        # Recount after to capture the journal file itself.
        append_journal(DB_PATH, "push", counter, 0)
        push_count = _count_diff(workspace, remote_path)
        entries = load_journal(DB_PATH)
        entries[-1]["files"] = push_count
        save_journal(DB_PATH, entries)
        print(f"Pushing local data to {remote_path}...")
        _run_rclone_logged(
            ["sync", workspace, remote_path,
             "--progress"]
        )
        _push_backups(workspace, remote_path)
        save_pull_counter(DB_PATH, counter)
        print("Push complete.")


def cmd_sync(remote_path: str, workspace: str,
             dry_run: bool = False, force: bool = False):
    """Bidirectional sync: pull new inputs, push local DB changes.

    1. Check for DB conflicts (remote modified since last pull)
    2. Pull input/ and config/ from remote (new statements)
    3. Push everything to remote (DB, backups, config)
    """
    remote_db = _remote_db_path(remote_path)

    # Step 1: Conflict check on DB
    if not force and os.path.exists(DB_PATH):
        remote_counter = get_remote_change_counter(remote_db)
        stored_counter = load_pull_counter(DB_PATH)

        if (remote_counter is not None
                and stored_counter is not None
                and remote_counter != stored_counter):
            journal = load_journal(DB_PATH)
            context = _format_conflict(
                journal, _get_hostname()
            )
            print("\n  CONFLICT: Remote was modified since "
                  "your last sync")
            if context:
                print(context)
            print(
                "\n  Options:\n"
                "    housebook-sync --force       "
                "Force bidirectional sync\n"
                "    housebook-sync pull/push      "
                "Manual control\n"
                "    housebook-sync status         "
                "Review the full state"
            )
            sys.exit(1)

    # Step 2: Pull remote → local
    if dry_run:
        print("--- DRY RUN: pull phase ---")
    else:
        print("Pulling remote changes...")

    bk = None
    if not dry_run and os.path.exists(DB_PATH):
        bk = backup_database(DB_PATH, BACKUP_DIR)
        if bk:
            print(f"  Pre-sync backup: {bk}")

    rclone_pull_args = [
        "sync", remote_path, workspace, "--update",
        "--exclude", "data/*.db",
    ]
    if dry_run:
        rclone_pull_args += ["--dry-run", "-v"]
        _run_rclone(rclone_pull_args)
    else:
        rclone_pull_args.append("--progress")
        counts_pull = _run_rclone_logged(rclone_pull_args)
        remote_db = _remote_db_path(remote_path)
        subprocess.run(
            ["rclone", "copyto", remote_db, DB_PATH], check=True,
        )

    if not dry_run and os.path.exists(DB_PATH):
        counter = get_file_change_counter(DB_PATH)
        save_pull_counter(DB_PATH, counter)
        files = (counts_pull[0] if counts_pull else 0) + 1
        append_journal(DB_PATH, "pull", counter, files)
        print(f"  Sync anchor: change counter {counter}")

    # Step 3: Push local → remote
    checkpoint_wal(DB_PATH)

    if dry_run:
        print("\n--- DRY RUN: push phase ---")

    rclone_push_args = [
        "sync", workspace, remote_path,
    ]
    if dry_run:
        rclone_push_args += ["--dry-run", "-v"]
        _run_rclone(rclone_push_args)
        _push_backups(workspace, remote_path, dry_run=True)
    else:
        counter = get_file_change_counter(DB_PATH)
        append_journal(DB_PATH, "push", counter, 0)
        push_count = _count_diff(workspace, remote_path)
        entries = load_journal(DB_PATH)
        entries[-1]["files"] = push_count
        save_journal(DB_PATH, entries)
        rclone_push_args.append("--progress")
        _run_rclone_logged(rclone_push_args)
        _push_backups(workspace, remote_path)

    if not dry_run and os.path.exists(DB_PATH):
        counter = get_file_change_counter(DB_PATH)
        save_pull_counter(DB_PATH, counter)
        print("Sync complete.")


def main():
    parser = argparse.ArgumentParser(
        description="Sync workspace with remote storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  (none)    Bidirectional sync: pull then push (default)
  status    Show differences without changing anything
  pull      Remote → local only
  push      Local → remote only (with conflict detection)
""",
    )
    parser.add_argument(
        "command", nargs="?", default=None,
        choices=["status", "pull", "push"],
        help="Sync direction (default: bidirectional)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview sync without making changes",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip conflict detection and force the operation",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show pending file list in status output",
    )
    parser.add_argument(
        "--remote",
        help="Override rclone remote path "
             "(default: HOUSEBOOK_RCLONE_REMOTE env)",
    )
    args = parser.parse_args()

    remote_path = args.remote or RCLONE_REMOTE
    if not remote_path:
        print(
            "Error: No rclone remote configured. Set "
            "HOUSEBOOK_RCLONE_REMOTE in .env or "
            "pass --remote."
        )
        sys.exit(1)

    workspace = str(WORKSPACE_DIR)

    _check_workspace(workspace)
    _check_rclone()
    _check_remote(remote_path)

    if args.command == "status":
        cmd_status(remote_path, workspace, verbose=args.verbose)
    elif args.command == "pull":
        cmd_pull(remote_path, workspace, args.dry_run, args.force)
    elif args.command == "push":
        cmd_push(remote_path, workspace, args.dry_run, args.force)
    else:
        cmd_sync(remote_path, workspace, args.dry_run, args.force)


if __name__ == "__main__":
    main()
