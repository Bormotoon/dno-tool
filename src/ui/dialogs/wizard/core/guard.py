"""ThreadGuard - единая точка учета фоновых потоков мастера.

Заменяет разбросанные _is_remote_install_active / _wait_veyon_threads /
_try_close_background старого монолита. Правила:
- каждый QThread мастера регистрируется здесь (thread или waiter-функция);
- ручные флаги занятости (remote-установка) - через set_busy;
- навигация и закрытие диалога блокируются пока is_active();
- закрытие всегда идет через shutdown_all() (abort + wait).
"""

from typing import Callable, Dict

from src.logger import logger


class ThreadGuard:
    """Учет потоков без владения виджетами."""

    def __init__(self):
        self._threads: Dict[str, dict] = {}
        self._waiters: Dict[str, dict] = {}
        self._busy: Dict[str, bool] = {}

    # --- регистрация ---

    def register_thread(self, name: str, thread, timeout_ms: int, abort=None) -> None:
        """Зарегистрировать QThread. abort - опциональный callable без аргументов."""
        existing = self._threads.get(name)
        if (
            existing
            and existing["thread"] is not thread
            and existing["thread"].isRunning()
        ):
            raise RuntimeError(f"Операция {name} ещё выполняется")
        self._threads[name] = {
            "thread": thread,
            "timeout_ms": timeout_ms,
            "abort": abort,
        }

    def unregister_thread(self, name: str) -> None:
        existing = self._threads.get(name)
        if (
            existing
            and existing["thread"] is not None
            and existing["thread"].isRunning()
        ):
            raise RuntimeError(f"Операция {name} ещё выполняется")
        self._threads.pop(name, None)

    def register_waiter(
        self, name: str, fn: Callable[[int], bool], timeout_ms: int, abort=None
    ) -> None:
        """Зарегистрировать функцию ожидания fn(timeout_ms) -> bool."""
        self._waiters[name] = {"fn": fn, "timeout_ms": timeout_ms, "abort": abort}

    def request_stop(self):
        """Request shutdown without waiting in the GUI thread."""
        for info in list(self._threads.values()) + list(self._waiters.values()):
            abort = info.get("abort")
            if abort:
                try:
                    abort()
                except Exception:
                    logger.exception("Wizard cancellation failed")
            thread = info.get("thread")
            if thread is not None:
                thread.requestInterruption()
                thread.quit()

    def unregister_waiter(self, name: str) -> None:
        self._waiters.pop(name, None)

    def set_busy(self, name: str, active: bool) -> None:
        self._busy[name] = bool(active)

    def is_busy(self, name: str) -> bool:
        return bool(self._busy.get(name, False))

    # --- состояние ---

    def _any_thread_running(self) -> bool:
        for info in self._threads.values():
            try:
                if info["thread"] is not None and info["thread"].isRunning():
                    return True
            except Exception:
                continue
        return False

    def is_active(self) -> bool:
        """True - навигация и закрытие заблокированы."""
        if any(self._busy.values()):
            return True
        if self._any_thread_running():
            return True
        for name, info in list(self._waiters.items()):
            try:
                # Неблокирующая проверка: waiter возвращает False,
                # если хотя бы один из его потоков ещё работает.
                if not info["fn"](0):
                    return True
            except Exception as e:
                logger.warning("Wizard guard: cannot check %s: %s", name, e)
                return True
        return False

    # --- завершение ---

    def shutdown_all(self) -> bool:
        """Остановить все зарегистрированное и дождаться завершения.

        Returns:
            True - все остановлено, диалог можно закрывать.
            False - что-то еще живо после таймаута, закрывать НЕЛЬЗЯ.
        """
        ok = True
        for name, info in list(self._threads.items()):
            thread = info.get("thread")
            if thread is None:
                continue
            try:
                abort = info.get("abort")
                if abort is not None:
                    try:
                        abort()
                    except Exception as e:
                        logger.warning(f"Wizard guard: abort {name} failed: {e}")
                # run()-потоки (переопределенный run) на quit() не реагируют,
                # но вызов безвреден для event-потоков.
                try:
                    thread.quit()
                except Exception:
                    pass
                if thread.isRunning():
                    if not thread.wait(info.get("timeout_ms", 5000)):
                        logger.warning(f"Wizard guard: thread {name} still running")
                        ok = False
            except Exception as e:
                logger.warning(f"Wizard guard: shutdown {name} failed: {e}")
                ok = False
        for name, info in list(self._waiters.items()):
            try:
                if not info["fn"](info.get("timeout_ms", 5000)):
                    logger.warning(f"Wizard guard: waiter {name} timed out")
                    ok = False
            except Exception as e:
                logger.warning(f"Wizard guard: waiter {name} failed: {e}")
                ok = False
        return ok
