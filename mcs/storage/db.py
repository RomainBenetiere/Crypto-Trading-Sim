"""Connexion SQLite : mode de journal, pragmas, création du schéma."""

from __future__ import annotations

import logging
import platform
import sqlite3
import subprocess
import time
from decimal import Decimal
from importlib import resources
from pathlib import Path

from mcs.config import JournalMode

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 30_000
NETWORK_FILESYSTEMS = frozenset(
    {"smbfs", "cifs", "smb3", "nfs", "nfs4", "afpfs", "webdav", "fuse.sshfs", "9p"}
)

# Les Decimal sont stockés en texte, sans perte de précision ni notation scientifique
# (0.000000000100 et non 1.00E-10, pour rester lisible en SQL).
sqlite3.register_adapter(Decimal, lambda d: format(d, "f"))


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: Path, journal_mode: JournalMode = JournalMode.WAL) -> sqlite3.Connection:
    """Ouvre la base, applique les pragmas et crée ou met à jour le schéma."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if journal_mode is JournalMode.WAL and is_network_path(path):
        log.warning(
            "Base %s sur un partage réseau avec SQLITE_JOURNAL_MODE=WAL : "
            "WAL n'est pas fiable sur un partage réseau, utilisez DELETE.",
            path,
        )
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    applied = conn.execute(f"PRAGMA journal_mode = {journal_mode.value}").fetchone()[0]
    if applied.upper() != journal_mode.value:
        raise RuntimeError(f"Mode de journal {journal_mode.value} refusé par SQLite ({applied}).")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Base en version {version}, plus récente que le code ({SCHEMA_VERSION})."
        )
    if version == SCHEMA_VERSION:
        return
    script = resources.files("mcs.storage").joinpath("schema.sql").read_text(encoding="utf-8")
    try:
        conn.executescript(
            f"""
            BEGIN IMMEDIATE;
            {script}
            INSERT OR IGNORE INTO engine_state (id, updated_at) VALUES (1, {now_ms()});
            PRAGMA user_version = {SCHEMA_VERSION};
            COMMIT;
            """
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class transaction:
    """Transaction explicite (la connexion est en mode autocommit)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")


def is_network_path(path: Path) -> bool:
    fs_type = filesystem_type(path)
    return fs_type is not None and fs_type.lower() in NETWORK_FILESYSTEMS


def filesystem_type(path: Path) -> str | None:
    """Type du système de fichiers contenant `path` (None si indéterminable)."""
    target = path.resolve()
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        if platform.system() == "Linux":
            mounts = [
                (fields[1], fields[2])
                for fields in (
                    line.split() for line in Path("/proc/mounts").read_text().splitlines()
                )
                if len(fields) >= 3
            ]
        elif platform.system() == "Darwin":
            output = subprocess.run(
                ["mount"], capture_output=True, text=True, check=True, timeout=5
            ).stdout
            # Format : "<source> on <point> (<type>, options...)"
            mounts = []
            for line in output.splitlines():
                if " on " in line and " (" in line:
                    rest = line.split(" on ", 1)[1]
                    point, details = rest.rsplit(" (", 1)
                    mounts.append((point, details.split(",")[0].strip(" )")))
        else:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    best: tuple[str, str] | None = None
    for point, fs_type in mounts:
        if target.is_relative_to(point) and (best is None or len(point) > len(best[0])):
            best = (point, fs_type)
    return best[1] if best else None
