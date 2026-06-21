#!/usr/bin/env python3
"""Incremental one-way mirror for local Windows/NTFS volumes.

The SQLite manifest is an index, not the source of truth. Destructive actions
are validated against the filesystem and deletions are moved to quarantine.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat as stat_module
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
FILE_ATTRIBUTE_DIRECTORY = 0x10
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
USN_REASON_HASH_INVALIDATING = (
    0x00000001  # DATA_OVERWRITE
    | 0x00000002  # DATA_EXTEND
    | 0x00000004  # DATA_TRUNCATION
    | 0x00000010  # NAMED_DATA_OVERWRITE
    | 0x00000020  # NAMED_DATA_EXTEND
    | 0x00000040  # NAMED_DATA_TRUNCATION
    | 0x00008000  # BASIC_INFO_CHANGE
    | 0x00020000  # COMPRESSION_CHANGE
    | 0x00040000  # ENCRYPTION_CHANGE
    | 0x00200000  # STREAM_CHANGE
)
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
ERROR_JOURNAL_ENTRY_DELETED = 1181
SCRIPT_DIR = Path(__file__).resolve().parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_key(path: str) -> str:
    return path.replace("/", "\\").strip("\\").casefold()


def native_path(path: str | os.PathLike[str]) -> str:
    """Use Win32 extended paths so reserved names address real NTFS files."""
    value = os.fspath(path)
    if os.name != "nt":
        return os.path.abspath(value)
    if value.startswith("\\\\?\\"):
        return value
    # ntpath.abspath/GetFullPathName interprets a final component such as NUL
    # as a DOS device and returns \\.\nul. Build the absolute path lexically.
    if not os.path.isabs(value):
        value = os.path.join(os.getcwd(), value)
    value = os.path.normpath(value)
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def native_stat(path: str | os.PathLike[str]) -> os.stat_result:
    return os.stat(native_path(path), follow_symlinks=False)


def native_exists(path: str | os.PathLike[str]) -> bool:
    try:
        native_stat(path)
        return True
    except FileNotFoundError:
        return False


def safe_unlink(path: str | os.PathLike[str]) -> None:
    """Delete a temporary file, clearing Windows ReadOnly when necessary."""
    target = native_path(path)
    try:
        os.unlink(target)
    except FileNotFoundError:
        return
    except PermissionError:
        os.chmod(target, stat_module.S_IWRITE)
        os.unlink(target)


def safe_rmtree(path: str | os.PathLike[str]) -> None:
    """Recursively delete a tree, clearing ReadOnly attributes when needed."""

    def clear_readonly_and_retry(function, failed_path, _exc_info):
        os.chmod(failed_path, stat_module.S_IWRITE)
        function(failed_path)

    shutil.rmtree(native_path(path), onerror=clear_readonly_and_retry)


def resolve_config_path(path: Path) -> Path:
    """Resolve relative config paths beside the executable script."""
    return path.resolve() if path.is_absolute() else (SCRIPT_DIR / path).resolve()


def ancestor_path_keys(key: str) -> Iterator[str]:
    """Yield parent path keys from nearest to farthest in O(path depth)."""
    end = len(key)
    while True:
        end = key.rfind("\\", 0, end)
        if end < 0:
            return
        yield key[:end]


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_link_like(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def volume_serial(path: Path) -> int:
    """Return a stable Windows volume serial, or st_dev on other platforms."""
    if os.name != "nt":
        return path.stat().st_dev
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_path = kernel32.GetVolumePathNameW
    get_volume_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_volume_path.restype = ctypes.c_int
    volume_root = ctypes.create_unicode_buffer(261)
    if not get_volume_path(str(path.resolve()), volume_root, len(volume_root)):
        raise ctypes.WinError(ctypes.get_last_error())
    get_info = kernel32.GetVolumeInformationW
    get_info.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    get_info.restype = ctypes.c_int
    serial = ctypes.c_uint32()
    if not get_info(volume_root.value, None, 0, ctypes.byref(serial), None, None, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return serial.value


@dataclass(frozen=True)
class Config:
    source: Path
    replica: Path
    database: Path
    quarantine: Path
    quarantine_flag: bool = True
    poll_seconds: int = 30
    full_reconcile_hours: int = 168
    trash_retention_days: int = 30
    hash_mode: str = "on_copy"
    copy_buffer_mb: int = 4
    max_copy_retries: int = 3
    exclude_dirs: tuple[str, ...] = (
        "$RECYCLE.BIN",
        "System Volume Information",
    )

    @classmethod
    def load(cls, filename: Path) -> "Config":
        raw = json.loads(filename.read_text(encoding="utf-8-sig"))
        required = {"source", "replica", "database"}
        missing = sorted(required.difference(raw))
        if missing:
            raise ValueError(
                f"Config file '{filename}' is missing required keys: "
                + ", ".join(missing)
            )
        quarantine_flag = raw.get("quarantine_flag", True)
        if not isinstance(quarantine_flag, bool):
            raise ValueError("quarantine_flag must be a JSON boolean: true or false")
        source = Path(os.path.abspath(os.path.expandvars(raw["source"])))
        replica = Path(os.path.abspath(os.path.expandvars(raw["replica"])))
        database = Path(os.path.abspath(os.path.expandvars(raw["database"])))
        default_trash = replica.parent / ".smart_mirror_trash"
        quarantine = Path(
            os.path.abspath(os.path.expandvars(raw.get("quarantine", str(default_trash))))
        )
        cfg = cls(
            source=source,
            replica=replica,
            database=database,
            quarantine=quarantine,
            quarantine_flag=quarantine_flag,
            poll_seconds=max(1, int(raw.get("poll_seconds", 30))),
            full_reconcile_hours=max(1, int(raw.get("full_reconcile_hours", 168))),
            trash_retention_days=max(0, int(raw.get("trash_retention_days", 30))),
            hash_mode=raw.get("hash_mode", "on_copy"),
            copy_buffer_mb=max(1, int(raw.get("copy_buffer_mb", 4))),
            max_copy_retries=max(1, int(raw.get("max_copy_retries", 3))),
            exclude_dirs=tuple(raw.get("exclude_dirs", cls.exclude_dirs)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.hash_mode not in {"never", "on_copy"}:
            raise ValueError("hash_mode must be 'never' or 'on_copy'")
        if self.source.resolve() == self.replica.resolve():
            raise ValueError("source and replica must be different")
        if is_relative_to(self.replica, self.source) or is_relative_to(self.source, self.replica):
            raise ValueError("source and replica must not contain each other")
        if is_relative_to(self.database, self.replica):
            raise ValueError("database must be outside replica")
        if is_relative_to(self.database, self.source) and (
            self.database.parent.resolve() == self.source.resolve()
        ):
            raise ValueError(
                "database inside source must use a dedicated subdirectory"
            )
        if self.quarantine_flag:
            if is_relative_to(self.quarantine, self.source) or is_relative_to(
                self.quarantine, self.replica
            ):
                raise ValueError("quarantine must be outside source and replica")
            if self.quarantine.drive.casefold() != self.replica.drive.casefold():
                raise ValueError("quarantine and replica must be on the same volume")


class ManifestDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nodes (
                side TEXT NOT NULL CHECK(side IN ('A','B')),
                file_id INTEGER NOT NULL,
                parent_id INTEGER,
                name TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                path_key TEXT NOT NULL,
                is_dir INTEGER NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                mtime_ns INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                origin_file_id INTEGER,
                present INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL,
                PRIMARY KEY(side, file_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS nodes_present_path
                ON nodes(side, path_key) WHERE present = 1;
            CREATE INDEX IF NOT EXISTS nodes_origin
                ON nodes(side, origin_file_id) WHERE present = 1;
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        version = self.get_meta("schema_version")
        if version not in (None, str(SCHEMA_VERSION)):
            raise RuntimeError(f"Unsupported database schema: {version}")
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def upsert_path(
        self,
        side: str,
        root: Path,
        path: Path,
        *,
        origin_file_id: int | None = None,
        invalidate_hash: bool = False,
    ) -> int:
        stat = native_stat(path)
        rel = "" if path == root else str(path.relative_to(root))
        parent_id = None if path == root else native_stat(path.parent).st_ino
        is_dir = stat_module.S_ISDIR(stat.st_mode) and not is_link_like(path)
        old = self.conn.execute(
            "SELECT rel_path,is_dir,size,mtime_ns,sha256,origin_file_id FROM nodes WHERE side=? AND file_id=?",
            (side, stat.st_ino),
        ).fetchone()
        old_rel = old["rel_path"] if old else None
        keep_hash = bool(
            old
            and old["sha256"]
            and not invalidate_hash
            and old_rel == rel
            and old["size"] == stat.st_size
            and old["mtime_ns"] == stat.st_mtime_ns
        )
        inherited_origin = origin_file_id
        if inherited_origin is None and old:
            inherited_origin = old["origin_file_id"]
        # Atomic replacement gives the destination a new NTFS file ID. Retire
        # the old row at this path before inserting the replacement row.
        self.conn.execute(
            "UPDATE nodes SET present=0,last_seen=? "
            "WHERE side=? AND path_key=? AND file_id<>? AND present=1",
            (utc_now(), side, path_key(rel), stat.st_ino),
        )
        self.conn.execute(
            """
            INSERT INTO nodes(side,file_id,parent_id,name,rel_path,path_key,is_dir,size,
                              mtime_ns,sha256,origin_file_id,present,last_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(side,file_id) DO UPDATE SET
                parent_id=excluded.parent_id,name=excluded.name,rel_path=excluded.rel_path,
                path_key=excluded.path_key,is_dir=excluded.is_dir,size=excluded.size,
                mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,
                origin_file_id=COALESCE(excluded.origin_file_id,nodes.origin_file_id),
                present=1,last_seen=excluded.last_seen
            """,
            (
                side,
                stat.st_ino,
                parent_id,
                path.name if rel else "",
                rel,
                path_key(rel),
                int(is_dir),
                0 if is_dir else stat.st_size,
                stat.st_mtime_ns,
                old["sha256"] if keep_hash else None,
                inherited_origin,
                1,
                utc_now(),
            ),
        )
        if old_rel is not None and old_rel != rel and bool(old["is_dir"]):
            old_prefix = old_rel + os.sep
            rows = self.conn.execute(
                "SELECT file_id,rel_path FROM nodes WHERE side=? AND present=1 AND rel_path LIKE ?",
                (side, old_prefix + "%"),
            ).fetchall()
            for child in rows:
                child_rel = rel + os.sep + child["rel_path"][len(old_prefix) :]
                self.conn.execute(
                    "UPDATE nodes SET rel_path=?,path_key=? WHERE side=? AND file_id=?",
                    (child_rel, path_key(child_rel), side, child["file_id"]),
                )
        return stat.st_ino

    def mark_missing_file_id(self, side: str, file_id: int) -> None:
        row = self.by_file_id(side, file_id)
        if not row or not row["present"]:
            return
        replacement = self.by_path(side, row["rel_path"])
        self.conn.execute(
            "UPDATE nodes SET present=0,last_seen=? WHERE side=? AND file_id=?",
            (utc_now(), side, file_id),
        )
        # A delayed USN delete for an atomically replaced file must not retire
        # the new file that now occupies the same relative path.
        if row["is_dir"] and (not replacement or replacement["file_id"] == file_id):
            prefix = path_key(row["rel_path"]) + "\\"
            self.conn.execute(
                "UPDATE nodes SET present=0,last_seen=? "
                "WHERE side=? AND present=1 AND path_key>=? AND path_key<?",
                (utc_now(), side, prefix, prefix + "\U0010ffff"),
            )

    def mark_missing_path(self, side: str, rel: str, is_dir: bool) -> None:
        key = path_key(rel)
        self.conn.execute(
            "UPDATE nodes SET present=0,last_seen=? WHERE side=? AND path_key=?",
            (utc_now(), side, key),
        )
        if is_dir and rel:
            prefix = key + "\\"
            self.conn.execute(
                "UPDATE nodes SET present=0,last_seen=? "
                "WHERE side=? AND present=1 AND path_key>=? AND path_key<?",
                (utc_now(), side, prefix, prefix + "\U0010ffff"),
            )

    def by_file_id(self, side: str, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM nodes WHERE side=? AND file_id=?", (side, file_id)
        ).fetchone()

    def by_path(self, side: str, rel: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM nodes WHERE side=? AND path_key=? AND present=1",
            (side, path_key(rel)),
        ).fetchone()

    def present(self, side: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE side=? AND present=1", (side,)
        ).fetchall()
        return {row["path_key"]: row for row in rows}

    def scan(
        self, side: str, root: Path, excludes: set[Path], *, invalidate_hashes: bool = False
    ) -> int:
        scan_id = uuid.uuid4().hex
        count = 0
        seen_paths: dict[int, Path] = {}
        self.upsert_path(side, root, root, invalidate_hash=invalidate_hashes)
        seen_paths[native_stat(root).st_ino] = root
        self.conn.execute("UPDATE nodes SET last_seen=? WHERE side=? AND rel_path=''", (scan_id, side))
        count += 1

        def onerror(exc: OSError) -> None:
            raise exc

        for current, dirs, files in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
            current_path = Path(current)
            retained_dirs = []
            for name in dirs:
                directory = current_path / name
                try:
                    if not is_link_like(directory) and directory.resolve() not in excludes:
                        retained_dirs.append(name)
                except FileNotFoundError:
                    continue
            dirs[:] = retained_dirs
            for name in dirs + files:
                item = current_path / name
                try:
                    if is_link_like(item) or item.resolve() in excludes:
                        continue
                    item_stat = native_stat(item)
                except FileNotFoundError:
                    continue
                item_id = item_stat.st_ino
                previous_path = seen_paths.get(item_id)
                if previous_path is not None:
                    previous_still_exists = False
                    try:
                        previous_still_exists = (
                            native_stat(previous_path).st_ino == item_id
                        )
                    except FileNotFoundError:
                        pass
                    if item_stat.st_nlink > 1 or previous_still_exists:
                        raise RuntimeError(f"Hard links are not supported safely: {item}")
                    logging.debug(
                        "Detected rename/move during scan: %s -> %s",
                        previous_path,
                        item,
                    )
                else:
                    count += 1
                seen_paths[item_id] = item
                try:
                    self.upsert_path(side, root, item, invalidate_hash=invalidate_hashes)
                except FileNotFoundError:
                    continue
                self.conn.execute(
                    "UPDATE nodes SET last_seen=? WHERE side=? AND file_id=?",
                    (scan_id, side, item_id),
                )
        self.conn.execute(
            "UPDATE nodes SET present=0 WHERE side=? AND present=1 AND last_seen<>?",
            (side, scan_id),
        )
        self.conn.commit()
        return count

    def scan_subtree(
        self, side: str, root: Path, subtree: Path, excludes: set[Path]
    ) -> int:
        """Upsert one newly discovered directory tree without reconciling A."""
        if subtree.resolve() in excludes:
            return 0
        count = 0
        try:
            subtree_stat = native_stat(subtree)
            if subtree_stat.st_nlink > 1:
                raise RuntimeError(f"Hard links are not supported safely: {subtree}")
            self.upsert_path(side, root, subtree)
            count += 1
        except FileNotFoundError:
            return 0

        def onerror(exc: OSError) -> None:
            if not isinstance(exc, FileNotFoundError):
                raise exc

        for current, dirs, files in os.walk(
            subtree, topdown=True, onerror=onerror, followlinks=False
        ):
            current_path = Path(current)
            retained_dirs = []
            for name in dirs:
                directory = current_path / name
                try:
                    if not is_link_like(directory) and directory.resolve() not in excludes:
                        retained_dirs.append(name)
                except FileNotFoundError:
                    continue
            dirs[:] = retained_dirs
            for name in dirs + files:
                item = current_path / name
                try:
                    if is_link_like(item) or item.resolve() in excludes:
                        continue
                    item_stat = native_stat(item)
                    if item_stat.st_nlink > 1:
                        raise RuntimeError(f"Hard links are not supported safely: {item}")
                    self.upsert_path(side, root, item)
                    count += 1
                    if count % 10_000 == 0:
                        logging.info(
                            "Source subtree scan progress: %s (%d entries)",
                            subtree,
                            count,
                        )
                except FileNotFoundError:
                    continue
        return count

    def bind_origins_by_path(self) -> None:
        self.conn.execute(
            """
            UPDATE nodes AS b SET origin_file_id=(
                SELECT a.file_id FROM nodes AS a
                WHERE a.side='A' AND a.present=1 AND a.path_key=b.path_key
            ) WHERE b.side='B' AND b.present=1 AND b.origin_file_id IS NULL
            """
        )
        self.conn.commit()

    def record(self, action: str, rel: str, status: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO operations(created_at,action,rel_path,status,detail) VALUES(?,?,?,?,?)",
            (utc_now(), action, rel, status, detail),
        )
        self.conn.commit()


@dataclass(frozen=True)
class JournalState:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int


@dataclass(frozen=True)
class UsnRecord:
    file_id: int
    parent_id: int
    usn: int
    reason: int
    attributes: int
    name: str


class UsnJournal:
    """Small ctypes wrapper for NTFS USN_RECORD_V2 journals."""

    def __init__(self, root: Path):
        if os.name != "nt":
            raise OSError("USN Journal is available only on Windows")
        drive = root.drive
        if not drive:
            raise OSError(f"Path has no drive letter: {root}")
        self.volume = "\\\\.\\" + drive.rstrip("\\")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._device_io = kernel32.DeviceIoControl
        self._device_io.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._device_io.restype = ctypes.c_int
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateFileW(
            self.volume,
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0,
            None,
        )
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._close = kernel32.CloseHandle
        self._close.argtypes = [ctypes.c_void_p]
        self._close.restype = ctypes.c_int

    def close(self) -> None:
        if self.handle:
            self._close(self.handle)
            self.handle = None

    def _ioctl(self, code: int, input_bytes: bytes | None, output_size: int) -> bytes:
        out = ctypes.create_string_buffer(output_size)
        returned = ctypes.c_uint32()
        inp = ctypes.create_string_buffer(input_bytes) if input_bytes else None
        ok = self._device_io(
            self.handle,
            code,
            inp,
            len(input_bytes) if input_bytes else 0,
            out,
            output_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return out.raw[: returned.value]

    def state(self) -> JournalState:
        raw = self._ioctl(FSCTL_QUERY_USN_JOURNAL, None, 80)
        journal_id, first, next_usn, lowest = struct.unpack_from("<Qqqq", raw)
        return JournalState(journal_id, first, next_usn, lowest)

    def record_batches(
        self, start_usn: int, journal_id: int
    ) -> Iterator[tuple[int, list[UsnRecord]]]:
        """Yield at most one DeviceIoControl buffer at a time.

        Incremental batches bound memory usage and let callers persist a new
        checkpoint before reading the next portion of a busy journal.
        """
        cursor = start_usn
        while True:
            request = struct.pack("<qIIQQQ", cursor, 0xFFFFFFFF, 0, 0, 0, journal_id)
            raw = self._ioctl(FSCTL_READ_USN_JOURNAL, request, 1024 * 1024)
            if len(raw) < 8:
                return
            next_cursor = struct.unpack_from("<q", raw)[0]
            records: list[UsnRecord] = []
            offset = 8
            while offset + 60 <= len(raw):
                length, major = struct.unpack_from("<IH", raw, offset)
                if length < 60 or offset + length > len(raw):
                    raise OSError("Invalid USN record returned by Windows")
                if major == 2:
                    file_id, parent_id, usn = struct.unpack_from("<QQq", raw, offset + 8)
                    reason = struct.unpack_from("<I", raw, offset + 40)[0]
                    attributes = struct.unpack_from("<I", raw, offset + 52)[0]
                    name_len, name_offset = struct.unpack_from("<HH", raw, offset + 56)
                    name = raw[
                        offset + name_offset : offset + name_offset + name_len
                    ].decode("utf-16-le", errors="strict")
                    records.append(UsnRecord(file_id, parent_id, usn, reason, attributes, name))
                else:
                    raise OSError(f"Unsupported USN record version: {major}")
                offset += length
            yield next_cursor, records
            if next_cursor <= cursor or len(raw) == 8:
                return
            cursor = next_cursor


class MirrorEngine:
    def __init__(self, cfg: Config, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.db = ManifestDB(cfg.database)
        self._exclude_cache: dict[str, set[Path]] = {}

    def close(self) -> None:
        self.db.close()

    def _absolute_excludes(self, root: Path) -> set[Path]:
        cache_key = str(root.resolve()).casefold()
        cached = self._exclude_cache.get(cache_key)
        if cached is not None:
            return cached
        result = {(root / name).resolve() for name in self.cfg.exclude_dirs}
        if is_relative_to(self.cfg.database, root):
            # Exclude SQLite plus its WAL/SHM files and any temporary backup
            # files by excluding the dedicated state directory as a whole.
            result.add(self.cfg.database.parent.resolve())
        if self.cfg.quarantine_flag and is_relative_to(self.cfg.quarantine, root):
            result.add(self.cfg.quarantine.resolve())
        self._exclude_cache[cache_key] = result
        return result

    def _is_excluded_path(self, path: Path, root: Path) -> bool:
        resolved = path.resolve()
        for excluded in self._absolute_excludes(root):
            if resolved == excluded:
                return True
            try:
                resolved.relative_to(excluded)
                return True
            except ValueError:
                pass
        return False

    def reconcile(self) -> None:
        self._validate_roots(initial=True)
        self._initialize_managed_replica_state()
        logging.info("Source reconciliation scan started (replica scan disabled)")
        verify_all = self.db.get_meta("verify_all_required") == "1"
        source_cursor = self._journal_next(self.cfg.source)
        a_count = self.db.scan(
            "A",
            self.cfg.source,
            self._absolute_excludes(self.cfg.source),
            invalidate_hashes=verify_all,
        )
        self.db.bind_origins_by_path()
        self._store_root_identity("A", self.cfg.source)
        self._store_root_identity("B", self.cfg.replica)
        self.db.set_meta("last_full_scan", utc_now())
        self.db.set_meta("verify_all_required", "0")
        if source_cursor:
            self.db.set_meta("A_journal_id", str(source_cursor[0]))
            self.db.set_meta("A_next_usn", str(source_cursor[1]))
        logging.info("Source scan complete: source=%d", a_count)
        # Capture changes that occurred while the scan was in progress.
        self.ingest_journals()

    def _initialize_managed_replica_state(self) -> None:
        """Create an empty expected-state manifest without scanning replica.

        Existing B rows are preserved for migration from the journal-tracked
        implementation. A fresh database may only adopt an empty replica.
        """
        root_row = self.db.by_path("B", "")
        any_b_row = self.db.conn.execute(
            "SELECT 1 FROM nodes WHERE side='B' AND present=1 LIMIT 1"
        ).fetchone()
        if root_row:
            self._mark_replica_as_managed()
            return
        if any_b_row:
            raise RuntimeError("Replica manifest is corrupt: entries exist without a root row")
        with os.scandir(self.cfg.replica) as entries:
            if next(entries, None) is not None:
                raise RuntimeError(
                    "Replica is not empty but has no managed manifest. "
                    "Use an empty replica or migrate an existing database."
                )
        self.db.upsert_path("B", self.cfg.replica, self.cfg.replica)
        self.db.conn.commit()
        self._mark_replica_as_managed()

    def _mark_replica_as_managed(self) -> None:
        # Remove legacy F: journal cursors so the database clearly records that
        # B is expected state, not a volume-journal-backed filesystem snapshot.
        self.db.conn.execute(
            "DELETE FROM meta WHERE key IN ('B_journal_id','B_next_usn')"
        )
        self.db.conn.commit()
        self.db.set_meta("replica_tracking_mode", "managed_expected_state")

    def _journal_next(self, root: Path) -> tuple[int, int] | None:
        try:
            journal = UsnJournal(root)
            try:
                state = journal.state()
                return state.journal_id, state.next_usn
            finally:
                journal.close()
        except OSError as exc:
            logging.warning("USN unavailable for %s: %s", root, exc)
            return None

    def ingest_journals(self) -> bool:
        # B is a managed expected-state manifest. It is updated transactionally
        # after successful operations and never reads the volume-wide F: journal.
        return self._ingest_side("A", self.cfg.source)

    def _ingest_side(self, side: str, root: Path) -> bool:
        stored_id = self.db.get_meta(f"{side}_journal_id")
        stored_usn = self.db.get_meta(f"{side}_next_usn")
        if stored_id is None or stored_usn is None:
            return False
        journal = None
        try:
            journal = UsnJournal(root)
            state = journal.state()
            minimum_readable_usn = max(state.first_usn, state.lowest_valid_usn)
            if int(stored_id) != state.journal_id or int(stored_usn) < minimum_readable_usn:
                self._mark_journal_gap(side)
                return True
            total_records = 0
            batch_start_usn = int(stored_usn)
            try:
                for next_usn, records in journal.record_batches(
                    int(stored_usn), state.journal_id
                ):
                    logging.info(
                        "Processing source USN batch: %d records (checkpoint %d -> %d)",
                        len(records),
                        batch_start_usn,
                        next_usn,
                    )
                    last_record_progress = time.monotonic()
                    for record_index, record in enumerate(records, start=1):
                        self._apply_usn(side, root, record)
                        now = time.monotonic()
                        if now - last_record_progress >= 10:
                            logging.info(
                                "Source USN batch progress: %d/%d records (current: %s)",
                                record_index,
                                len(records),
                                record.name,
                            )
                            last_record_progress = now
                    self.db.conn.commit()
                    self.db.set_meta(f"{side}_next_usn", str(next_usn))
                    total_records += len(records)
                    batch_start_usn = next_usn
                    if total_records and total_records % 100_000 < len(records):
                        logging.info("Ingested %d USN records for %s...", total_records, side)
            except OSError as exc:
                if getattr(exc, "winerror", None) == ERROR_JOURNAL_ENTRY_DELETED:
                    self._mark_journal_gap(side)
                    return True
                raise
            if total_records:
                logging.info("Ingested %d USN records for %s", total_records, side)
            return bool(total_records)
        finally:
            if journal:
                journal.close()

    def _mark_journal_gap(self, side: str) -> None:
        logging.warning(
            "%s USN journal reset, wrapped, or deleted an unread entry; "
            "forcing verified reconciliation",
            side,
        )
        self.db.set_meta("reconcile_required", "1")
        self.db.set_meta("verify_all_required", "1")

    def _apply_usn(self, side: str, root: Path, record: UsnRecord) -> None:
        if record.reason & USN_REASON_RENAME_OLD_NAME:
            return
        parent = self.db.by_file_id(side, record.parent_id)
        existing = self.db.by_file_id(side, record.file_id)
        if parent and parent["present"]:
            rel = os.path.join(parent["rel_path"], record.name) if parent["rel_path"] else record.name
        elif existing:
            rel = existing["rel_path"]
        else:
            # The USN journal is volume-wide. An unknown node with an unknown
            # parent is normally unrelated activity outside the configured root.
            return
        absolute = root / rel
        try:
            if self._is_excluded_path(absolute, root):
                if existing:
                    self.db.mark_missing_file_id(side, record.file_id)
                return
            if native_exists(absolute) and not is_link_like(absolute):
                self.db.upsert_path(
                    side,
                    root,
                    absolute,
                    invalidate_hash=bool(record.reason & USN_REASON_HASH_INVALIDATING),
                )
                if (record.attributes & FILE_ATTRIBUTE_DIRECTORY) and existing is None:
                    # A populated directory may have moved into the configured
                    # root without one USN record per child. Scan only this tree.
                    logging.info(
                        "Scanning newly discovered source subtree: %s", absolute
                    )
                    count = self.db.scan_subtree(
                        side,
                        root,
                        absolute,
                        self._absolute_excludes(root),
                    )
                    logging.info(
                        "Scanned newly discovered source subtree: %s (%d entries)",
                        absolute,
                        count,
                    )
            else:
                self.db.mark_missing_file_id(side, record.file_id)
        except FileNotFoundError:
            self.db.mark_missing_file_id(side, record.file_id)
        except OSError as exc:
            logging.warning("USN path update failed for %s: %s", absolute, exc)
            self.db.set_meta("reconcile_required", "1")

    def _ensure_initialized(self) -> None:
        if self.db.get_meta("last_full_scan") is None:
            self.reconcile()

    def _store_root_identity(self, side: str, root: Path) -> None:
        self.db.set_meta(f"{side}_volume_serial", str(volume_serial(root)))
        self.db.set_meta(f"{side}_root_file_id", str(root.stat(follow_symlinks=False).st_ino))

    def _validate_roots(self, *, initial: bool = False) -> None:
        for side, root in (("A", self.cfg.source), ("B", self.cfg.replica)):
            if not root.is_dir():
                raise RuntimeError(f"{side} root is unavailable; refusing to sync: {root}")
            expected_volume = self.db.get_meta(f"{side}_volume_serial")
            expected_root = self.db.get_meta(f"{side}_root_file_id")
            if expected_volume is not None and int(expected_volume) != volume_serial(root):
                raise RuntimeError(f"{side} volume identity changed; refusing to sync: {root}")
            if expected_root is not None and int(expected_root) != root.stat(follow_symlinks=False).st_ino:
                raise RuntimeError(f"{side} root identity changed; refusing to sync: {root}")
            if not initial and (expected_volume is None or expected_root is None):
                raise RuntimeError(f"{side} root identity is missing; run init/reconcile first")

    def plan(self) -> list[tuple[str, sqlite3.Row | None, sqlite3.Row | None]]:
        source = self.db.present("A")
        replica = self.db.present("B")
        actions: list[tuple[str, sqlite3.Row | None, sqlite3.Row | None]] = []
        source_by_depth = sorted(
            source.items(), key=lambda item: item[1]["rel_path"].count(os.sep)
        )

        # Rename/move before copy. Mapping survives source path changes.
        replica_by_origin = {
            row["origin_file_id"]: row
            for row in replica.values()
            if row["origin_file_id"] is not None
        }
        claimed_replica_keys: set[str] = set()
        moved_directories: dict[str, str] = {}
        moved_source_ids: set[int] = set()
        covered_source_keys: set[str] = set()
        for key, src in source_by_depth:
            if not src["rel_path"]:
                continue
            dst = replica.get(key)
            old = replica_by_origin.get(src["file_id"])
            if dst is None and old and old["path_key"] != key:
                covered = False
                for parent_key in ancestor_path_keys(key):
                    old_parent_key = moved_directories.get(parent_key)
                    if old_parent_key and old["path_key"].startswith(old_parent_key + "\\"):
                        covered = True
                        break
                if covered:
                    covered_source_keys.add(key)
                    claimed_replica_keys.add(old["path_key"])
                    continue
                actions.append(("move", src, old))
                moved_source_ids.add(src["file_id"])
                claimed_replica_keys.add(old["path_key"])
                if src["is_dir"]:
                    moved_directories[key] = old["path_key"]

        for key, src in source_by_depth:
            if not src["rel_path"]:
                continue
            under_moved_directory = any(
                parent_key in moved_directories for parent_key in ancestor_path_keys(key)
            )
            if key in covered_source_keys or under_moved_directory:
                # A parent directory move updates this subtree in the database.
                # A subsequent planning round handles content changes inside it.
                continue
            dst = replica.get(key)
            if dst is None:
                if src["file_id"] not in moved_source_ids:
                    actions.append(("mkdir" if src["is_dir"] else "copy", src, None))
            elif bool(src["is_dir"]) != bool(dst["is_dir"]):
                actions.append(("replace_type", src, dst))
            elif not src["is_dir"] and (
                src["size"] != dst["size"]
                or src["mtime_ns"] != dst["mtime_ns"]
                or (src["sha256"] and dst["sha256"] and src["sha256"] != dst["sha256"])
            ):
                actions.append(("copy", src, dst))
            elif (
                not src["is_dir"]
                and self.cfg.hash_mode == "on_copy"
                and (not src["sha256"] or not dst["sha256"])
            ):
                actions.append(("verify", src, dst))

        extra = [
            row
            for key, row in replica.items()
            if row["rel_path"] and key not in source and key not in claimed_replica_keys
        ]
        extra_keys = {row["path_key"] for row in extra}
        for row in sorted(extra, key=lambda item: item["rel_path"].count(os.sep)):
            parts = Path(row["rel_path"]).parts
            ancestor_keys = {path_key(str(Path(*parts[:i]))) for i in range(1, len(parts))}
            if not ancestor_keys.intersection(extra_keys):
                action = "trash" if self.cfg.quarantine_flag else "delete"
                actions.append((action, None, row))
        return actions

    def sync(self) -> dict[str, int]:
        logging.info("Synchronization started")
        self._ensure_initialized()
        logging.info("Validating source and replica identities...")
        self._validate_roots()
        logging.info("Validating managed replica state...")
        self._initialize_managed_replica_state()
        if self.db.get_meta("reconcile_required") == "1":
            self.reconcile()
            self.db.set_meta("reconcile_required", "0")
        else:
            logging.info("Reading source USN changes...")
            self.ingest_journals()
            if self.db.get_meta("reconcile_required") == "1":
                self.reconcile()
                self.db.set_meta("reconcile_required", "0")
        totals = {"planned": 0, "completed": 0, "skipped": 0, "failed": 0}
        for round_number in range(1, 11):
            planning_started = time.monotonic()
            logging.info("Planning synchronization actions (round %d)...", round_number)
            actions = self.plan()
            planning_seconds = time.monotonic() - planning_started
            totals["planned"] += len(actions)
            logging.info(
                "Planning complete: %d actions in %.2f seconds",
                len(actions),
                planning_seconds,
            )
            if not actions:
                break
            last_progress = time.monotonic()
            for action_index, (action, src, dst) in enumerate(actions, start=1):
                rel = src["rel_path"] if src else dst["rel_path"]
                try:
                    if self.dry_run:
                        logging.info("DRY-RUN %-12s %s", action, rel)
                        continue
                    if action_index == 1:
                        logging.info(
                            "Executing actions: 1/%d %s %s", len(actions), action, rel
                        )
                    if action == "mkdir":
                        self._mkdir(src)
                    elif action == "copy":
                        self._copy(src)
                    elif action == "verify":
                        self._verify_pair(src, dst)
                    elif action == "move":
                        self._move(src, dst)
                    elif action == "replace_type":
                        self._trash(dst)
                        self._mkdir(src) if src["is_dir"] else self._copy(src)
                    elif action in {"trash", "delete"}:
                        self._trash(dst)
                    totals["completed"] += 1
                    self.db.record(action, rel, "completed")
                except FileNotFoundError as exc:
                    if src is None:
                        totals["failed"] += 1
                        self.db.record(action, rel, "failed", repr(exc))
                        logging.exception("Failed %s: %s", action, rel)
                    else:
                        self.db.mark_missing_file_id("A", src["file_id"])
                        self.db.conn.commit()
                        totals["skipped"] += 1
                        self.db.record(action, rel, "skipped", repr(exc))
                        logging.info("Skipped vanished source: %s", rel)
                except Exception as exc:
                    totals["failed"] += 1
                    self.db.record(action, rel, "failed", repr(exc))
                    logging.exception("Failed %s: %s", action, rel)
                now = time.monotonic()
                if action_index == len(actions) or now - last_progress >= 10:
                    logging.info(
                        "Execution progress: %d/%d actions "
                        "(completed=%d skipped=%d failed=%d)",
                        action_index,
                        len(actions),
                        totals["completed"],
                        totals["skipped"],
                        totals["failed"],
                    )
                    last_progress = now
            if self.dry_run or totals["failed"]:
                break
        else:
            logging.warning("Sync stopped after 10 planning rounds")
        self.purge_old_trash()
        return totals

    def _source_path(self, row: sqlite3.Row) -> Path:
        return self.cfg.source / row["rel_path"]

    def _replica_path(self, row: sqlite3.Row) -> Path:
        return self.cfg.replica / row["rel_path"]

    def _verify_source(self, row: sqlite3.Row) -> Path:
        path = self._source_path(row)
        stat = native_stat(path)
        if stat.st_ino != row["file_id"]:
            raise RuntimeError(f"Source changed since manifest update: {path}")
        return path

    def _mkdir(self, src: sqlite3.Row) -> None:
        source = self._verify_source(src)
        destination = self.cfg.replica / src["rel_path"]
        os.makedirs(native_path(destination), exist_ok=True)
        shutil.copystat(native_path(source), native_path(destination), follow_symlinks=False)
        self.db.upsert_path("B", self.cfg.replica, destination, origin_file_id=src["file_id"])
        self.db.conn.commit()

    def _copy(self, src: sqlite3.Row) -> None:
        source = self._verify_source(src)
        destination = self.cfg.replica / src["rel_path"]
        os.makedirs(native_path(destination.parent), exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(self.cfg.max_copy_retries):
            temp = destination.with_name(f".{destination.name}.smartmirror-{uuid.uuid4().hex}.tmp")
            try:
                before = native_stat(source)
                digest = hashlib.sha256() if self.cfg.hash_mode == "on_copy" else None
                with open(native_path(source), "rb") as reader, open(
                    native_path(temp), "xb"
                ) as writer:
                    while chunk := reader.read(self.cfg.copy_buffer_mb * 1024 * 1024):
                        writer.write(chunk)
                        if digest:
                            digest.update(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                after = native_stat(source)
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise RuntimeError("Source changed while being copied")
                shutil.copystat(native_path(source), native_path(temp), follow_symlinks=False)
                os.replace(native_path(temp), native_path(destination))
                source_id = self.db.upsert_path("A", self.cfg.source, source)
                destination_id = self.db.upsert_path(
                    "B", self.cfg.replica, destination, origin_file_id=source_id
                )
                if digest:
                    value = digest.hexdigest()
                    self.db.conn.execute(
                        "UPDATE nodes SET sha256=? WHERE (side='A' AND file_id=?) OR (side='B' AND file_id=?)",
                        (value, source_id, destination_id),
                    )
                self.db.conn.commit()
                return
            except FileNotFoundError:
                try:
                    safe_unlink(temp)
                except OSError as cleanup_exc:
                    logging.warning("Could not remove temporary file %s: %s", temp, cleanup_exc)
                raise
            except Exception as exc:
                last_error = exc
                try:
                    safe_unlink(temp)
                except OSError as cleanup_exc:
                    logging.warning("Could not remove temporary file %s: %s", temp, cleanup_exc)
                if attempt + 1 < self.cfg.max_copy_retries:
                    time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f"Copy failed after retries: {last_error}") from last_error

    @staticmethod
    def _stable_hash(path: Path, buffer_size: int) -> str:
        before = native_stat(path)
        digest = hashlib.sha256()
        with open(native_path(path), "rb") as stream:
            while chunk := stream.read(buffer_size):
                digest.update(chunk)
        after = native_stat(path)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"File changed while hashing: {path}")
        return digest.hexdigest()

    def _verify_pair(self, src: sqlite3.Row, dst: sqlite3.Row) -> None:
        source = self._verify_source(src)
        destination = self._replica_path(dst)
        current = native_stat(destination)
        if current.st_ino != dst["file_id"]:
            raise RuntimeError(f"Destination changed before verification: {destination}")
        buffer_size = self.cfg.copy_buffer_mb * 1024 * 1024
        source_hash = src["sha256"] or self._stable_hash(source, buffer_size)
        destination_hash = dst["sha256"] or self._stable_hash(destination, buffer_size)
        if source_hash != destination_hash:
            self._copy(src)
            return
        self.db.conn.execute(
            "UPDATE nodes SET sha256=? WHERE (side='A' AND file_id=?) OR (side='B' AND file_id=?)",
            (source_hash, src["file_id"], dst["file_id"]),
        )
        self.db.conn.commit()

    def _move(self, src: sqlite3.Row, dst: sqlite3.Row) -> None:
        self._verify_source(src)
        old = self._replica_path(dst)
        new = self.cfg.replica / src["rel_path"]
        if not native_exists(old):
            raise RuntimeError(f"Replica move source is missing: {old}")
        if native_exists(new):
            raise RuntimeError(f"Replica move destination already exists: {new}")
        os.makedirs(native_path(new.parent), exist_ok=True)
        os.replace(native_path(old), native_path(new))
        self.db.upsert_path("B", self.cfg.replica, new, origin_file_id=src["file_id"])
        self.db.conn.commit()

    def _trash(self, dst: sqlite3.Row) -> None:
        path = self._replica_path(dst)
        if not native_exists(path):
            self.db.mark_missing_file_id("B", dst["file_id"])
            self.db.conn.commit()
            return
        current = native_stat(path)
        if current.st_ino != dst["file_id"]:
            raise RuntimeError(f"Refusing to delete changed destination: {path}")
        if self.cfg.quarantine_flag:
            bucket = datetime.now().strftime("%Y-%m-%d")
            target = self.cfg.quarantine / bucket / dst["rel_path"]
            if native_exists(target):
                target = target.with_name(target.name + "." + uuid.uuid4().hex)
            os.makedirs(native_path(target.parent), exist_ok=True)
            os.replace(native_path(path), native_path(target))
        elif dst["is_dir"]:
            safe_rmtree(path)
        else:
            safe_unlink(path)
        self.db.mark_missing_path("B", dst["rel_path"], bool(dst["is_dir"]))
        self.db.conn.commit()

    def purge_old_trash(self) -> None:
        if (
            not self.cfg.quarantine_flag
            or self.dry_run
            or self.cfg.trash_retention_days <= 0
            or not self.cfg.quarantine.exists()
        ):
            return
        cutoff = time.time() - self.cfg.trash_retention_days * 86400
        for child in self.cfg.quarantine.iterdir():
            if child.stat().st_mtime < cutoff:
                if child.is_dir():
                    safe_rmtree(child)
                else:
                    safe_unlink(child)

    def run_forever(self) -> None:
        self._ensure_initialized()
        if self.db.get_meta("A_next_usn") is None:
            raise RuntimeError(
                "Continuous mode requires a readable NTFS USN journal on source. "
                "Run as Administrator and verify that source is NTFS."
            )
        while True:
            try:
                changed = self.ingest_journals()
                last_scan = self.db.get_meta("last_full_scan")
                scan_due = not last_scan or (
                    datetime.now(timezone.utc) - datetime.fromisoformat(last_scan)
                ).total_seconds() >= self.cfg.full_reconcile_hours * 3600
                if scan_due or self.db.get_meta("reconcile_required") == "1":
                    self.reconcile()
                    self.db.set_meta("reconcile_required", "0")
                    changed = True
                if changed or self.plan():
                    totals = self.sync()
                    logging.info("Sync result: %s", totals)
            except Exception:
                logging.exception("Worker cycle failed; will retry")
            time.sleep(self.cfg.poll_seconds)


def configure_logging(log_file: Path | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


class InstanceLock:
    """Hold a process-level lock for one manifest database."""

    def __init__(self, database: Path):
        self.path = database.with_name(database.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            self.file.close()
            raise RuntimeError(
                "Another Smart Mirror process is using this database. "
                "Stop the existing 'run' or 'sync' process before starting another one: "
                f"{database}"
            ) from exc

    def close(self) -> None:
        if self.file.closed:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe incremental one-way folder mirror")
    parser.add_argument("command", choices=("init", "sync", "run", "reconcile", "status"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    config_path = resolve_config_path(args.config)
    try:
        cfg = Config.load(config_path)
    except (OSError, ValueError) as exc:
        parser.error(f"Could not load config '{config_path}': {exc}")
    log_file = Path(os.path.abspath(args.log)) if args.log else None
    if log_file and (
        is_relative_to(log_file, cfg.source) or is_relative_to(log_file, cfg.replica)
    ):
        raise SystemExit("Log file must be outside source and replica to avoid a sync loop")
    configure_logging(log_file, args.verbose)
    logging.info("Config file: %s", config_path)
    logging.info("Manifest database: %s", cfg.database)
    if not cfg.source.is_dir():
        raise SystemExit(f"Source directory does not exist; refusing to create it: {cfg.source}")
    cfg.replica.mkdir(parents=True, exist_ok=True)
    try:
        instance_lock = InstanceLock(cfg.database)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    engine = None
    try:
        logging.info("Opening manifest database...")
        try:
            engine = MirrorEngine(cfg, dry_run=args.dry_run)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold():
                raise SystemExit(
                    "Manifest database is locked by another process. "
                    "Stop the existing Smart Mirror 'run' or 'sync' process, then retry."
                ) from exc
            raise
        logging.info("Manifest database opened")
        if args.command in {"init", "reconcile"}:
            if args.command == "reconcile":
                engine.db.set_meta("verify_all_required", "1")
            engine.reconcile()
        elif args.command == "sync":
            print(json.dumps(engine.sync(), ensure_ascii=False))
        elif args.command == "run":
            engine.run_forever()
        elif args.command == "status":
            print(
                json.dumps(
                    {
                        "last_full_scan": engine.db.get_meta("last_full_scan"),
                        "source_entries": len(engine.db.present("A")),
                        "replica_entries": len(engine.db.present("B")),
                        "replica_tracking_mode": engine.db.get_meta(
                            "replica_tracking_mode"
                        )
                        or "legacy_or_uninitialized",
                        "planned_actions": len(engine.plan()),
                        "reconcile_required": engine.db.get_meta("reconcile_required") == "1",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    finally:
        if engine is not None:
            engine.close()
        instance_lock.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
