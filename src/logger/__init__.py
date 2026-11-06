"""Модуль логирования — экспортирует глобальный логгер."""

from .logger import logger, install_qt_message_handler, is_debug_mode

__all__ = ["logger", "install_qt_message_handler", "is_debug_mode"]
