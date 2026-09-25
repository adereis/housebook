import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from housebook.sync import (
    _journal_path,
    _recommendation,
    append_journal,
    cmd_push,
    get_file_change_counter,
    load_journal,
    load_pull_counter,
    main,
    save_journal,
    save_pull_counter,
    workspace_contains_checkout,
)


class TestChangeCounter(unittest.TestCase):
    """Test reading the SQLite file header change counter."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        # Create a minimal SQLite DB so the header exists
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_reads_counter(self):
        counter = get_file_change_counter(self.db_path)
        self.assertIsInstance(counter, int)
        self.assertGreater(counter, 0)

    def test_counter_increments_on_write(self):
        c1 = get_file_change_counter(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        c2 = get_file_change_counter(self.db_path)
        self.assertGreater(c2, c1)

    def test_counter_stable_without_writes(self):
        c1 = get_file_change_counter(self.db_path)

        # Read-only access should not change counter
        conn = sqlite3.connect(self.db_path)
        conn.execute("SELECT * FROM t")
        conn.close()

        c2 = get_file_change_counter(self.db_path)
        self.assertEqual(c2, c1)

    def test_counter_survives_copy(self):
        """Simulate rclone copy — counter is in the file bytes."""
        import shutil

        c1 = get_file_change_counter(self.db_path)

        fd2, copy_path = tempfile.mkstemp(suffix=".db")
        os.close(fd2)
        shutil.copy2(self.db_path, copy_path)

        try:
            c2 = get_file_change_counter(copy_path)
            self.assertEqual(c2, c1)
        finally:
            os.unlink(copy_path)


class TestSyncMetadata(unittest.TestCase):
    """Test save/load of pull counter in sync_metadata table."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        anchor = os.path.splitext(self.db_path)[0] + ".sync_anchor"
        if os.path.exists(anchor):
            os.unlink(anchor)

    def test_save_and_load(self):
        save_pull_counter(self.db_path, 4045)
        loaded = load_pull_counter(self.db_path)
        self.assertEqual(loaded, 4045)

    def test_load_returns_none_before_save(self):
        loaded = load_pull_counter(self.db_path)
        self.assertIsNone(loaded)

    def test_save_overwrites_previous(self):
        save_pull_counter(self.db_path, 100)
        save_pull_counter(self.db_path, 200)
        loaded = load_pull_counter(self.db_path)
        self.assertEqual(loaded, 200)

    def test_counter_is_sidecar_file(self):
        """The anchor lives in a sidecar file, not inside the DB."""
        save_pull_counter(self.db_path, 4045)

        anchor = os.path.splitext(self.db_path)[0] + ".sync_anchor"
        self.assertTrue(os.path.exists(anchor))
        with open(anchor) as f:
            self.assertEqual(f.read().strip(), "4045")

    def test_migrates_from_sync_metadata(self):
        """Falls back to sync_metadata table for old DBs."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sync_metadata (key, value) "
            "VALUES ('remote_change_counter', '3000')"
        )
        conn.commit()
        conn.close()

        loaded = load_pull_counter(self.db_path)
        self.assertEqual(loaded, 3000)
        # Migration should have created the sidecar
        anchor = os.path.splitext(self.db_path)[0] + ".sync_anchor"
        self.assertTrue(os.path.exists(anchor))


class TestConflictDetection(unittest.TestCase):
    """Test the conflict detection logic end-to-end."""

    def setUp(self):
        # Simulate two machines with copies of the same DB
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_no_conflict_when_counters_match(self):
        """After pull, remote counter matches stored → safe."""
        counter = get_file_change_counter(self.db_path)
        save_pull_counter(self.db_path, counter)

        stored = load_pull_counter(self.db_path)

        # The stored VALUE should equal what we originally saved.
        self.assertEqual(stored, counter)

    def test_conflict_when_remote_modified(self):
        """Remote DB modified after pull → counters diverge."""
        import shutil

        # Machine A pulls, records counter
        counter_at_pull = get_file_change_counter(self.db_path)
        save_pull_counter(self.db_path, counter_at_pull)

        # Simulate "remote" as a copy, then modify it
        fd2, remote_path = tempfile.mkstemp(suffix=".db")
        os.close(fd2)
        shutil.copy2(self.db_path, remote_path)

        try:
            # "Machine B" modifies the remote
            conn = sqlite3.connect(remote_path)
            conn.execute("INSERT INTO t VALUES (99)")
            conn.commit()
            conn.close()

            remote_counter = get_file_change_counter(remote_path)
            stored = load_pull_counter(self.db_path)

            # Counters should differ → conflict
            self.assertNotEqual(remote_counter, stored)
        finally:
            os.unlink(remote_path)


class TestSyncJournal(unittest.TestCase):
    """Test sync journal save/load/append operations."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        journal = _journal_path(self.db_path)
        if os.path.exists(journal):
            os.unlink(journal)

    def test_journal_path(self):
        path = _journal_path("/tmp/finance.db")
        self.assertEqual(path, "/tmp/finance.sync_journal")

    def test_journal_save_load(self):
        entries = [
            {"timestamp": "2026-05-01T10:00:00+00:00",
             "direction": "push", "hostname": "laptop",
             "anchor": 42, "files": 3},
            {"timestamp": "2026-05-02T11:00:00+00:00",
             "direction": "pull", "hostname": "desktop",
             "anchor": 45, "files": 5},
        ]
        save_journal(self.db_path, entries)
        loaded = load_journal(self.db_path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["hostname"], "laptop")
        self.assertEqual(loaded[1]["anchor"], 45)

    def test_journal_append(self):
        append_journal(self.db_path, "push", 42, 3)
        append_journal(self.db_path, "pull", 45, 5)
        loaded = load_journal(self.db_path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["direction"], "push")
        self.assertEqual(loaded[1]["direction"], "pull")
        self.assertIn("timestamp", loaded[0])

    def test_journal_caps_at_max(self):
        for i in range(25):
            append_journal(self.db_path, "push", i, 1)
        loaded = load_journal(self.db_path)
        self.assertEqual(len(loaded), 20)
        self.assertEqual(loaded[0]["anchor"], 5)

    def test_journal_empty_when_missing(self):
        loaded = load_journal(self.db_path)
        self.assertEqual(loaded, [])

    def test_journal_empty_on_corrupt(self):
        path = _journal_path(self.db_path)
        with open(path, "w") as f:
            f.write("{not valid json")
        loaded = load_journal(self.db_path)
        self.assertEqual(loaded, [])


class TestRecommendation(unittest.TestCase):
    """Test the sync state recommendation logic."""

    def _state(self, local=47, anchor=47, remote=47,
               push=0, pull=0):
        return {"local_counter": local, "anchor": anchor,
                "remote_counter": remote,
                "push_count": push, "pull_count": pull}

    def test_in_sync(self):
        sym, msg = _recommendation(self._state())
        self.assertEqual(sym, "=")
        self.assertIn("In sync", msg)

    def test_local_db_changes(self):
        sym, msg = _recommendation(self._state(local=52))
        self.assertEqual(sym, ">")
        self.assertIn("Safe to push", msg)

    def test_remote_db_changes(self):
        sym, msg = _recommendation(self._state(remote=52))
        self.assertEqual(sym, "<")
        self.assertIn("Safe to pull", msg)

    def test_both_changed(self):
        sym, msg = _recommendation(
            self._state(local=52, remote=49))
        self.assertEqual(sym, "!")
        self.assertIn("Both sides changed", msg)

    def test_no_anchor(self):
        sym, msg = _recommendation(self._state(anchor=None))
        self.assertEqual(sym, "*")
        self.assertIn("No sync history", msg)

    def test_no_remote(self):
        sym, msg = _recommendation(self._state(remote=None))
        self.assertEqual(sym, "*")
        self.assertIn("initial sync", msg)

    def test_db_clean_files_to_push(self):
        sym, msg = _recommendation(self._state(push=5))
        self.assertEqual(sym, ">")
        self.assertIn("files to push", msg)

    def test_db_clean_files_to_pull(self):
        sym, msg = _recommendation(self._state(pull=3))
        self.assertEqual(sym, "<")
        self.assertIn("files to pull", msg)

    def test_db_clean_files_both_ways(self):
        sym, msg = _recommendation(self._state(push=5, pull=3))
        self.assertEqual(sym, ">")
        self.assertIn("files pending both ways", msg)


class TestPushBackupsGate(unittest.TestCase):
    """cmd_push must not early-return when only the backups tier differs.

    The file-tier diff excludes data/backups/**, so before the fix a
    backups-only change (rotation, or cleanup of stray test backups)
    reported "Nothing to push" and never reached _push_backups — so the
    change was never mirrored to the remote.
    """

    def _run_push(self, count_diff, backups_diff):
        with patch("housebook.sync.checkpoint_wal"), \
             patch("housebook.sync._count_diff",
                   return_value=count_diff), \
             patch("housebook.sync._backups_have_diff",
                   return_value=backups_diff), \
             patch("housebook.sync.get_remote_change_counter",
                   return_value=None), \
             patch("housebook.sync.load_pull_counter",
                   return_value=None), \
             patch("housebook.sync.get_file_change_counter",
                   return_value=0), \
             patch("housebook.sync.append_journal"), \
             patch("housebook.sync.load_journal",
                   return_value=[{"files": 0}]), \
             patch("housebook.sync.save_journal"), \
             patch("housebook.sync.save_pull_counter"), \
             patch("housebook.sync._run_rclone_logged") \
                as mock_rclone, \
             patch("housebook.sync._push_backups") \
                as mock_backups, \
             patch("builtins.print"):
            cmd_push("remote:path", "/ws", dry_run=False, force=False)
        return mock_rclone, mock_backups

    def test_backups_only_change_triggers_push(self):
        _, mock_backups = self._run_push(count_diff=0, backups_diff=True)
        mock_backups.assert_called_once()

    def test_nothing_to_push_when_both_clean(self):
        mock_rclone, mock_backups = self._run_push(
            count_diff=0, backups_diff=False)
        mock_rclone.assert_not_called()
        mock_backups.assert_not_called()

    def test_file_tier_change_still_pushes(self):
        _, mock_backups = self._run_push(count_diff=3, backups_diff=False)
        mock_backups.assert_called_once()


class TestWorkspaceGuard(unittest.TestCase):
    """rclone sync mirrors its source, so a workspace that contains the
    checkout would upload the repo (.env included) on push and delete
    it on pull. That is the default when the workspace env is unset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)

    def test_repo_root_as_workspace_is_refused(self):
        self.assertTrue(workspace_contains_checkout(self.repo, self.repo))

    def test_ancestor_of_repo_is_refused(self):
        self.assertTrue(
            workspace_contains_checkout(self.tmp.name, self.repo))

    def test_filesystem_root_is_refused(self):
        self.assertTrue(workspace_contains_checkout(os.sep, self.repo))

    def test_sibling_directory_is_allowed(self):
        sibling = os.path.join(self.tmp.name, "repo-workspace")
        os.makedirs(sibling)
        self.assertFalse(workspace_contains_checkout(sibling, self.repo))

    def test_workspace_inside_repo_is_allowed(self):
        """The demo workspace lives inside the checkout; syncing it only
        touches that subtree."""
        demo = os.path.join(self.repo, "demo-workspace")
        os.makedirs(demo)
        self.assertFalse(workspace_contains_checkout(demo, self.repo))

    def test_symlinked_workspace_resolves_to_repo(self):
        link = os.path.join(self.tmp.name, "ws-link")
        os.symlink(self.repo, link)
        self.assertTrue(workspace_contains_checkout(link, self.repo))

    def test_main_refuses_before_touching_rclone(self):
        with patch("housebook.sync.WORKSPACE_DIR", self.repo), \
             patch("housebook.sync.PROJECT_ROOT", self.repo), \
             patch("housebook.sync.RCLONE_REMOTE", "remote:ws"), \
             patch("housebook.sync._check_rclone") as check, \
             patch("housebook.sync.cmd_pull") as pull, \
             patch("sys.argv", ["housebook-sync", "pull"]), \
             patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                main()
        self.assertEqual(cm.exception.code, 1)
        check.assert_not_called()
        pull.assert_not_called()


if __name__ == "__main__":
    unittest.main()
