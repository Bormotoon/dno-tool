"""Neutral thumbnail timers in the legacy Veyon worker package."""

from .thumbnail_timer import (
    ThumbnailTimer,
    ThumbnailTimerManager,
    get_thumbnail_timer_manager,
    reset_thumbnail_timer_manager,
    VeyonThumbnailTimer,
    VeyonThumbnailTimerManager,
    get_veyon_thumbnail_timer_manager,
    reset_veyon_thumbnail_timer_manager,
)

__all__ = [
    "ThumbnailTimer",
    "ThumbnailTimerManager",
    "get_thumbnail_timer_manager",
    "reset_thumbnail_timer_manager",
    "VeyonThumbnailTimer",
    "VeyonThumbnailTimerManager",
    "get_veyon_thumbnail_timer_manager",
    "reset_veyon_thumbnail_timer_manager",
]
