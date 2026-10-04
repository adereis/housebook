import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from housebook.sync import (
    DB_PATH,
    RCLONE_EXCLUDES,
    SyncError,
    SyncPlan,
    _journal_path,
    _modtime_second,
    _recommendation,
    append_journal,
    cmd_pull,
    cmd_push,
    cmd_sync,
    diff_trees,
    get_file_change_counter,
    list_tree,
    load_journal,
    load_manifest,
    load_pull_counter,
    lost_deletions,
    main,
    merge_journals,
    pair_renames,
    reanchor,
    same_file,
    save_journal,
    save_manifest,
    save_pull_counter,
    synthesize_anchor,
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


def _entry(md5="aaa", size=10, mtime="2026-01-01T00:00:00Z"):
    return {"size": size, "mtime": mtime, "md5": md5}


def _plan(anchor=None, local=None, remote=None, manifest=True,
          local_db=None, remote_db=None, counters=(47, 47, 47)):
    """Build a SyncPlan from trees, the way build_plan would."""
    local = local or {}
    remote = remote or {}
    if not manifest:
        anchor = synthesize_anchor(local, remote)
    anchor = anchor or {}
    lc, ac, rc = counters
    return SyncPlan(
        local_tree=local, remote_tree=remote, anchor=anchor,
        manifest=({"saved": "2026-01-01T00:00:00+00:00", "files": anchor}
                  if manifest else None),
        files=diff_trees(anchor, local, remote),
        local_db=local_db or _entry("db"),
        remote_db=remote_db or _entry("db"),
        local_counter=lc, anchor_counter=ac, remote_counter=rc,
    )


class TestModtime(unittest.TestCase):
    def test_local_and_drive_forms_of_one_instant_agree(self):
        """rclone lists local files with nanoseconds and the local
        offset, Drive files with milliseconds in UTC."""
        local = _modtime_second("2026-04-30T16:22:46.123456789-04:00")
        drive = _modtime_second("2026-04-30T20:22:46.123Z")
        self.assertEqual(local, drive)
        self.assertEqual(local, "2026-04-30T20:22:46Z")

    def test_no_fraction(self):
        self.assertEqual(_modtime_second("2026-01-02T03:04:05Z"),
                         "2026-01-02T03:04:05Z")

    def test_unrecognized_value_fails_loudly(self):
        with self.assertRaises(SyncError):
            _modtime_second("yesterday")


class TestSameFile(unittest.TestCase):
    def test_md5_decides_when_both_have_one(self):
        self.assertTrue(same_file(_entry(mtime="2026-01-01T00:00:00Z"),
                                  _entry(mtime="2026-02-02T00:00:00Z")))
        self.assertFalse(same_file(_entry("aaa"), _entry("bbb")))

    def test_mtime_stands_in_without_md5(self):
        """An rclone crypt remote lists no MD5."""
        self.assertTrue(same_file(_entry(), _entry(md5=None)))
        self.assertFalse(same_file(
            _entry(), _entry(md5=None, mtime="2026-03-03T00:00:00Z")))

    def test_size_mismatch_differs(self):
        self.assertFalse(same_file(_entry(size=1), _entry(size=2)))

    def test_absence(self):
        self.assertTrue(same_file(None, None))
        self.assertFalse(same_file(_entry(), None))
        self.assertFalse(same_file(None, _entry()))


class TestDiffTrees(unittest.TestCase):
    """Each difference is pinned on the side that moved from the anchor."""

    def setUp(self):
        self.anchor = {"cc/a.pdf": _entry("a"), "cc/b.pdf": _entry("b")}

    def test_nothing_changed(self):
        changes = diff_trees(self.anchor, dict(self.anchor),
                             dict(self.anchor))
        self.assertEqual((changes.local, changes.remote,
                          changes.conflicts), ({}, {}, []))

    def test_local_changes(self):
        local = {"cc/a.pdf": _entry("a2"), "cc/c.pdf": _entry("c")}
        changes = diff_trees(self.anchor, local, dict(self.anchor))
        self.assertEqual(changes.local, {"cc/a.pdf": "modified",
                                         "cc/b.pdf": "deleted",
                                         "cc/c.pdf": "added"})
        self.assertEqual(changes.remote, {})

    def test_remote_changes(self):
        remote = {"cc/a.pdf": _entry("a"), "cc/c.pdf": _entry("c")}
        changes = diff_trees(self.anchor, dict(self.anchor), remote)
        self.assertEqual(changes.remote, {"cc/b.pdf": "deleted",
                                          "cc/c.pdf": "added"})
        self.assertEqual(changes.local, {})

    def test_new_local_file_is_a_local_change(self):
        """A statement just dropped in _inbox is a change here, so a
        pull leaves it alone (a mirror would have deleted it)."""
        local = dict(self.anchor, **{"cc/_inbox/new.pdf": _entry("n")})
        changes = diff_trees(self.anchor, local, dict(self.anchor))
        self.assertEqual(changes.local, {"cc/_inbox/new.pdf": "added"})

    def test_both_changed_differently_conflicts(self):
        local = dict(self.anchor, **{"cc/a.pdf": _entry("mine")})
        remote = dict(self.anchor, **{"cc/a.pdf": _entry("theirs")})
        changes = diff_trees(self.anchor, local, remote)
        self.assertEqual(changes.conflicts, ["cc/a.pdf"])

    def test_both_changed_identically_converges(self):
        local = dict(self.anchor, **{"cc/a.pdf": _entry("same")})
        remote = dict(self.anchor, **{"cc/a.pdf": _entry("same")})
        changes = diff_trees(self.anchor, local, remote)
        self.assertEqual((changes.local, changes.remote,
                          changes.conflicts), ({}, {}, []))

    def test_synthesized_anchor_keeps_only_identical_files(self):
        local = {"same.pdf": _entry("s"), "mine.pdf": _entry("m"),
                 "both.pdf": _entry("x")}
        remote = {"same.pdf": _entry("s"), "theirs.pdf": _entry("t"),
                  "both.pdf": _entry("y")}
        anchor = synthesize_anchor(local, remote)
        self.assertEqual(set(anchor), {"same.pdf"})
        changes = diff_trees(anchor, local, remote)
        self.assertEqual(changes.local, {"mine.pdf": "added"})
        self.assertEqual(changes.remote, {"theirs.pdf": "added"})
        self.assertEqual(changes.conflicts, ["both.pdf"])


class TestReanchor(unittest.TestCase):
    def test_partial_push_keeps_the_remote_changes_attributed(self):
        """After a push that left the remote's own change in place,
        that change must still read as the remote's."""
        anchor = {"a.pdf": _entry("a"), "b.pdf": _entry("b")}
        local = {"a.pdf": _entry("a2"), "b.pdf": _entry("b")}
        remote_after = {"a.pdf": _entry("a2"), "b.pdf": _entry("b2")}
        new = reanchor(anchor, local, remote_after)
        self.assertEqual(new["a.pdf"], _entry("a2"))
        self.assertEqual(new["b.pdf"], _entry("b"))
        changes = diff_trees(new, local, remote_after)
        self.assertEqual(changes.remote, {"b.pdf": "modified"})
        self.assertEqual(changes.local, {})

    def test_deleted_on_both_sides_leaves_the_anchor(self):
        new = reanchor({"a.pdf": _entry()}, {}, {})
        self.assertEqual(new, {})


class TestPairRenames(unittest.TestCase):
    def test_same_content_pairs(self):
        before = {"hsa/old.pdf": _entry("p")}
        after = {"hsa/new.pdf": _entry("p")}
        self.assertEqual(
            pair_renames(["hsa/old.pdf"], ["hsa/new.pdf"], before, after),
            {"hsa/old.pdf": "hsa/new.pdf"})

    def test_rewritten_sidecar_follows_its_document(self):
        before = {"hsa/old.pdf": _entry("p"), "hsa/old.json": _entry("j1")}
        after = {"hsa/new.pdf": _entry("p"), "hsa/new.json": _entry("j2")}
        self.assertEqual(
            pair_renames(["hsa/old.pdf", "hsa/old.json"],
                         ["hsa/new.pdf", "hsa/new.json"], before, after),
            {"hsa/old.pdf": "hsa/new.pdf",
             "hsa/old.json": "hsa/new.json"})

    def test_unrelated_changes_stay_unpaired(self):
        before = {"a.pdf": _entry("1")}
        after = {"b.pdf": _entry("2")}
        self.assertEqual(
            pair_renames(["a.pdf"], ["b.pdf"], before, after), {})

    def test_without_md5_size_and_time_pair(self):
        """A rename keeps size and mtime, so a crypt remote still pairs."""
        before = {"a.pdf": _entry(None)}
        self.assertEqual(
            pair_renames(["a.pdf"], ["b.pdf"], before,
                         {"b.pdf": _entry(None)}),
            {"a.pdf": "b.pdf"})
        self.assertEqual(
            pair_renames(["a.pdf"], ["b.pdf"], before,
                         {"b.pdf": _entry(None,
                                          mtime="2026-05-05T00:00:00Z")}),
            {})


class TestLostDeletions(unittest.TestCase):
    """Only deletions whose content survives nowhere need confirmation."""

    def test_rename_loses_nothing(self):
        gone = {"cc/old.pdf": _entry("p")}
        kept = {"cc/new.pdf": _entry("p")}
        self.assertEqual(
            lost_deletions(["cc/old.pdf"], gone, kept, ["cc/new.pdf"]), [])

    def test_move_to_trash_loses_nothing(self):
        gone = {"hsa/2026/x.pdf": _entry("p")}
        kept = {"hsa/_trash/x.pdf": _entry("p")}
        self.assertEqual(lost_deletions(["hsa/2026/x.pdf"], gone, kept,
                                        ["hsa/_trash/x.pdf"]), [])

    def test_duplicate_whose_twin_stays_loses_nothing(self):
        gone = {"cc/copy.pdf": _entry("p")}
        kept = {"cc/original.pdf": _entry("p")}
        self.assertEqual(
            lost_deletions(["cc/copy.pdf"], gone, kept, []), [])

    def test_sidecar_following_a_rename_loses_nothing(self):
        gone = {"hsa/old.pdf": _entry("p"), "hsa/old.json": _entry("j1")}
        kept = {"hsa/new.pdf": _entry("p"), "hsa/new.json": _entry("j2")}
        self.assertEqual(
            lost_deletions(["hsa/old.pdf", "hsa/old.json"], gone, kept,
                           ["hsa/new.pdf", "hsa/new.json"]), [])

    def test_true_deletion_is_lost(self):
        gone = {"cc/only.pdf": _entry("p")}
        self.assertEqual(
            lost_deletions(["cc/only.pdf"], gone, {}, []), ["cc/only.pdf"])


class TestManifest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "finance.db")

    def test_round_trip(self):
        files = {"cc/a.pdf": _entry()}
        save_manifest(self.db_path, "remote:ws", files)
        loaded = load_manifest(self.db_path, "remote:ws")
        self.assertEqual(loaded["files"], files)
        self.assertTrue(os.path.exists(
            os.path.join(os.path.dirname(self.db_path),
                         "finance.sync_manifest")))

    def test_missing_is_none(self):
        self.assertIsNone(load_manifest(self.db_path, "remote:ws"))

    def test_other_remote_anchors_nothing(self):
        """Switching remotes (say, to an encrypted one) starts over."""
        save_manifest(self.db_path, "remote:ws", {"a": _entry()})
        self.assertIsNone(load_manifest(self.db_path, "crypt:ws"))

    def test_corrupt_is_reported_and_ignored(self):
        with open(os.path.join(os.path.dirname(self.db_path),
                               "finance.sync_manifest"), "w") as f:
            f.write("{truncated")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(load_manifest(self.db_path, "remote:ws"))
        self.assertIn("ignoring unreadable sync manifest", err.getvalue())

    def test_private_state_is_never_transferred(self):
        """The anchors belong to one machine; the journal and backups
        travel by their own rules."""
        for pattern in ("*.sync_manifest*", "*.sync_anchor",
                        "*.sync_journal", "data/backups/**"):
            self.assertIn(pattern, RCLONE_EXCLUDES)


class TestMergeJournals(unittest.TestCase):
    def test_union_oldest_first_without_duplicates(self):
        a = {"timestamp": "2026-01-01T00:00:00+00:00", "hostname": "one",
             "direction": "push", "anchor": 1, "files": 2}
        b = {"timestamp": "2026-01-02T00:00:00+00:00", "hostname": "two",
             "direction": "push", "anchor": 2, "files": 1}
        c = {"timestamp": "2026-01-03T00:00:00+00:00", "hostname": "one",
             "direction": "pull", "anchor": 2, "files": 1}
        self.assertEqual(merge_journals([a, c], [a, b]), [a, b, c])

    def test_ignores_garbage(self):
        self.assertEqual(merge_journals(["x", None], []), [])


class TestListTree(unittest.TestCase):
    def _run(self, returncode=0, stdout="[]", stderr=""):
        result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch("housebook.sync.subprocess.run",
                   return_value=result) as run:
            return list_tree("remote:ws"), run

    def test_parses_entries(self):
        stdout = json.dumps([{
            "Path": "cc/a.pdf", "Size": 3, "IsDir": False,
            "ModTime": "2026-01-01T00:00:00.5Z",
            "Hashes": {"md5": "abc"},
        }])
        tree, run = self._run(stdout=stdout)
        self.assertEqual(tree, {"cc/a.pdf": {
            "size": 3, "mtime": "2026-01-01T00:00:00Z", "md5": "abc"}})
        args = run.call_args.args[0]
        self.assertIn("data/backups/**", args)
        self.assertIn("*.sync_journal", args)

    def test_missing_directory_is_an_empty_tree(self):
        for code in (3, 4):
            tree, _ = self._run(returncode=code, stdout="")
            self.assertEqual(tree, {})

    def test_other_failures_raise(self):
        with self.assertRaises(SyncError) as cm:
            self._run(returncode=1, stdout="", stderr="token expired")
        self.assertIn("token expired", str(cm.exception))


class TestDatabaseAttribution(unittest.TestCase):
    """The DB counter, not the manifest, says which side wrote it."""

    def test_identical_dbs_are_clean(self):
        plan = _plan(counters=(50, 47, 47))
        self.assertFalse(plan.db_differs)
        self.assertFalse(plan.has_differences)

    def test_local_write(self):
        plan = _plan(local_db=_entry("new"), counters=(52, 47, 47))
        self.assertTrue(plan.db_local_changed)
        self.assertFalse(plan.db_remote_changed)
        self.assertFalse(plan.conflicted)

    def test_remote_write(self):
        plan = _plan(remote_db=_entry("new"), counters=(47, 47, 52))
        self.assertTrue(plan.db_remote_changed)
        self.assertFalse(plan.db_local_changed)

    def test_both_wrote(self):
        """Equal counters on both sides prove nothing: both moved off
        the anchor, so the files can differ while the counts match."""
        plan = _plan(local_db=_entry("x"), remote_db=_entry("y"),
                     counters=(52, 47, 52))
        self.assertTrue(plan.db_conflict)

    def test_no_counter_anchor_cannot_attribute(self):
        plan = _plan(local_db=_entry("x"), remote_db=_entry("y"),
                     counters=(52, None, 49))
        self.assertTrue(plan.db_conflict)

    def test_no_remote_db_is_a_local_addition(self):
        plan = _plan(counters=(5, None, None))
        plan.remote_db = None
        self.assertTrue(plan.db_local_changed)
        self.assertFalse(plan.db_conflict)


class TestNeedsSide(unittest.TestCase):
    """Without a manifest, a file one side alone has may be new there
    or deleted on the other; merging would resurrect deletions."""

    def test_unanchored_differences_need_a_side(self):
        plan = _plan(local={"stale.pdf": _entry("s")},
                     remote={"new.pdf": _entry("n")}, manifest=False)
        self.assertTrue(plan.needs_side)

    def test_unanchored_but_identical_is_fine(self):
        plan = _plan(local={"a.pdf": _entry()}, remote={"a.pdf": _entry()},
                     manifest=False)
        self.assertFalse(plan.needs_side)

    def test_empty_remote_needs_no_choice(self):
        plan = _plan(local={"a.pdf": _entry()}, manifest=False,
                     counters=(5, None, None))
        plan.remote_db = None
        self.assertFalse(plan.needs_side)

    def test_empty_workspace_needs_no_choice(self):
        plan = _plan(remote={"a.pdf": _entry()}, manifest=False,
                     counters=(None, None, 5))
        plan.local_db = None
        self.assertFalse(plan.needs_side)


class TestRecommendation(unittest.TestCase):
    def setUp(self):
        self.anchor = {"cc/a.pdf": _entry("a")}

    def _rec(self, *args, **kwargs):
        return _recommendation(_plan(*args, **kwargs))

    def test_in_sync(self):
        sym, msg = self._rec(self.anchor, dict(self.anchor),
                             dict(self.anchor))
        self.assertEqual(sym, "=")
        self.assertIn("In sync", msg)

    def test_only_local_changed(self):
        sym, msg = self._rec(self.anchor, {"cc/a.pdf": _entry("a2")},
                             dict(self.anchor))
        self.assertEqual(sym, ">")
        self.assertIn("Safe to push", msg)

    def test_only_remote_changed(self):
        sym, msg = self._rec(self.anchor, dict(self.anchor),
                             {"cc/a.pdf": _entry("a2")})
        self.assertEqual(sym, "<")
        self.assertIn("Safe to pull", msg)

    def test_different_files_on_each_side_merge(self):
        sym, msg = self._rec(
            self.anchor, dict(self.anchor, **{"mine.pdf": _entry("m")}),
            dict(self.anchor, **{"theirs.pdf": _entry("t")}))
        self.assertEqual(sym, "<>")
        self.assertIn("merges", msg)

    def test_same_file_on_both_sides_conflicts(self):
        sym, msg = self._rec(self.anchor, {"cc/a.pdf": _entry("mine")},
                             {"cc/a.pdf": _entry("theirs")})
        self.assertEqual(sym, "!")
        self.assertIn("both sides", msg)

    def test_no_anchor_says_so(self):
        sym, msg = self._rec(None, {"a.pdf": _entry("x")},
                             {"a.pdf": _entry("y")}, manifest=False)
        self.assertEqual(sym, "!")
        self.assertIn("No sync anchor", msg)

    def test_empty_remote(self):
        plan = _plan(None, {"a.pdf": _entry()}, {}, manifest=False,
                     counters=(5, None, None))
        plan.remote_db = None
        sym, msg = _recommendation(plan)
        self.assertEqual(sym, "*")
        self.assertIn("initial sync", msg)


class _CommandHarness(unittest.TestCase):
    """Mocks every rclone call so a test sees what a command attempts."""

    def setUp(self):
        self.anchor = {"cc/a.pdf": _entry("a")}
        self.mocks = {}
        for name, kwargs in [
            ("_run_rclone_logged", {"return_value": (1, 0)}),
            ("_copy_listed", {"return_value": (1, 0)}),
            ("_displace_local", {}),
            ("_push_backups", {}),
            ("_backups_pending", {"return_value": False}),
            ("_record_after", {}),
            ("_remote_journal", {"return_value": []}),
            ("save_pull_counter", {}),
            ("append_journal", {}),
            ("save_journal", {}),
            ("load_journal", {"return_value": []}),
            ("_local_counter", {"return_value": 47}),
            ("backup_database", {"return_value": ""}),
            ("_stdin_is_tty", {"return_value": False}),
        ]:
            p = patch(f"housebook.sync.{name}", **kwargs)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        p = patch("housebook.sync.subprocess.run")
        self.mocks["subprocess.run"] = p.start()
        self.addCleanup(p.stop)
        p = patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)

    def transferred(self) -> bool:
        return any(self.mocks[name].called for name in (
            "_run_rclone_logged", "_copy_listed", "_displace_local",
            "subprocess.run"))

    def copied(self) -> list[str]:
        return [p for call in self.mocks["_copy_listed"].call_args_list
                for p in call.args[2]]


class TestPull(_CommandHarness):
    def test_keeps_a_new_local_file_and_takes_the_remote_change(self):
        """The bug this replaces: a mirror pull deleted every file the
        remote had not seen yet, such as a just-acquired statement."""
        plan = _plan(self.anchor,
                     dict(self.anchor, **{"cc/_inbox/new.pdf": _entry("n")}),
                     dict(self.anchor, **{"cc/b.pdf": _entry("b")}))
        cmd_pull("remote:ws", "/ws", plan=plan)
        self.assertEqual(self.copied(), ["cc/b.pdf"])
        self.mocks["_displace_local"].assert_not_called()
        self.mocks["_run_rclone_logged"].assert_not_called()

    def test_remote_deletion_is_displaced_not_deleted(self):
        plan = _plan(dict(self.anchor, **{"cc/b.pdf": _entry("b")}),
                     dict(self.anchor, **{"cc/b.pdf": _entry("b")}),
                     dict(self.anchor))
        cmd_pull("remote:ws", "/ws", plan=plan)
        self.assertEqual(
            self.mocks["_displace_local"].call_args.args[1], ["cc/b.pdf"])

    def test_leaves_unpushed_db_alone(self):
        plan = _plan(self.anchor, dict(self.anchor), dict(self.anchor),
                     local_db=_entry("new"), counters=(52, 47, 47))
        cmd_pull("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())

    def test_takes_remote_db(self):
        plan = _plan(self.anchor, dict(self.anchor), dict(self.anchor),
                     remote_db=_entry("new"), counters=(47, 47, 52))
        cmd_pull("remote:ws", "/ws", plan=plan)
        self.mocks["backup_database"].assert_called_once()
        args = self.mocks["subprocess.run"].call_args.args[0]
        self.assertEqual(args[:2], ["rclone", "copyto"])

    def test_refuses_a_conflict(self):
        plan = _plan(self.anchor, {"cc/a.pdf": _entry("mine")},
                     {"cc/a.pdf": _entry("theirs")})
        with self.assertRaises(SystemExit):
            cmd_pull("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())

    def test_refuses_without_anchor(self):
        plan = _plan(None, {"stale.pdf": _entry("s")},
                     {"new.pdf": _entry("n")}, manifest=False)
        with self.assertRaises(SystemExit):
            cmd_pull("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())

    def test_force_mirrors_with_backup_dir(self):
        plan = _plan(None, {"stale.pdf": _entry("s")},
                     {"new.pdf": _entry("n")}, manifest=False)
        cmd_pull("remote:ws", "/ws", force=True, plan=plan)
        args = self.mocks["_run_rclone_logged"].call_args.args[0]
        self.assertEqual(args[:3], ["sync", "remote:ws", "/ws"])
        self.assertIn("--backup-dir", args)

    def test_in_sync_anchors_without_transfer(self):
        plan = _plan(None, dict(self.anchor), dict(self.anchor),
                     manifest=False)
        cmd_pull("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())
        self.mocks["_record_after"].assert_called_once()

    def test_dry_run_changes_nothing(self):
        plan = _plan(self.anchor, dict(self.anchor),
                     dict(self.anchor, **{"cc/b.pdf": _entry("b")}))
        cmd_pull("remote:ws", "/ws", dry_run=True, plan=plan)
        self.assertFalse(self.transferred())
        self.mocks["_record_after"].assert_not_called()


class TestPush(_CommandHarness):
    def test_sends_only_this_machines_changes(self):
        """A file another machine added stays: a push touches only the
        paths it planned."""
        plan = _plan(self.anchor, {"cc/a.pdf": _entry("a2")},
                     dict(self.anchor, **{"cc/theirs.pdf": _entry("t")}))
        cmd_push("remote:ws", "/ws", plan=plan)
        self.assertEqual(self.copied(), ["cc/a.pdf"])
        self.mocks["_run_rclone_logged"].assert_not_called()

    def test_rename_needs_no_confirmation(self):
        plan = _plan(self.anchor, {"cc/renamed.pdf": _entry("a")},
                     dict(self.anchor))
        cmd_push("remote:ws", "/ws", plan=plan)
        self.assertEqual(self.copied(), ["cc/renamed.pdf"])
        delete = self.mocks["_run_rclone_logged"].call_args.args[0]
        self.assertEqual(delete[:2], ["delete", "remote:ws"])

    def test_lost_deletion_refused_noninteractive(self):
        plan = _plan(self.anchor, {}, dict(self.anchor))
        with self.assertRaises(SystemExit):
            cmd_push("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())

    def test_lost_deletion_allowed_by_flag(self):
        plan = _plan(self.anchor, {}, dict(self.anchor))
        cmd_push("remote:ws", "/ws", allow_deletions=True, plan=plan)
        delete = self.mocks["_run_rclone_logged"].call_args.args[0]
        self.assertEqual(delete[0], "delete")

    def test_lost_deletion_asks_at_a_terminal(self):
        self.mocks["_stdin_is_tty"].return_value = True
        plan = _plan(self.anchor, {}, dict(self.anchor))
        with patch("builtins.input", return_value="n"):
            with self.assertRaises(SystemExit):
                cmd_push("remote:ws", "/ws", plan=plan)
        with patch("builtins.input", return_value="y"):
            cmd_push("remote:ws", "/ws", plan=plan)
        self.assertTrue(self.mocks["_run_rclone_logged"].called)

    def test_refuses_a_conflict(self):
        plan = _plan(self.anchor, {"cc/a.pdf": _entry("mine")},
                     {"cc/a.pdf": _entry("theirs")})
        with self.assertRaises(SystemExit):
            cmd_push("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())

    def test_force_mirrors_but_still_asks_about_lost_content(self):
        """--force picks a side; it does not also allow deletions."""
        plan = _plan(self.anchor, dict(self.anchor),
                     dict(self.anchor, **{"cc/theirs.pdf": _entry("t")}))
        with self.assertRaises(SystemExit):
            cmd_push("remote:ws", "/ws", force=True, plan=plan)
        cmd_push("remote:ws", "/ws", force=True, allow_deletions=True,
                 plan=plan)
        args = self.mocks["_run_rclone_logged"].call_args.args[0]
        self.assertEqual(args[:3], ["sync", "/ws", "remote:ws"])

    def test_sends_db_and_journal(self):
        plan = _plan(self.anchor, dict(self.anchor), dict(self.anchor),
                     local_db=_entry("new"), counters=(52, 47, 47))
        cmd_push("remote:ws", "/ws", plan=plan)
        copytos = [c.args[0] for c in
                   self.mocks["subprocess.run"].call_args_list]
        self.assertEqual(len(copytos), 2)
        self.assertTrue(copytos[0][3].endswith("data/finance.db"))
        self.assertTrue(copytos[1][3].endswith("finance.sync_journal"))

    def test_failed_push_leaves_no_journal_entry(self):
        plan = _plan(self.anchor, {"cc/a.pdf": _entry("a2")},
                     dict(self.anchor))
        before = [{"direction": "pull"}]
        self.mocks["load_journal"].return_value = before
        self.mocks["_copy_listed"].side_effect = (
            subprocess.CalledProcessError(1, "rclone"))
        with self.assertRaises(subprocess.CalledProcessError):
            cmd_push("remote:ws", "/ws", plan=plan)
        self.assertEqual(self.mocks["save_journal"].call_args.args,
                         (DB_PATH, before))
        self.mocks["_record_after"].assert_not_called()

    def test_backups_only_push_copies_backups(self):
        self.mocks["_backups_pending"].return_value = True
        plan = _plan(self.anchor, dict(self.anchor), dict(self.anchor))
        cmd_push("remote:ws", "/ws", plan=plan)
        self.mocks["_push_backups"].assert_called_once()
        self.mocks["append_journal"].assert_not_called()

    def test_nothing_to_push(self):
        plan = _plan(self.anchor, dict(self.anchor), dict(self.anchor))
        cmd_push("remote:ws", "/ws", plan=plan)
        self.assertFalse(self.transferred())
        self.mocks["_push_backups"].assert_not_called()


class TestBareSync(_CommandHarness):
    def _sync(self, plan, **kwargs):
        with patch("housebook.sync.build_plan", return_value=plan), \
             patch("housebook.sync.cmd_push") as push, \
             patch("housebook.sync.cmd_pull") as pull:
            cmd_sync("remote:ws", "/ws", **kwargs)
        return push, pull

    def test_merges_different_files(self):
        push, pull = self._sync(_plan(
            self.anchor, dict(self.anchor, **{"mine.pdf": _entry("m")}),
            dict(self.anchor, **{"theirs.pdf": _entry("t")})))
        pull.assert_called_once()
        push.assert_called_once()

    def test_only_local_changed_skips_pull(self):
        push, pull = self._sync(_plan(
            self.anchor, {"cc/a.pdf": _entry("a2")}, dict(self.anchor)))
        pull.assert_not_called()
        push.assert_called_once()

    def test_conflict_stops_before_anything(self):
        with self.assertRaises(SystemExit):
            self._sync(_plan(self.anchor, {"cc/a.pdf": _entry("mine")},
                             {"cc/a.pdf": _entry("theirs")}))
        self.assertFalse(self.transferred())

    def test_asks_about_deletions_before_pulling(self):
        plan = _plan(dict(self.anchor, **{"cc/gone.pdf": _entry("g")}),
                     dict(self.anchor),
                     dict(self.anchor, **{"cc/gone.pdf": _entry("g"),
                                          "cc/b.pdf": _entry("b")}))
        with self.assertRaises(SystemExit):
            push, pull = self._sync(plan)
        push, pull = self._sync(plan, allow_deletions=True)
        pull.assert_called_once()
        self.assertEqual(push.call_args.kwargs["confirmed"],
                         frozenset({"cc/gone.pdf"}))

    def test_force_needs_a_direction(self):
        with patch("sys.argv", ["housebook-sync", "--force"]), \
             patch("housebook.sync._check_rclone") as check:
            with self.assertRaises(SystemExit):
                main()
        check.assert_not_called()

    def test_allow_deletions_is_for_pushes(self):
        with patch("sys.argv", ["housebook-sync", "pull",
                                "--allow-deletions"]), \
             patch("housebook.sync._check_rclone") as check:
            with self.assertRaises(SystemExit):
                main()
        check.assert_not_called()


@contextlib.contextmanager
def _quiet_fds():
    """Silence rclone, which writes progress straight to fds 1 and 2."""
    saved = [os.dup(1), os.dup(2)]
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in saved + [devnull]:
            os.close(fd)


@unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
class TestSyncWithRclone(unittest.TestCase):
    """End to end through real rclone, a local directory as the remote.

    The mocked tests pin the decisions; these pin the contract with
    rclone itself: its listing format, filters, --files-from-raw, and
    --backup-dir inside the workspace.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = os.path.join(tmp.name, "ws")
        self.remote = os.path.join(tmp.name, "remote")
        self.db = os.path.join(self.ws, "data", "finance.db")
        os.makedirs(os.path.dirname(self.db))
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        self.write(self.ws, "cc/2026/statement-a.pdf", "a")
        for name, value in (
                ("DB_PATH", self.db),
                ("BACKUP_DIR", os.path.join(self.ws, "data", "backups")),
                ("_stdin_is_tty", lambda: False)):
            p = patch(f"housebook.sync.{name}", value)
            p.start()
            self.addCleanup(p.stop)
        self.run_cmd(cmd_push, self.remote, self.ws)

    @staticmethod
    def write(root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    @staticmethod
    def run_cmd(cmd, *args, **kwargs):
        with _quiet_fds(), patch("builtins.print"):
            cmd(*args, **kwargs)

    def exists(self, root, rel):
        return os.path.exists(os.path.join(root, rel))

    def db_rows(self, path):
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT id FROM t").fetchall()
        conn.close()
        return rows

    def test_pull_keeps_a_new_local_file(self):
        self.write(self.ws, "cc/_inbox/new.pdf", "fresh")
        self.write(self.remote, "cc/2026/theirs.pdf", "t")
        self.run_cmd(cmd_pull, self.remote, self.ws)
        self.assertTrue(self.exists(self.ws, "cc/_inbox/new.pdf"))
        self.assertTrue(self.exists(self.ws, "cc/2026/theirs.pdf"))

    def test_sync_merges_different_files(self):
        self.write(self.ws, "cc/2026/mine.pdf", "m")
        self.write(self.remote, "cc/2026/theirs.pdf", "t")
        self.run_cmd(cmd_sync, self.remote, self.ws)
        for root in (self.ws, self.remote):
            self.assertTrue(self.exists(root, "cc/2026/mine.pdf"))
            self.assertTrue(self.exists(root, "cc/2026/theirs.pdf"))

    def test_pull_carries_a_remote_rename_and_displaces_the_old_name(self):
        os.rename(os.path.join(self.remote, "cc/2026/statement-a.pdf"),
                  os.path.join(self.remote, "cc/2026/statement-b.pdf"))
        self.run_cmd(cmd_pull, self.remote, self.ws)
        self.assertTrue(self.exists(self.ws, "cc/2026/statement-b.pdf"))
        self.assertFalse(self.exists(self.ws, "cc/2026/statement-a.pdf"))
        backups = os.path.join(self.ws, "data", "backups")
        displaced = [d for d in os.listdir(backups)
                     if d.startswith("displaced-")]
        self.assertTrue(self.exists(os.path.join(backups, displaced[0]),
                                    "cc/2026/statement-a.pdf"))

    def test_push_carries_a_local_rename_without_asking(self):
        os.rename(os.path.join(self.ws, "cc/2026/statement-a.pdf"),
                  os.path.join(self.ws, "cc/2026/statement-a2.pdf"))
        self.run_cmd(cmd_push, self.remote, self.ws)
        self.assertTrue(self.exists(self.remote, "cc/2026/statement-a2.pdf"))
        self.assertFalse(self.exists(self.remote, "cc/2026/statement-a.pdf"))

    def test_push_asks_before_losing_content(self):
        os.remove(os.path.join(self.ws, "cc/2026/statement-a.pdf"))
        with self.assertRaises(SystemExit):
            self.run_cmd(cmd_push, self.remote, self.ws)
        self.assertTrue(self.exists(self.remote, "cc/2026/statement-a.pdf"))
        self.run_cmd(cmd_push, self.remote, self.ws, allow_deletions=True)
        self.assertFalse(self.exists(self.remote, "cc/2026/statement-a.pdf"))

    def test_push_keeps_a_file_another_machine_added(self):
        self.write(self.remote, "cc/2026/theirs.pdf", "t")
        self.write(self.ws, "cc/2026/mine.pdf", "m")
        self.run_cmd(cmd_push, self.remote, self.ws)
        self.assertTrue(self.exists(self.remote, "cc/2026/theirs.pdf"))
        self.assertTrue(self.exists(self.remote, "cc/2026/mine.pdf"))

    def test_sync_stops_when_one_file_changed_on_both_sides(self):
        self.write(self.ws, "cc/2026/statement-a.pdf", "mine")
        self.write(self.remote, "cc/2026/statement-a.pdf", "theirs!")
        with self.assertRaises(SystemExit):
            self.run_cmd(cmd_sync, self.remote, self.ws)
        with open(os.path.join(self.ws, "cc/2026/statement-a.pdf")) as f:
            self.assertEqual(f.read(), "mine")
        with open(os.path.join(self.remote,
                               "cc/2026/statement-a.pdf")) as f:
            self.assertEqual(f.read(), "theirs!")

    def test_forced_pull_moves_local_changes_aside(self):
        self.write(self.ws, "cc/2026/mine.pdf", "m")
        self.run_cmd(cmd_pull, self.remote, self.ws, force=True)
        self.assertFalse(self.exists(self.ws, "cc/2026/mine.pdf"))
        backups = os.path.join(self.ws, "data", "backups")
        displaced = [d for d in os.listdir(backups)
                     if d.startswith("displaced-")]
        self.assertTrue(self.exists(os.path.join(backups, displaced[0]),
                                    "cc/2026/mine.pdf"))

    def test_db_write_here_survives_pull_and_reaches_the_remote(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        self.write(self.remote, "cc/2026/theirs.pdf", "t")
        self.run_cmd(cmd_pull, self.remote, self.ws)
        self.assertEqual(self.db_rows(self.db), [(1,)])
        self.run_cmd(cmd_sync, self.remote, self.ws)
        self.assertEqual(
            self.db_rows(os.path.join(self.remote, "data", "finance.db")),
            [(1,)])

    def test_backups_reach_the_remote_additively(self):
        self.write(self.remote, "data/backups/finance.db.other", "theirs")
        self.write(self.ws, "data/backups/finance.db.mine", "mine")
        self.run_cmd(cmd_push, self.remote, self.ws)
        self.assertTrue(self.exists(self.remote,
                                    "data/backups/finance.db.mine"))
        self.assertTrue(self.exists(self.remote,
                                    "data/backups/finance.db.other"))
        self.run_cmd(cmd_pull, self.remote, self.ws)
        self.assertFalse(self.exists(self.ws,
                                     "data/backups/finance.db.other"))

    def test_private_state_stays_and_the_journal_merges(self):
        other = {"timestamp": "2020-01-01T00:00:00+00:00",
                 "hostname": "other-machine", "direction": "push",
                 "anchor": 1, "files": 1}
        journal = os.path.join(self.remote, "data", "finance.sync_journal")
        with open(journal) as f:
            entries = json.load(f)
        with open(journal, "w") as f:
            json.dump([other] + entries, f)
        self.write(self.ws, "cc/2026/mine.pdf", "m")
        self.run_cmd(cmd_push, self.remote, self.ws)
        with open(journal) as f:
            hosts = [e["hostname"] for e in json.load(f)]
        self.assertIn("other-machine", hosts)
        self.assertEqual(hosts.count("other-machine"), 1)
        self.assertGreaterEqual(len(hosts), 3)
        for private in ("finance.sync_anchor", "finance.sync_manifest"):
            self.assertFalse(self.exists(self.remote, f"data/{private}"))

    def test_machine_without_anchor_picks_a_side_once(self):
        os.remove(os.path.join(self.ws, "data", "finance.sync_manifest"))
        self.write(self.ws, "cc/2026/stale.pdf", "s")
        self.write(self.remote, "cc/2026/new.pdf", "n")
        with self.assertRaises(SystemExit):
            self.run_cmd(cmd_sync, self.remote, self.ws)
        self.run_cmd(cmd_pull, self.remote, self.ws, force=True)
        self.assertTrue(self.exists(self.ws, "cc/2026/new.pdf"))
        self.assertFalse(self.exists(self.ws, "cc/2026/stale.pdf"))
        self.run_cmd(cmd_sync, self.remote, self.ws)  # anchored now


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
