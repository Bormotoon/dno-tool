"""Запрет запуска второй копии программы.

Механизм - ``QLockFile`` в каталоге пользователя ``~/.dno-tool/``
(один lock на пользователя ОС). Lock держится всю сессию, поэтому
``setStaleLockTime(0)``: stale определяется только по факту "процесс
владельца мертв" (проверка PID + имени процесса + bootid), без
fallback по возрасту файла. После краша (процесс мертв) ``tryLock()``
сам удаляет stale-файл и занимает lock - ручная чистка не нужна.
Живая первая копия lock не отдает никогда.

Использование (см. ``__main__.py``)::

    guard = SingleInstanceGuard()
    if not guard.try_lock():
        # показать QMessageBox и выйти с кодом 1
    app._single_instance_guard = guard  # держать живым до конца процесса
"""

import os

from PySide6.QtCore import QLockFile

LOCK_FILENAME = "dno-tool.lock"


def default_lock_path() -> str:
    """Путь к lock-файлу (per-user, рядом с settings.json)."""
    from src.config import LOGS_PATH

    folder = LOGS_PATH
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    return os.path.join(folder, LOCK_FILENAME)


class SingleInstanceGuard:
    """Держатель ``QLockFile`` на время жизни приложения."""

    def __init__(self, lock_path: str | None = None) -> None:
        self.lock_path = lock_path or default_lock_path()
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        except OSError:
            pass
        self._lock = QLockFile(self.lock_path)
        # Долгоживущий lock на всю сессию: stale - только мертвый процесс
        # владельца. Без этого дефолт 30с считал бы "похоже stale" живой lock
        # старше 30 секунд, и вторая копия могла бы удалить чужой lock.
        try:
            self._lock.setStaleLockTime(0)
        except Exception:  # noqa: BLE001 - Qt может отсутствовать частично
            pass

    def try_lock(self) -> bool:
        """Занять lock. False - другая копия уже запущена.

        Таймаут 0 - неблокирующая попытка: в биндинге PySide6 аргумент
        обязателен (``tryLock()`` без аргументов бросает TypeError).
        """
        try:
            return bool(self._lock.tryLock(0))
        except Exception:  # noqa: BLE001 - любой сбой = считать lock занятым
            return False

    def lock_error(self) -> int:
        """Код ошибки QLockFile после неудачного try_lock (для лога)."""
        try:
            err = self._lock.error()
            return err.value if hasattr(err, "value") else int(err)
        except Exception:  # noqa: BLE001 - fallback для лога
            return -1

    def lock_info(self) -> dict:
        """PID/hostname/appname владельца lock (для лога и диалога).

        Пустой словарь - информацию получить не удалось (файла уже нет
        или он не в формате QLockFile).
        """
        try:
            pid, hostname, appname = self._lock.getLockInfo()
        except (TypeError, ValueError):
            return {}
        except Exception:  # noqa: BLE001 - нет lock-файла или чужой формат
            return {}
        return {"pid": int(pid), "hostname": str(hostname), "appname": str(appname)}

    def unlock(self) -> None:
        """Освободить lock (обычно не требуется - снимается деструктором)."""
        try:
            self._lock.unlock()
        except Exception:  # noqa: BLE001 - освобождение best-effort
            pass
