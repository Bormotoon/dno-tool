"""
WebServer — встроенный HTTP/WebSocket сервер на базе uvicorn + FastAPI.

Запускается в отдельном потоке внутри PySide6-приложения. Переиспользует
глобальные сервисы (DeviceService, CommandService, ConfigService) и
исполнители команд (SSHWorker, SFTPWorker, LocalWorker) для удалённого
управления через веб-браузер.

start, stop и restart могут ждать секунды и не вызываются из GUI-потока, кроме
запуска приложения. То, что обязано создаваться в GUI-потоке, готовит
prepare_on_gui_thread.
"""

import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from src.logger import logger
from src.config import config

if TYPE_CHECKING:
    import uvicorn


class WebServer:
    """Встроенный веб-сервер управления."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None
        self._lock = threading.RLock()
        self._started = False
        self._stop_event = threading.Event()
        # Установлен, пока запуск не идёт: остановка дожидается его окончания.
        self._not_starting = threading.Event()
        self._not_starting.set()
        # Причина последней неудачи запуска; текст показывается пользователю.
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_running(self) -> bool:
        # Без блокировки: свойство читают GUI и event loop сервера нод, пока другой
        # поток выполняет запуск или остановку.
        thread = self._thread
        return self._started and thread is not None and thread.is_alive()

    @property
    def is_starting(self) -> bool:
        return not self._not_starting.is_set()

    def prepare_on_gui_thread(self) -> None:
        """Создать в GUI-потоке всё, что нельзя создать в рабочем.

        Мост записи камер создаётся только в GUI-потоке, а первое разрешение сервиса
        через DI могло бы создать QObject с принадлежностью к рабочему потоку.
        """
        from src.di import get_container
        from src.services import (
            CameraService,
            CommandService,
            ConfigService,
            DeviceService,
            HostControlService,
            VeyonService,
        )
        from src.services.thumbnails.thumbnail_service import ThumbnailService

        container = get_container()
        for service in (
            DeviceService,
            CommandService,
            ConfigService,
            VeyonService,
            HostControlService,
            ThumbnailService,
        ):
            container.resolve_optional(service)
        container.resolve(CameraService).web_recording_bridge()

    def start(self, host: str = None, port: int = None) -> bool:
        """Запустить веб-сервер в фоновом потоке.

        Не запускается если список пользователей пуст (multi-user only).
        Возвращает False и логирует причину.
        """
        with self._lock:
            if self.is_starting:
                self._last_error = "Запуск сервера уже выполняется."
                logger.warning("WebServer: запуск уже выполняется")
                return False
            if self.is_running:
                self._last_error = "Сервер уже запущен."
                logger.warning("WebServer: уже запущен")
                return False
            if self._thread is not None and self._thread.is_alive():
                self._last_error = (
                    "Предыдущий сервер ещё завершается, повторите через несколько секунд."
                )
                logger.warning("WebServer: previous server thread is still stopping")
                return False

            # Multi-user only: не запускать, если войти некому.
            from src.web.auth import has_web_accounts

            if not has_web_accounts():
                self._last_error = "Нет пользователей веб-доступа и не включён вход через LDAP."
                logger.warning(
                    "WebServer: запуск отменён — нет пользователей и LDAP выключен."
                )
                return False
            self._not_starting.clear()
            self._stop_event.clear()
            self._last_error = ""
        # Блокировка не удерживается на время запуска: иначе остальные методы
        # ждали бы секунды, пока сервер поднимается.
        try:
            return self._start_server(host, port)
        finally:
            self._not_starting.set()

    def _start_server(self, host: str | None, port: int | None) -> bool:
        # Ленивый импорт uvicorn/fastapi (чтобы не тянуть при простое)
        import uvicorn

        from src.web.app import create_app

        bind_host = host or config.app.web.host or "0.0.0.0"
        bind_port = int(port or config.app.web.port or 8765)

        app = create_app()

        # Внедряем сервисы из DI-контейнера приложения
        try:
            from src.di import get_container
            from src.services import (
                DeviceService,
                CommandService,
                ConfigService,
                VeyonService,
                HostControlService,
                CameraService,
            )

            container = get_container()
            camera_service = container.resolve(CameraService)
            camera_service.web_recording_bridge()
            from src.services.thumbnails.thumbnail_service import ThumbnailService
            from .app import configure_services

            veyon = None
            host_control = None
            try:
                veyon = container.resolve(VeyonService)
            except Exception as e:
                logger.warning(f"WebServer: VeyonService недоступен: {e}")
            try:
                host_control = container.resolve(HostControlService)
            except Exception as e:
                logger.warning(f"WebServer: HostControlService недоступен: {e}")
            configure_services(
                container.resolve(DeviceService),
                container.resolve(CommandService),
                container.resolve(ConfigService),
                veyon_service=veyon,
                host_control_service=host_control,
                thumbnail_service=container.resolve(ThumbnailService),
                camera_service=camera_service,
            )
        except Exception as e:
            # Сервер без сервисов на каждый запрос отвечал бы ошибкой.
            logger.error(f"WebServer: не удалось внедрить сервисы: {e}")
            return False

        if self._stop_event.is_set():
            return False

        cfg = uvicorn.Config(
            app,
            host=bind_host,
            port=bind_port,
            log_level="warning",
            access_log=False,
            workers=1,
            timeout_graceful_shutdown=5,
            # Отключаем встроенную конфигурацию логирования uvicorn:
            # она использует dictConfig с dotted-path форматтерами
            # (uvicorn.logging.DefaultFormatter), которые не резолвятся
            # в PyInstaller frozen-бинаре → 'Unable to configure
            # formatter default'. dno-tool имеет собственный логгер.
            log_config=None,
        )
        from src.utils.network.urls import web_base_url
        from src.utils.security.web_tls import WebTlsError, configure_uvicorn

        try:
            tls_enabled = configure_uvicorn(cfg, config.app.web)
        except WebTlsError as error:
            # Перехода на HTTP нет: пользователь рассчитывает на шифрование.
            self._last_error = f"HTTPS: {error}"
            logger.error(f"WebServer: {self._last_error}")
            return False
        from src.web.uvicorn_server import (
            create_uvicorn_server,
            startup_failure_message,
            uvicorn_log_relay,
            wait_for_startup,
        )

        # Браузер держит соединение между запросами и на закрытие TLS не отвечает.
        # С HTTPS адрес http:// на том же порту перенаправляется на https://.
        server = create_uvicorn_server(cfg, redirect_http=tls_enabled)

        def _run():
            try:
                server.run()
            except Exception as e:
                logger.error(f"WebServer: ошибка в потоке сервера: {e}")
            finally:
                with self._lock:
                    self._started = False

        thread = threading.Thread(target=_run, name="dno-tool-web", daemon=True)
        relay = uvicorn_log_relay()
        relay.forget(thread.name)
        with self._lock:
            self._server = server
            self._thread = thread
            self._started = True
        thread.start()

        # Ждём, пока сервер запустится или его поток завершится. Не успевший
        # запуститься сервер останавливается: иначе он занял бы порт позже,
        # а окно показывало бы «Остановлен» и ошибку.
        outcome = wait_for_startup(server, thread, self._stop_event)
        if outcome == "started":
            address = web_base_url(replace(config.app.web, port=bind_port), bind_host)
            logger.info(f"WebServer: запущен на {address}/")
            return True
        with self._lock:
            self._started = False
        if outcome != "exited":
            server.should_exit = True
        if outcome == "timeout":
            # При отмене поток дожидается stop(); здесь — чтобы освободить порт.
            thread.join(timeout=5)
        self._last_error = startup_failure_message(
            bind_port, outcome, relay.take(thread.name)
        )
        if outcome == "stopped":
            logger.info(f"WebServer: {self._last_error}")
        else:
            logger.error(f"WebServer: {self._last_error}")
        return False

    def stop(self) -> bool:
        """Остановить веб-сервер."""
        self._stop_event.set()
        # Идущий запуск увидит запрос и завершится; его поток ещё нужно остановить.
        self._not_starting.wait(timeout=5)
        with self._lock:
            srv = self._server
            thread = self._thread
            running = self.is_running
            if running:
                if srv is not None:
                    try:
                        srv.should_exit = True
                    except Exception as e:
                        logger.warning(f"WebServer: ошибка сигнала остановки: {e}")
                self._started = False
            elif thread is not None and thread.is_alive() and srv is not None:
                # Запуск прерван или не удался, но поток сервера ещё жив.
                srv.should_exit = True

        # Дождаться завершения вне блокировки
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("WebServer: поток не завершился за 5 секунд")

        # Гасим пулы веб-сервера, даже если он не был запущен: они могли быть созданы
        # в create_app(). Здесь же прерываются задания: их SSH/SFTP-циклы выходят
        # в течение ~0.1с вместо ожидания таймаутов. Обработчик остановки uvicorn
        # делает то же, но мог не выполниться, если сервер не дошёл до старта.
        try:
            from src.web.app import shutdown_web_pools

            shutdown_web_pools()
        except Exception as e:
            logger.warning(f"WebServer: ошибка shutdown пулов: {e}")

        if not running:
            return False
        logger.info("WebServer: остановлен")
        return True

    def restart(self, host: str = None, port: int = None) -> bool:
        """Перезапустить сервер с новыми настройками."""
        self.stop()
        return self.start(host=host, port=port)

    def has_pending_workers(self) -> bool:
        thread = self._thread
        return self.is_starting or (thread is not None and thread.is_alive())


_web_server: Optional[WebServer] = None
_web_server_lock = threading.Lock()


def get_web_server() -> WebServer:
    """Получить глобальный экземпляр веб-сервера (singleton)."""
    global _web_server
    if _web_server is None:
        with _web_server_lock:
            if _web_server is None:
                _web_server = WebServer()
    return _web_server
