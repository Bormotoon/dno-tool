"""Доставка результатов медиапула только актуальному поколению проекта."""

from __future__ import annotations

from PySide6.QtCore import QObject, Slot


class MediaResultBridge(QObject):
    def __init__(self, owner: QObject, generation: int) -> None:
        super().__init__(owner)
        self._owner = owner
        self._generation = generation

    @Slot(str, str, str)
    def ready(self, key: str, path: str, mode: str) -> None:
        if (
            self._generation == self._owner._media_generation
            and not self._owner._closing
        ):
            pool = self._owner._thumb_pool
            request = pool.resolve(key, mode)
            try:
                if request is not None and self._owner._media_request_is_current(request):
                    self._owner._on_thumb_ok(request.key, path, mode)
                elif request is not None:
                    self._owner._media_request_discarded(request)
            finally:
                pool.acknowledge(key)

    @Slot(str, str, str)
    def failed(self, key: str, mode: str, error: str) -> None:
        if (
            self._generation == self._owner._media_generation
            and not self._owner._closing
        ):
            pool = self._owner._thumb_pool
            request = pool.resolve(key, mode)
            try:
                if request is not None:
                    self._owner._on_thumb_failed(request.key, mode, error)
            finally:
                pool.acknowledge(key)
