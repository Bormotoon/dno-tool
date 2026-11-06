"""
Dependency Injection Module - Модуль внедрения зависимостей.
"""

from .container import DIContainer, get_container, reset_container

__all__ = [
    'DIContainer',
    'get_container',
    'reset_container',
]
