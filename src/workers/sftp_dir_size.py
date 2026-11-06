"""Асинхронный подсчёт размера каталога для SFTP и локальных путей."""

import os
from pathlib import PurePosixPath
from stat import S_ISDIR
from typing import Optional

from PySide6.QtCore import QThread, Signal

from src.logger import logger


class DirSizeWorker(QThread):
    """Рекурсивный подсчёт размера локального или удалённого каталога.

    Сигнал ``result(int)`` эмитится с итоговым размером в байтах.
    """

    result = Signal(int)
    finished_ok = Signal()

    def __init__(self, sftp: Optional[object], path: str, is_remote: bool, parent=None):
        super().__init__(parent)
        self.sftp = sftp
        self.path = path
        self.is_remote = is_remote
        self._cancelled = False

    def run(self) -> None:
        try:
            if self.is_remote:
                size = self._size_remote(self.path)
            else:
                size = self._size_local(self.path)
            if not self._cancelled:
                self.result.emit(size)
        except Exception as e:
            logger.error(f"DirSizeWorker error: {e}")
        finally:
            if not self._cancelled:
                self.finished_ok.emit()

    def _size_local(self, path: str) -> int:
        total = 0
        try:
            for entry in os.scandir(path):
                if self._cancelled:
                    break
                if entry.is_dir(follow_symlinks=False):
                    total += self._size_local(entry.path)
                else:
                    try:
                        total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def _size_remote(self, path: str) -> int:
        if self.sftp is None:
            return 0
        total = 0
        try:
            for entry in self.sftp.listdir_attr(path):
                if self._cancelled:
                    break
                mode = getattr(entry, "st_mode", None)
                if mode is not None and S_ISDIR(mode):
                    total += self._size_remote(
                        str(PurePosixPath(path).joinpath(entry.filename))
                    )
                else:
                    size = getattr(entry, "st_size", None)
                    if size is not None:
                        total += size
        except Exception:
            pass
        return total

    def cancel(self) -> None:
        self._cancelled = True
