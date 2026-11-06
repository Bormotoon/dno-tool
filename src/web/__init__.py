"""
Веб-сервер управления DNO Tool.

Предоставляет REST API и WebSocket для удалённого управления устройствами
и выполнения команд через веб-браузер. Запускается как фоновый поток
внутри PySide6-приложения и переиспует существующие сервисы и исполнители
команд (SSHWorker, SFTPWorker, LocalWorker).
"""

from .server import WebServer, get_web_server
from .app import create_app

__all__ = [
    'WebServer',
    'get_web_server',
    'create_app',
]