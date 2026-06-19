"""Destructive integration test limited to uniquely named temporary folders."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from smart_mirror import Config, MirrorEngine


def main() -> None:
    token = uuid.uuid4().hex
    workspace = Path(__file__).resolve().parent
    source = workspace / f".integration-source-{token}"
    replica = Path("F:/") / f"SmartMirrorIntegration-{token}"
    quarantine = Path("F:/") / f"SmartMirrorIntegrationTrash-{token}"
    state_dir = workspace / f".integration-state-{token}"
    source.mkdir()
    staging = workspace / f".integration-staging-{token}"
    staging.mkdir()
    replica.mkdir()
    engine = MirrorEngine(
        Config(
            source=source,
            replica=replica,
            database=state_dir / "manifest.sqlite3",
            quarantine=quarantine,
            poll_seconds=1,
            full_reconcile_hours=168,
            trash_retention_days=30,
        )
    )
    try:
        old = source / "old"
        old.mkdir()
        (old / "modified.txt").write_text("v1", encoding="utf-8")
        (source / "deleted.txt").write_text("delete me", encoding="utf-8")
        engine.reconcile()
        first = engine.sync()
        assert first["failed"] == 0, first

        preserved_mtime = (old / "modified.txt").stat().st_mtime_ns
        old.rename(source / "new")
        modified = source / "new" / "modified.txt"
        modified.write_text("v2", encoding="utf-8")
        os.utime(modified, ns=(preserved_mtime, preserved_mtime))
        (source / "created.txt").write_text("created after checkpoint", encoding="utf-8")
        (source / "deleted.txt").unlink()
        populated = staging / "moved-in"
        populated.mkdir()
        (populated / "child.txt").write_text("directory move", encoding="utf-8")
        populated.rename(source / "moved-in")
        (replica / "external.txt").write_text("must be quarantined", encoding="utf-8")

        second = engine.sync()  # No full scan: changes must come from USN.
        assert second["failed"] == 0, second
        assert not (replica / "old").exists()
        assert (replica / "new" / "modified.txt").read_text(encoding="utf-8") == "v2"
        assert (replica / "created.txt").read_text(encoding="utf-8") == "created after checkpoint"
        assert not (replica / "deleted.txt").exists()
        assert not (replica / "external.txt").exists()
        assert (replica / "moved-in" / "child.txt").read_text(encoding="utf-8") == "directory move"
        assert list(quarantine.rglob("deleted.txt"))
        assert list(quarantine.rglob("external.txt"))
        print(f"USN integration test passed: first={first}, second={second}")
    finally:
        engine.close()
        for path in (source, staging, replica, quarantine, state_dir):
            resolved = path.resolve()
            allowed = (
                resolved.parent == workspace
                or resolved.parent == Path("F:/").resolve()
            )
            if allowed and (
                resolved.name.startswith(".integration-")
                or resolved.name.startswith("SmartMirrorIntegration")
            ):
                shutil.rmtree(resolved, ignore_errors=True)


if __name__ == "__main__":
    main()
