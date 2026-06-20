import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_mirror import (
    Config,
    JournalState,
    MirrorEngine,
    UsnRecord,
    native_exists,
    native_path,
    native_stat,
    safe_unlink,
)


class MirrorEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.source = base / "source"
        self.replica = base / "replica"
        self.source.mkdir()
        self.replica.mkdir()
        self.cfg = Config(
            source=self.source,
            replica=self.replica,
            database=base / "state" / "manifest.sqlite3",
            quarantine=base / "trash",
            full_reconcile_hours=168,
            trash_retention_days=30,
        )
        self.engine = MirrorEngine(self.cfg)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def reconcile_and_sync(self):
        self.engine.reconcile()
        result = self.engine.sync()
        self.assertEqual(result["failed"], 0)
        return result

    def test_create_modify_and_delete(self):
        item = self.source / "folder" / "data.txt"
        item.parent.mkdir()
        item.write_text("version one", encoding="utf-8")
        self.reconcile_and_sync()
        self.assertEqual((self.replica / "folder" / "data.txt").read_text(), "version one")

        item.write_text("version two is larger", encoding="utf-8")
        self.reconcile_and_sync()
        self.assertEqual((self.replica / "folder" / "data.txt").read_text(), "version two is larger")

        item.unlink()
        self.reconcile_and_sync()
        self.assertFalse((self.replica / "folder" / "data.txt").exists())
        self.assertTrue(any(path.name == "data.txt" for path in self.cfg.quarantine.rglob("*")))

    def test_directory_rename_is_mirrored(self):
        old_dir = self.source / "old"
        old_dir.mkdir()
        (old_dir / "nested.txt").write_text("unchanged content", encoding="utf-8")
        self.reconcile_and_sync()

        old_dir.rename(self.source / "new")
        self.reconcile_and_sync()
        self.assertFalse((self.replica / "old").exists())
        self.assertEqual((self.replica / "new" / "nested.txt").read_text(), "unchanged content")

    def test_nonempty_unmanaged_replica_is_rejected(self):
        extra = self.replica / "not-in-source.bin"
        extra.write_bytes(b"do not delete permanently")
        with self.assertRaisesRegex(RuntimeError, "Replica is not empty"):
            self.engine.reconcile()
        self.assertTrue(extra.exists())

    def test_dry_run_does_not_change_replica(self):
        (self.source / "planned.txt").write_text("planned", encoding="utf-8")
        self.engine.reconcile()
        self.engine.dry_run = True
        result = self.engine.sync()
        self.assertGreater(result["planned"], 0)
        self.assertFalse((self.replica / "planned.txt").exists())

    def test_reconcile_does_not_scan_external_replica_content(self):
        source_file = self.source / "managed.bin"
        source_file.write_bytes(b"managed")
        self.reconcile_and_sync()
        external = self.replica / "external.bin"
        external.write_bytes(b"not in managed manifest")

        self.engine.reconcile()

        self.assertTrue(external.exists())
        self.assertIsNone(self.engine.db.by_path("B", "external.bin"))

    def test_existing_replica_manifest_is_preserved_during_migration(self):
        source_file = self.source / "existing.bin"
        replica_file = self.replica / "existing.bin"
        source_file.write_bytes(b"already synchronized")
        replica_file.write_bytes(b"already synchronized")
        self.engine.db.upsert_path("B", self.replica, self.replica)
        self.engine.db.upsert_path(
            "B",
            self.replica,
            replica_file,
            origin_file_id=source_file.stat().st_ino,
        )
        self.engine.db.set_meta("B_journal_id", "legacy-id")
        self.engine.db.set_meta("B_next_usn", "12345")
        self.engine.db.conn.commit()

        self.engine.reconcile()

        row = self.engine.db.by_path("B", "existing.bin")
        self.assertIsNotNone(row)
        self.assertEqual(row["size"], len(b"already synchronized"))
        self.assertEqual(
            self.engine.db.get_meta("replica_tracking_mode"),
            "managed_expected_state",
        )
        self.assertIsNone(self.engine.db.get_meta("B_journal_id"))
        self.assertIsNone(self.engine.db.get_meta("B_next_usn"))

    def test_source_rename_during_scan_is_not_misclassified_as_hard_link(self):
        old_path = self.source / "old.bin"
        new_path = self.source / "new.bin"
        old_path.write_bytes(b"moving")

        def moving_walk():
            yield str(self.source), [], [old_path.name]
            old_path.rename(new_path)
            yield str(self.source), [], [new_path.name]

        with patch("smart_mirror.os.walk", return_value=moving_walk()):
            count = self.engine.db.scan("A", self.source, set())

        self.assertEqual(count, 2)  # root plus one physical file
        self.assertIsNone(self.engine.db.by_path("A", old_path.name))
        self.assertIsNotNone(self.engine.db.by_path("A", new_path.name))

    def test_real_hard_link_is_still_rejected(self):
        original = self.source / "original.bin"
        linked = self.source / "linked.bin"
        original.write_bytes(b"hard-linked")
        os.link(original, linked)

        with self.assertRaisesRegex(RuntimeError, "Hard links are not supported"):
            self.engine.db.scan("A", self.source, set())

    def test_database_subdirectory_inside_source_is_excluded_from_scan_and_usn(self):
        internal_db = self.source / "state" / "manifest.sqlite3"
        cfg = Config(
            source=self.source,
            replica=self.replica,
            database=internal_db,
            quarantine=self.cfg.quarantine,
        )
        cfg.validate()
        internal_engine = MirrorEngine(cfg)
        try:
            count = internal_engine.db.scan(
                "A",
                self.source,
                internal_engine._absolute_excludes(self.source),
            )
            self.assertEqual(count, 1)  # source root only
            self.assertIsNone(internal_engine.db.by_path("A", "state"))

            state_dir = internal_db.parent
            internal_engine._apply_usn(
                "A",
                self.source,
                UsnRecord(
                    file_id=state_dir.stat().st_ino,
                    parent_id=self.source.stat().st_ino,
                    usn=1,
                    reason=0,
                    attributes=0x10,
                    name=state_dir.name,
                ),
            )
            self.assertIsNone(internal_engine.db.by_path("A", "state"))
        finally:
            internal_engine.close()

    @unittest.skipUnless(os.name == "nt", "Windows reserved-name behavior")
    def test_reserved_nul_filename_is_copied_as_real_ntfs_file(self):
        source_file = self.source / "nul"
        replica_file = self.replica / "nul"
        payload = b"real file named nul"
        with open(native_path(source_file), "wb") as stream:
            stream.write(payload)
        os.chmod(native_path(source_file), stat.S_IREAD)
        try:
            self.reconcile_and_sync()
            with open(native_path(replica_file), "rb") as stream:
                self.assertEqual(stream.read(), payload)
            self.assertEqual(native_stat(replica_file).st_size, len(payload))
            self.assertNotEqual(native_stat(replica_file).st_ino, 0)
        finally:
            safe_unlink(source_file)
            safe_unlink(replica_file)

    @unittest.skipUnless(os.name == "nt", "Windows ReadOnly behavior")
    def test_safe_unlink_clears_readonly_on_temporary_file(self):
        temp_file = self.replica / ".nul.smartmirror-test.tmp"
        temp_file.write_bytes(b"temporary")
        os.chmod(native_path(temp_file), stat.S_IREAD)

        safe_unlink(temp_file)

        self.assertFalse(native_exists(temp_file))

    def test_replaced_replica_root_is_rejected(self):
        self.engine.reconcile()
        shutil.rmtree(self.replica)
        self.replica.mkdir()
        with self.assertRaisesRegex(RuntimeError, "root identity changed"):
            self.engine.sync()

    def test_large_empty_replica_plan_does_not_degrade_quadratically(self):
        root_a = {
            "file_id": 1,
            "origin_file_id": None,
            "rel_path": "",
            "path_key": "",
            "is_dir": 1,
            "size": 0,
            "mtime_ns": 0,
            "sha256": None,
        }
        root_b = dict(root_a)
        source = {"": root_a}
        replica = {"": root_b}
        for index in range(20_000):
            rel = f"folder\\file-{index:05d}.bin"
            source[rel.casefold()] = {
                "file_id": index + 2,
                "origin_file_id": None,
                "rel_path": rel,
                "path_key": rel.casefold(),
                "is_dir": 0,
                "size": index,
                "mtime_ns": index,
                "sha256": None,
            }
        self.engine.db.present = lambda side: source if side == "A" else replica

        started = time.perf_counter()
        actions = self.engine.plan()
        elapsed = time.perf_counter() - started

        self.assertEqual(len(actions), 20_000)
        self.assertLess(elapsed, 2.0, f"planning took {elapsed:.2f}s")

    def test_usn_checkpoint_older_than_first_usn_requests_reconciliation(self):
        self.engine.db.set_meta("A_journal_id", "7")
        self.engine.db.set_meta("A_next_usn", "50")

        class FakeJournal:
            def __init__(self, _root):
                pass

            def state(self):
                return JournalState(journal_id=7, first_usn=100, next_usn=200, lowest_valid_usn=0)

            def close(self):
                pass

        with patch("smart_mirror.UsnJournal", FakeJournal):
            changed = self.engine._ingest_side("A", self.source)

        self.assertTrue(changed)
        self.assertEqual(self.engine.db.get_meta("reconcile_required"), "1")
        self.assertEqual(self.engine.db.get_meta("verify_all_required"), "1")

    def test_deleted_source_usn_entry_error_requests_reconciliation(self):
        self.engine.db.set_meta("A_journal_id", "11")
        self.engine.db.set_meta("A_next_usn", "100")

        class FakeJournal:
            def __init__(self, _root):
                pass

            def state(self):
                return JournalState(journal_id=11, first_usn=0, next_usn=200, lowest_valid_usn=0)

            def record_batches(self, _start_usn, _journal_id):
                error = OSError("journal entry deleted")
                error.winerror = 1181
                raise error
                yield  # pragma: no cover

            def close(self):
                pass

        with patch("smart_mirror.UsnJournal", FakeJournal):
            changed = self.engine._ingest_side("A", self.source)

        self.assertTrue(changed)
        self.assertEqual(self.engine.db.get_meta("reconcile_required"), "1")
        self.assertEqual(self.engine.db.get_meta("verify_all_required"), "1")

    def test_ingest_journals_never_opens_replica_journal(self):
        self.engine.db.set_meta("A_journal_id", "9")
        self.engine.db.set_meta("A_next_usn", "100")
        opened_roots = []

        class FakeJournal:
            def __init__(self, root):
                opened_roots.append(root)

            def state(self):
                return JournalState(journal_id=9, first_usn=0, next_usn=200, lowest_valid_usn=0)

            def record_batches(self, _start_usn, _journal_id):
                yield 200, []

            def close(self):
                pass

        with patch("smart_mirror.UsnJournal", FakeJournal):
            changed = self.engine.ingest_journals()

        self.assertFalse(changed)
        self.assertEqual(opened_roots, [self.source])


if __name__ == "__main__":
    unittest.main()
