"""Kill-switch: необратимая очистка данных этой копии программы.

Удаляет весь каталог ``~/.dno-tool`` (settings.json с ключом ноды и связками
учётных данных, hosts.json, commands.json, логи, кэши), перед удалением
перезаписывая содержимое файлов нулями. Каталог ``~/.ssh`` (ключи,
known_hosts, authorized_keys) удаляется так же и целиком.

Перед очисткой копия программы блокируется: отметка хранится вне
``~/.dno-tool`` и переживает очистку (``src.utils.security.ban``).

Модуль не зависит от Qt. После очистки процесс завершается без штатного
shutdown, чтобы конфигурация из памяти не была записана обратно.
"""

import os
import shutil
import threading
import time
from pathlib import Path

from src.config.settings import LOGS_PATH
from src.logger import logger

_wipe_lock = threading.Lock()
_wipe_started = False


def _overwrite_file(path: Path) -> None:
    """Перезаписать содержимое файла нулями (best effort)."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as handle:
            remaining = size
            block = b"\0" * 65536
            while remaining > 0:
                chunk = min(remaining, len(block))
                handle.write(block[:chunk])
                remaining -= chunk
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def wipe_data_dir(directory: str | None = None) -> bool:
    """Удалить каталог данных; True, если он больше не существует."""
    root = Path(directory or LOGS_PATH)
    if not root.exists():
        return True
    # Только .dno-tool или .ssh: защита от неверного пути.
    if root.name not in (".dno-tool", ".ssh"):
        logger.error("Kill-switch: неожиданный каталог данных, очистка отменена")
        return False
    for current, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            file_path = Path(current, name)
            if not file_path.is_symlink():
                _overwrite_file(file_path)
    for _attempt in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return True
        time.sleep(0.3)
    return not root.exists()


def schedule_wipe_and_exit(delay_sec: float = 0.5) -> bool:
    """Запустить очистку в фоне и завершить процесс; повторный вызов — no-op.

    Задержка нужна, чтобы HTTP-ответ инициатору успел уйти.
    """
    global _wipe_started
    with _wipe_lock:
        if _wipe_started:
            return False
        _wipe_started = True

    # Блокировка действует в процессе сразу, даже если отметку записать не удалось.
    from src.utils.security.ban import ban_instance

    try:
        if not ban_instance():
            logger.error("Kill-switch: отметка блокировки не записана")
    except Exception as error:
        logger.error(f"Kill-switch: ошибка записи блокировки: {type(error).__name__}")

    def _run() -> None:
        time.sleep(delay_sec)
        try:
            wipe_data_dir(os.path.join(os.path.expanduser("~"), ".ssh"))
            wipe_data_dir()
        finally:
            os._exit(0)

    threading.Thread(target=_run, name="killswitch", daemon=False).start()
    return True
