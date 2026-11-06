"""Пакет диалогов управления настройками KDE Plasma на удалённых хостах."""

"""
KDE Config Dialog Module - Модуль управления настройками KDE.
"""

from src.workers.kde.operation import KDEConfigWorker

from .kde_config_dialog import KDEConfigDialog

__all__ = ["KDEConfigDialog", "KDEConfigWorker"]
