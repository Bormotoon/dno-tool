"""Изолированные recovery-сессии, ограничение хранения и защита живых окон."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import psutil

from .project_file import ProjectFileStore


class RecoveryStore:
    TTL = 7 * 24 * 3600
    MAX_FILES = 20
    MAX_BYTES = 256 * 1024 * 1024

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _paths(self, session: str) -> tuple[Path, Path]:
        if len(session) != 32 or any(c not in "0123456789abcdef" for c in session):
            raise ValueError("Некорректный идентификатор recovery-сессии.")
        return (
            self.root / f"session-{session}.dnovideo",
            self.root / f"session-{session}.lock",
        )

    def register(self, session: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _, marker = self._paths(session)
        if not marker.exists():
            with marker.open("x", encoding="utf-8") as file:
                json.dump(
                    {"pid": os.getpid(), "started": psutil.Process().create_time()},
                    file,
                )

    def is_active(self, session: str) -> bool:
        _, marker = self._paths(session)
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            process = psutil.Process(data["pid"])
            return abs(process.create_time() - data["started"]) < 0.01
        except (psutil.NoSuchProcess, FileNotFoundError):
            return False
        except psutil.AccessDenied:
            return True
        except (ValueError, KeyError, TypeError, OSError):
            try:
                return time.time() - marker.stat().st_mtime < 120
            except OSError:
                return False

    def write(self, session: str, snapshot: dict) -> None:
        self.register(session)
        path, _ = self._paths(session)
        with self._budget_lock():

            def admit(size: int) -> None:
                records = []
                for candidate in self.root.glob("session-*.dnovideo"):
                    if candidate == path or candidate.resolve().parent != self.root:
                        continue
                    other = candidate.stem.removeprefix("session-")
                    if len(other) != 32 or any(
                        c not in "0123456789abcdef" for c in other
                    ):
                        continue
                    stat = candidate.stat()
                    records.append((stat.st_mtime, stat.st_size, other))
                used = sum(record[1] for record in records)
                for _, amount, other in sorted(records):
                    if used + size <= self.MAX_BYTES:
                        break
                    if not self.is_active(other):
                        self.clear(other, close=True)
                        used -= amount
                if used + size > self.MAX_BYTES:
                    raise OSError(
                        "Лимит recovery-копий исчерпан живыми проектами. Сохраните или закройте часть проектов."
                    )

            ProjectFileStore.save(snapshot, str(path), admit=admit)

    @contextmanager
    def _budget_lock(self) -> Iterator[None]:
        """Один бюджет для всех процессов; блокировка освобождается и при crash."""
        with (self.root / ".budget.lock").open("a+b") as file:
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
            deadline = time.monotonic() + 2
            while True:
                try:
                    file.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Каталог recovery занят другим процессом.")
                    time.sleep(0.05)
            try:
                yield
            finally:
                file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def clear(self, session: str, close: bool = False) -> None:
        path, marker = self._paths(session)
        path.unlink(missing_ok=True)
        if close:
            marker.unlink(missing_ok=True)

    def discard_inactive(self, path: str) -> None:
        source = Path(path)
        if (
            source.resolve().parent != self.root
            or not source.name.startswith("session-")
            or source.suffix != ".dnovideo"
        ):
            raise ValueError("Recovery-файл находится вне каталога сессий.")
        session = source.stem.removeprefix("session-")
        if not self.is_active(session):
            self.clear(session, close=True)

    def catalog(self, session: str) -> list[dict]:
        self.register(session)
        candidates: list[tuple[float, int, Path, str]] = []
        for path in self.root.glob("session-*.dnovideo"):
            other = path.stem.removeprefix("session-")
            if len(other) != 32 or any(c not in "0123456789abcdef" for c in other):
                continue
            if path.resolve().parent != self.root or self.is_active(other):
                continue
            try:
                stat = path.stat()
                candidates.append((stat.st_mtime, stat.st_size, path, other))
            except OSError:
                continue
        result = []
        used = 0
        retained = 0
        for modified, size, path, other in sorted(candidates, reverse=True):
            if (
                time.time() - modified > self.TTL
                or retained >= self.MAX_FILES
                or used + size > self.MAX_BYTES
            ):
                self.clear(other, close=True)
                continue
            retained += 1
            used += size
            try:
                snapshot = ProjectFileStore.load(str(path))
                result.append(
                    {"path": str(path), "name": snapshot["name"], "modified": modified}
                )
            except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                continue
        for marker in self.root.glob("session-*.lock"):
            other = marker.stem.removeprefix("session-")
            if len(other) != 32 or any(c not in "0123456789abcdef" for c in other):
                continue
            try:
                if (
                    not self._paths(other)[0].exists()
                    and not self.is_active(other)
                    and time.time() - marker.stat().st_mtime > self.TTL
                ):
                    marker.unlink(missing_ok=True)
            except OSError:
                continue
        return result
