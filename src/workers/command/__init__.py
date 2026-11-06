"""Пакет исполнителей команд — SSH, SFTP и локальные команды.

Экспортирует CommandWorker (оркестратор), BaseCommandExecutor (базовый класс),
SSHWorker, SFTPWorker и LocalWorker."""

from .orchestrator import CommandWorker
from .executor_base import BaseCommandExecutor
from .ssh import SSHWorker
from .sftp import SFTPWorker
from .local import LocalWorker

__all__ = [
    'CommandWorker',
    'BaseCommandExecutor',
    'SSHWorker',
    'SFTPWorker',
    'LocalWorker',
]
