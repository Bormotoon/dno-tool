"""
Veyon WebAPI Python-клиент (переписан с нуля по протоколу upstream).

Протокол: REST HTTP на WebAPI-сервере Veyon (порт 11080 по умолчанию).
Аутентификация — по Veyon key-file (RSA). Канонический паттерн Veyon:
одна авторизация → много GET /framebuffer по таймеру. Соединение живёт
по Connection-Uid независимо от HTTP-сокетов; idle-таймер сервера (60 сек)
сбрасывается каждым авторизованным запросом, lifetime — 3 часа (hard cap,
возвращается в поле validUntil ответа auth).

Ссылки на upstream (ветка main, plugins/webapi/):
  - WebApiController.cpp: performAuthentication, checkConnection, getFramebuffer,
    listFeatures, getFeatureStatus, setFeatureStatus
  - WebApiConnection.cpp: encodedFramebufferData (кэш 1 сек, preencode heuristic)
  - WebApiConfiguration.h: ConnectionLimit=32, ConnectionIdleTimeout=60,
    ConnectionLifetime=3, ConnectionAuthenticationTimeout=15

Клиент использует requests.Session для HTTP keep-alive (переиспользование
TCP-сокета между запросами). Поддерживает как framebuffer (миниатюры), так
и управление фичами (lock/unlock/message/...) через PUT /api/v1/feature/{uid}.
Транспорт фичей выбирается в VeyonConfig.feature_transport ("webapi" | "cli").
"""

from __future__ import annotations

import json
import logging
import time
from typing import ClassVar

import requests

_logger = logging.getLogger(__name__)


class WebAPIClient:
    """Клиент Veyon WebAPI для доступа к framebuffer (миниатюрам) и управления фичами.

    Поток получения миниатюр (канонический паттерн Veyon):
        1. client = WebAPIClient(api_url, host)
        2. client.authenticate_with_key_file(keyname, keydata)
        3. client.wait_for_framebuffer(timeout=10)   # опционально, при cold start
        4. image = client.get_image(width, height, format, compression, quality)
        5. ... повторять шаг 4 по таймеру (idle-таймер сервера сбрасывается) ...
        6. client.close_connection()                  # при остановке

    Соединение персистентно: между запросами VNC-стрим на хосте активен,
    framebuffer обновляется в фоне, get_image мгновенно отдаёт свежий кадр
    из 1-сек кэша сервера.

    Управление фичами (lock/unlock/message/...) через то же соединение:
        client.set_feature_status(uid, active=True, arguments={"text": "..."})
        client.get_feature_status(uid)  # → bool
        client.list_features()          # → [{"name", "uid", "parentUid", "active"}, ...]
    """

    # ── Feature UIDs (совпадают с Veyon core, используются VeyonService._FEATURE_CLI_MAP) ──
    FEATURE_SCREEN_LOCK = "ccb535a2-1d24-4cc1-a709-8b47d2b2ac79"
    FEATURE_LOCK_INPUT_DEVICES = "e4a77879-e544-4fec-bc18-e534f33b934c"
    FEATURE_USER_LOGOFF = "7311d43d-ab53-439e-a03a-8cb25f7ed526"
    FEATURE_POWER_DOWN = "6f5a27a0-0e2f-496e-afcc-7aae62eede10"
    FEATURE_REBOOT = "4f7d98f0-395a-4fff-b968-e49b8d0f748c"
    FEATURE_USER_LOGIN = "7310707d-3918-460d-a949-65bd152cb958"
    FEATURE_START_APP = "da9ca56a-b2ad-4fff-8f8a-929b2927b442"
    FEATURE_OPEN_WEBSITE = "8a11a75d-b3db-48b6-b9cb-f8422ddd5b0c"
    FEATURE_TEXT_MESSAGE = "e75ae9c8-ac17-4d00-8f0d-019348346208"

    # ── Auth method UIDs (WebApiAuthenticationProxy.cpp) ──
    AUTH_METHOD_KEY_FILE = "0c69b301-81b4-42d6-8fae-128cdd113314"
    AUTH_METHOD_LOGON = "63611f7c-b457-42c7-832e-67d0f9281085"

    # ── Error codes (WebApiController.h::enum class Error) ──
    ERROR_NONE = 0
    ERROR_INVALID_DATA = 1
    ERROR_INVALID_CONNECTION = 2
    ERROR_INVALID_FEATURE = 3
    ERROR_INVALID_CREDENTIALS = 4
    ERROR_AUTH_METHOD_NOT_AVAILABLE = 5
    ERROR_AUTH_FAILED = 6
    ERROR_CONN_LIMIT_REACHED = 7
    ERROR_CONN_TIMED_OUT = 8
    ERROR_UNSUPPORTED_IMAGE_FORMAT = 9
    ERROR_FRAMEBUFFER_NOT_AVAILABLE = 10
    ERROR_FRAMEBUFFER_ENCODING_ERROR = 11
    ERROR_PROTOCOL_MISMATCH = 12

    # Заголовок auth-токена (сервер сравнивает case-insensitively)
    CONNECTION_UID_HEADER = "Connection-Uid"

    # Never expose a server-supplied message: it may echo credentials or tokens.
    _ERROR_MESSAGES: ClassVar[dict[int, str]] = {
        1: "Invalid request data",
        2: "Invalid connection",
        3: "Invalid feature",
        4: "Invalid credentials",
        5: "Authentication method unavailable",
        6: "Authentication failed",
        7: "Connection limit reached",
        8: "Connection timed out",
        9: "Unsupported image format",
        10: "Framebuffer unavailable",
        11: "Framebuffer encoding failed",
        12: "Protocol mismatch",
    }

    class Error(RuntimeError):
        """Parse only a numeric server code, with a locally defined safe message."""

        def __init__(self, error) -> None:
            self.code: int = -1
            try:
                error_object = json.loads(error)
                code = error_object["error"]["code"]
                if isinstance(code, int) and not isinstance(code, bool):
                    self.code = code
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            self.message = WebAPIClient._ERROR_MESSAGES.get(
                self.code, "Invalid WebAPI response"
            )
            super().__init__(f"WebAPI error {self.code}: {self.message}")

    def __init__(self, api_url: str, client: str) -> None:
        """Args:
        api_url: базовый URL WebAPI, например 'http://localhost:11080/api/v1/'
        client: адрес удалённой машины (host или IP)
        """
        self.api_url = api_url
        self.client = client
        self._session = requests.Session()
        self._connection_uid: str | None = None
        self._valid_until: float = 0.0
        self._authenticated_at: float = 0.0
        self.last_auth_error: WebAPIClient.Error | None = None
        self.last_auth_error_code: str | None = None

    def __del__(self) -> None:
        self.close_connection()

    # ── Публичный API ──

    def is_authenticated(self) -> bool:
        """Аутентифицировано ли соединение и не истёк ли lifetime (3 часа).

        Сервер удаляет соединение по истечении ConnectionLifetime (validUntil
        из ответа auth). После этого любой запрос вернёт InvalidConnection (2).
        Проверка _valid_until позволяет клиенту заранее знать срок и
        пересоздать соединение до получения ошибки.
        """
        return self._connection_uid is not None and time.time() < self._valid_until

    def authenticate(self, method: str, credentials: dict) -> bool:
        """Аутентифицироваться по методу UID с учётными данными.

        POST /authentication/{client} → {connection-uid, validUntil}.
        """
        self.last_auth_error = None
        self.last_auth_error_code = None
        try:
            result = self._post(
                f"authentication/{self.client}",
                {"method": method, "credentials": credentials},
            )
        except WebAPIClient.Error as error:
            self.last_auth_error = error
            self.last_auth_error_code = f"webapi_{error.code}"
            return False
        except requests.Timeout:
            self.last_auth_error_code = "webapi_timeout"
            return False
        except requests.RequestException:
            self.last_auth_error_code = "webapi_connection_error"
            return False

        if not isinstance(result, dict):
            self.last_auth_error_code = "webapi_invalid_auth_response"
            return False

        uid = result.get("connection-uid")
        if not isinstance(uid, str) or not uid:
            self.last_auth_error_code = "webapi_invalid_auth_response"
            return False

        self._connection_uid = uid
        self._authenticated_at = time.time()
        # validUntil — unix-секунды (now + ConnectionLifetime * 3600)
        valid_until = result.get("validUntil")
        if isinstance(valid_until, (int, float)):
            self._valid_until = float(valid_until)
        else:
            # fallback: assume default 3h lifetime
            self._valid_until = self._authenticated_at + 3 * 3600
        return True

    def authenticate_with_key_file(
        self, keyname: str, keydata: str | None = None
    ) -> bool:
        """Аутентифицироваться по Veyon key-file (RSA).

        Args:
            keyname: имя ключа (например 'teacher')
            keydata: содержимое закрытого ключа PEM. Если None — читается из
                     стандартного расположения Veyon.
        """
        if not keydata:
            import os
            import sys

            if sys.platform == "win32":
                key_path = os.path.join(
                    os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                    "Veyon",
                    "keys",
                    "private",
                    keyname,
                    "key",
                )
            else:
                key_path = os.path.join("/etc/veyon/keys/private", keyname, "key")
            with open(key_path, "r") as keyfile:
                keydata = keyfile.read()

        return self.authenticate(
            self.AUTH_METHOD_KEY_FILE,
            {"keyname": keyname, "keydata": keydata},
        )

    def wait_for_framebuffer(self, timeout: int = 30) -> bool:
        """Ждать, пока framebuffer станет доступен.

        Опрашивает get_image() раз в 1 сек. ERROR_FRAMEBUFFER_NOT_AVAILABLE (10)
        подавляется (cold start — VNC-стрим ещё не прогрелся), остальные ошибки
        пробрасываются. Каждый запрос сбрасывает idle-таймер сервера (60 сек),
        сохраняя соединение живым во время ожидания.

        Returns:
            True если framebuffer получен, False по таймауту.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.get_image():
                    return True
            except WebAPIClient.Error as error:
                if error.code != self.ERROR_FRAMEBUFFER_NOT_AVAILABLE:
                    raise
            time.sleep(1)
        return False

    def get_image(
        self,
        width: int = 0,
        height: int = 0,
        format: str = "png",
        compression: int = 5,
        quality: int = 75,
    ) -> bytes | None:
        """Получить framebuffer (скриншот экрана) как бинарные данные.

        GET /framebuffer?format=...&compression=...&quality=...&width=...&height=...
        Сервер кэширует результат 1 сек (WebApiConnection::EncodingResult.expired).

        Args:
            width: желаемая ширина (0 = исходный размер)
            height: желаемая высота (0 = исходный размер)
            format: формат ('png', 'jpeg', 'bmp', ...)
            compression: уровень сжатия (0-9; сервер умножает на 11 для Qt PNG)
            quality: качество JPEG (0-100)

        Returns:
            Бинарные данные изображения (bytes) или None при пустом ответе.

        Raises:
            WebAPIClient.Error при non-200 (с кодом ошибки WebAPI).
        """
        params: dict = {
            "format": format,
            "compression": compression,
            "quality": quality,
        }
        if width > 0:
            params["width"] = width
        if height > 0:
            params["height"] = height

        return self._get_binary("framebuffer", params)

    # ── Управление фичами (Veyon 4.10+: PUT /api/v1/feature/{uid}) ──

    def list_features(self) -> list:
        """Получить список всех фич с их состоянием (GET /api/v1/feature).

        Returns:
            Список dict: [{"name": str, "uid": str, "parentUid": str, "active": bool}, ...]
            Пустой список при ошибке.
        """
        data = self._get("feature")
        if isinstance(data, list):
            return data
        return []

    def get_feature_status(self, uid: str) -> bool | None:
        """Проверить, активна ли фича (GET /api/v1/feature/{uid}).

        Returns:
            True/False если запрос успешен, None при ошибке.
        """
        data = self._get(f"feature/{uid}")
        if isinstance(data, dict):
            return bool(data.get("active", False))
        return None

    def set_feature_status(
        self, uid: str, active: bool, arguments: dict | None = None
    ) -> None:
        """Запустить или остановить фичу (PUT /api/v1/feature/{uid}).

        Args:
            uid: UID фичи (без фигурных скобок, например FEATURE_SCREEN_LOCK)
            active: True → Start, False → Stop
            arguments: dict camelCase-аргументов фичи (например
                       {"username": "...", "password": "..."} для login)
                       или None для безаргументных фичей

        Raises:
            WebAPIClient.Error: при ошибке сервера (код ошибки в .code).
                ERROR_INVALID_CONNECTION (2) — соединение закрыто/истекло.
                ERROR_INVALID_FEATURE (3) — неизвестный UID.
                ERROR_CONN_LIMIT_REACHED (7) — лимит соединений исчерпан.
        """
        payload: dict[str, object] = {"active": active}
        if arguments:
            payload["arguments"] = arguments
        self._put(f"feature/{uid}", payload)

    def close_connection(self) -> None:
        """Закрыть соединение (DELETE /authentication/{client}).

         Idempotent: no-op если не аутентифицирован. Сервер удаляет
         WebApiConnection из m_connections, освобождая слот лимита.
         Также закрывает HTTP-сессию (requests.Session) чтобы освободить
        下层 TCP-сокет keep-alive — предотвращает утечку файловых
         дескрипторов при массовых одноразовых захватах миниатюр.
        """
        if self._connection_uid is not None:
            try:
                # Короткий таймаут: close вызывается при снятии подписки
                # (stop_thumbnail/offline/гейтинг) и не должен висеть 10с
                # на мёртвом хосте — соединение всё равно будет добито
                # idle-свипером VeyonService.
                self._delete(f"authentication/{self.client}", timeout=(2, 2))
            except Exception as error:  # noqa: BLE001 - best-effort network cleanup
                _logger.debug("WebAPI close failed (%s)", type(error).__name__)
            finally:
                self._connection_uid = None
                self._valid_until = 0.0
        # Закрываем HTTP-сессию независимо от состояния аутентификации —
        # requests.Session удерживает TCP keep-alive сокет к WebAPI даже
        # после DELETE. При close-after-use паттерне (get_thumbnail создаёт
        # новый WebAPIClient на каждый кадр) это приводит к накоплению
        # CLOSE_WAIT сокетов на стороне veyon-service при падении/рестарте
        # сервиса. Явный close() гарантирует освобождение сокета.
        try:
            self._session.close()
        except Exception as error:  # noqa: BLE001 - destructor-safe session cleanup
            _logger.debug("WebAPI HTTP close failed (%s)", type(error).__name__)

    # ── Внутренние HTTP-хелперы (через requests.Session для keep-alive) ──

    def _headers(self) -> dict:
        """Заголовки с Connection-Uid для авторизованных запросов."""
        if self._connection_uid is None:
            return {}
        return {self.CONNECTION_UID_HEADER: self._connection_uid}

    def _get(self, method: str, params: dict | None = None) -> dict | None:
        response = self._session.get(
            self.api_url + method,
            headers=self._headers(),
            params=params or {},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    def _get_binary(self, method: str, params: dict | None = None) -> bytes | None:
        response = self._session.get(
            self.api_url + method,
            headers=self._headers(),
            params=params or {},
            timeout=10,
        )
        if response.status_code != 200:
            raise WebAPIClient.Error(response.text)
        return response.content

    def _post(self, method: str, data: dict | None = None) -> dict | None:
        response = self._session.post(
            self.api_url + method,
            headers=self._headers(),
            json=data or {},
            timeout=10,
        )
        if response.status_code != 200:
            raise WebAPIClient.Error(response.text)
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    def _put(self, method: str, data: dict | None = None) -> dict | None:
        response = self._session.put(
            self.api_url + method,
            headers=self._headers(),
            json=data or {},
            timeout=10,
        )
        if response.status_code != 200:
            raise WebAPIClient.Error(response.text)
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    def _delete(self, method: str, timeout=10) -> dict | None:
        response = self._session.delete(
            self.api_url + method,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code != 200:
            raise WebAPIClient.Error(response.text)
        try:
            return response.json()
        except json.JSONDecodeError:
            return None
