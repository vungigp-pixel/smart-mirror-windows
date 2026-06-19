import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from smart_mirror import Config, MirrorEngine


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

    def test_extra_replica_content_goes_to_quarantine(self):
        extra = self.replica / "not-in-source.bin"
        extra.write_bytes(b"do not delete permanently")
        self.reconcile_and_sync()
        self.assertFalse(extra.exists())
        recovered = list(self.cfg.quarantine.rglob("not-in-source.bin"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"do not delete permanently")

    def test_dry_run_does_not_change_replica(self):
        (self.source / "planned.txt").write_text("planned", encoding="utf-8")
        self.engine.reconcile()
        self.engine.dry_run = True
        result = self.engine.sync()
        self.assertGreater(result["planned"], 0)
        self.assertFalse((self.replica / "planned.txt").exists())

    def test_same_metadata_but_different_content_is_detected(self):
        source_file = self.source / "collision.bin"
        replica_file = self.replica / "collision.bin"
        source_file.write_bytes(b"AAAA")
        replica_file.write_bytes(b"BBBB")
        timestamp_ns = 1_700_000_000_000_000_000
        os.utime(source_file, ns=(timestamp_ns, timestamp_ns))
        os.utime(replica_file, ns=(timestamp_ns, timestamp_ns))
        self.reconcile_and_sync()
        self.assertEqual(replica_file.read_bytes(), b"AAAA")

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


if __name__ == "__main__":
    unittest.main()
