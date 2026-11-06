"""Межпоточное и межпроцессное владение именем результата."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class OutputLease:
    _lock = threading.Lock()
    _active: set[str] = set()

    def __init__(self, target: Path) -> None:
        self.key = os.path.normcase(str(target.resolve()))
        self.file: BinaryIO | None = None
        self._registered = False
        self._locked = False
        self._path: Path | None = None

    @staticmethod
    def _acquire(file: BinaryIO) -> None:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @classmethod
    @contextmanager
    def _registry(cls, root: Path) -> Iterator[None]:
        """Защитить создание/удаление lock-файлов от гонки с новым владельцем."""
        with (root / "registry.lock").open("a+b") as registry:
            deadline = time.monotonic() + 2
            while True:
                try:
                    cls._acquire(registry)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            yield

    def __enter__(self) -> OutputLease:
        with self._lock:
            if self.key in self._active:
                raise ValueError("Этот результат уже обрабатывается другим заданием.")
            self._active.add(self.key)
            self._registered = True
        try:
            root = Path(tempfile.gettempdir()) / "dno-video-output-locks"
            root.mkdir(exist_ok=True)
            path = root / (hashlib.sha256(self.key.encode()).hexdigest() + ".lock")
            self._path = path
            with self._registry(root):
                self.file = path.open("a+b")
                self._acquire(self.file)
                self._locked = True
            return self
        except Exception as error:
            self.__exit__(None, None, None)
            raise ValueError(
                "Не удалось занять имя результата; возможно, его обрабатывает другой процесс."
            ) from error

    def __exit__(self, *_: object) -> None:
        if self.file is not None:
            try:
                if self._locked and self._path is not None:
                    with self._registry(self._path.parent):
                        self.file.close()
                        self._path.unlink(missing_ok=True)
                else:
                    self.file.close()
            except OSError:
                self.file.close()
            finally:
                self.file = None
                self._locked = False
        if self._registered:
            with self._lock:
                self._active.discard(self.key)
                self._registered = False
