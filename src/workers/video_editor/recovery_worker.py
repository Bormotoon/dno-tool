"""Последовательные операции recovery вне GUI."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from src.domain.video.recovery import RecoveryStore


class RecoveryWorker(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        root: str,
        session: str,
        operation: str,
        snapshot: dict | None = None,
        discard: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.root, self.session, self.operation = root, session, operation
        self.snapshot, self.discard = snapshot, discard
        self.success = False

    def run(self) -> None:
        try:
            store = RecoveryStore(self.root)
            if self.operation == "catalog":
                catalog = store.catalog(self.session)
                self.success = True
                self.result.emit(catalog)
                return
            if self.operation == "write":
                store.write(self.session, self.snapshot)
            else:
                store.clear(self.session, close=self.operation == "close")
            if self.discard:
                store.discard_inactive(self.discard)
            self.success = True
        except Exception as exc:
            self.error.emit(str(exc))
