"""Публичный фасад общего пула производных видеоредактора."""

from .thumbnail_backend import (
    MAX_PREVIEW_TASKS,
    PREVIEW_PARALLEL,
    STRIP_FRAME_TIMEOUT,
    STRIP_H,
    STRIP_N,
    WAVE_H,
    WAVE_TIMEOUT,
    WAVE_W,
    MediaRequest,
    ThumbnailPool,
    ThumbSignals,
    _ThumbTask,
    shutdown_media_pools,
    thumb_path,
)

__all__ = [
    "MAX_PREVIEW_TASKS",
    "PREVIEW_PARALLEL",
    "STRIP_FRAME_TIMEOUT",
    "STRIP_H",
    "STRIP_N",
    "WAVE_H",
    "WAVE_TIMEOUT",
    "WAVE_W",
    "MediaRequest",
    "ThumbnailPool",
    "ThumbSignals",
    "_ThumbTask",
    "shutdown_media_pools",
    "thumb_path",
]
