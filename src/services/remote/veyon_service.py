"""
Veyon Service - Сервис для управления удалённым доступом Veyon.

Предоставляет интерфейс для:
- Запуска окон Veyon (remoteaccess view/control) через veyon-cli
- Получения живых миниатюр экранов через Veyon WebAPI (HTTP REST API)

WebAPI плагин Veyon должен быть включён на машине оператора
(порт 11080 по умолчанию). Аутентификация — по Veyon key-file (RSA).
"""

import hashlib
import json
import locale
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
import zlib
from _thread import RLock
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

import requests

from src.architecture import EventType
from src.architecture.event_bus import EventBus
from src.config import config
from src.domain.models.device import DeviceModel
from src.utils.network.net_utils import tcp_ping
from src.utils.system.subprocess_env import build_clean_env

from src.services.base import BaseService
from src.services.operation_errors import ScopedLastError
from src.services.thumbnails.types import ThumbnailRequest, ThumbnailResult, ThumbnailStatus
from src.services.remote.veyon_webapi import WebAPIClient


@dataclass(frozen=True)
class _WebAPIEndpoint:
    """Connection parameters copied together before any blocking operation."""

    url: str
    server_port: int
    key_name: str


def _find_veyon_cli(prefer_wcli: bool = sys.platform == "win32") -> str:
    """
    Найти путь к veyon-cli / veyon-wcli с поддержкой всех платформ.

    Сначала ищет в PATH, затем в стандартных директориях установки.

    Args:
        prefer_wcli: True — ищет veyon-wcli без консоли (только Windows),
                     False — ищет veyon-cli (консольная версия)

    Returns:
        Полный путь к исполняемому файлу или имя для поиска в PATH.
    """
    # Определить имя исполняемого файла в зависимости от платформы
    if sys.platform == "win32":
        exe_name = "veyon-wcli.exe" if prefer_wcli else "veyon-cli.exe"
    else:
        # На Unix-подобных системах (Linux, macOS) всегда используется veyon-cli
        exe_name = "veyon-cli"

    # Шаг 1: поиск в PATH (самый быстрый способ)
    path_in_path = shutil.which(exe_name)
    if path_in_path:
        return path_in_path

    # Шаг 2: платформо-специфичный поиск в стандартных директориях
    search_dirs = []

    if sys.platform == "win32":
        # Windows: переменные окружения + стандартные пути
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get(
            "ProgramFiles(x86)", r"C:\Program Files (x86)"
        )
        search_dirs = [
            os.path.join(program_files, "Veyon"),
            os.path.join(program_files_x86, "Veyon"),
        ]
    elif sys.platform == "darwin":
        # macOS: стандартные пути установки
        search_dirs = [
            "/Applications/Veyon",
            "/usr/local/bin",
            "/opt/veyon/bin",
        ]
    else:
        # Linux и другие Unix-системы
        search_dirs = [
            "/usr/bin",
            "/usr/local/bin",
            "/opt/veyon/bin",
            "/snap/bin",
        ]

    for directory in search_dirs:
        candidate = os.path.join(directory, exe_name)
        if os.path.isfile(candidate):
            return candidate

    # Fallback: вернуть имя исполняемого файла (попытка найти в PATH при запуске)
    return exe_name


# Закрытие соединений при выходе из программы.
_SHUTDOWN_CLOSE_BUDGET_SEC = 3.0
_SHUTDOWN_CLOSE_THREADS = 16


class VeyonService(BaseService):
    """
    Сервис для управления удалённым доступом через Veyon.

    Управляет:
    - Процессами veyon-cli remoteaccess view/control (GUI-окна)
    - Persistent WebAPI-соединениями для живых миниатюр
    """

    _last_error = ScopedLastError()
    _last_error_detail = ScopedLastError()

    # Карта UI-токенов Veyon-зависимых фичей → (метод VeyonService, нужны_аргументы).
    # Единый источник истины для GUI (MainWindow) и веб-сервера (REST endpoint).
    #
    # Содержит ТОЛЬКО фичи, использующие Veyon WebAPI / RFB / veyon-cli.
    # SSH/systemd/UDP-операции (power_on/reboot/powerdown/logoff/login_ssh)
    # вынесены в HostControlService.FEATURE_MAP — см. src/services/hosts/host_control_service.py.
    FEATURE_MAP = {
        "lock": ("lock_screen", False),
        "unlock": ("unlock_screen", False),
        "restart_service": ("restart_service_remote", False),
        "logoff_webapi": ("logoff_user_webapi", False),
        "lock_input": ("lock_input_devices", False),
        "unlock_input": ("unlock_input_devices", False),
        "login": ("login_user", True),
        "website": ("open_website", True),
        "start_app": ("start_app", True),
        "message": ("text_message", True),
    }

    # Карта feature-методов VeyonService → параметры для ``veyon-cli feature``
    # subcommand. Формат: method_name → (feature_uid, action, arg_keys)
    #   - feature_uid: UID фичи (совпадает с WebAPIClient.FEATURE_*)
    #   - action: "start" | "stop" (для lock/unlock пар)
    #   - arg_keys: tuple camelCase-ключей аргументов для JSON, или None для
    #     безаргументных фичей
    #
    # Используется ``_invoke_feature_cli`` для построения команды:
    #   veyon-cli feature <action> <host> <uid> [<json_args>]
    #
    # CLI approach (fire-and-forget subprocess) не имеет лимита одновременных
    # соединений (в отличие от WebAPI ~10) и не плодит veyon-worker процессы —
    # subprocess подключается к Veyon Server на хосте, отправляет feature-команду,
    # завершается. Feature остаётся активной на удалённом хосте.
    _FEATURE_CLI_MAP = {
        "lock_screen": (WebAPIClient.FEATURE_SCREEN_LOCK, "start", None),
        "unlock_screen": (WebAPIClient.FEATURE_SCREEN_LOCK, "stop", None),
        "lock_input_devices": (WebAPIClient.FEATURE_LOCK_INPUT_DEVICES, "start", None),
        "unlock_input_devices": (WebAPIClient.FEATURE_LOCK_INPUT_DEVICES, "stop", None),
        "logoff_user": (WebAPIClient.FEATURE_USER_LOGOFF, "start", None),
        "login_user": (
            WebAPIClient.FEATURE_USER_LOGIN,
            "start",
            ("username", "password"),
        ),
        "start_app": (WebAPIClient.FEATURE_START_APP, "start", ("applications",)),
        "open_website": (WebAPIClient.FEATURE_OPEN_WEBSITE, "start", ("websiteUrls",)),
        "text_message": (WebAPIClient.FEATURE_TEXT_MESSAGE, "start", ("text", "icon")),
    }

    # Feature-методы, которые можно отправлять через WebAPI (PUT /feature/{uid})
    # вместо veyon-cli subprocess. Только синхронные фичи, не убивающие сессию.
    # logoff_user_webapi / login_user / restart_service_remote исключены:
    #   - logoff: WebAPI UserLogoffFeature асинхронен → чёрный экран вместо DM
    #   - login: SSH/DBus надёжнее для MOS/ROSA входа
    #   - restart_service: SSH (перезапуск Veyon на хосте, не feature UID)
    _WEBAPI_FEATURE_METHODS = frozenset(
        {
            "lock_screen",
            "unlock_screen",
            "lock_input_devices",
            "unlock_input_devices",
            "start_app",
            "open_website",
            "text_message",
        }
    )

    def __init__(self, event_bus: Optional["EventBus"] = None) -> None:
        super().__init__(event_bus)
        self._active_processes: Dict[str, subprocess.Popen] = {}
        self._cli_gui_path: str | None = None
        self._last_error = ""
        # Кэш детектора установки (CLI найден + сервис активен).
        # TTL = 30 сек; обновляется lazily при вызове is_installed().
        self._installed_cache: Optional[bool] = None
        self._installed_at: float = 0.0
        # Кэш статуса сервиса (systemctl/sc query — дорого, subprocess).
        # TTL = 5 сек; обновляется lazily при вызове get_service_status().
        # Сбрасывается вместе с _installed_cache в invalidate_installed_cache().
        self._service_status_cache: Optional[str] = None
        self._service_status_at: float = 0.0

        # ── Метрики захвата миниатюр (для Pool Monitor) ──
        # Persistent per-host: одно соединение на хост, переиспользуется
        # между циклами захвата (auth → get_image × N → close при остановке).
        # Устраняет утечку памяти veyon-service от частого create/close
        # WebApiConnection (деструктор waitForFinished блокирует worker-thread).
        self._thumb_metrics: Dict[str, object] = {
            "total_captures": 0,
            "black_frames": 0,
            "server_port_closed": 0,
            "idle_sweeps": 0,
            "errors_by_code": {},
        }

        # ── Persistent per-host WebAPI-клиенты ──
        # Один WebAPIClient на хост: auth один раз, get_image по таймеру,
        # close_connection(host) при остановке/ошибке/офлайне. Сервер Veyon
        # создаёт WebApiConnection один раз на хост вместо 2×/мин × N хостов.
        self._webapi_clients: Dict[str, WebAPIClient] = {}
        self._webapi_locks: dict[str, RLock] = {}
        self._webapi_global_lock = threading.Lock()
        # Время последнего обращения к persistent-клиенту (time.monotonic).
        # Питает sweep_idle_connections: соединения хостов, снятых оператором
        # (например, нод-хосты, чьи миниатюры запрашивались через /api/node/
        # thumbnail и прекратились), закрываются после простоя — иначе клиент
        # остаётся навсегда (слот ConnectionLimit + утечка veyon-service).
        self._webapi_last_use: Dict[str, float] = {}
        self._webapi_feature_last_use: dict[str, float] = {}
        self._webapi_endpoints: dict[str, _WebAPIEndpoint] = {}
        self._webapi_session_keys: dict[str, tuple] = {}
        self._thumbnail_releases: dict[str, tuple] = {}
        self._webapi_auth_backoff: dict[str, tuple] = {}
        self._thumbnails_shutdown = False
        # Свипер idle-соединений: лениво стартует при первом get_thumbnail,
        # тикает раз в _IDLE_SWEEP_TICK_SEC, дёргает sweep_idle_connections.
        self._idle_sweeper_thread: Optional[threading.Thread] = None
        self._idle_sweeper_stop = threading.Event()
        self._idle_sweeper_wake = threading.Event()
        self._last_sweep_at: float = 0.0
        self._sweep_lock = threading.Lock()

        # ── In-flight дедупликация миниатюр (web-поток) ──
        # Когда хост онлайн, но Veyon на нём сломан, get_thumbnail висит ~10-20с
        # (auth + wait_for_framebuffer + get_image timeout + InvalidConnection
        # retry). Веб-фронтенд переопрашивает каждые 5с, и без дедупликации
        # второй зонд сабмитит свежую задачу в _thumb_pool, пока первая ещё
        # выполняется → пул (8 воркеров) забивается зависшими future'ами.
        # Флаг: True = для хоста уже выполняется get_thumbnail. Второй вызов
        # мгновенно возвращает None — миниатюра обновится в следующем цикле.
        # GUI-поток имеет собственный _pending (thumbnail_timer.py), эта защита
        # для web-потока (SSE /api/hosts/probe) и как вторая линия для обоих.
        self._thumb_inflight: Dict[str, bool] = {}
        self._thumb_inflight_global_lock = threading.Lock()

        # Encoded image ownership and caching belong to ThumbnailService only.
        self._thumb_metrics_lock = threading.Lock()

        # Кэш статистики процесса veyon-service (psutil — итерация процессов
        # и net_connections недёшевы; монитор пулов опрашивает каждые 500мс,
        # web — по запросу). TTL = 5 сек, как у get_service_status().
        self._server_stats_cache: Optional[Dict] = None
        self._server_stats_at: float = 0.0

    @property
    def _cli_path(self) -> str:
        """Платформозависимый путь к Veyon CLI (без консоли — для GUI-операций)."""
        if self._cli_gui_path is None:
            # veyon-wcli существует только на Windows; на других платформах используем veyon-cli
            self._cli_gui_path = _find_veyon_cli(prefer_wcli=(sys.platform == "win32"))
        return self._cli_gui_path

    @property
    def _cli_path_console(self) -> str:
        """Путь к консольной версии Veyon CLI (veyon-cli, не veyon-wcli).

        Используется для операций с ключами (authkeys), где важен вывод в stdout/stderr.
        На Linux совпадает с _cli_path.
        """
        if sys.platform == "win32":
            return _find_veyon_cli(prefer_wcli=False)
        return self._cli_path

    @property
    def _webapi_url(self) -> str:
        return config.app.veyon.webapi_url

    @property
    def _key_name(self) -> str:
        return config.app.veyon.key_name

    @property
    def _feature_transport(self) -> str:
        """Транспорт фичей: 'webapi' (PUT /feature/{uid}) | 'cli' (subprocess)."""
        return getattr(config.app.veyon, "feature_transport", "webapi") or "webapi"

    def _find_key_file(self, key_name: str | None = None) -> str | None:
        """
        Найти файл закрытого ключа Veyon.

        Сначала ищет по имени из конфига (teacher), затем — первый доступный
        закрытый ключ в стандартной директории Veyon.
        """
        import os
        import glob

        if sys.platform == "win32":
            keys_dir = os.path.join(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                "Veyon",
                "keys",
                "private",
            )
        else:
            keys_dir = "/etc/veyon/keys/private"

        # Сначала — ключ из конфига
        name = self._key_name if key_name is None else key_name
        configured = os.path.join(keys_dir, name, "key")
        if os.path.isfile(configured):
            return configured

        # Fallback: первый найденный закрытый ключ
        pattern = os.path.join(keys_dir, "*", "key")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

        return None

    # ========== Детектор установки Veyon ==========

    # TTL кэша is_installed() в секундах.
    _INSTALLED_CACHE_TTL = 30.0
    # TTL кэша get_service_status() в секундах.
    # 5 сек — баланс: статус меняется редко, но UI-монитор пулов
    # обновляется каждые 500мс и не должен молотить subprocess.
    _SERVICE_STATUS_CACHE_TTL = 5.0

    def _cli_found(self) -> bool:
        """True если veyon-cli / veyon-wcli реально найден на диске.

        `_find_veyon_cli` возвращает fallback-имя ('veyon-cli') если бинарник
        не обнаружен; отличаем реальный путь по наличию разделителя каталогов
        и существованию файла.
        """
        path = self._cli_path_console
        return bool(path) and os.path.sep in path and os.path.isfile(path)

    def is_cli_available(self) -> bool:
        """Публичный alias для `_cli_found()`.

        True если veyon-cli/veyon-wcli найден на диске — **без требования**
        активного сервиса Veyon. Используется диалогом «Настройки Veyon»,
        где операции с ключами и конфигом (authkeys list, config get/set)
        выполняются через veyon-cli локально и не требуют запущенного сервиса.

        Для скрытия Veyon-фичей в основном UI (MainWindow) используйте строгий
        `is_installed()` — он дополнительно проверяет активность сервиса.
        """
        return self._cli_found()

    def _service_active(self) -> bool:
        """True если локальный сервис Veyon запущен (running/active).

        Переиспользует системную проверку из get_service_status() —
        systemctl is-active / sc query. Возвращает False для
        stopped/unknown.
        """
        try:
            return self.get_service_status() == "running"
        except Exception:
            return False

    def invalidate_installed_cache(self) -> None:
        """Принудительно сбросить кэш is_installed() и get_service_status().

        Вызывать при подозрении на изменение состояния Veyon
        (запуск/остановка сервиса, установка/удаление пакета).
        """
        self._installed_cache = None
        self._installed_at = 0.0
        self._service_status_cache = None
        self._service_status_at = 0.0
        self.invalidate_server_stats_cache()

    def _publish_state_changed(self) -> None:
        """Уведомить UI об изменении состояния локального Veyon.

        Публикует DATA_SAVED с type=veyon_config — тот же сигнал, что и
        после сохранения настроек Veyon: MainWindow._on_data_saved →
        _refresh_veyon_ui_visibility пересчитывает видимость Veyon-тулбара,
        кнопок и миниатюр немедленно (без ожидания таймера 10-60 сек).
        """
        if not self._event_bus:
            return
        try:
            self._event_bus.publish_typed(
                event_type=EventType.DATA_SAVED,
                source=self.__class__.__name__,
                data={"type": "veyon_config"},
            )
        except Exception as e:
            self._logger.debug(f"VeyonService: publish state changed failed: {e}")

    def is_installed(self) -> bool:
        """True если Veyon готов к работе на этой машине.

        Критерий (по решению проектирования): veyon-cli/veyon-wcli
        найден на диске **И** локальный сервис Veyon активен.

        Результат кэшируется на `_INSTALLED_CACHE_TTL` секунд.
        """
        now = time.time()
        if (
            self._installed_cache is not None
            and (now - self._installed_at) < self._INSTALLED_CACHE_TTL
        ):
            return self._installed_cache
        result = self._cli_found() and self._service_active()
        self._installed_cache = result
        self._installed_at = now
        return result

    def is_available(self) -> bool:
        """Глобальный статус Veyon-интеграции.

        Внутренний master-флаг: True если Veyon установлен и активен.
        Используется как верхнеуровневое условие для скрытия UI-элементов.
        """
        return self.is_installed()

    def is_thumbnails_available(self) -> bool:
        """Доступны ли живые миниатюры экранов через Veyon WebAPI."""
        return self.is_available()

    def is_features_available(self) -> bool:
        """Доступны ли Veyon-фичи (lock/unlock/powerdown/...).

        Эти фичи используют WebAPI-пул соединений, который требует
        работающий сервис Veyon на машине оператора.
        """
        return self.is_available()

    def is_power_features_available(self) -> bool:
        """Доступны ли power-фичи (power_on/reboot/powerdown/logoff).

        Power-фичи работают через SSH/UDP, не требуют Veyon WebAPI.
        Всегда True — доступны на любом хосте с SSH-доступом (или MAC
        для WoL). Используется UI-слоем для гранулярного gating:
        power-кнопки остаются видимыми при отсутствии Veyon.
        """
        return True

    def is_remote_access_available(self) -> bool:
        """Доступен ли запуск GUI-окон Veyon (просмотр/управление)."""
        return self.is_available()

    def is_webapi_available(self) -> bool:
        """Доступен ли Veyon WebAPI (HTTP REST на порту 11080)."""
        return self.is_available()

    def is_rfb_bridge_available(self) -> bool:
        """Доступен ли браузерный RFB-мост через Veyon.

        Требует установленный Veyon **И** включённый в конфиге
        `rfb_bridge_enabled`.
        """
        return self.is_available() and bool(
            getattr(config.app.veyon, "rfb_bridge_enabled", True)
        )

    # ========== Remote Access (GUI окна) ==========

    def start_remote_access(self, host: str, mode: str) -> Optional[subprocess.Popen]:
        """
        Запустить окно Veyon для удалённого доступа к хосту.

        Args:
            host: адрес удалённой машины (IP или hostname)
            mode: режим доступа — "view" (просмотр) или "control" (управление)

        Returns:
            Объект процесса Popen или None при ошибке.
        """
        if mode not in ("view", "control"):
            self._logger.error(
                f"VeyonService: invalid mode '{mode}', expected 'view' or 'control'"
            )
            return None

        if not host:
            self._logger.error("VeyonService: empty host address")
            return None

        self.stop_remote_access(host)

        cmd = [self._cli_path, "remoteaccess", mode, host]

        try:
            self._logger.info(
                f"VeyonService: starting {mode} for {host}: {' '.join(cmd)}"
            )
            proc = subprocess.Popen(
                cmd,
                env=build_clean_env(headless=False),
            )
            self._active_processes[host] = proc

            if self._event_bus:
                self._event_bus.publish_typed(
                    event_type=EventType.CUSTOM,
                    source=self.__class__.__name__,
                    data={
                        "feature": "veyon_access",
                        "host": host,
                        "mode": mode,
                        "action": "started",
                    },
                )

            return proc
        except FileNotFoundError:
            self._logger.error(
                f"VeyonService: veyon-cli not found at '{self._cli_path}'"
            )
            return None
        except Exception as e:
            self._logger.error(f"VeyonService: failed to start {mode} for {host}: {e}")
            return None

    def stop_remote_access(self, host: str) -> bool:
        """Остановить активное окно Veyon для хоста."""
        proc = self._active_processes.pop(host, None)
        if proc is None:
            return False

        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception as e:
            self._logger.error(f"VeyonService: error stopping process for {host}: {e}")

        if self._event_bus:
            self._event_bus.publish_typed(
                event_type=EventType.CUSTOM,
                source=self.__class__.__name__,
                data={"feature": "veyon_access", "host": host, "action": "stopped"},
            )
        return True

    def stop_all(self, deadline_sec: float | None = None) -> None:
        """Остановить все активные окна Veyon и закрыть все WebAPI-соединения."""
        hosts = list(self._active_processes.keys())
        for host in hosts:
            self.stop_remote_access(host)
        self.close_all_connections(deadline_sec)
        self._logger.info("VeyonService: all processes stopped and connections closed")

    # ========== Idle-свипер persistent WebAPI-соединений ==========

    # Тик ленивого daemon-свипера (сек).
    _IDLE_SWEEP_TICK_SEC = 30.0
    # Минимальный пауза между свипами в maybe_sweep_idle_connections (сек).
    _IDLE_SWEEP_MIN_INTERVAL = 30.0
    # Фолбэк TTL простоя, если thumbnails.interval_sec нечитаем (сек).
    _IDLE_SWEEP_DEFAULT_TTL = 90.0

    @staticmethod
    def _idle_ttl_sec() -> float:
        """TTL простоя persistent-соединения (сек).

        max(90, 3 * thumbnails.interval_sec): периодические опросы (интервал 5с
        ±10% джиттер) никогда не попадают в окно простоя; соединение хоста,
        запросы к которому прекратились (снята отметка, скрыта нода, вкладка
        веба закрыта), закрывается через ~90с.
        """
        try:
            interval = float(config.app.thumbnails.interval_sec)
        except (AttributeError, TypeError, ValueError):
            interval = 0.0
        return max(90.0, interval * 3.0) if interval > 0 else 90.0

    def _touch_webapi_client(self, host: str, feature: bool = False) -> None:
        """Отметить время последнего обращения к persistent-клиенту хоста."""
        if not host:
            return
        with self._webapi_global_lock:
            if host not in self._webapi_clients:
                return
            now = time.monotonic()
            self._webapi_last_use[host] = now
            if feature:
                if not hasattr(self, "_webapi_feature_last_use"):
                    self._webapi_feature_last_use = {}
                self._webapi_feature_last_use[host] = now

    def release_thumbnail_session(self, session_key: tuple) -> None:
        """Request safe release without waiting for a lock or doing network I/O."""
        if len(session_key) < 2 or session_key[0] != "veyon":
            return
        host = session_key[1]
        with self._webapi_global_lock:
            if self._webapi_session_keys.get(host) == session_key:
                self._thumbnail_releases[host] = session_key
        self._idle_sweeper_wake.set()

    def sweep_idle_connections(
        self, max_idle_sec: float | None = None, *, now: float | None = None
    ) -> int:
        """Закрыть persistent WebAPI-соединения, к которым долго не обращались.

        Цель — нод-хосты, снятые оператором: их миниатюры запрашивались через
        /api/node/thumbnail, соединение на ноде осталось в _webapi_clients
        (локального таймера у ноды нет — некому вызвать close_connection).
        Также покрывает: обрыв связи ноды, закрытый веб-вкладки, флейпы.

        TTL — max_idle_sec (default max(90, 3×интервал миниатюр)). Захваты
        в полёте (in-flight) и хосты под активным захватом (per-host lock
        занят) пропускаются — соединение продолжит использоваться.

        Returns:
            Число закрытых соединений.
        """
        ttl = float(max_idle_sec) if max_idle_sec is not None else self._idle_ttl_sec()
        now = time.monotonic() if now is None else now
        with self._webapi_global_lock:
            candidates = [
                host
                for host, last in self._webapi_last_use.items()
                if now - last > ttl or host in self._thumbnail_releases
            ]
        closed = 0
        for host in candidates:
            # Захват в полёте — соединение активно, не трогаем (иначе
            # закроем клиент из-под get_thumbnail).
            with self._thumb_inflight_global_lock:
                if self._thumb_inflight.get(host):
                    continue
            lock = self._get_host_lock(host)
            if not lock.acquire(blocking=False):
                # Другой поток держит lock (создание/использование клиента) —
                # пропускаем: не конвой, свипер придёт в следующий тик.
                continue
            try:
                with self._webapi_global_lock:
                    last = self._webapi_last_use.get(host)
                    release = self._thumbnail_releases.get(host)
                    current = self._webapi_session_keys.get(host)
                    feature_last = self._webapi_feature_last_use.get(host)
                # Double-check под lock: между снапшотом и acquire мог
                # прийти свежий запрос (get_thumbnail).
                released = release is not None and release == current
                if last is None or (not released and now - last <= ttl):
                    continue
                # Features share these clients. A recently used feature keeps
                # its connection until idle TTL even after thumbnail release.
                if feature_last is not None and now - feature_last <= ttl:
                    continue
                with self._webapi_global_lock:
                    client = self._webapi_clients.get(host)
                if client is None:
                    # Клиента уже нет — подчистить запись last_use.
                    with self._webapi_global_lock:
                        self._webapi_last_use.pop(host, None)
                    continue
                self._close_client_locked(host)
                closed += 1
            finally:
                lock.release()
        if closed:
            self._inc_metric("idle_sweeps", closed)
            self._logger.debug(
                f"VeyonService: swept {closed} idle WebAPI connection(s) "
                f"(idle > {ttl:.0f}s)"
            )
        return closed

    def maybe_sweep_idle_connections(self) -> None:
        """Свип idle-соединений не чаще раза в _IDLE_SWEEP_MIN_INTERVAL.

        Вызывается в начале get_thumbnail — покрывает всех вызывающих
        (нод-API, веб-зонд, GUI) без правок каждого эндпоинта. Неблокирующе
        (сам свип быстрый: dict-снапшот + try-acquire per host).
        """
        now = time.monotonic()
        with self._sweep_lock:
            if now - self._last_sweep_at < self._IDLE_SWEEP_MIN_INTERVAL:
                return
            self._last_sweep_at = now
        try:
            self.sweep_idle_connections()
        except Exception as error:  # noqa: BLE001 - safe background cleanup boundary
            self._logger.debug(
                f"VeyonService: idle sweep failed ({type(error).__name__})"
            )

    def _ensure_idle_sweeper(self) -> None:
        """Лениво запустить daemon-свипер (один раз на процесс).

        Гарантирует закрытие соединений, когда запросы прекратились целиком
        (оператор снял все отметки / закрыл вкладку) — get_thumbnail больше
        не вызывается, maybe_sweep не дёргается.
        """
        if self._idle_sweeper_stop.is_set():
            return
        if (
            self._idle_sweeper_thread is not None
            and self._idle_sweeper_thread.is_alive()
        ):
            return
        with self._sweep_lock:
            if (
                self._idle_sweeper_thread is not None
                and self._idle_sweeper_thread.is_alive()
            ):
                return
            thread = threading.Thread(
                target=self._idle_sweeper_loop,
                name="veyon-idle-sweeper",
                daemon=True,
            )
            self._idle_sweeper_thread = thread
            thread.start()

    def _idle_sweeper_loop(self) -> None:
        """Цикл daemon-свипера: тик раз в _IDLE_SWEEP_TICK_SEC."""
        tick = self._IDLE_SWEEP_TICK_SEC
        while not self._idle_sweeper_stop.is_set():
            self._idle_sweeper_wake.wait(tick)
            self._idle_sweeper_wake.clear()
            if self._idle_sweeper_stop.is_set():
                break
            try:
                self.sweep_idle_connections()
            except Exception as error:  # noqa: BLE001 - keep background cleanup alive
                self._logger.debug(
                    f"VeyonService: idle sweeper ({type(error).__name__})"
                )

    def shutdown_sweeper(self) -> None:
        """Остановить daemon-свипер (вызывается при shutdown процесса)."""
        self._idle_sweeper_stop.set()
        self._idle_sweeper_wake.set()
        thread = self._idle_sweeper_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def shutdown_thumbnail_backend(self) -> None:
        """Stop cleanup and close the Veyon pool at process shutdown only."""
        self._thumbnails_shutdown = True
        self.shutdown_sweeper()
        self.close_all_connections(_SHUTDOWN_CLOSE_BUDGET_SEC)
        with self._webapi_global_lock:
            self._webapi_auth_backoff.clear()

    # ========== WebAPI Miniaturen (живые миниатюры, persistent per-host) ==========

    def _inc_metric(self, key: str, amount: int = 1) -> None:
        """Increment a capture counter under the metrics lock."""
        with self._thumb_metrics_lock:
            value = self._thumb_metrics.get(key, 0)
            if isinstance(value, int):
                self._thumb_metrics[key] = value + amount

    def _inc_error(self, code: int) -> None:
        """Инкрементировать счётчик ошибок по коду WebAPI."""
        with self._thumb_metrics_lock:
            errors = self._thumb_metrics.setdefault("errors_by_code", {})
            if isinstance(errors, dict):
                errors[code] = errors.get(code, 0) + 1

    def get_thumb_metrics(self) -> dict:
        """Метрики захвата миниатюр для Pool Monitor (snapshot)."""
        with self._thumb_metrics_lock:
            errors = self._thumb_metrics.get("errors_by_code", {})
            return {
                "total_captures": self._thumb_metrics.get("total_captures", 0),
                "black_frames": self._thumb_metrics.get("black_frames", 0),
                "server_port_closed": self._thumb_metrics.get("server_port_closed", 0),
                "idle_sweeps": self._thumb_metrics.get("idle_sweeps", 0),
                "errors_by_code": dict(errors) if isinstance(errors, dict) else {},
            }

    @staticmethod
    def _is_black_frame(data: bytes) -> bool:
        """Эвристическая проверка: полностью чёрный PNG.

        PNG: парсит IDAT-чанки, zlib-decompress, проверяет все байты 0 (с
        допуском на PNG filter bytes). Быстрая, без Qt.
        JPEG/другое — пропускаем (возвращаем False).
        Маленькие данные (<200 байт) — подозрительно, считаем чёрным.
        """
        if not data or len(data) < 200:
            return True
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return False  # не PNG — не можем дёшево проверить
        idat = bytearray()
        offset = 8
        while offset + 8 <= len(data):
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            chunk_type = data[offset + 4 : offset + 8]
            if chunk_type == b"IDAT":
                idat.extend(data[offset + 8 : offset + 8 + length])
            offset += 8 + length + 4  # length + type + data + CRC
        if not idat:
            return False
        try:
            raw = zlib.decompress(bytes(idat))
        except zlib.error:
            return False
        if not raw:
            return False
        non_zero = sum(1 for b in raw if b != 0)
        return non_zero < len(raw) * 0.02  # <2% non-zero → чёрный

    def get_thumbnail(self, host: str, width: int = 0, height: int = 0) -> bytes | None:
        """Compatibility wrapper returning bytes using canonical image settings."""
        if not host:
            return None
        endpoint = self._thumbnail_endpoint_snapshot()
        settings = vars(config.app.thumbnails).copy()
        request = ThumbnailRequest(
            width=width,
            height=height,
            format=settings["format"],
            quality=settings["quality"],
            compression=settings["compression"],
            backend="veyon",
            host=host,
            port=endpoint.server_port,
            password="",
            session_key=(
                "veyon",
                host,
                endpoint.server_port,
                hashlib.sha256(
                    f"{endpoint.url}\0{endpoint.key_name}".encode()
                ).digest(),
            ),
            config_version=0,
            interval_sec=settings["interval_sec"],
        )
        return self.get_thumbnail_result(host, request).data

    @staticmethod
    def _thumbnail_endpoint_snapshot() -> _WebAPIEndpoint:
        # Copy the section's dict in one operation. Do not read live properties
        # again during ping/auth/cold-start/retry: saves may replace the section.
        values = vars(config.app.veyon).copy()
        return _WebAPIEndpoint(
            url=values["webapi_url"],
            server_port=values.get("server_port", 11100),
            key_name=values["key_name"],
        )

    @staticmethod
    def _thumbnail_failure(
        request: ThumbnailRequest | None, status: ThumbnailStatus, error_code: str
    ) -> ThumbnailResult:
        return ThumbnailResult(
            backend="veyon",
            status=status,
            config_version=request.config_version if request is not None else 0,
            error_code=error_code,
        )

    def _thumbnail_error_result(
        self, request: ThumbnailRequest | None, error: Exception
    ) -> ThumbnailResult:
        if isinstance(error, WebAPIClient.Error):
            self._inc_error(error.code)
            statuses = {
                WebAPIClient.ERROR_INVALID_CREDENTIALS: ThumbnailStatus.AUTH_FAILED,
                WebAPIClient.ERROR_AUTH_FAILED: ThumbnailStatus.AUTH_FAILED,
                WebAPIClient.ERROR_AUTH_METHOD_NOT_AVAILABLE: ThumbnailStatus.UNSUPPORTED,
                WebAPIClient.ERROR_CONN_LIMIT_REACHED: ThumbnailStatus.BUSY,
                WebAPIClient.ERROR_CONN_TIMED_OUT: ThumbnailStatus.TIMEOUT,
                WebAPIClient.ERROR_UNSUPPORTED_IMAGE_FORMAT: ThumbnailStatus.UNSUPPORTED,
                WebAPIClient.ERROR_FRAMEBUFFER_NOT_AVAILABLE: ThumbnailStatus.UNAVAILABLE,
                WebAPIClient.ERROR_INVALID_CONNECTION: ThumbnailStatus.UNAVAILABLE,
            }
            return self._thumbnail_failure(
                request,
                statuses.get(error.code, ThumbnailStatus.PROTOCOL_ERROR),
                f"webapi_{error.code}",
            )
        if isinstance(error, requests.Timeout):
            return self._thumbnail_failure(
                request, ThumbnailStatus.TIMEOUT, "webapi_timeout"
            )
        if isinstance(error, requests.RequestException):
            return self._thumbnail_failure(
                request, ThumbnailStatus.UNAVAILABLE, "webapi_connection_error"
            )
        # Exception strings, URLs and raw response bodies are not diagnostics.
        self._logger.debug(f"VeyonService: capture failed ({type(error).__name__})")
        return self._thumbnail_failure(
            request, ThumbnailStatus.PROTOCOL_ERROR, "webapi_protocol_error"
        )

    def get_thumbnail_result(
        self, host: str, request: ThumbnailRequest
    ) -> ThumbnailResult:
        """Capture in a worker; the facade owns encoded image caching."""
        if self._thumbnails_shutdown:
            return self._thumbnail_failure(
                request, ThumbnailStatus.CANCELLED, "veyon_shutdown"
            )
        if not host or host != request.host:
            return self._thumbnail_failure(
                request, ThumbnailStatus.UNAVAILABLE, "veyon_invalid_host"
            )
        if request.format not in ("png", "jpeg"):
            return self._thumbnail_failure(
                request, ThumbnailStatus.UNSUPPORTED, "veyon_image_format"
            )
        if not self.is_thumbnails_available():
            return self._thumbnail_failure(
                request, ThumbnailStatus.UNAVAILABLE, "veyon_local_unavailable"
            )
        endpoint = self._thumbnail_endpoint_snapshot()
        fingerprint = hashlib.sha256(
            f"{endpoint.url}\0{endpoint.key_name}".encode()
        ).digest()
        if request.port != endpoint.server_port or (
            len(request.session_key) >= 4
            and isinstance(request.session_key[3], bytes)
            and request.session_key[3] != fingerprint
        ):
            return self._thumbnail_failure(
                request, ThumbnailStatus.CANCELLED, "veyon_config_changed"
            )
        self._ensure_idle_sweeper()
        if not self._acquire_inflight(host):
            return self._thumbnail_failure(
                request, ThumbnailStatus.BUSY, "veyon_inflight"
            )
        lock = self._get_host_lock(host)
        if not lock.acquire(blocking=False):
            self._release_inflight(host)
            return self._thumbnail_failure(
                request, ThumbnailStatus.BUSY, "veyon_inflight"
            )
        try:
            # A new accepted capture supersedes an older queued release. Do
            # this once, not during retry: release arriving in-flight must stay.
            with self._webapi_global_lock:
                self._thumbnail_releases.pop(host, None)
            for attempt in range(2):
                client, failure = self._get_or_create_webapi_client_result(
                    host, endpoint, request
                )
                if failure is not None:
                    return failure
                if client is None:
                    return self._thumbnail_failure(
                        request, ThumbnailStatus.UNAVAILABLE, "veyon_client_unavailable"
                    )
                try:
                    data = client.get_image(
                        width=request.width,
                        height=request.height,
                        format=request.format,
                        compression=request.compression,
                        quality=request.quality,
                    )
                except WebAPIClient.Error as error:
                    if (
                        error.code == WebAPIClient.ERROR_INVALID_CONNECTION
                        and not attempt
                    ):
                        self._inc_error(error.code)
                        self._close_client_locked(host)
                        continue
                    return self._thumbnail_error_result(request, error)
                except requests.RequestException as error:
                    self._close_client_locked(host)
                    return self._thumbnail_error_result(request, error)
                except Exception as error:  # noqa: BLE001 - typed, secret-free boundary
                    return self._thumbnail_error_result(request, error)
                if (
                    self._thumbnails_shutdown
                    or self._thumbnail_endpoint_snapshot() != endpoint
                ):
                    self._close_client_locked(host)
                    return self._thumbnail_failure(
                        request, ThumbnailStatus.CANCELLED, "veyon_config_changed"
                    )
                self._touch_webapi_client(host)
                self._inc_metric("total_captures")
                if not data:
                    return self._thumbnail_failure(
                        request, ThumbnailStatus.UNAVAILABLE, "veyon_empty_framebuffer"
                    )
                if self._is_black_frame(data):
                    self._inc_metric("black_frames")
                result = ThumbnailResult(
                    data=data,
                    mime="image/" + request.format,
                    backend="veyon",
                    status=ThumbnailStatus.OK,
                    frame_time=time.monotonic(),
                    config_version=request.config_version,
                )
                return result
            return self._thumbnail_failure(
                request, ThumbnailStatus.UNAVAILABLE, "webapi_2"
            )
        finally:
            lock.release()
            self._release_inflight(host)

    def _acquire_inflight(self, host: str) -> bool:
        """Попытаться отметить get_thumbnail как in-flight для хоста.

        Возвращает True, если хост был свободен (флаг установлен — вызывающий
        обязан вызвать _release_inflight в finally). Возвращает False, если
        для хоста уже выполняется get_thumbnail — вызывающий должен дропнуть.
        """
        with self._thumb_inflight_global_lock:
            if self._thumb_inflight.get(host):
                return False
            self._thumb_inflight[host] = True
            return True

    def _release_inflight(self, host: str) -> None:
        """Снять флаг in-flight для хоста (вызывается в finally)."""
        with self._thumb_inflight_global_lock:
            self._thumb_inflight.pop(host, None)

    def _get_or_create_webapi_client(self, host: str) -> WebAPIClient | None:
        """Compatibility entrypoint to the same authenticated client pool."""
        if not host:
            return None
        endpoint = self._thumbnail_endpoint_snapshot()
        client, _ = self._get_or_create_webapi_client_result(host, endpoint, None)
        return client

    def _get_or_create_webapi_client_result(
        self,
        host: str,
        endpoint: _WebAPIEndpoint,
        request: ThumbnailRequest | None,
    ) -> tuple[WebAPIClient | None, ThumbnailResult | None]:
        """Create or reuse using one endpoint snapshot, returning typed failures."""
        lock = self._get_host_lock(host)
        with lock:
            if self._thumbnails_shutdown:
                return None, self._thumbnail_failure(
                    request, ThumbnailStatus.CANCELLED, "veyon_shutdown"
                )
            session_key = request.session_key if request is not None else None
            with self._webapi_global_lock:
                existing = self._webapi_clients.get(host)
                old_endpoint = self._webapi_endpoints.get(host)
                old_session = self._webapi_session_keys.get(host)
                backoff = self._webapi_auth_backoff.get(host)
            if (
                existing is not None
                and existing.is_authenticated()
                and old_endpoint == endpoint
                and (session_key is None or old_session == session_key)
            ):
                self._touch_webapi_client(host)
                return existing, None
            if existing is not None:
                self._close_client_locked(host)
            if backoff is not None:
                failed_endpoint, failed_session, failure, retry_at = backoff
                if (
                    failed_endpoint == endpoint
                    and failed_session == session_key
                    and time.monotonic() < retry_at
                ):
                    return None, replace(
                        failure,
                        config_version=request.config_version if request else 0,
                    )
                with self._webapi_global_lock:
                    self._webapi_auth_backoff.pop(host, None)
            # Keep the existing fast-fail gate and cold-start budgets.
            if not tcp_ping(host, endpoint.server_port, timeout=1.0):
                self._inc_metric("server_port_closed")
                return None, self._thumbnail_failure(
                    request, ThumbnailStatus.UNAVAILABLE, "veyon_server_port_closed"
                )
            client, failure = self._create_webapi_client_result(host, endpoint, request)
            if (
                self._thumbnails_shutdown
                or self._thumbnail_endpoint_snapshot() != endpoint
            ):
                if client is not None:
                    client.close_connection()
                return None, self._thumbnail_failure(
                    request, ThumbnailStatus.CANCELLED, "veyon_config_changed"
                )
            if failure is not None:
                if failure.status == ThumbnailStatus.AUTH_FAILED:
                    with self._webapi_global_lock:
                        self._webapi_auth_backoff[host] = (
                            endpoint,
                            session_key,
                            failure,
                            time.monotonic() + 30.0,
                        )
                return None, failure
            if client is None:
                return None, self._thumbnail_failure(
                    request, ThumbnailStatus.UNAVAILABLE, "veyon_client_unavailable"
                )
            with self._webapi_global_lock:
                self._webapi_clients[host] = client
                self._webapi_endpoints[host] = endpoint
                if session_key is not None:
                    self._webapi_session_keys[host] = session_key
            self._touch_webapi_client(host)
            return client, None

    def is_host_veyon_port_open(self, host: str, timeout: float = 1.0) -> bool:
        """Открыт ли Veyon Server (порт server_port, default 11100) на хосте.

        Быстрый TCP-пинг (connect_ex, ~мгновенно для онлайн-хоста с закрытым
        портом — RST). Используется циклом захвата миниатюр ДО get_thumbnail,
        чтобы UI мог отличить «хост онлайн, но Veyon на нём не работает»
        (оранжевая иконка) от transient-ошибки захвата (зелёная иконка).

        При закрытом порте инкрементирует метрику `server_port_closed`
        (секция «Без Veyon» в Мониторе пулов).
        """
        if not host:
            return False
        ok = tcp_ping(host, self._server_port, timeout=timeout)
        if not ok:
            self._inc_metric("server_port_closed")
        return ok

    def _get_host_lock(self, host: str) -> RLock:
        """Получить (или создать) per-host Lock для синхронизации клиента."""
        with self._webapi_global_lock:
            lock = self._webapi_locks.get(host)
            if lock is None:
                # The capture owns this lock through auth, read and retry.
                # Reentrancy lets compatibility helpers use the same path.
                lock = threading.RLock()
                self._webapi_locks[host] = lock
            return lock

    def _create_webapi_client(self, host: str) -> WebAPIClient | None:
        """Compatibility wrapper around the typed auth/cold-start result."""
        if not host:
            return None
        client, _ = self._create_webapi_client_result(
            host, self._thumbnail_endpoint_snapshot(), None
        )
        return client

    def _create_webapi_client_result(
        self,
        host: str,
        endpoint: _WebAPIEndpoint,
        request: ThumbnailRequest | None,
    ) -> tuple[WebAPIClient | None, ThumbnailResult | None]:
        """Reuse WebAPI authentication with safe detailed auth failures."""
        client = WebAPIClient(endpoint.url, host)
        successful = False
        try:
            key_file = self._find_key_file(endpoint.key_name)
            if key_file is None:
                return None, self._thumbnail_failure(
                    request, ThumbnailStatus.AUTH_FAILED, "veyon_key_missing"
                )
            with open(key_file, "r", encoding="utf-8") as f:
                key_data = f.read()
            key_name = os.path.basename(os.path.dirname(key_file))
            ok = client.authenticate_with_key_file(key_name, key_data)
            if not ok:
                if client.last_auth_error is not None:
                    return None, self._thumbnail_error_result(
                        request, client.last_auth_error
                    )
                code = client.last_auth_error_code or "veyon_auth_failed"
                statuses = {
                    "webapi_timeout": ThumbnailStatus.TIMEOUT,
                    "webapi_connection_error": ThumbnailStatus.UNAVAILABLE,
                    "webapi_invalid_auth_response": ThumbnailStatus.PROTOCOL_ERROR,
                }
                return None, self._thumbnail_failure(
                    request, statuses.get(code, ThumbnailStatus.AUTH_FAILED), code
                )
            # Do not reduce the established ten-second cold-start budget.
            if not client.wait_for_framebuffer(timeout=10):
                return None, self._thumbnail_failure(
                    request, ThumbnailStatus.TIMEOUT, "veyon_framebuffer_timeout"
                )
            successful = True
            return client, None
        except requests.RequestException as error:
            return None, self._thumbnail_error_result(request, error)
        except (OSError, UnicodeError):
            return None, self._thumbnail_failure(
                request, ThumbnailStatus.AUTH_FAILED, "veyon_key_unreadable"
            )
        except Exception as error:  # noqa: BLE001 - typed, secret-free auth boundary
            return None, self._thumbnail_error_result(request, error)
        finally:
            if not successful:
                client.close_connection()

    def _close_client_locked(self, host: str) -> None:
        """Закрыть persistent-клиента и удалить из dict.

        Вызывается под per-host Lock (без получения нового).
        """
        with self._webapi_global_lock:
            client = self._webapi_clients.pop(host, None)
            self._webapi_last_use.pop(host, None)
            self._webapi_feature_last_use.pop(host, None)
            self._webapi_endpoints.pop(host, None)
            self._webapi_session_keys.pop(host, None)
            self._thumbnail_releases.pop(host, None)
        if client is not None:
            try:
                client.close_connection()
            except Exception as error:  # noqa: BLE001 - best-effort pool cleanup
                self._logger.debug(
                    f"VeyonService: client close failed ({type(error).__name__})"
                )

    def _close_client(self, host: str) -> None:
        """Закрыть persistent-клиента и удалить из dict (потокобезопасно)."""
        lock = self._get_host_lock(host)
        with lock:
            self._close_client_locked(host)

    def close_connection(self, host: str) -> None:
        """Закрыть persistent WebAPI-соединение для хоста.

        Вызывается VeyonThumbnailTimerManager при остановке миниатюр для хоста
        (stop_thumbnail / host offline) и HostControlService перед убийством
        сессии. Освобождает слот WebAPI/ConnectionLimit на сервере Veyon.
        """
        self._close_client(host)

    def close_all_connections(self, deadline_sec: float | None = None) -> None:
        """Закрыть все persistent WebAPI-соединения.

        Вызывается при stop_all() (shutdown / deactivate_thumbnails).
        С deadline_sec закрывает параллельно и не дольше бюджета: хост, занятый
        захватом или недоступный, не должен задерживать выход из программы.
        Незакрытые соединения освобождаются при завершении процесса.
        """
        with self._webapi_global_lock:
            hosts = list(self._webapi_clients.keys())
        if deadline_sec is None:
            for host in hosts:
                self._close_client(host)
            return
        if not hosts:
            return
        queue = deque(hosts)

        def worker() -> None:
            while True:
                try:
                    host = queue.popleft()
                except IndexError:
                    return
                try:
                    self._close_client(host)
                except Exception:  # noqa: BLE001 - закрытие при выходе best-effort
                    self._logger.debug("Veyon close failed during shutdown")

        threads = [
            threading.Thread(target=worker, name="veyon-close", daemon=True)
            for _ in range(min(_SHUTDOWN_CLOSE_THREADS, len(hosts)))
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + deadline_sec
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def get_webapi_client_count(self) -> int:
        """Количество активных persistent WebAPI-соединений.

        Потокобезопасно (под _webapi_global_lock). Для монитора пулов.
        """
        with self._webapi_global_lock:
            return len(self._webapi_clients)

    # ========== Статистика процесса veyon-service (для монитора пулов / web) ==========

    # TTL кэша get_webapi_server_stats() в секундах.
    _SERVER_STATS_CACHE_TTL = 5.0

    def get_webapi_server_stats(self) -> Dict:
        """Метрики процесса veyon-service и TCP-соединений к WebAPI.

        Возвращает (все поля Optional — None при недоступности psutil/процесса):
            pid:              PID процесса veyon-service
            rss_mb:           рабочий набор (RAM), МБ
            commit_mb:        private bytes (commit charge), МБ
            threads:          число потоков
            handles:          число хендлов (Windows)
            webapi_established: число ESTABLISHED TCP-соединений к WebAPI-порту
            webapi_close_wait:  число CLOSE_WAIT к WebAPI-порту (0 в норме;
                               рост = сервер не закрывает сокеты)

        Кэшируется на _SERVER_STATS_CACHE_TTL (5 сек): psutil-итерации
        (process_iter, net_connections) стоят миллисекунды-десятки, а монитор
        пулов опрашивает каждые 500мс.
        """
        now = time.time()
        if (
            self._server_stats_cache is not None
            and (now - self._server_stats_at) < self._SERVER_STATS_CACHE_TTL
        ):
            return self._server_stats_cache

        stats = self._get_webapi_server_stats_uncached()
        self._server_stats_cache = stats
        self._server_stats_at = now
        return stats

    def _get_webapi_server_stats_uncached(self) -> Dict:
        """Фактический сбор статистики veyon-service. Не кэшируется."""
        import urllib.parse

        stats: Dict = {
            "pid": None,
            "rss_mb": None,
            "commit_mb": None,
            "threads": None,
            "handles": None,
            "webapi_established": None,
            "webapi_close_wait": None,
        }

        proc = self._find_service_process()
        if proc is not None:
            stats["pid"] = proc.pid
            try:
                mem = proc.memory_info()
                stats["rss_mb"] = round(mem.rss / (1024 * 1024), 1)
                stats["commit_mb"] = round(mem.private / (1024 * 1024), 1)
            except Exception:
                pass
            try:
                stats["threads"] = proc.num_threads()
            except Exception:
                pass
            try:
                stats["handles"] = proc.num_handles()
            except (AttributeError, Exception):
                pass

        # ── TCP-соединения к WebAPI-порту (как слушаемые, так и установленные) ──
        try:
            port = urllib.parse.urlparse(self._webapi_url).port or 80
        except ValueError:
            port = 80

        established = 0
        close_wait = 0
        try:
            import psutil

            for conn in psutil.net_connections(kind="tcp"):
                laddr = getattr(conn, "laddr", None)
                if laddr is None or laddr.port != port:
                    continue
                if conn.status == psutil.CONN_ESTABLISHED:
                    established += 1
                elif conn.status == psutil.CONN_CLOSE_WAIT:
                    close_wait += 1
        except (psutil.Error, PermissionError, OSError):
            # net_connections требует elevation на некоторых ОС — оставляем None
            pass
        else:
            stats["webapi_established"] = established
            stats["webapi_close_wait"] = close_wait

        return stats

    @staticmethod
    def _find_service_process():
        """Найти процесс veyon-service через psutil. None если не найден."""
        try:
            import psutil
        except ImportError:
            return None
        if sys.platform == "win32":
            proc_name = "veyon-service.exe"
        else:
            proc_name = "veyon-service"
        try:
            for proc in psutil.process_iter(["name"]):
                if (proc.info["name"] or "").lower() == proc_name:
                    return proc
        except psutil.Error:
            pass
        return None

    def _get_service_pid(self):
        """PID процесса veyon-service (без кэша). None если не запущен."""
        proc = self._find_service_process()
        return proc.pid if proc is not None else None

    def invalidate_server_stats_cache(self) -> None:
        """Сбросить кэш статистики veyon-service (после рестарта сервиса)."""
        self._server_stats_cache = None
        self._server_stats_at = 0.0

    # ========== RFB Bridge (браузерный remote desktop через Veyon) ==========

    @property
    def rfb_bridge_enabled(self) -> bool:
        return getattr(config.app.veyon, "rfb_bridge_enabled", True)

    @property
    def _server_port(self) -> int:
        return getattr(config.app.veyon, "server_port", 11100)

    def open_rfb_connection(
        self,
        host: str,
        username: str = "",
        device: Optional[DeviceModel] = None,
        mode: str = "vnc",
    ) -> Optional[Tuple[socket.socket, bytes]]:
        """Открыть RFB-соединение с удалённым хостом.

        Ветвление по mode (явный параметр от WS-прокси, определяется per-host
        фронтендом через /api/vnc/available + /api/veyon/available + /api/krfb/available):
          - "vnc": прямое TCP-подключение к VNC-порту (x11vnc на хосте).
            Возвращает (sock, None) — без Veyon handshake; RFB handshake
            выполняется браузерным noVNC напрямую через WS-прокси.
          - "krfb": прямое TCP-подключение к KRFB-порту (krfb на хосте).
            Аналогично vnc — (sock, None), RFB handshake делает noVNC.
          - "veyon": Veyon security type 40 + RSA key-file аутентификация.
            Возвращает (sock, server_init_bytes) — сокет в RFB running state,
            server_init_bytes — raw rfbServerInit для пересылки браузеру.

        Args:
            host: адрес удалённой машины.
            username: имя пользователя для Veyon access-control dialog
                      (пусто = текущий пользователь ОС; только для Veyon).
            device: DeviceModel для per-host vnc_port override
                    (vnc и krfb).
            mode: "vnc" | "krfb" | "veyon" — режим подключения.

        Returns:
            (socket, server_init_bytes|None) или None при ошибке.
        """
        if not host:
            return None

        if mode == "vnc":
            return self._open_vnc_connection(host, device)

        if mode == "krfb":
            return self._open_krfb_connection(host, device)

        # Veyon-режим
        if not self.rfb_bridge_enabled:
            self._logger.warning(f"VeyonService: RFB bridge disabled in config")
            return None

        key_file = self._find_key_file()
        if key_file is None:
            self._logger.error(
                f"VeyonService: no Veyon private key found for RFB bridge to {host}"
            )
            return None

        try:
            with open(key_file, "r") as f:
                key_data = f.read()
        except Exception as e:
            self._logger.error(f"VeyonService: cannot read key file {key_file}: {e}")
            return None

        key_name = os.path.basename(os.path.dirname(key_file))
        port = self._server_port

        from src.services.remote.veyon_rfb_bridge import (
            VeyonRFBHandshake,
            VeyonRFBError,
            VeyonAuthError,
            VeyonAccessControlError,
        )

        hs = VeyonRFBHandshake(
            host=host,
            port=port,
            key_data=key_data,
            key_name=key_name,
            username=username,
        )
        try:
            sock, server_init_bytes = hs.connect()
            if self._event_bus:
                self._event_bus.publish_typed(
                    event_type=EventType.CUSTOM,
                    source=self.__class__.__name__,
                    data={"feature": "veyon_rfb", "host": host, "action": "connected"},
                )
            return sock, server_init_bytes
        except VeyonAuthError as e:
            self._logger.error(f"VeyonService: RFB auth failed for {host}: {e}")
            return None
        except VeyonAccessControlError as e:
            self._logger.warning(
                f"VeyonService: RFB access control denied for {host}: {e}"
            )
            return None
        except VeyonRFBError as e:
            self._logger.error(f"VeyonService: RFB bridge error for {host}: {e}")
            return None
        except Exception as e:
            self._logger.error(
                f"VeyonService: RFB bridge unexpected error for {host}: {e}"
            )
            return None

    def _open_vnc_connection(
        self, host: str, device: Optional[DeviceModel] = None
    ) -> Optional[Tuple[socket.socket, bytes]]:
        """Прямое TCP-подключение к VNC-серверу (x11vnc) на хосте.

        Возвращает (sock, None) — сокет в raw-состоянии (до RFB handshake);
        WS-прокси передаёт байты напрямую, noVNC сам выполняет RFB handshake
        (ProtocolVersion → security negotiation → ServerInit).

        Port: device.vnc_port (per-host override) или config.app.veyon.vnc_port.
        """
        port = 5900
        if device is not None and getattr(device, "vnc_port", None):
            port = int(device.vnc_port)
        else:
            port = int(getattr(config.app.veyon, "vnc_port", 5900) or 5900)

        try:
            sock = socket.create_connection((host, port), timeout=10.0)
            sock.settimeout(None)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            if self._event_bus:
                self._event_bus.publish_typed(
                    event_type=EventType.CUSTOM,
                    source=self.__class__.__name__,
                    data={"feature": "vnc_rfb", "host": host, "action": "connected"},
                )
            self._logger.info(f"VeyonService: VNC raw connection to {host}:{port}")
            return sock, None
        except Exception as e:
            self._logger.error(f"VeyonService: VNC connect failed {host}:{port}: {e}")
            return None

    def _open_krfb_connection(
        self, host: str, device: Optional[DeviceModel] = None
    ) -> Optional[Tuple[socket.socket, bytes]]:
        """Прямое TCP-подключение к KRFB-серверу (krfb) на хосте.

        krfb - стандартный RFB-сервер (VNC Auth): сокет в raw-состоянии
        (до RFB handshake); WS-прокси передаёт байты напрямую, noVNC сам
        выполняет RFB handshake и VNC-аутентификацию (пароль браузер
        берёт из /api/krfb/password).

        Port: device.vnc_port (per-host override) или config.app.veyon.vnc_port.
        """
        port = 5900
        if device is not None and getattr(device, "vnc_port", None):
            port = int(device.vnc_port)
        else:
            port = int(getattr(config.app.veyon, "vnc_port", 5900) or 5900)

        try:
            sock = socket.create_connection((host, port), timeout=10.0)
            sock.settimeout(None)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            if self._event_bus:
                self._event_bus.publish_typed(
                    event_type=EventType.CUSTOM,
                    source=self.__class__.__name__,
                    data={"feature": "krfb_rfb", "host": host, "action": "connected"},
                )
            self._logger.info(f"VeyonService: KRFB raw connection to {host}:{port}")
            return sock, None
        except Exception as e:
            self._logger.error(f"VeyonService: KRFB connect failed {host}:{port}: {e}")
            return None

    # ========== Veyon Features (lock/unlock через veyon-cli feature, power/reboot/logoff через SSH) ==========

    def lock_screen(self, host: str) -> bool:
        """Заблокировать экран на удалённом хосте."""
        return self._invoke_feature(host, "lock_screen")

    def unlock_screen(self, host: str) -> bool:
        """Разблокировать экран на удалённом хосте."""
        return self._invoke_feature(host, "unlock_screen")

    def logoff_user_webapi(self, host: str) -> bool:
        """Завершить сеанс пользователя через Veyon CLI ``UserLogoffFeature``.

        Альтернатива SSH-пути ``HostControlService.logoff_user`` для случаев,
        когда нужен штатный механизм Veyon (без убийства всех процессов
        сессии systemd-ом). Выполняется через ``veyon-cli feature start``.
        """
        return self._invoke_feature(host, "logoff_user")

    def lock_input_devices(self, host: str) -> bool:
        """Заблокировать устройства ввода на удалённом хосте."""
        return self._invoke_feature(host, "lock_input_devices")

    def unlock_input_devices(self, host: str) -> bool:
        """Разблокировать устройства ввода на удалённом хосте."""
        return self._invoke_feature(host, "unlock_input_devices")

    def login_user(self, host: str, username: str, password: str) -> bool:
        """Выполнить вход пользователя на удалённом хосте."""
        return self._invoke_feature(host, "login_user", args=(username, password))

    def start_app(self, host: str, applications) -> bool:
        """Запустить приложение на удалённом хосте.

        Args:
            applications: строка команды или список строк.
        """
        return self._invoke_feature(host, "start_app", args=(applications,))

    def open_website(self, host: str, url: str) -> bool:
        """Открыть веб-сайт на удалённом хосте."""
        return self._invoke_feature(host, "open_website", args=(url,))

    def text_message(self, host: str, text: str, icon: int = 1) -> bool:
        """Отправить текстовое сообщение пользователю на удалённом хосте.

        Args:
            icon: 1=Information, 2=Warning, 3=Critical, 4=Question
        """
        return self._invoke_feature(host, "text_message", args=(text, icon))

    def restart_service_remote(self, host: str) -> bool:
        """Перезапустить Veyon-сервис на удалённом хосте через SSH.

        Veyon WebAPI не предоставляет feature UID для рестарта собственного
        сервиса, поэтому команда выполняется напрямую через SSH
        (systemctl restart veyon). Требует root-доступа на хосте.

        Returns:
            True при успехе, False при ошибке.
        """
        if not host:
            return False
        try:
            from src.di import get_container
            from src.services import DeviceService
            from src.workers.command.ssh import SSHWorker
            from src.workers.command.sudo_utils import should_use_sudo

            container = get_container()
            ds = container.resolve(DeviceService)
            dev = ds.get_device(host)
            if dev is None:
                self._logger.error(
                    f"VeyonService: restart_service_remote — host {host} не найден"
                )
                return False

            cmd = "systemctl restart veyon"
            # sudo-обёртка для non-root логина (PTY-expect в SSHWorker подаёт пароль)
            if should_use_sudo(dev):
                cmd = f"sudo {cmd}"

            worker = SSHWorker()
            output, ok = worker.execute(dev, cmd, timeout=15)
            if ok:
                self._logger.info(f"VeyonService: service restarted on {host}")
                if self._event_bus:
                    self._event_bus.publish_typed(
                        event_type=EventType.CUSTOM,
                        source=self.__class__.__name__,
                        data={
                            "type": "veyon_feature",
                            "feature": "restart_service",
                            "host": host,
                            "success": True,
                        },
                    )
                return True
            self._logger.debug(
                f"VeyonService: restart_service_remote failed for {host}: {output}"
            )
            return False
        except Exception as e:
            self._logger.debug(
                f"VeyonService: restart_service_remote error for {host}: {e}"
            )
            return False

    def _invoke_feature_webapi(
        self, host: str, method_name: str, args: tuple | None = None
    ) -> bool | None:
        """Serialize feature I/O with capture and protect it from safe cleanup."""
        lock = self._get_host_lock(host)
        with lock:
            endpoint = getattr(self, "_webapi_endpoints", {}).get(host)
            if endpoint is not None and endpoint != self._thumbnail_endpoint_snapshot():
                self._close_client_locked(host)
                return None
            self._touch_webapi_client(host, feature=True)
            try:
                return self._invoke_feature_webapi_locked(host, method_name, args)
            finally:
                self._touch_webapi_client(host, feature=True)

    def _invoke_feature_webapi_locked(
        self, host: str, method_name: str, args: tuple | None = None
    ) -> bool | None:
        """Вызвать Veyon feature через WebAPI (PUT /api/v1/feature/{uid}).

        Использует существующий persistent WebAPI-клиент из ``_webapi_clients``
        (тот же, что для миниатюр). Не требует subprocess, переиспользует
        открытое соединение — latency ~10-20мс против ~150мс для CLI.

        Args:
            host: адрес удалённой машины
            method_name: имя feature-метода (должно быть в _WEBAPI_FEATURE_METHODS)
            args: позиционные аргументы (для start_app, open_website, text_message)

        Returns:
            True  — фича успешно отправлена через WebAPI.
            None  — fallback на CLI: persistent-клиента нет (миниатюры выключены,
                    хост офлайн, Veyon недоступен), либо соединение истекло/закрыто,
                    либо ConnectionLimit исчерпан.
            False — ошибочный запрос (invalid feature UID и т.п.) — не fallback.
        """
        cli_entry = self._FEATURE_CLI_MAP.get(method_name)
        if cli_entry is None:
            return False

        feature_uid, action, arg_keys = cli_entry
        active = action == "start"

        # Построение arguments dict (camelCase) — та же логика что в CLI path
        arguments = None
        if arg_keys is not None and args is not None:
            if method_name == "start_app" and isinstance(args[0], str):
                arguments = {arg_keys[0]: [args[0]]}
            else:
                arguments = dict(zip(arg_keys, args))

        # Получить persistent-клиент (не создавать новый — только если уже есть)
        with self._webapi_global_lock:
            client = self._webapi_clients.get(host)
        if client is None or not client.is_authenticated():
            # Нет открытого соединения — fallback на CLI
            return None

        try:
            client.set_feature_status(feature_uid, active, arguments)
            self._touch_webapi_client(host)
            if self._event_bus:
                self._event_bus.publish_typed(
                    event_type=EventType.CUSTOM,
                    source=self.__class__.__name__,
                    data={
                        "feature": "veyon_feature",
                        "host": host,
                        "method": method_name,
                        "result": True,
                        "transport": "webapi",
                    },
                )
            self._logger.info(
                f"VeyonService: feature {method_name} on {host}: OK (webapi)"
            )
            return True
        except WebAPIClient.Error as e:
            # InvalidConnection (2) — соединение закрылось/истекло → fallback
            # ConnLimitReached (7) — лимит исчерпан → fallback
            if e.code in (
                WebAPIClient.ERROR_INVALID_CONNECTION,
                WebAPIClient.ERROR_CONN_LIMIT_REACHED,
            ):
                self._logger.debug(
                    f"VeyonService: webapi feature {method_name} on {host}: "
                    f"fallback to CLI (error {e.code}: {e.message})"
                )
                # Закрыть истёкший клиент чтобы следующий вызов пересоздал
                if e.code == WebAPIClient.ERROR_INVALID_CONNECTION:
                    self._close_client(host)
                return None
            # Другие ошибки (InvalidFeature=3, InvalidData=1, ...) — не fallback
            self._last_error = e.message
            self._logger.debug(
                f"VeyonService: webapi feature {method_name} on {host}: "
                f"FAIL (error {e.code}: {e.message})"
            )
            return False
        except requests.exceptions.Timeout as e:
            # WebAPI не ответил (veyon-service завис/перегружен ИЛИ хост
            # выключился между пингом и PUT). Закрываем persistent-клиент —
            # иначе «живой» (is_authenticated) клиент навсегда пропустит
            # fast-fail пинг и каждая фича будет платить PUT-таймаут.
            self._close_client(host)
            # Fallback на CLI оправдан только если veyon-cli есть — иначе
            # 45-секундный subprocess на уже сломанном пути лишь затянет
            # ожидание.
            return self._webapi_net_error_fallback(host, method_name, e)
        except requests.exceptions.ConnectionError as e:
            # Соединение с локальным WebAPI разорвано — та же логика.
            self._close_client(host)
            return self._webapi_net_error_fallback(host, method_name, e)
        except Exception as e:
            self._logger.debug(
                f"VeyonService: webapi feature {method_name} on {host}: "
                f"fallback to CLI (exception: {e})"
            )
            return None

    def _webapi_net_error_fallback(
        self, host: str, method_name: str, e: Exception
    ) -> Optional[bool]:
        """Сетевая ошибка WebAPI (Timeout/ConnectionError): fallback на CLI
        только если veyon-cli вообще есть на машине.

        На нодах без Veyon Master (нет veyon-cli) fallback превращался в
        45-секундный subprocess с гарантированным фейлом — вместо честной
        мгновенной ошибки. С CLI (обычная машина оператора) фолбэк сохранён:
        CLI может сработать, когда WebAPI-соединение флапнуло.
        """
        if not self.is_cli_available():
            self._last_error = (
                "Veyon WebAPI не ответил на хосте "
                f"{host} ({type(e).__name__}), CLI недоступен"
            )
            self._logger.debug(
                f"VeyonService: webapi feature {method_name} on {host}: "
                f"net error {type(e).__name__}, no CLI fallback"
            )
            return False
        self._logger.debug(
            f"VeyonService: webapi feature {method_name} on {host}: "
            f"net error {type(e).__name__}, fallback to CLI"
        )
        return None

    def _tcp_ping_skip(self, method_name: str, host: str) -> None:
        """Общая обработка SKIP после неудачного fast-fail пинга."""
        self._inc_metric("server_port_closed")
        self._last_error = "Veyon Server недоступен (хост офлайн или порт закрыт)"
        self._logger.debug(
            f"VeyonService: feature {method_name} on {host}: SKIP "
            f"(port {self._server_port} closed)"
        )

    def _tcp_ping_retried(self, host: str) -> bool:
        """TCP-пинг Veyon-порта хоста с одной повторной попыткой.

        Одиночный 1с-пинг флапает при загруженной сети/firewall-правилах
        (миниатюры при этом работают через persistent-клиент). Для ручных
        фич второго шанса достаточно: две попытки по 1.5с между собой
        ~3с — приемлемо для интерактивной операции.
        """
        if tcp_ping(host, self._server_port, timeout=1.5):
            return True
        self._logger.debug(
            f"VeyonService: tcp_ping {host}:{self._server_port} (1) failed, retrying"
        )
        time.sleep(0.3)
        return tcp_ping(host, self._server_port, timeout=1.5)

    def _invoke_feature(self, host: str, method_name: str, args: tuple = None) -> bool:
        """Вызвать Veyon feature на удалённом хосте.

        Маршрутизация транспорта (``VeyonConfig.feature_transport``):
          - ``"webapi"`` (default): PUT /api/v1/feature/{uid} через persistent
            WebAPI-клиент. Если клиент не существует (миниатюры выключены, хост
            офлайн) или соединение истекло — fallback на CLI.
          - ``"cli"``: ``veyon-cli feature start/stop <host> <uid> [json_args]``
            subprocess. Fire-and-forget: subprocess подключается к Veyon Server
            на хосте (порт 11100), отправляет feature-команду, завершается.

        Только методы из ``_WEBAPI_FEATURE_METHODS`` могут идти через WebAPI.
        ``logoff_user_webapi``, ``login_user`` всегда через CLI (асинхронные,
        риски чёрного экрана / ненадёжность).

        Fast-fail: перед любым транспортом быстрый TCP-пинг порта Veyon
        (1 сек). Офлайн-хост или неработающий Veyon дают мгновенный отказ
        вместо ~30-45 сек (subprocess veyon-cli сам ждёт TCP connect до
        истечения timeout=45).

        Args:
            host: адрес удалённой машины
            method_name: имя feature-метода ('lock_screen', 'unlock_screen', ...)
            args: позиционные аргументы (для login_user, start_app, text_message, ...)

        Returns:
            True при успехе, False при ошибке.
        """
        if not host:
            return False
        from src.utils.security.ban import is_banned

        if is_banned():
            self._logger.warning(f"VeyonService: {method_name} отклонён: команды запрещены")
            return False

        # Fast-fail: хост офлайн / Veyon не работает → не ждать таймаутов
        # PUT (10с) и veyon-cli (subprocess ~30-45с на закрытом порту).
        # Пинг выполняется ВСЕГДА: persistent-клиент (is_authenticated)
        # не гарантирует, что хост ещё жив — клиент мог быть создан,
        # когда хост был включён (validUntil в будущем после выключения),
        # и без пинга фича ждала бы PUT-таймаут + CLI на мёртвом хосте.
        # Живой клиент → 1 быстрый пинг (1с); мёртвый/нет → ретрай
        # (2×1.5с) против флапов.
        with self._webapi_global_lock:
            _live_client = self._webapi_clients.get(host)
            _has_live_client = (
                _live_client is not None and _live_client.is_authenticated()
            )
        if _has_live_client:
            if not tcp_ping(host, self._server_port, timeout=1.0):
                self._tcp_ping_skip(method_name, host)
                return False
        elif not self._tcp_ping_retried(host):
            self._tcp_ping_skip(method_name, host)
            return False

        # Routing: WebAPI-транспорт для синхронных фичей
        if (
            self._feature_transport == "webapi"
            and method_name in self._WEBAPI_FEATURE_METHODS
        ):
            result = self._invoke_feature_webapi(host, method_name, args)
            if result is not None:
                return result
            # None = fallback на CLI (нет persistent-клиента, истёк, conn limit)

        cli_entry = self._FEATURE_CLI_MAP.get(method_name)
        if cli_entry is None:
            self._logger.error(
                f"VeyonService: unknown feature method '{method_name}' for CLI"
            )
            return False

        feature_uid, action, arg_keys = cli_entry

        # Формируем JSON-аргументы для feature start (если есть)
        json_args_str = None
        if arg_keys is not None and args is not None:
            # Специальная обработка: start_app принимает строку или список
            if method_name == "start_app" and isinstance(args[0], str):
                args_payload = {arg_keys[0]: [args[0]]}
            else:
                args_payload = dict(zip(arg_keys, args))
            json_args_str = json.dumps(args_payload, ensure_ascii=False)

        # Строим команду: veyon-cli feature <action> <host> <uid> [json_args]
        cli_args = ["feature", action, host, feature_uid]
        if json_args_str is not None:
            cli_args.append(json_args_str)

        # Feature-команды могут требовать до 30с (подключение к Veyon Server +
        # ожидание подтверждения). _run_cli_elevated имеет timeout=15 —
        # используем отдельный subprocess.run с timeout=45.
        cli = self._cli_path_console
        cmd = [cli] + cli_args
        kwargs = {"capture_output": True, "timeout": 45}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self._logger.info(
                f"VeyonService: feature {method_name} on {host}: {' '.join(cli_args)}"
            )
            result = subprocess.run(cmd, env=build_clean_env(headless=True), **kwargs)
            if isinstance(result.stdout, bytes):
                result.stdout = result.stdout.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes):
                result.stderr = result.stderr.decode("utf-8", errors="replace")
            self._last_error = result.stderr or ""

            ok = self._cli_succeeded(result)
            if ok:
                if self._event_bus:
                    self._event_bus.publish_typed(
                        event_type=EventType.CUSTOM,
                        source=self.__class__.__name__,
                        data={
                            "feature": "veyon_feature",
                            "host": host,
                            "method": method_name,
                            "result": True,
                            "transport": "cli",
                        },
                    )
                self._logger.info(f"VeyonService: feature {method_name} on {host}: OK")
                return True
            self._logger.debug(
                f"VeyonService: feature {method_name} on {host}: FAIL "
                f"rc={result.returncode} stderr={result.stderr.strip()[:200]}"
            )
            return False
        except subprocess.TimeoutExpired:
            self._logger.debug(
                f"VeyonService: feature {method_name} on {host}: TIMEOUT (45s)"
            )
            self._last_error = "veyon-cli feature timeout"
            return False
        except FileNotFoundError:
            self._logger.error(f"VeyonService: veyon-cli not found at '{cli}'")
            self._last_error = "veyon-cli not found"
            return False
        except Exception as e:
            self._logger.debug(
                f"VeyonService: feature {method_name} exception on {host}: {e}"
            )
            self._last_error = str(e)
            return False

    # ========== Управление ключами аутентификации ==========

    def _run_cli_elevated(self, args: list) -> subprocess.CompletedProcess:
        """Выполнить команду veyon-cli напрямую."""
        cli = self._cli_path_console
        cmd = [cli] + args
        kwargs = {"capture_output": True, "timeout": 15}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(cmd, env=build_clean_env(headless=True), **kwargs)
            if isinstance(result.stdout, bytes):
                result.stdout = result.stdout.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes):
                result.stderr = result.stderr.decode("utf-8", errors="replace")
            self._last_error = result.stderr or ""

            return result
        except FileNotFoundError:
            self._last_error = "veyon-cli not found"
            return subprocess.CompletedProcess(cmd, -1, "", "veyon-cli not found")
        except Exception as e:
            self._last_error = str(e)
            return subprocess.CompletedProcess(cmd, -1, "", str(e))

    def _cli_succeeded(self, result: subprocess.CompletedProcess) -> bool:
        """Считать операцию veyon-cli успешной.

        veyon-wcli на Windows возвращает ненулевой код даже при успехе,
        но при этом stderr пуст. Поэтому успех = (returncode==0) ИЛИ (stderr пуст).
        """
        if result is None:
            return False
        if result.returncode == 0:
            return True
        err = result.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace") if err else ""
        return not (err or "").strip()

    def _run_cli_write(
        self, args: list, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
        """Выполнить write-команду veyon-cli с повышением прав по требованию.

        Write-операции (config set, authkeys, service) требуют прав
        администратора/root. Если приложение запущено elevated — команда
        выполняется напрямую (``_run_cli_elevated``). Иначе — через
        системный запрос авторизации (UAC на Windows / pkexec на Linux):
        пользователь подтверждает или отклоняет элевацию в диалоге.

        Windows-путь (ShellExecuteExW) не возвращает exit-код и вывод
        elevated-процесса, поэтому при успехе возвращается синтетический
        CompletedProcess(0). Фактический успех проверяйте чтением
        конфигурации/списка ключей (verify) в вызывающих методах.

        Args:
            args: Аргументы veyon-cli (без имени исполняемого файла).
            timeout: Ожидание elevated-процесса (пользователь может
                медленно подтверждать диалог авторизации).

        Returns:
            CompletedProcess (синтетический при not-elevated).
        """
        from src.utils.system.subprocess_env import is_running_elevated, run_elevated

        cli = self._cli_path_console
        cmd = [cli] + args
        if not is_running_elevated():
            ok, err = run_elevated(cmd, timeout=timeout)
            self._last_error = err or ""
            if ok:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, -1, "", err)
        return self._run_cli_elevated(args)

    def _run_shell_write(
        self, script: str, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
        """Выполнить shell-строку с повышением прав по требованию.

        Обёртка над ``_run_cli_write`` для батч-команд (несколько
        veyon-вызовов в одном elevated-процессе — один диалог авторизации
        вместо нескольких). Windows: ``cmd /s /c "<script>"``;
        Linux: ``bash -c "<script>"`` (при not-root — через pkexec).

        Returns:
            CompletedProcess (синтетический при not-elevated).
        """
        from src.utils.system.subprocess_env import is_running_elevated, run_elevated

        if sys.platform == "win32":
            comspec = os.environ.get("ComSpec", "cmd.exe")
            cmd = [comspec, "/s", "/c", script]
        else:
            cmd = ["bash", "-c", script]

        if not is_running_elevated():
            ok, err = run_elevated(cmd, timeout=timeout)
            self._last_error = err or ""
            if ok:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, -1, "", err)

        kwargs = {"capture_output": True, "timeout": timeout}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["env"] = build_clean_env(headless=True)
        try:
            result = subprocess.run(cmd, **kwargs)
            if isinstance(result.stdout, bytes):
                result.stdout = result.stdout.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes):
                result.stderr = result.stderr.decode("utf-8", errors="replace")
            self._last_error = result.stderr or ""
            return result
        except FileNotFoundError:
            self._last_error = "команда не найдена"
            return subprocess.CompletedProcess(cmd, -1, "", self._last_error)
        except subprocess.TimeoutExpired:
            self._last_error = f"Превышено время ожидания ({int(timeout)} сек)"
            return subprocess.CompletedProcess(cmd, -1, "", self._last_error)
        except Exception as e:
            self._last_error = str(e)
            return subprocess.CompletedProcess(cmd, -1, "", str(e))

    @staticmethod
    def _key_file_exists(name: str, key_type: str) -> bool:
        """Проверить наличие файла ключа Veyon в стандартной директории."""
        if sys.platform == "win32":
            base = os.path.join(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                "Veyon",
                "keys",
                key_type,
            )
        else:
            base = os.path.join("/etc/veyon/keys", key_type)
        return os.path.isfile(os.path.join(base, name, "key"))

    def _fix_export_ownership(self, file_path: str):
        """Linux, elevated-режим: вернуть файл экспорта исходному пользователю.

        veyon-cli от root создаёт root-owned файл — пользователь не сможет
        его перезаписать при следующем экспорте. При not-elevated запуске
        chown выполняется внутри pkexec-скрипта (см. export_auth_key).
        """
        if sys.platform == "win32" or not file_path:
            return
        try:
            if os.geteuid() != 0 or not os.path.isfile(file_path):
                return
            from src.utils.system.subprocess_env import _original_username

            user = _original_username()
            if user:
                subprocess.run(
                    ["chown", user, file_path], capture_output=True, timeout=5
                )
        except Exception:
            pass

    @staticmethod
    def _is_permission_error(result_or_str) -> bool:
        """Проверить, связана ли ошибка с недостатком прав."""
        if isinstance(result_or_str, str):
            err = result_or_str.lower()
        elif hasattr(result_or_str, "stderr"):
            err = (result_or_str.stderr or "").lower()
        else:
            return False
        return any(
            msg in err
            for msg in [
                "недоступна для записи",
                "not writable",
                "permission denied",
                "доступа для записи",
                "access denied",
                "access is denied",
                "operation not permitted",
                "requires root",
                "requires administrator",
            ]
        )

    def last_error(self) -> str:
        """Текст последней ошибки."""
        return self._last_error

    def list_auth_keys(self) -> list:
        """Получить список имён ключей аутентификации (без дубликатов private/public)."""
        result = self._run_cli_elevated(["authkeys", "list"])
        if not self._cli_succeeded(result):
            self._last_error = (
                f"Получение списка ключей не удалось. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
            return []
        stdout = result.stdout or ""
        names = set()
        for line in stdout.strip().splitlines():
            line = line.strip()
            if "/" in line:
                name = line.split("/")[0]
                if name:
                    names.add(name)
        return sorted(names)

    def list_auth_keys_detailed(self) -> list:
        """Получить детальный список ключей: [{name, type, pair_id, group}, ...]."""
        result = self._run_cli_elevated(["authkeys", "list", "details"])
        if not self._cli_succeeded(result):
            return []
        stdout = result.stdout or ""
        keys = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            # Пропускаем заголовки таблицы и разделители
            if not line.startswith("|"):
                continue
            if "ИМЯ" in line or "NAME" in line or "---" in line or "─" in line:
                continue
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                name = parts[0]
                key_type = parts[1]
                pair_id = parts[2] if len(parts) > 2 else ""
                group = parts[3] if len(parts) > 3 else ""
                keys.append(
                    {"name": name, "type": key_type, "pair_id": pair_id, "group": group}
                )
        # Постобработка: public-ключу заимствовать pair_id у одноимённого
        # private (Veyon CLI от обычного пользователя не может вычислить
        # хеш ключа, если файл нечитаем). Group НЕ заимствуется: у private
        # и public группы доступа независимы, а «???»/пустая группа —
        # признак того, что файл ограничен группой, в которую текущий
        # пользователь не входит. Маскировать это состояние нельзя.
        private_map = {}
        for k in keys:
            if k["type"] == "private":
                private_map[k["name"]] = k["pair_id"]
        for k in keys:
            if k["type"] == "public":
                pid = private_map.get(k["name"], "")
                if k["pair_id"] in ("???", "", None):
                    k["pair_id"] = pid
        return keys

    def create_auth_key(self, name: str) -> bool:
        """Создать новую пару ключей аутентификации (private + public).

        При not-elevated запуске появится системный запрос авторизации
        (UAC / pkexec). Успех проверяется фактическим наличием private-ключа.
        """
        if not name:
            return False
        result = self._run_cli_write(["authkeys", "create", name])
        ok = self._cli_succeeded(result) and self._key_file_exists(name, "private")
        if not ok:
            self._last_error = (
                f"Создание ключа {name} не удалось. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def delete_auth_key(self, name: str) -> bool:
        """Удалить пару ключей (private и public).

        Батчинг: оба удаления выполняются в одном elevated-процессе —
        один диалог авторизации вместо двух.
        """
        if not name:
            return False
        if sys.platform == "win32":
            q = f'"{self._cli_path_console}"'
            script = (
                f"{q} authkeys delete {name}/private "
                f"&& {q} authkeys delete {name}/public"
            )
        else:
            q = self._cli_path_console
            script = (
                f"'{q}' authkeys delete '{name}/private' && "
                f"'{q}' authkeys delete '{name}/public'"
            )
        result = self._run_shell_write(script)
        ok = self._cli_succeeded(result) and not (
            self._key_file_exists(name, "private")
            or self._key_file_exists(name, "public")
        )
        if not ok:
            self._last_error = (
                f"Удаление ключа {name} не удалось. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def delete_auth_key_single(self, name: str, key_type: str) -> bool:
        """Удалить один ключ (private или public)."""
        if not name or key_type not in ("private", "public"):
            return False
        result = self._run_cli_write(["authkeys", "delete", f"{name}/{key_type}"])
        ok = self._cli_succeeded(result) and not self._key_file_exists(name, key_type)
        if not ok:
            self._last_error = (
                f"Удаление ключа {name}/{key_type} не удалось. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def read_auth_key(self, name: str, key_type: str) -> str:
        """Read exactly the existing named key, without export or key fallback."""
        import re
        from pathlib import Path

        if key_type not in ("private", "public") or not re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}", name or ""
        ):
            raise ValueError("Недопустимое имя или тип ключа Veyon")
        base = (
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Veyon" / "keys"
            if sys.platform == "win32"
            else Path("/etc/veyon/keys")
        )
        value = (base / key_type / name / "key").read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError("Ключ Veyon пуст")
        return value

    def export_auth_key(self, name: str, key_type: str, file_path: str) -> bool:
        """Экспортировать ключ (private или public) в файл.

        При not-elevated (Linux) экспорт выполняется через pkexec-скрипт:
        veyon-cli export + chown файла исходному пользователю — иначе
        root-owned файл будет недоступен пользователю для перезаписи.
        """
        if key_type not in ("private", "public"):
            return False
        result = self._run_cli_write(
            ["authkeys", "export", f"{name}/{key_type}", file_path]
        )
        ok = self._cli_succeeded(result)
        # Дополнительная проверка: файл должен существовать и быть непустым
        if ok:
            import os

            if not os.path.isfile(file_path):
                self._last_error = (
                    f"Экспорт ключа {name}/{key_type} завершился без ошибки, "
                    f"но файл {file_path} не создан. "
                    f"stdout: {result.stdout or ''}"
                )
                self._logger.error(self._last_error)
                return False
            if os.path.getsize(file_path) == 0:
                self._last_error = (
                    f"Файл ключа {file_path} пустой после экспорта {name}/{key_type}. "
                    f"stdout: {result.stdout or ''}"
                )
                self._logger.error(self._last_error)
                return False
            # Linux, elevated-запуск: файл создан root — вернуть пользователю
            self._fix_export_ownership(file_path)
        else:
            self._last_error = (
                f"Экспорт ключа {name}/{key_type} не удался. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def import_auth_key(self, name: str, key_type: str, file_path: str) -> bool:
        """Импортировать ключ (private или public) из файла."""
        if key_type not in ("private", "public"):
            return False
        result = self._run_cli_write(
            ["authkeys", "import", f"{name}/{key_type}", file_path]
        )
        ok = self._cli_succeeded(result) and self._key_file_exists(name, key_type)
        if not ok:
            self._last_error = (
                f"Импорт ключа {name}/{key_type} не удался. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def extract_public_key(self, name: str) -> bool:
        """Извлечь открытый ключ из закрытого."""
        result = self._run_cli_write(["authkeys", "extract", name])
        ok = self._cli_succeeded(result) and self._key_file_exists(name, "public")
        if not ok:
            self._last_error = (
                f"Извлечение публичного ключа из {name} не удалось. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def set_key_access_group(self, name: str, key_type: str, group: str) -> bool:
        """Установить группу доступа для ключа.

        Fail-fast: группа проверяется на существование до элевации —
        опечатка даёт ошибку без UAC/pkexec-диалога.

        Успех проверяется read-back (veyon-cli authkeys list details):
        Windows-версия Veyon ставит группу владельцем файла ключа и заменяет
        DACL на «группа:R + Администраторы:F». Если текущий пользователь
        не входит в группу, ключ становится нечитаем — read-back возвращает
        пустую группу/«???», что является ОЖИДАЕМЫМ результатом, а не ошибкой.
        """
        group = (group or "").strip()
        if not group:
            self._last_error = "Имя группы доступа не задано"
            return False
        if not self._group_exists(group):
            self._last_error = (
                f"Группа '{group}' не найдена на этом компьютере. "
                f"Проверьте имя группы (например, «Пользователи» или «Администраторы»)."
            )
            self._logger.error(self._last_error)
            return False

        result = self._run_cli_write(
            ["authkeys", "setaccessgroup", f"{name}/{key_type}", group]
        )
        ok = self._cli_succeeded(result)
        if not ok:
            self._last_error = (
                f"Изменение группы доступа для {name}/{key_type} не удалось. "
                f"stderr: {result.stderr or ''}, stdout: {result.stdout or ''}"
            )
            self._logger.error(self._last_error)
            return ok

        # Read-back verify: группа должна совпасть с фактической.
        # Пустая группа в read-back = ключ ограничен группой, в которую
        # пользователь не входит (не может прочитать) — считаем успехом.
        actual = self._read_back_access_group(name, key_type)
        if actual and not self._groups_equivalent(actual, group):
            self._last_error = (
                f"Изменение группы доступа для {name}/{key_type} не подтвердилось: "
                f"фактическая группа '{actual}', ожидалась '{group}'."
            )
            self._logger.error(self._last_error)
            return False
        return True

    @staticmethod
    def _normalize_group_name(group: str) -> str:
        """Нормализовать имя группы для сравнения: без префикса домена/BUILTIN, lower."""
        return group.replace("BUILTIN\\", "").rsplit("\\", 1)[-1].strip().lower()

    @classmethod
    def _groups_equivalent(cls, a: str, b: str) -> bool:
        """Сравнить имена групп: 'BUILTIN\\Пользователи' == 'Пользователи'."""
        return cls._normalize_group_name(a) == cls._normalize_group_name(b)

    def _read_back_access_group(self, name: str, key_type: str) -> str:
        """Перечитать фактическую группу доступа ключа (для verify)."""
        for entry in self.list_auth_keys_detailed():
            if entry.get("name") == name and entry.get("type") == key_type:
                return entry.get("group", "") or ""
        return ""

    def _group_exists(self, group: str) -> bool:
        """Проверить существование группы на локальной машине (без элевации)."""
        if sys.platform == "win32":
            return self._group_exists_windows(group)
        return self._group_exists_unix(group)

    @staticmethod
    def _group_exists_windows(group: str) -> bool:
        """Windows: LookupAccountNameW — существует ли учётная запись/группа."""
        import ctypes
        from ctypes import wintypes

        sid = ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
        cb_sid = wintypes.DWORD(68)
        domain = ctypes.create_unicode_buffer(256)
        cb_domain = wintypes.DWORD(256)
        use = wintypes.DWORD()
        name = group.replace("BUILTIN\\", "")
        ok = ctypes.windll.advapi32.LookupAccountNameW(
            None,
            name,
            sid,
            ctypes.byref(cb_sid),
            domain,
            ctypes.byref(cb_domain),
            ctypes.byref(use),
        )
        return bool(ok)

    @staticmethod
    def _group_exists_unix(group: str) -> bool:
        """Unix: getent group <name> (exit 0 = существует)."""
        try:
            kwargs = {"capture_output": True, "timeout": 5}
            if sys.platform != "win32":
                kwargs["env"] = build_clean_env(headless=True)
            result = subprocess.run(["getent", "group", group], **kwargs)
            return result.returncode == 0
        except Exception:
            # getent недоступен — не блокировать операцию
            return True

    def list_user_groups(self, include_domain: bool = False) -> list:
        """Получить список групп пользователей (локальных + опционально доменных).

        Поведение повторяет Veyon UserGroupsBackendManager:
        - Windows: NetLocalGroupEnum (net localgroup) + net group /domain при include_domain
        - Linux/macOS: getent group (+ доменные через getent group 2>/dev/null; LDAP/SSSD)

        Args:
            include_domain: включить доменные группы (медленнее, требует DC/SSSD)

        Returns:
            Список имён групп (отсортированный, без дубликатов).
        """
        if sys.platform == "win32":
            groups = self._list_local_groups_windows()
            if include_domain:
                groups.extend(self._list_domain_groups_windows())
        else:
            groups = self._list_local_groups_unix()
            if include_domain:
                groups.extend(self._list_domain_groups_unix())

        # Дедупликация с сохранением порядка, затем сортировка
        seen = set()
        unique = []
        for g in groups:
            if g and g not in seen:
                seen.add(g)
                unique.append(g)
        unique.sort(key=str.lower)
        return unique

    @staticmethod
    def _run_capture(args: list, timeout: int = 10) -> Optional[str]:
        """Выполнить команду и вернуть декодированный stdout или None при ошибке.

        На Windows консольные утилиты (net localgroup, net group) выводят в OEM-кодировке
        (cp866 для ru-RU), которую определяем через GetOEMCP(). getpreferredencoding
        здесь ненадёжна — она возвращает cp1251/utf-8 (ANSI), а не OEM.
        """
        try:
            kwargs = {"capture_output": True, "timeout": timeout}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(args, **kwargs)
            out = result.stdout
            if isinstance(out, bytes):
                if sys.platform == "win32":
                    enc = VeyonService._windows_oem_encoding()
                else:
                    enc = locale.getpreferredencoding(False) or "utf-8"
                out = out.decode(enc, errors="replace")
            return out or ""
        except Exception:
            return None

    @staticmethod
    def _windows_oem_encoding() -> str:
        """OEM-кодировка консоли Windows (cp866 для ru-RU и т.д.)."""
        try:
            import ctypes

            cp = ctypes.windll.kernel32.GetOEMCP()
            return f"cp{cp}"
        except Exception:
            return "cp866"

    def _list_local_groups_windows(self) -> list:
        """Локальные псевдогруппы Windows через `net localgroup`."""
        out = self._run_capture(["net", "localgroup"], timeout=10)
        if not out:
            return []
        groups = []
        # Строки вида "*Администраторы" между разделителями "---" и "The command completed"
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("*"):
                name = line[1:].strip()
                if name:
                    groups.append(name)
        return groups

    def _list_domain_groups_windows(self) -> list:
        """Доменные группы Windows через `net group /domain`."""
        out = self._run_capture(["net", "group", "/domain"], timeout=20)
        if not out:
            return []
        groups = []
        # Заголовок: "Group Accounts for \\DOMAIN" затем "---" и строки с *GroupName
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("*"):
                name = line[1:].strip()
                if name:
                    groups.append(name)
        return groups

    def _list_local_groups_unix(self) -> list:
        """Локальные группы Unix через `getent group` с fallback на /etc/group."""
        out = self._run_capture(["getent", "group"], timeout=10)
        if out:
            return [ln.split(":", 1)[0] for ln in out.splitlines() if ln.strip()]
        # Fallback: чтение /etc/group напрямую
        try:
            with open("/etc/group", "r", encoding="utf-8", errors="replace") as f:
                return [ln.split(":", 1)[0] for ln in f.readlines() if ln.strip()]
        except Exception:
            return []

    def _list_domain_groups_unix(self) -> list:
        """Доменные группы Unix (SSSD/LDAP) — getent group уже включает их при настроенном SSSD.

        Дополнительный запрос не требуется; возвращаем пустой список, чтобы
        не дублировать локальные группы. При настроенном SSSD доменные группы
        уже присутствуют в выводе getent group.
        """
        return []

    # ========== Управление WebAPI и сервисом ==========

    def get_webapi_enabled(self) -> bool:
        """Проверить, включен ли WebAPI сервер в конфигурации Veyon."""
        result = self._run_cli_elevated(["config", "get", "WebAPI/HttpServerEnabled"])
        stdout = result.stdout or ""
        return "true" in stdout.lower()

    def get_webapi_port(self) -> int:
        """Получить порт WebAPI сервера из конфигурации Veyon."""
        result = self._run_cli_elevated(["config", "list"])
        stdout = result.stdout or ""
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("WebAPI/HttpServerPort="):
                try:
                    return int(line.split("=")[1])
                except (ValueError, IndexError):
                    return 11080
        return 11080

    def set_webapi_enabled(self, enabled: bool) -> bool:
        """Включить/выключить WebAPI сервер (с элевацией по требованию)."""
        value = "true" if enabled else "false"
        result = self._run_cli_write(
            ["config", "set", "WebAPI/HttpServerEnabled", value]
        )
        ok = self._cli_succeeded(result) and self.get_webapi_enabled() == enabled
        if not ok:
            self._last_error = (
                f"Изменение состояния WebAPI не удалось. stderr: {result.stderr or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def set_webapi_port(self, port: int) -> bool:
        """Установить порт WebAPI сервера (с элевацией по требованию)."""
        result = self._run_cli_write(
            ["config", "set", "WebAPI/HttpServerPort", str(port)]
        )
        ok = self._cli_succeeded(result) and self.get_webapi_port() == port
        if not ok:
            self._last_error = (
                f"Изменение порта WebAPI не удалось. stderr: {result.stderr or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def get_webapi_connection_limit(self) -> int:
        """Получить лимит одновременных WebAPI-соединений из конфигурации Veyon.

        Returns:
            Текущее значение WebAPI/ConnectionLimit (default 32).
        """
        result = self._run_cli_elevated(["config", "get", "WebAPI/ConnectionLimit"])
        stdout = (result.stdout or "").strip()
        try:
            val = int(stdout)
            return val if val > 0 else 32
        except (ValueError, TypeError):
            return 32

    def set_webapi_connection_limit(self, limit: int) -> bool:
        """Установить лимит одновременных WebAPI-соединений.

        Соответствует upstream ``WebAPI/ConnectionLimit``
        (WebApiConfiguration.h, default 32). Степень двойки
        (32/64/128/256/512). С элевацией по требованию.

        Args:
            limit: новый лимит (32-1024).

        Returns:
            True при успехе (проверено перечитыванием конфигурации).
        """
        result = self._run_cli_write(
            ["config", "set", "WebAPI/ConnectionLimit", str(int(limit))]
        )
        ok = self._cli_succeeded(result) and (
            self.get_webapi_connection_limit() == int(limit)
        )
        if not ok:
            self._last_error = (
                f"Изменение лимита соединений WebAPI не удалось. "
                f"stderr: {result.stderr or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def get_auth_method(self) -> Optional[int]:
        """Получить метод аутентификации Veyon (0=none, 1=key file, 2=password)."""
        result = self._run_cli_elevated(["config", "get", "Authentication/Method"])
        stdout = (result.stdout or "").strip()
        try:
            return int(stdout)
        except (ValueError, TypeError):
            return None

    def set_auth_method_key_file(self) -> bool:
        """Включить метод аутентификации по ключу (Authentication/Method=1).

        С элевацией по требованию.
        """
        result = self._run_cli_write(["config", "set", "Authentication/Method", "1"])
        ok = self._cli_succeeded(result) and self.get_auth_method() == 1
        if not ok:
            self._last_error = (
                f"Включение аутентификации по ключу не удалось. "
                f"stderr: {result.stderr or ''}"
            )
            self._logger.error(self._last_error)
        return ok

    def get_service_status(self) -> str:
        """Получить статус Veyon Service.

        Приоритет: системная проверка (systemctl/sc query) > veyon-cli service status.
        Результат кэшируется на `_SERVICE_STATUS_CACHE_TTL` секунд (5с), чтобы
        частые опросы из Pool Monitor (каждые 500мс) не молотили subprocess.
        Сбрасывается через `invalidate_installed_cache()`.
        """
        now = time.time()
        if (
            self._service_status_cache is not None
            and (now - self._service_status_at) < self._SERVICE_STATUS_CACHE_TTL
        ):
            return self._service_status_cache
        result = self._get_service_status_uncached()
        self._service_status_cache = result
        self._service_status_at = now
        return result

    def _get_service_status_uncached(self) -> str:
        """Фактическая проверка статуса сервиса (subprocess). Не кэшируется."""
        # 1. Системная проверка фактического статуса процесса
        if sys.platform == "win32":
            try:
                r = subprocess.run(
                    ["sc", "query", "VeyonService"],
                    capture_output=True,
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                out = r.stdout.decode("utf-8", errors="replace").lower()
                if "running" in out and "state" in out:
                    return "running"
                if "stopped" in out and "state" in out:
                    return "stopped"
            except Exception:
                pass
        else:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", "veyon"],
                    capture_output=True,
                    timeout=5,
                    env=build_clean_env(),
                )
                out = r.stdout.decode("utf-8", errors="replace").strip().lower()
                if out == "active":
                    return "running"
                if out in ("inactive", "failed", "dead"):
                    return "stopped"
            except Exception:
                pass

        # 2. Fallback: veyon-cli service status
        cli = self._cli_path
        cmd = [cli, "service", "status"]
        kwargs = {
            "capture_output": True,
            "timeout": 10,
            "env": build_clean_env(headless=False),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(cmd, **kwargs)
            try:
                stdout = (
                    result.stdout.decode("utf-8", errors="replace")
                    if isinstance(result.stdout, bytes)
                    else (result.stdout or "")
                )
            except Exception:
                stdout = str(result.stdout) if result.stdout else ""
            try:
                stderr = (
                    result.stderr.decode("utf-8", errors="replace")
                    if isinstance(result.stderr, bytes)
                    else (result.stderr or "")
                )
            except Exception:
                stderr = str(result.stderr) if result.stderr else ""
            output = (stdout + " " + stderr).lower()

            if any(x in output for x in ["running", "работает", "active", "запущен"]):
                return "running"
            if any(
                x in output
                for x in [
                    "not running",
                    "не работает",
                    "inactive",
                    "stopped",
                    "dead",
                    "остановлен",
                ]
            ):
                return "stopped"
            if result.returncode == 0 and output.strip():
                return "running"
            if result.returncode != 0 and not output.strip():
                return "stopped"
        except Exception:
            pass
        return "unknown"

    def start_service(self) -> bool:
        """Запустить Veyon Service (с элевацией по требованию)."""
        from src.utils.system.subprocess_env import is_running_elevated

        result = self._run_cli_write(["service", "start"], timeout=60)
        if self._cli_succeeded(result):
            import time

            time.sleep(1)
            ok = self.get_service_status() == "running"
            if ok:
                self._publish_state_changed()
            return ok
        # Fallback
        if sys.platform == "win32":
            if not is_running_elevated():
                try:
                    from src.utils.system.subprocess_env import run_elevated

                    run_elevated(["net", "start", "VeyonService"], timeout=60)
                except Exception:
                    pass
            else:
                try:
                    subprocess.run(
                        ["net", "start", "VeyonService"],
                        capture_output=True,
                        timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception:
                    pass
        else:
            ok, _err = self._run_elevated_systemctl(["start"], timeout=60)
            if not ok:
                try:
                    subprocess.run(
                        ["systemctl", "start", "veyon"],
                        capture_output=True,
                        timeout=15,
                        env=build_clean_env(),
                    )
                except Exception:
                    pass
        import time

        time.sleep(1)
        ok = self.get_service_status() == "running"
        if ok:
            self._publish_state_changed()
        return ok

    def stop_service(self) -> bool:
        """Остановить Veyon Service (с элевацией по требованию)."""
        from src.utils.system.subprocess_env import is_running_elevated

        result = self._run_cli_write(["service", "stop"], timeout=60)
        if self._cli_succeeded(result):
            import time

            time.sleep(1)
            ok = self.get_service_status() == "stopped"
            if ok:
                self._publish_state_changed()
            return ok
        # Fallback
        if sys.platform == "win32":
            if not is_running_elevated():
                try:
                    from src.utils.system.subprocess_env import run_elevated

                    run_elevated(["net", "stop", "VeyonService"], timeout=60)
                except Exception:
                    pass
            else:
                try:
                    subprocess.run(
                        ["net", "stop", "VeyonService"],
                        capture_output=True,
                        timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception:
                    pass
        else:
            ok, _err = self._run_elevated_systemctl(["stop"], timeout=60)
            if not ok:
                try:
                    subprocess.run(
                        ["systemctl", "stop", "veyon"],
                        capture_output=True,
                        timeout=15,
                        env=build_clean_env(),
                    )
                except Exception:
                    pass
        import time

        time.sleep(1)
        ok = self.get_service_status() == "stopped"
        if ok:
            self._publish_state_changed()
        return ok

    def _run_elevated_systemctl(self, args: list, timeout: float = 60.0):
        """Linux: выполнить systemctl-команду с элевацией по требованию.

        Returns:
            ``(ok, err)`` — как run_elevated.
        """
        from src.utils.system.subprocess_env import is_running_elevated, run_elevated

        cmd = ["systemctl"] + args + ["veyon"]
        if is_running_elevated():
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    env=build_clean_env(),
                )
                ok = result.returncode == 0
                err = (
                    (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    if result.returncode != 0
                    else ""
                )
                return ok, err
            except Exception as e:
                return False, str(e)
        return run_elevated(cmd, timeout=timeout)

    def restart_service(self) -> bool:
        """Перезапустить Veyon Service.

        Возвращает True только при фактическом перезапуске: после операции
        сервис должен быть running, а PID процесса veyon-service должен
        измениться (если сервис был запущен до вызова). Иначе — False с
        пояснением в ``last_error()``. Это защищает от «молчаливых» отказов
        veyon-cli при недостатке прав (успешный exit-код, но рестарта нет).

        Если приложение запущено без прав администратора — операция
        выполняется через системный запрос авторизации (UAC на Windows /
        pkexec на Linux); при отказе пользователя возвращается False.
        """
        import time as _t
        from src.utils.system.subprocess_env import is_running_elevated, run_elevated

        old_pid = self._get_service_pid()
        self._last_error = ""
        # Текст ошибки элевации: exit-код net.exe не считается провалом
        # перезапуска — фактический исход определяет верификация ниже
        # («service has not been started» при остановленном сервисе —
        # штатная ситуация для restart).
        elevation_err = ""

        # ── Попытка перезапуска ──
        if is_running_elevated():
            result = self._run_cli_elevated(["service", "restart"])
            if not self._cli_succeeded(result):
                # Fallback
                if sys.platform != "win32":
                    try:
                        subprocess.run(
                            ["systemctl", "restart", "veyon"],
                            capture_output=True,
                            timeout=20,
                            env=build_clean_env(),
                        )
                    except Exception:
                        pass
                else:
                    self.stop_service()
                    _t.sleep(2)
                    self.start_service()
        else:
            # Без прав: прямой veyon-cli не сможет перезапустить сервис —
            # сразу идём через системный диалог (UAC / polkit).
            # Windows: "&" (не "&&") — net start выполняется и когда сервис
            # был остановлен (net stop вернёт код 2 «не запущена»).
            if sys.platform == "win32":
                cmd = ["cmd", "/c", "net stop VeyonService & net start VeyonService"]
            else:
                cmd = ["systemctl", "restart", "veyon"]
            ok, err = run_elevated(cmd, timeout=60)
            if not ok:
                # Не выходя из метода: верификация ниже (статус + PID)
                # определит, выполнен ли перезапуск фактически. Это лечит
                # «Команда завершилась с кодом 2» от net stop на
                # остановленном сервисе при реально удавшемся старте.
                elevation_err = err or "Операция отклонена или не выполнена"
                self._logger.warning(
                    f"VeyonService: restart via elevation reported failure "
                    f"({self._last_error or elevation_err}) — verifying actual service state"
                )

        # ── Верификация: ждём новый PID и статус running ──
        # Сервис стартует не мгновенно; также stop/start на Windows может
        # задержаться. Поллинг до 25 сек.
        deadline = _t.monotonic() + 25
        new_pid = None
        while _t.monotonic() < deadline:
            _t.sleep(2)
            self._service_status_cache = None
            self._service_status_at = 0.0
            status = self.get_service_status()
            new_pid = self._get_service_pid()
            if status == "running":
                break

        if self.get_service_status() != "running":
            self._last_error = elevation_err or "Сервис не запущен после перезапуска"
            self._logger.error(
                f"VeyonService: not running after restart (old_pid={old_pid}, new_pid={new_pid})"
            )
            return False

        if old_pid is not None and new_pid == old_pid:
            self._last_error = (
                elevation_err
                or "Перезапуск не выполнен — процесс сервиса не изменился (недостаточно прав?)"
            )
            self._logger.error(
                f"VeyonService: restart did not change pid (pid={old_pid}) — likely silent permission failure"
            )
            return False

        self._last_error = ""
        self.invalidate_server_stats_cache()
        self._publish_state_changed()
        return True

    def _get_service_start_mode_windows(self) -> str:
        """Старт-тип сервиса VeyonService на Windows: 'auto'|'manual'|'disabled'|'unknown'.

        Парсинг реестра (HKLM\\SYSTEM\\CurrentControlSet\\Services\\VeyonService,
        значение Start: 2=auto, 3=manual, 4=disabled) — НЕ вывод `sc qc`,
        который локализован («START_TYPE» существует только в английской
        локали; на русской — «ТИП_ЗАПУСКА»).
        """
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\VeyonService",
                0,
                winreg.KEY_READ,
            ) as key:
                start, _ = winreg.QueryValueEx(key, "Start")
                return {2: "auto", 3: "manual", 4: "disabled"}.get(
                    int(start), "unknown"
                )
        except Exception:
            return "unknown"

    def _set_service_start_mode_windows(self, mode: str) -> bool:
        """Установить старт-тип VeyonService (`sc config ... start= auto|demand|disabled`).

        Замена `veyon-cli service register` для уже установленного сервиса:
        upstream `install()` делает CreateService, который на существующем
        сервисе возвращает ERROR_SERVICE_EXISTS → install()=false →
        setStartMode() не вызывается (проверено на 4.11.2: exit 0, но
        Start=3 в реестре). `sc config` меняет старт-тип напрямую.

        Returns:
            True если режим фактически установлен (verify через реестр).
        """
        from src.utils.system.subprocess_env import is_running_elevated, run_elevated

        sc_value = {"auto": "auto", "manual": "demand", "disabled": "disabled"}.get(
            mode
        )
        if sc_value is None:
            self._last_error = f"Неизвестный режим автозапуска: {mode}"
            return False

        cmd = ["sc", "config", "VeyonService", "start=", sc_value]
        if is_running_elevated():
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                ok = r.returncode == 0
                if not ok:
                    self._last_error = (r.stderr or b"").decode(
                        "utf-8", errors="replace"
                    ).strip() or f"sc config завершилась с кодом {r.returncode}"
            except Exception as e:
                ok = False
                self._last_error = str(e)
        else:
            ok, err = run_elevated(cmd, timeout=30)
            self._last_error = err or ""
            if not ok and not err:
                self._last_error = "Операция отклонена или не выполнена"

        # Verify: фактическое состояние реестра, не exit-код
        actual = self._get_service_start_mode_windows()
        if ok and actual != mode:
            self._last_error = (
                f"Режим автозапуска не применён (требуется {mode}, фактически {actual})"
            )
            return False
        return ok and actual == mode

    def register_service(self) -> bool:
        """Зарегистрировать Veyon Service в системе (автозапуск).

        С элевацией по требованию; успех проверяется фактическим
        состоянием автозапуска.

        Windows: `veyon-cli service register` НЕ включает автозапуск
        у уже установленного сервиса (CreateService → ERROR_SERVICE_EXISTS,
        см. `_set_service_start_mode_windows`), поэтому основной путь —
        `sc config VeyonService start= auto`; CLI вызывается только если
        сервис ещё не установлен.
        """
        if sys.platform == "win32":
            if self._get_service_start_mode_windows() == "unknown":
                # Сервис не установлен (нет ключа реестра) — CLI создаст его.
                self._run_cli_write(["service", "register"], timeout=60)
                if self._get_service_start_mode_windows() == "unknown":
                    self._last_error = (
                        "Сервис Veyon не установлен — veyon-cli service register "
                        "не смог его зарегистрировать"
                    )
                    return False
            return self._set_service_start_mode_windows("auto")
        result = self._run_cli_write(["service", "register"])
        if self._cli_succeeded(result):
            return True
        self._run_elevated_systemctl(["enable"], timeout=60)
        try:
            r = subprocess.run(
                ["systemctl", "is-enabled", "veyon"],
                capture_output=True,
                timeout=5,
                env=build_clean_env(),
            )
            return r.returncode == 0
        except Exception:
            return False

    def unregister_service(self) -> bool:
        """Отменить регистрацию Veyon Service (без автозапуска).

        С элевацией по требованию; успех проверяется фактическим
        состоянием автозапуска.

        Windows: НЕ вызывает `veyon-cli service unregister` — он удаляет
        сервис целиком (stop + DeleteService), после чего WebAPI/миниатюры
        перестают работать до переустановки Veyon. «Убрать из автозапуска»
        = `sc config VeyonService start= demand` (старт вручную).
        """
        if sys.platform == "win32":
            mode = self._get_service_start_mode_windows()
            if mode == "unknown":
                self._last_error = "Сервис VeyonService не найден"
                return False
            if mode == "disabled":
                return True  # уже отключён — идемпотентно
            return self._set_service_start_mode_windows("manual")
        result = self._run_cli_write(["service", "unregister"])
        if self._cli_succeeded(result):
            return True
        self._run_elevated_systemctl(["disable"], timeout=60)
        try:
            r = subprocess.run(
                ["systemctl", "is-enabled", "veyon"],
                capture_output=True,
                timeout=5,
                env=build_clean_env(),
            )
            # is-enabled возвращает ненулевой код, если сервис не в автозапуске
            return r.returncode != 0
        except Exception:
            return False
