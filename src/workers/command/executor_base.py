"""Базовый класс исполнителя команд и вспомогательные функции.

Предоставляет CredentialsResult dataclass, get_credentials() и BaseCommandExecutor
с общим интерфейсом для SSH/SFTP/local команд."""

import os
import threading
from abc import ABC, abstractmethod
from typing import Any, NamedTuple, Optional, Tuple

import paramiko
from PySide6.QtCore import QObject

from src.config import config
from src.domain.models.device import DeviceModel
from src.logger import logger

_SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")

_KEY_TYPE_NAMES = {
    "id_ed25519": "ssh-ed25519",
    "id_rsa": "ssh-rsa",
    "id_ecdsa": "ecdsa-sha2-nistp256",
}

_KEY_CLASSES = [
    paramiko.Ed25519Key,
    paramiko.RSAKey,
    paramiko.ECDSAKey,
]


def _detect_key_type(privkey_path: str) -> Optional[str]:
    """Определяет тип ключа по имени файла (без чтения файла)."""
    basename = os.path.basename(privkey_path)
    for prefix, type_name in _KEY_TYPE_NAMES.items():
        if basename == prefix or basename.startswith(prefix + "."):
            return type_name
    return None


def load_private_key(key_path: str, password: str = "") -> Optional[paramiko.PKey]:
    """Загружает закрытый SSH-ключ, автоматически определяя тип (Ed25519, RSA, ECDSA, DSA).

    Args:
        key_path: Путь к файлу закрытого ключа.
        password: Пароль от ключа (пустая строка — без пароля).

    Returns:
        Объект PKey или None, если ключ не удалось загрузить.

    Raises:
        paramiko.PasswordRequiredException: если ключ защищён паролем.
    """
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key_file(key_path, password)
        except paramiko.PasswordRequiredException:
            raise
        except Exception:
            continue
    return None


def get_default_key_path() -> str:
    """Возвращает путь к закрытому ключу по умолчанию.

    Приоритет: id_ed25519 > id_rsa > id_ecdsa.
    Если ни один файл не найден, возвращает id_ed25519.
    """
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        path = os.path.join(_SSH_DIR, name)
        if os.path.exists(path):
            return path
    return os.path.join(_SSH_DIR, "id_ed25519")


def _is_set(value: Optional[str]) -> bool:
    """Проверяет что значение установлено (не None и не пустая строка)"""
    return value is not None and value.strip() != ""


class CredentialsResult(NamedTuple):
    """Результат получения учётных данных"""

    username: str
    password: Optional[str]
    private_key: Optional[Any]  # paramiko.PKey


def get_credentials(
    device: DeviceModel, use_key: bool = True, key_path: Optional[str] = None
) -> CredentialsResult:
    """
    Получить учётные данные для SSH подключения.

    Приоритет параметров:
    1. Связка учётных данных (device.credential_bundle_iid → default_bundle)
    2. 'root' (username) / SSH ключ (если use_key=True)

    Args:
        device: Устройство для получения данных
        use_key: Использовать ли SSH ключ (False для диалогов где ключ не должен учитываться)
        key_path: Путь к закрытому ключу (по умолчанию ~/.ssh/id_rsa)

    Returns:
        CredentialsResult с username, password и private_key
    """
    if key_path is None:
        key_path = get_default_key_path()

    # Приоритет 0: связки учётных данных (CredentialBundle)
    bundle_login = None
    bundle_password = None
    try:
        from src.di import get_container

        svc = get_container().resolve_optional(
            __import__(
                "src.services.configuration.credential_service",
                fromlist=["CredentialService"],
            ).CredentialService
        )
        if svc is not None:
            bundle_login, bundle_password = svc.resolve(device)
    except Exception:
        pass

    # Приоритет 0 -> 1: имя пользователя (связка -> 'root')
    username = (bundle_login or "").strip() or "root"

    # Пароль — ТОЛЬКО из связки учётных данных (никаких override)
    password = bundle_password or None

    # Приоритет 4: SSH ключ (только если use_key=True)
    private_key = None
    if use_key and os.path.exists(key_path):
        try:
            private_key = load_private_key(key_path)
            if private_key:
                logger.debug(
                    f"Загружен SSH ключ ({private_key.get_name()}): {key_path}"
                )
            else:
                logger.warning(f"Не удалось определить тип закрытого ключа {key_path}")
        except paramiko.PasswordRequiredException:
            logger.warning(f"Закрытый ключ {key_path} защищён паролем, пропускаем")
        except Exception as e:
            logger.error(f"Ошибка загрузки закрытого ключа {key_path}: {e}")

    return CredentialsResult(
        username=username, password=password, private_key=private_key
    )


def create_ssh_client(
    device: DeviceModel,
    *,
    client: paramiko.SSHClient | None = None,
    connect_timeout: float | None = None,
) -> paramiko.SSHClient:
    """Подключить SSH с общей политикой ключей и закрытием при любой ошибке."""
    client = client if client is not None else paramiko.SSHClient()
    try:
        if config.app.ssh.strict_host_checking:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        creds = get_credentials(device)
        connect_timeout = config.app.ssh.connect_timeout if connect_timeout is None else connect_timeout
        client.connect(
            hostname=device.host,
            port=device.port or config.app.ssh.port or 22,
            username=creds.username,
            password=creds.password,
            pkey=creds.private_key,
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            channel_timeout=10,
        )
        return client
    except Exception:
        client.close()
        raise


class BaseCommandExecutor(ABC):
    """
    Базовый класс для всех исполнителей команд.

    Предоставляет:
    - Общую логику замены параметров
    - Методы для создания SSH клиента
    - Базовую обработку ошибок
    """

    def __init__(self, progress_callback: Optional[QObject] = None):
        """
        Инициализация исполнителя.

        Args:
            progress_callback: Объект для отправки прогресса (сигнал progress_update)
        """
        self._progress_callback = progress_callback
        self._abort_event = threading.Event()

    @property
    def aborting(self) -> bool:
        """Проверить, запрошено ли прерывание."""
        return self._abort_event.is_set()

    @aborting.setter
    def aborting(self, value: bool) -> None:
        """Установить или снять флаг прерывания."""
        if value:
            self._abort_event.set()
        else:
            self._abort_event.clear()

    def get_client(
        self, device: DeviceModel, timeout: int = config.app.ssh.command_timeout
    ) -> paramiko.SSHClient:
        """Создать соединение с общей политикой SSH для команд и инструментов."""
        return create_ssh_client(device)

    def get_session(self, client: paramiko.SSHClient) -> paramiko.Channel:
        """
        Создание SSH сессии.

        Args:
            client: SSH клиент

        Returns:
            SSH сессия (Channel)
        """
        transport = client.get_transport()
        session = transport.open_session()
        session.get_pty()  # Запрос псевдо-терминала для интерактивного вывода
        return session

    def emit_progress(self, device: DeviceModel, message: str) -> None:
        """
        Отправка сообщения о прогрессе.

        Args:
            device: Устройство
            message: Сообщение
        """
        if self._progress_callback and hasattr(
            self._progress_callback, "progress_update"
        ):
            self._progress_callback.progress_update.emit(device, message)

    def emit_input_requested(self, device: DeviceModel) -> None:
        """
        Уведомление о запросе ввода от команды (детекция промпта в выводе).

        Пробрасывается через callback (CommandWorker) как сигнал input_requested,
        по образцу emit_progress.

        Args:
            device: Устройство, на котором команда ждёт ввод.
        """
        if self._progress_callback and hasattr(
            self._progress_callback, "input_requested"
        ):
            self._progress_callback.input_requested.emit(device)

    def emit_input_wait_changed(self, device: DeviceModel, waiting: bool) -> None:
        """Уведомить об изменении ожидания; старый сигнал сохранить для входа."""
        if self._progress_callback and hasattr(
            self._progress_callback, "input_wait_changed"
        ):
            self._progress_callback.input_wait_changed.emit(device, waiting)
        if waiting:
            self.emit_input_requested(device)

    @abstractmethod
    def execute(
        self,
        device: DeviceModel,
        command_text: str,
        timeout: int = config.app.ssh.command_timeout,
    ) -> Tuple[str, bool]:
        """
        Выполнение команды.

        Args:
            device: Устройство для выполнения
            command_text: Текст команды
            timeout: Таймаут выполнения

        Returns:
            (вывод, успех)
        """
        pass

    def abort(self) -> None:
        """Запросить прерывание выполнения."""
        logger.info(f"{self.__class__.__name__}: Запрошено прерывание")
        self._abort_event.set()
