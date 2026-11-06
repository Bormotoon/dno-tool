"""
Сервер uvicorn, остановка которого не ждёт клиентов, не отвечающих на закрытие.

Uvicorn при остановке закрывает простаивающие соединения и ждёт их потери до
timeout_graceful_shutdown. Закрытие TLS ждёт close_notify от клиента, а браузер и
клиент нод держат соединение между запросами и не читают его: сервер ждал весь срок
и писал «Cancel 0 running task(s), timeout graceful shutdown exceeded».

Запросы в работе по-прежнему получают весь срок. Когда их задачи завершились,
соединениям даётся короткий срок дописать буфер, затем они разрываются.

С redirect_http=True порт с TLS отвечает на обычный HTTP перенаправлением на
https:// (src/web/https_redirect.py).

Сообщения uvicorn (логгер ``uvicorn``) передаются в журнал программы: с
``log_config=None`` у него нет обработчиков, и причина неудачного запуска (занятый
порт, запрет доступа) терялась. Последняя ошибка запоминается по имени потока
сервера и показывается пользователю.
"""

import asyncio
import logging
import re
import sys
import threading
import time

from src.logger import logger

# Сколько ждать штатного закрытия после завершения всех запросов.
CLOSE_GRACE_SEC = 0.5
# Сколько ждать запуска сервера. Сервер, не успевший запуститься, останавливается:
# иначе он поднялся бы позже и занял порт, пока окно показывает «Остановлен».
START_TIMEOUT_SEC = 30.0

# Коды ошибок привязки: Windows (WSA*), Linux, macOS.
_ADDRESS_IN_USE = {10048, 98, 48}
_ACCESS_DENIED = {10013, 13}
_ERRNO = re.compile(r"\[(?:Errno|WinError) (\d+)\]")


class _UvicornLogRelay(logging.Handler):
    """Передаёт сообщения uvicorn в журнал программы и помнит ошибки потоков."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.setFormatter(logging.Formatter("%(message)s"))
        self._errors: dict[str, str] = {}
        self._errors_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            text = str(record.msg)
        logger.log(record.levelno, f"uvicorn [{record.threadName}]: {text}")
        if record.levelno >= logging.ERROR:
            lines = record.getMessage().strip().splitlines()
            with self._errors_lock:
                self._errors[record.threadName] = lines[-1] if lines else ""

    def forget(self, thread_name: str) -> None:
        with self._errors_lock:
            self._errors.pop(thread_name, None)

    def take(self, thread_name: str) -> str:
        with self._errors_lock:
            return self._errors.pop(thread_name, "")


_relay_lock = threading.Lock()
_relay: _UvicornLogRelay | None = None


def uvicorn_log_relay() -> _UvicornLogRelay:
    """Подключить передачу сообщений uvicorn в журнал (один раз на процесс)."""
    global _relay
    with _relay_lock:
        if _relay is None:
            _relay = _UvicornLogRelay()
            logging.getLogger("uvicorn").addHandler(_relay)
        return _relay


def wait_for_startup(server, thread, stop_event=None, timeout=None) -> str:
    """Дождаться запуска: started, exited (поток завершился), stopped или timeout."""
    deadline = time.monotonic() + (START_TIMEOUT_SEC if timeout is None else timeout)
    while True:
        if server.started:
            return "started"
        if not thread.is_alive():
            return "exited"
        if stop_event is not None and stop_event.is_set():
            return "stopped"
        if time.monotonic() >= deadline:
            return "timeout"
        thread.join(0.05)


def startup_failure_message(port: int, outcome: str, detail: str) -> str:
    """Понятная причина неудачного запуска по итогу ожидания и ошибке uvicorn."""
    if outcome == "timeout":
        return (
            f"Сервер не запустился за {START_TIMEOUT_SEC:.0f} с и остановлен "
            f"(порт {port})."
        )
    if outcome == "stopped":
        return "Запуск сервера отменён."
    match = _ERRNO.search(detail or "")
    code = int(match.group(1)) if match else None
    if code in _ADDRESS_IN_USE:
        return f"Порт {port} уже занят другой программой."
    if code in _ACCESS_DENIED:
        return (
            f"Доступ к порту {port} запрещён: порт зарезервирован системой "
            "или требует прав администратора."
        )
    if detail:
        return f"Не удалось запустить сервер на порту {port}: {detail}"
    return f"Не удалось запустить сервер на порту {port}. Подробности в журнале."


def _abort(connection) -> None:
    # Закрытие обычно уже начато uvicorn; abort прерывает ожидание ответа клиента.
    transport = getattr(connection, "transport", None)
    if transport is not None:
        transport.abort()


def create_uvicorn_server(cfg, redirect_http: bool = False):
    """Создать uvicorn.Server с быстрым завершением простаивающих соединений.

    redirect_http: при настроенном TLS отвечать на HTTP перенаправлением на https://.
    """
    import uvicorn
    from uvicorn.server import STARTUP_FAILURE, logger as uvicorn_logger

    uvicorn_log_relay()

    class GracefulUvicornServer(uvicorn.Server):
        async def startup(self, sockets=None) -> None:
            config = self.config
            if not redirect_http or config.ssl is None or sockets is not None:
                await super().startup(sockets)
                return
            from src.web.https_redirect import HttpsRedirectListener, bind_sockets

            try:
                listeners = bind_sockets(config.host, config.port, config.backlog)
            except OSError as error:
                uvicorn_logger.error(f"{error} (адрес {config.host}:{config.port})")
                sys.exit(STARTUP_FAILURE)
            try:
                # Пустой список: uvicorn запускает lifespan, но порт не открывает.
                await super().startup(sockets=[])
            except BaseException:
                for sock in listeners:
                    sock.close()
                raise
            loop = asyncio.get_running_loop()

            def create_protocol():
                return config.http_protocol_class(
                    config=config,
                    server_state=self.server_state,
                    app_state=self.lifespan.state,
                    _loop=loop,
                )

            listener = HttpsRedirectListener(listeners, create_protocol, config.ssl)
            listener.start(loop)
            self.servers = [listener]

        async def _wait_tasks_to_complete(self) -> None:
            state = self.server_state
            # Задачи — запросы и WebSocket в работе: им остаётся весь срок остановки.
            while state.tasks and not self.force_exit:
                await asyncio.sleep(0.1)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CLOSE_GRACE_SEC
            while state.connections and not self.force_exit and loop.time() < deadline:
                await asyncio.sleep(0.05)
            # Оставшиеся соединения ждут только ответа клиента на закрытие.
            for connection in list(state.connections):
                _abort(connection)
            await super()._wait_tasks_to_complete()

    return GracefulUvicornServer(cfg)
