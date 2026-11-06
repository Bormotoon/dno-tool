"""Общая политика показа: один последний кадр и размеры без увеличения."""

from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar

FrameT = TypeVar("FrameT")


def camera_frame_size(
    source: tuple[int, int], tile: tuple[int, int], maximum: tuple[int, int]
) -> tuple[int, int]:
    """Вписать исходник в физические размеры плитки и выбранный потолок."""
    width, height = source
    if width <= 0 or height <= 0:
        return 1, 1
    scale = min(
        1.0,
        max(1, tile[0]) / width,
        max(1, tile[1]) / height,
        maximum[0] / width,
        maximum[1] / height,
    )
    return max(1, int(width * scale)), max(1, int(height * scale))


def camera_fit_size(
    source: tuple[int, int], tile: tuple[int, int]
) -> tuple[int, int]:
    """Вписать исходник в плитку с сохранением пропорций, растягивая при нужде.

    В отличие от camera_frame_size не ограничивает масштаб единицей и не
    зависит от потолка декодирования: потолок задаёт размер текстуры,
    а показ всегда занимает всю плитку.
    """
    width, height = source
    if width <= 0 or height <= 0:
        return 1, 1
    scale = min(max(1, tile[0]) / width, max(1, tile[1]) / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


class CameraFrameScheduler(Generic[FrameT]):
    """Пропускать кадры до копирования; не повторять последний на редком источнике."""

    def __init__(self, fps: int = 25) -> None:
        self._lock = threading.Lock()
        self._frame: FrameT | None = None
        self._next: float | None = None
        self._fps = 25
        self._closed = False
        self.received_at = 0.0
        self.configure(fps)

    def configure(self, fps: int) -> None:
        """Применить частоту со следующего отбора без сброса декодера."""
        if type(fps) is not int or fps not in (1, 3, 12, 25):
            raise ValueError("Частота просмотра: 1, 3, 12 или 25 кадров/с")
        with self._lock:
            if fps != self._fps:
                self._next = None
            self._fps = fps

    def offer(self, frame: FrameT, now: float | None = None) -> None:
        """Заменить ожидающий кадр; освободить старую поверхность вне блокировки."""
        with self._lock:
            if self._closed:
                return
            previous, self._frame = self._frame, frame
            self.received_at = time.monotonic() if now is None else now
        del previous

    def take_due(self, now: float | None = None) -> FrameT | None:
        """Забрать только новый кадр, если наступило время следующего показа."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._frame is None or (
                self._next is not None and now + 1e-9 < self._next
            ):
                return None
            frame, self._frame = self._frame, None
            interval = 1 / self._fps
            # Сохранять фазу при джиттере; не накапливать долг после зависания GUI.
            self._next = (
                self._next + interval
                if self._next is not None and now - self._next < interval
                else now + interval
            )
            return frame

    def clear(self, *, close: bool = False) -> None:
        """Отпустить кадры перед освобождением декодера и видеовыхода."""
        with self._lock:
            previous, self._frame = self._frame, None
            self._next = None
            self._closed = close
            self.received_at = 0.0
        del previous
