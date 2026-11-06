"""Отменяемый ICMP-опрос для инструмента «Пингер»."""

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from threading import Event

from PySide6.QtCore import QThread, Signal


def parse_ping_delay(output: str) -> float | None:
    """Извлечь RTT ответа, не принимая сводку и ICMP-ошибки за ответ."""
    match = re.search(
        r"(?:time|время)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)",
        output,
        re.IGNORECASE,
    )
    if match is None:
        return None
    value = float(match[2].replace(",", "."))
    return value


class PingThread(QThread):
    """Один последовательный опрос хоста с ограниченным ожиданием процесса."""

    ping_result = Signal(float, datetime, bool)
    error = Signal(str)

    def __init__(self, target: str, interval: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self.target = target
        self.interval = interval
        self._stop_event = Event()

    def run(self) -> None:
        """Опросить хост; отмена и дедлайн проверяются каждые 100 мс."""
        windows = sys.platform == "win32"
        command = (
            ["ping", "-n", "1", "-w", "1000", self.target]
            if windows
            else ["ping", "-n", "-c", "1", "-W", "1", self.target]
        )
        environment = None if windows else {**os.environ, "LC_ALL": "C"}
        while not self._stop_event.is_set():
            started = time.monotonic()
            process = None
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
                )
                deadline = started + 3.0
                while True:
                    try:
                        output, _ = process.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        if self._stop_event.is_set() or time.monotonic() >= deadline:
                            process.kill()
                            output, _ = process.communicate()
                            break
                if self._stop_event.is_set():
                    break
                decoded = output.decode(
                    "cp866" if windows else "utf-8", errors="replace"
                )
                delay = parse_ping_delay(decoded)
                self.ping_result.emit(
                    delay if delay is not None else -1.0,
                    datetime.now(timezone.utc),
                    bool(re.search(r"(?:time|время)\s*<", decoded, re.IGNORECASE)),
                )
            except OSError as exc:
                self.error.emit(str(exc))
                break
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()
            self._stop_event.wait(
                max(0.0, self.interval - (time.monotonic() - started))
            )

    def stop(self) -> None:
        """Запросить остановку без ожидания в GUI-потоке."""
        self._stop_event.set()
