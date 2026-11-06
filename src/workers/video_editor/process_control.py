"""Отмена spawn, конечное завершение процесса и ограниченный stderr-хвост."""

from __future__ import annotations

import subprocess
import threading
from typing import Any

from src.utils.system.process_tree import terminate_process_tree


class ProcessControl:
    def __init__(self, cancelled: threading.Event) -> None:
        self.cancelled = cancelled

    def check(self) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Экспорт отменен")

    def spawn(self, cmd: list[str], **kwargs: Any) -> subprocess.Popen:
        self.check()
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, **kwargs)
        if self.cancelled.is_set():
            self.stop(process)
            self.close_pipes(process)
            self.check()
        return process

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        """Сначала terminate (0.5 с), затем kill и обязательный wait (2 с)."""
        terminate_process_tree(process)

    @staticmethod
    def close_pipes(process: subprocess.Popen) -> None:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
