"""Видеоредактор: модальный диалог базового нелинейного монтажа."""

from src.utils.system.qt_multimedia import configure_video_decoding

configure_video_decoding()

from .dialog import VideoEditorDialog

__all__ = ["VideoEditorDialog"]
