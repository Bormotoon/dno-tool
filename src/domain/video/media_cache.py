"""Общий дисковый LRU медиапроизводных с межпроцессными арендами."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psutil


class MediaDiskCache:
    MAX_BYTES = 256 * 1024 * 1024
    MAX_ENTRIES = 4096
    MAX_ARTIFACT = 2 * 1024 * 1024
    MAX_AGE = 7 * 86400
    VERSION = 3

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path("~/.dno-tool/video-cache").expanduser()

    @staticmethod
    def fingerprint(source: str) -> tuple[str, int, int, int, int]:
        path = Path(source).resolve()
        stat = path.stat()
        return (
            os.path.normcase(str(path)),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )

    @classmethod
    def key(
        cls, fingerprint: tuple, mode: str, position: float, extra: dict, binary: tuple
    ) -> str:
        raw = json.dumps(
            (cls.VERSION, fingerprint, mode, position, extra, binary),
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def path(self, key: str) -> Path:
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Недопустимый ключ медиакэша.")
        return self.root / f"{key}.png"

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.root / "index.sqlite3", timeout=2)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, size INTEGER NOT NULL, accessed REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS leases (token TEXT PRIMARY KEY, key TEXT NOT NULL, owner TEXT NOT NULL, pid INTEGER NOT NULL, started REAL NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _reap(self, connection: sqlite3.Connection) -> None:
        for pid, started in connection.execute(
            "SELECT DISTINCT pid, started FROM leases"
        ).fetchall():
            try:
                active = abs(psutil.Process(pid).create_time() - started) < 0.001
            except psutil.NoSuchProcess:
                active = False
            except psutil.AccessDenied:
                active = True
            if not active:
                abandoned = connection.execute(
                    "SELECT token, key, pid FROM leases WHERE pid=? AND started=?",
                    (pid, started),
                ).fetchall()
                connection.execute(
                    "DELETE FROM leases WHERE pid=? AND started=?", (pid, started)
                )
                for token, key, owner_pid in abandoned:
                    self._cleanup_orphan(connection, token, key, owner_pid, dead=True)

    def _cleanup_orphan(
        self,
        connection: sqlite3.Connection,
        token: str,
        key: str,
        pid: int,
        *,
        dead: bool = False,
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM files WHERE key=? UNION SELECT 1 FROM leases WHERE key=?",
                (key, key),
            ).fetchone()
            is None
        ):
            self.path(key).unlink(missing_ok=True)
        if (
            dead
            and len(token) == 32
            and all(char in "0123456789abcdef" for char in token)
        ):
            root = self.root.resolve()
            for job in self.root.glob(f".job-{pid}-{token}-*"):
                resolved = job.resolve()
                if (
                    resolved.parent == root
                    and not job.is_symlink()
                    and not getattr(job, "is_junction", lambda: False)()
                ):
                    shutil.rmtree(resolved)

    @staticmethod
    def _lease(
        connection: sqlite3.Connection, key: str, token: str, owner: str
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO leases VALUES (?, ?, ?, ?, ?)",
            (token, key, owner, os.getpid(), psutil.Process().create_time()),
        )

    def borrow(self, key: str, token: str, owner: str) -> str | None:
        path = self.path(key)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT size FROM files WHERE key=?", (key,)
            ).fetchone()
            if row is None or not path.is_file() or path.stat().st_size != row[0]:
                connection.execute("DELETE FROM files WHERE key=?", (key,))
                self._reap(connection)
                if (
                    connection.execute(
                        "SELECT 1 FROM leases WHERE key=?", (key,)
                    ).fetchone()
                    is None
                ):
                    self._admit(connection, self.MAX_ARTIFACT)
                self._lease(connection, key, token, owner)
                return None
            self._lease(connection, key, token, owner)
            connection.execute(
                "UPDATE files SET accessed=? WHERE key=?", (time.time(), key)
            )
        return str(path)

    def _admit(self, connection: sqlite3.Connection, amount: int) -> None:
        used, count = connection.execute(
            "SELECT COALESCE(SUM(size),0), COUNT(*) FROM files"
        ).fetchone()
        reserved = connection.execute(
            "SELECT COUNT(DISTINCT key) FROM leases WHERE key NOT IN (SELECT key FROM files)"
        ).fetchone()[0]
        used += reserved * self.MAX_ARTIFACT
        count += reserved
        candidates = connection.execute(
            "SELECT key, size, accessed FROM files WHERE key NOT IN (SELECT key FROM leases) ORDER BY accessed"
        ).fetchall()
        for candidate, size, accessed in candidates:
            if (
                used + amount <= self.MAX_BYTES
                and count < self.MAX_ENTRIES
                and time.time() - accessed <= self.MAX_AGE
            ):
                break
            try:
                self.path(candidate).unlink(missing_ok=True)
            except OSError:
                continue
            connection.execute("DELETE FROM files WHERE key=?", (candidate,))
            used -= size
            count -= 1
        if used + amount > self.MAX_BYTES or count >= self.MAX_ENTRIES:
            raise ValueError("Медиакэш занят активными заданиями; повторите позже.")

    def publish(self, key: str, staging: Path, token: str, owner: str) -> str:
        target = self.path(key)
        size = staging.stat().st_size
        if not 0 < size <= self.MAX_ARTIFACT:
            raise ValueError("Производный файл превысил бюджет медиакэша.")
        self.validate_artifact(staging)
        with self._transaction() as connection:
            self._reap(connection)
            existing = connection.execute(
                "SELECT size FROM files WHERE key=?", (key,)
            ).fetchone()
            if existing and target.is_file():
                self._lease(connection, key, token, owner)
                connection.execute(
                    "UPDATE files SET accessed=? WHERE key=?", (time.time(), key)
                )
                return str(target)
            connection.execute("DELETE FROM files WHERE key=?", (key,))
            connection.execute("DELETE FROM leases WHERE token=?", (token,))
            self._admit(connection, size)
            now = time.time()
            os.replace(staging, target)
            connection.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (key, size, now)
            )
            self._lease(connection, key, token, owner)
        return str(target)

    def validate_artifact(self, staging: Path) -> None:
        """Проверка формата отделена для кэшей производных видеофайлов."""
        with staging.open("rb") as source:
            if source.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("Некорректный PNG медиакэша.")

    def release(self, *, token: str = "", owner: str = "") -> None:
        if not (self.root / "index.sqlite3").is_file():
            return
        with self._transaction() as connection:
            rows = []
            if token:
                rows = connection.execute(
                    "SELECT token, key, pid FROM leases WHERE token=?", (token,)
                ).fetchall()
                connection.execute("DELETE FROM leases WHERE token=?", (token,))
            elif owner:
                rows = connection.execute(
                    "SELECT token, key, pid FROM leases WHERE owner=?", (owner,)
                ).fetchall()
                connection.execute("DELETE FROM leases WHERE owner=?", (owner,))
            for identity, key, pid in rows:
                self._cleanup_orphan(connection, identity, key, pid)

    def stats(self) -> tuple[int, int, int]:
        with self._transaction() as connection:
            size, count = connection.execute(
                "SELECT COALESCE(SUM(size),0), COUNT(*) FROM files"
            ).fetchone()
            leases = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        return size, count, leases
