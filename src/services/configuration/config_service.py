"""
Config Service - Сервис для управления конфигурацией.

Предоставляет бизнес-логику для операций с конфигурацией:
- Чтение/запись настроек
- Валидация
- Публикация событий об изменениях
"""

from dataclasses import asdict, replace
from typing import Any, Dict, Optional, Tuple

from src.architecture import EventBus, EventType
from src.config import DEFAULT_PORTS, PORTS, config
from src.config.settings import (
    THUMBNAIL_CONFIG_LOCK,
    VNC_SERVERS,
    ExecutionConfig,
    merge_thumbnail_settings,
    validate_thread_count,
)

from src.services.base import BaseService


class ConfigService(BaseService):
    """
    Сервис для управления конфигурацией приложения.

    Инкапсулирует всю бизнес-логику работы с настройками,
    предоставляя UI компонентам простой интерфейс.
    """

    def __init__(self, event_bus: Optional["EventBus"] = None) -> None:
        super().__init__(event_bus)
        self._config = config
        self._thumbnail_config_lock = THUMBNAIL_CONFIG_LOCK

    # ========== ЧТЕНИЕ НАСТРОЕК ==========

    def setup_snapshot(self, datastore):
        from copy import deepcopy
        from pathlib import Path
        from src.config import DEFAULT_COMMANDS_FILE, DEFAULT_HOSTS_FILE

        with THUMBNAIL_CONFIG_LOCK, datastore._lock:
            paths = (DEFAULT_HOSTS_FILE, DEFAULT_COMMANDS_FILE, self._config.config_file)
            return dict(
                app=deepcopy(self._config.app),
                data=datastore.setup_snapshot(),
                files={str(Path(p).resolve()): Path(p).read_bytes() if Path(p).exists() else None for p in paths},
            )

    def save_setup_config(self, ctx, datastore):
        """Commit the wizard draft using canonical settings serialization."""
        from copy import copy, deepcopy
        import json
        import os
        from pathlib import Path
        import tempfile
        from src.config import DEFAULT_COMMANDS_FILE, DEFAULT_HOSTS_FILE
        from src.utils.filesystem.file_transaction import replace_file_set

        try:
            with THUMBNAIL_CONFIG_LOCK:
                baseline = ctx.baseline
                current = self._config.app
                # A changed credentials list can change the meaning of selected iids.
                for name in ("ssh", "veyon", "credentials"):
                    if getattr(current, name) != getattr(baseline["app"], name):
                        raise RuntimeError("Настройки подключения изменились. Откройте мастер заново.")
                next_app = deepcopy(current)
                next_app.credentials = deepcopy(ctx.credentials)
                if ctx.services.get("ssh"):
                    next_app.ssh.use_sudo = ctx.ssh.use_sudo
                    next_app.credentials.default_bundle_iid = ctx.ssh.bundle_iid
                if ctx.services.get("veyon"):
                    allowed = {"key_name", "webapi_url", "feature_transport", "webapi_connection_limit", "server_port"}
                    if set(ctx.veyon.settings) - allowed:
                        raise ValueError("Неизвестные настройки Veyon")
                    for name, value in ctx.veyon.settings.items():
                        setattr(next_app.veyon, name, value)
                if ctx.services.get("vnc"):
                    if not isinstance(ctx.vnc.password, str) or any(char in ctx.vnc.password for char in ("\0", "\n", "\r")):
                        raise ValueError("Некорректный пароль VNC")
                    next_app.veyon.vnc_server = ctx.vnc_server
                    next_app.veyon.vnc_port = ctx.vnc.port
                    next_app.veyon.vnc_password = ctx.vnc.password
                if next_app.veyon.vnc_server not in VNC_SERVERS:
                    raise ValueError("Неизвестный VNC backend")
                for port in (next_app.veyon.vnc_port, next_app.veyon.server_port):
                    if type(port) is not int or not 1 <= port <= 65535:
                        raise ValueError("Недопустимый порт")
                clone = copy(self._config)
                clone.app = next_app
                directory = Path(self._config.config_file).resolve().parent
                directory.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".wizard-settings-", dir=directory)
                os.close(fd)
                try:
                    clone.config_file = temporary
                    if clone.save() is False:
                        raise RuntimeError("Не удалось записать настройки")
                    settings_bytes = Path(temporary).read_bytes()
                finally:
                    Path(temporary).unlink(missing_ok=True)

                def persist(hosts, commands):
                    payloads = {
                        DEFAULT_HOSTS_FILE: json.dumps(hosts, ensure_ascii=False, indent=4).encode("utf-8"),
                        DEFAULT_COMMANDS_FILE: json.dumps(commands, ensure_ascii=False, indent=4).encode("utf-8"),
                        self._config.config_file: settings_bytes,
                    }
                    replace_file_set(payloads, directory / ".setup-wizard-transaction.json", baseline["files"])
                    self._config.app = next_app
                    for field in ("vnc_port", "vnc_password"):
                        if getattr(current.veyon, field) != getattr(next_app.veyon, field):
                            revision = "_" + field + "_revision"
                            setattr(self._config, revision, getattr(self._config, revision, 0) + 1)

                datastore.commit_setup_snapshot(baseline["data"], ctx.hosts_data, ctx.commands_data, persist)
            try:
                self.notify_thumbnail_settings_changed()
                if self._event_bus:
                    self._event_bus.publish_typed(EventType.DATA_SAVED, source="SetupWizard", data={"type": "wizard_finished"})
            except Exception:
                self._logger.exception("Setup files committed but notification failed")
            return True, ""
        except Exception as error:
            self._logger.error("Setup wizard save failed: %s", error)
            return False, f"Не удалось сохранить настройки:\n{error}"

    def get_ssh_config(self) -> Dict[str, Any]:
        """
        Получить SSH настройки.

        Returns:
            Словарь с SSH настройками
        """
        return {
            "port": self._config.app.ssh.port,
            "strict_host_checking": self._config.app.ssh.strict_host_checking,
            "command_timeout": self._config.app.ssh.command_timeout,
            "connect_timeout": self._config.app.ssh.connect_timeout,
            "ssh_connect_timeout": self._config.app.ssh.ssh_connect_timeout,
            "max_as_completed_timeout": self._config.app.ssh.max_as_completed_timeout,
        }

    def get_network_config(self) -> Dict[str, Any]:
        """
        Получить сетевые настройки.

        Returns:
            Словарь с сетевыми настройками
        """
        return {
            "ping_timeout": self._config.app.network.ping_timeout,
            "ping_interval": self._config.app.network.ping_interval,
            "thread_count": self._config.app.network.thread_count,
            "ports": dict(PORTS),
        }

    def get_expand_setting(self) -> bool:
        """
        Получить настройку разворачивания деревьев.

        Returns:
            True если деревья разворачиваются
        """
        return self._config.app.expand

    def get_execution_config(self) -> dict[str, int]:
        """Return shared command execution limits (applied on restart)."""
        return asdict(self._config.app.execution)

    def update_execution_config(self, thread_count: int | None = None) -> bool:
        """Validate and update shared command concurrency without saving."""
        try:
            if thread_count is not None:
                count = validate_thread_count(thread_count)
                self._config.app.execution = replace(
                    self._config.app.execution, thread_count=count
                )
            return True
        except ValueError as e:
            self._logger.warning(f"ConfigService: Invalid execution settings: {e}")
            return False

    # ========== THUMBNAIL SETTINGS ==========

    def get_thumbnail_config(self) -> dict:
        """Return an independent snapshot of the canonical thumbnail settings."""
        with self._thumbnail_config_lock:
            return asdict(self._config.app.thumbnails)

    def update_thumbnail_config(self, data: dict | None = None, **kwargs: Any) -> bool:
        """Validate and replace thumbnail settings atomically, without saving.

        Keyword arguments take priority over the optional partial dictionary.
        Invalid updates return False and leave the previous configuration intact.
        Call save_config() to persist and publish the settings-change event.
        """
        try:
            if data is not None and not isinstance(data, dict):
                raise ValueError("Thumbnail settings must be a dictionary")
            with self._thumbnail_config_lock:
                old = self._config.app.thumbnails
                new = merge_thumbnail_settings(old, {**(data or {}), **kwargs})
                changed = {
                    key
                    for key in old.__dataclass_fields__
                    if getattr(old, key) != getattr(new, key)
                }
                if "backend" in changed:
                    self._config._thumbnail_backend_revision = (
                        getattr(self._config, "_thumbnail_backend_revision", 0) + 1
                    )
                if changed - {
                    "backend",
                    "style",
                    "interval_sec",
                    "stagger_sec",
                    "resources",
                    "vnc",
                    "thread_count",
                }:
                    self._config._thumbnail_image_revision = (
                        getattr(self._config, "_thumbnail_image_revision", 0) + 1
                    )
                self._config.app.thumbnails = new
            return True
        except ValueError as e:
            self._logger.warning(f"ConfigService: Invalid thumbnail settings: {e}")
            return False

    def save_thumbnail_config(self, data: dict) -> tuple[bool, str]:
        """Validate, replace and persist under one lock; roll back failed writes."""
        with self._thumbnail_config_lock:
            previous = self._config.app.thumbnails
            revisions = {
                name: getattr(self._config, name, 0)
                for name in ("_thumbnail_backend_revision", "_thumbnail_image_revision")
            }
            if not self.update_thumbnail_config(data):
                return False, "Неверные настройки миниатюр"
            success, message = self.save_config()
            if not success:
                self._config.app.thumbnails = previous
                for name, value in revisions.items():
                    setattr(self._config, name, value)
            return success, message

    def notify_thumbnail_settings_changed(self) -> None:
        """Publish a neutral invalidation event after a successful settings save.

        VNC port/password saves use this too; never include secret values here.
        """
        with self._thumbnail_config_lock:
            if self._event_bus is not None:
                self._event_bus.publish_typed(
                    EventType.DATA_SAVED,
                    source="ConfigService",
                    data={"type": "thumbnails_config"},
                )

    def update_vnc_config(
        self,
        port: int | None = None,
        password: str | None = None,
        server: str | None = None,
    ) -> bool:
        """Validate and atomically replace VNC connection settings without saving.

        None preserves a field; an empty password explicitly disables VNC Auth.
        After a direct Config.save(), call notify_thumbnail_settings_changed();
        save_config() already publishes this event after successful persistence.
        """
        changes: dict[str, Any] = {}
        if port is not None:
            if type(port) is not int or not 1 <= port <= 65535:
                self._logger.warning("ConfigService: Invalid VNC port")
                return False
            changes["vnc_port"] = port
        if password is not None:
            if not isinstance(password, str):
                self._logger.warning("ConfigService: Invalid VNC password type")
                return False
            changes["vnc_password"] = password
        if server is not None:
            if not isinstance(server, str) or server not in VNC_SERVERS:
                self._logger.warning("ConfigService: Invalid VNC server")
                return False
            changes["vnc_server"] = server
        with self._thumbnail_config_lock:
            old = self._config.app.veyon
            if (
                "vnc_password" in changes
                and changes["vnc_password"] != old.vnc_password
            ):
                self._config._vnc_password_revision = (
                    getattr(self._config, "_vnc_password_revision", 0) + 1
                )
            if "vnc_port" in changes and changes["vnc_port"] != old.vnc_port:
                self._config._vnc_port_revision = (
                    getattr(self._config, "_vnc_port_revision", 0) + 1
                )
            self._config.app.veyon = replace(self._config.app.veyon, **changes)
        return True

    # ========== WEB SERVER НАСТРОЙКИ ==========

    def get_web_config(self) -> Dict[str, Any]:
        """
        Получить настройки веб-сервера.

        Returns:
            Словарь с настройками веб-сервера
        """
        w = self._config.app.web
        # users: отдаём реальные пароли — диалог маскирует их через
        # PasswordDelegate (видны только при редактировании ячейки).
        users_out = []
        for u in w.users or []:
            users_out.append(
                {
                    "username": u.username,
                    "password": u.password or "",
                }
            )
        return {
            "enabled": w.enabled,
            "host": w.host,
            "port": w.port,
            "public_url": w.public_url,
            "tls_mode": w.tls_mode,
            "tls_cert_path": w.tls_cert_path,
            "tls_key_path": w.tls_key_path,
            "tls_names": list(w.tls_names or []),
            "username": w.username,
            "password": w.password,
            "users": users_out,
            # Пароль служебной записи LDAP окно показывает только в поле ввода.
            "ldap": asdict(w.ldap),
            "job_ttl_minutes": w.job_ttl_minutes,
            # Legacy API aliases only; dataclass legacy fields are not authority.
            "thread_count": self._config.app.thumbnails.thread_count,
            "command_thread_count": self._config.app.execution.thread_count,
            "rfb_thread_count": w.rfb_thread_count,
        }

    def update_web_config(
        self,
        enabled: bool = None,
        host: str = None,
        port: int = None,
        username: str = None,
        password: str = None,
        users: list = None,
        job_ttl_minutes: int = None,
        thread_count: int = None,
        command_thread_count: int = None,
        rfb_thread_count: int | None = None,
        public_url: str | None = None,
        tls_mode: str | None = None,
        tls_cert_path: str | None = None,
        tls_key_path: str | None = None,
        tls_names: list | None = None,
        ldap: dict | None = None,
    ) -> bool:
        """
        Обновить настройки веб-сервера.

        Args:
            users: список dict {'username','password'}. Пароль '********'
                   пропускается (не перезаписывается) — для сохранения
                   существующего пароля при редактировании других полей.
            ldap: полная настройка входа через LDAP (поля ``WebLdapConfig``);
                  ошибка проверки отменяет всё сохранение. Причину заранее
                  показывает окно через ``normalize_ldap_config``.
            thread_count: Legacy alias updating both status/service and thumbnail
                concurrency. New callers should update canonical sections.
            command_thread_count: Legacy alias for execution.thread_count.

        Returns:
            True если успешно
        """
        try:
            from src.config.settings import WebUser

            # Validate all pool aliases before touching any settings. Build the
            # detached web snapshot first, then commit canonical changes together.
            for count in (thread_count, command_thread_count, rfb_thread_count):
                if count is not None:
                    validate_thread_count(count)
            w = replace(self._config.app.web)
            if public_url is not None:
                from src.utils.network.urls import normalize_public_web_url

                w.public_url = normalize_public_web_url(public_url)
            if tls_mode is not None:
                from src.utils.security.web_tls import TLS_MODES

                if tls_mode not in TLS_MODES:
                    return False
                w.tls_mode = tls_mode
            if tls_cert_path is not None:
                w.tls_cert_path = str(tls_cert_path)
            if tls_key_path is not None:
                w.tls_key_path = str(tls_key_path)
            if tls_names is not None:
                w.tls_names = [str(name) for name in tls_names]
            if enabled is not None:
                w.enabled = bool(enabled)
            if host is not None:
                w.host = host
            if port is not None:
                if not isinstance(port, int) or not (1 <= port <= 65535):
                    return False
                w.port = port
            if username is not None:
                w.username = username
            if password is not None:
                w.password = password
            if users is not None and isinstance(users, list):
                # Слить с существующими: '********' → сохранить старый пароль
                existing = {u.username: u.password for u in (w.users or [])}
                new_list = []
                for entry in users:
                    if not isinstance(entry, dict):
                        continue
                    un = (entry.get("username") or "").strip()
                    if not un:
                        continue
                    pw = entry.get("password", "")
                    if pw == "********":
                        pw = existing.get(un, "")
                    new_list.append(WebUser(username=un, password=pw or ""))
                w.users = new_list
            if ldap is not None:
                from src.config.web_ldap import normalize_ldap_config

                w.ldap = normalize_ldap_config(ldap, strict=True)
            if job_ttl_minutes is not None:
                try:
                    w.job_ttl_minutes = max(0, int(job_ttl_minutes))
                except Exception:
                    return False
            if rfb_thread_count is not None:
                w.rfb_thread_count = rfb_thread_count
            with self._thumbnail_config_lock:
                new_thumbnails = self._config.app.thumbnails
                if thread_count is not None:
                    new_thumbnails = merge_thumbnail_settings(
                        new_thumbnails, {"thread_count": thread_count}
                    )
                self._config.app.web = w
                self._config.app.thumbnails = new_thumbnails
                if thread_count is not None:
                    self._config.app.network.thread_count = thread_count
                if command_thread_count is not None:
                    self._config.app.execution = replace(
                        self._config.app.execution, thread_count=command_thread_count
                    )
            self._logger.info("ConfigService: Web server settings updated")
            return True
        except Exception as e:
            self._logger.error(f"ConfigService: Error updating web settings: {e}")
            return False

    def get_all_config(self) -> Dict[str, Any]:
        """
        Получить всю конфигурацию.

        Returns:
            Словарь со всеми настройками
        """
        return {
            "ssh": self.get_ssh_config(),
            "network": self.get_network_config(),
            "execution": self.get_execution_config(),
            "web": self.get_web_config(),
            "thumbnails": self.get_thumbnail_config(),
            "expand": self.get_expand_setting(),
        }

    # ========== ИЗМЕНЕНИЕ НАСТРОЕК ==========

    def update_ssh_config(
        self,
        port: int = None,
        strict_host_checking: bool = None,
        command_timeout: int = None,
        connect_timeout: int = None,
    ) -> bool:
        """
        Обновить SSH настройки.

        Args:
            port: Новый порт
            strict_host_checking: Строгая проверка ключей
            command_timeout: Таймаут команд
            connect_timeout: Таймаут подключения (для оффлайн хостов)

        Returns:
            True если успешно
        """
        try:
            if port is not None:
                self._config.app.ssh.port = port
            if strict_host_checking is not None:
                self._config.app.ssh.strict_host_checking = strict_host_checking
            if command_timeout is not None:
                self._config.app.ssh.command_timeout = command_timeout
            if connect_timeout is not None:
                self._config.app.ssh.connect_timeout = connect_timeout

            self._logger.info("ConfigService: SSH settings updated")
            return True

        except Exception as e:
            self._logger.error(f"ConfigService: Error updating SSH settings: {e}")
            return False

    def update_network_config(
        self,
        ping_timeout: float = None,
        ping_interval: int = None,
        thread_count: int = None,
        ports: Dict[int, bool] = None,
    ) -> bool:
        """
        Обновить общие настройки статусов и служебных операций.

        Args:
            ping_timeout: Таймаут пинга
            ping_interval: Интервал пинга
            thread_count: Общий лимит статусов и служебных операций Qt/web/node
            ports: Словарь портов

        Returns:
            True если успешно
        """
        try:
            if thread_count is not None:
                validate_thread_count(thread_count)
            if ping_timeout is not None:
                self._config.app.network.ping_timeout = ping_timeout
            if ping_interval is not None:
                self._config.app.network.ping_interval = ping_interval
            if thread_count is not None:
                self._config.app.network.thread_count = thread_count
            if ports is not None:
                PORTS.clear()
                PORTS.update(ports)

            self._logger.info("ConfigService: Network settings updated")
            return True

        except Exception as e:
            self._logger.error(f"ConfigService: Error updating network settings: {e}")
            return False

    def update_expand_setting(self, expand: bool) -> bool:
        """
        Обновить настройку разворачивания деревьев.

        Args:
            expand: Разворачивать ли деревья

        Returns:
            True если успешно
        """
        try:
            self._config.app.expand = expand
            self._logger.info(f"ConfigService: Expand setting updated to {expand}")
            return True

        except Exception as e:
            self._logger.error(f"ConfigService: Error updating expand setting: {e}")
            return False

    # ========== СОХРАНЕНИЕ/ЗАГРУЗКА ==========

    def save_topology_config(self, **options) -> Tuple[bool, str]:
        """Проверить и сохранить настройки карты с откатом при ошибке persist."""
        from copy import deepcopy
        from src.utils.network.topology_settings import validate_option
        with self._thumbnail_config_lock:
            network = self._config.app.network
            previous = {}
            try:
                validated = {name: validate_option(name, value) for name, value in options.items()}
                previous = {name: deepcopy(getattr(network, name)) for name in validated}
                for name, value in validated.items():
                    setattr(network, name, value)
                success, message = self.save_config()
                if not success:
                    raise OSError(message)
                return True, message
            except Exception as exc:
                for name, value in previous.items():
                    setattr(network, name, value)
                return False, str(exc)

    def save_scanner_config(self, *, ports=None, **options) -> Tuple[bool, str]:
        """Validate and persist a scanner snapshot, rolling back on failure."""
        ranges = {
            "scanner_thread_count": (1, 128),
            "scanner_ping_timeout_ms": (100, 10000),
            "scanner_sort_column": (0, 5),
            "scanner_sort_order": (0, 1),
        }
        with self._thumbnail_config_lock:
            network = self._config.app.network
            previous_ports = PORTS.copy()
            previous = {}
            try:
                for name, value in options.items():
                    if name == "scanner_ping_backend":
                        if value not in ("system", "icmplib"):
                            raise ValueError("Способ пинга: допустимы system и icmplib")
                    elif name not in ranges:
                        raise ValueError(f"Неизвестная настройка сканера: {name}")
                    else:
                        low, high = ranges[name]
                        if type(value) is not int or not low <= value <= high:
                            raise ValueError(f"{name}: допустимый диапазон {low}..{high}")
                    if name == "scanner_thread_count" and value & (value - 1):
                        raise ValueError("Число потоков должно быть степенью двойки")
                    previous[name] = getattr(network, name)
                if ports is not None:
                    if not isinstance(ports, dict) or any(
                        type(port) is not int or not 1 <= port <= 65535 or type(enabled) is not bool
                        for port, enabled in ports.items()
                    ):
                        raise ValueError("Допустимы уникальные порты 1..65535 и булевы флаги")
                    PORTS.replace(ports)
                for name, value in options.items():
                    setattr(network, name, value)
                success, message = self.save_config()
                if not success:
                    raise OSError(message)
                return True, message
            except Exception as exc:
                PORTS.replace(previous_ports)
                for name, value in previous.items():
                    setattr(network, name, value)
                return False, str(exc)

    def save_config(self) -> Tuple[bool, str]:
        """
        Сохранить конфигурацию в файл.

        Returns:
            (success, message)
        """
        try:
            with self._thumbnail_config_lock:
                self._config.save()
                self._logger.info("ConfigService: Configuration saved to file")

                # Publish under the snapshot lock, after successful persistence.
                if self._event_bus is not None:
                    self._event_bus.publish_typed(
                        EventType.DATA_SAVED,
                        source="ConfigService",
                        data={"type": "config"},
                    )
                self.notify_thumbnail_settings_changed()

            return True, "Конфигурация сохранена"

        except Exception as e:
            self._logger.error(f"ConfigService: Error saving configuration: {e}")
            return False, str(e)

    def load_config(self) -> Tuple[bool, str]:
        """
        Загрузить конфигурацию из файла.

        Returns:
            (success, message)
        """
        try:
            self._config.load()
            self._logger.info("ConfigService: Configuration loaded from file")

            # Публикуем событие
            self._event_bus.publish_typed(
                EventType.DATA_LOADED,
                source="ConfigService",
                data={"type": "config", "file": self._config.config_file},
            )

            return True, "Конфигурация загружена"

        except Exception as e:
            self._logger.error(f"ConfigService: Error loading configuration: {e}")
            return False, str(e)

    # ========== МЕДИА НАСТРОЙКИ ==========

    def get_media_config(self) -> Dict[str, Any]:
        """Получить медиа-настройки (заглушка — возвращает пустой словарь)."""
        return {}

    def update_media_config(self, **kwargs) -> bool:
        """Обновить медиа-настройки (заглушка — всегда True)."""
        return True

    def get_media_path(self, name: str) -> str:
        """Получить путь медиа-ресурса по имени (заглушка)."""
        return name

    def update_media_path(self, name: str, path: str) -> bool:
        """Обновить путь медиа-ресурса (заглушка — всегда True)."""
        return True

    # ========== ВАЛИДАЦИЯ ==========

    def validate_config(self, config_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Валидировать конфигурацию.

        Args:
            config_data: Данные для валидации

        Returns:
            (is_valid, error_message)
        """
        for section in ("ssh", "network", "execution", "web", "thumbnails"):
            if section in config_data and not isinstance(config_data[section], dict):
                return False, f"Section {section} must be a dictionary"
        if set(config_data.get("execution", {})) - {"thread_count"}:
            return False, "Unknown execution setting"
        try:
            for section, keys in (
                ("network", ("thread_count",)),
                ("execution", ("thread_count",)),
                ("web", ("rfb_thread_count", "thread_count", "command_thread_count")),
            ):
                for key in keys:
                    if key in config_data.get(section, {}):
                        validate_thread_count(config_data[section][key])
        except ValueError as e:
            return False, str(e)

        if "thumbnails" in config_data:
            try:
                with self._thumbnail_config_lock:
                    merge_thumbnail_settings(
                        self._config.app.thumbnails, config_data["thumbnails"]
                    )
            except ValueError as e:
                return False, str(e)

        # Проверка SSH настроек
        if "ssh" in config_data:
            ssh = config_data["ssh"]

            if "port" in ssh:
                port = ssh["port"]
                if not isinstance(port, int) or not (1 <= port <= 65535):
                    return False, f"Неверный SSH порт: {port}"

            if "command_timeout" in ssh:
                timeout = ssh["command_timeout"]
                if not isinstance(timeout, int) or timeout < 1:
                    return False, f"Неверный таймаут команд: {timeout}"

            if "connect_timeout" in ssh:
                timeout = ssh["connect_timeout"]
                if not isinstance(timeout, int) or timeout < 1:
                    return False, f"Неверный таймаут подключения: {timeout}"

        # Проверка сетевых настроек
        if "network" in config_data:
            network = config_data["network"]

            if "ping_timeout" in network:
                timeout = network["ping_timeout"]
                if not isinstance(timeout, (int, float)) or timeout < 0:
                    return False, f"Неверный таймаут пинга: {timeout}"

            if "ping_interval" in network:
                interval = network["ping_interval"]
                if not isinstance(interval, int) or interval < 1:
                    return False, f"Неверный интервал пинга: {interval}"

        return True, ""

    # ========== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ==========

    def reset_to_defaults(self) -> bool:
        """
        Сбросить конфигурацию к значениям по умолчанию.

        Returns:
            True если успешно
        """
        try:
            # Сброс SSH настроек
            self._config.app.ssh.port = 22
            self._config.app.ssh.strict_host_checking = False
            self._config.app.ssh.command_timeout = 30
            self._config.app.ssh.connect_timeout = 5

            # Сброс сетевых настроек
            self._config.app.network.ping_timeout = 2.0
            self._config.app.network.ping_interval = 30
            self._config.app.network.thread_count = 8
            self._config.app.execution = ExecutionConfig()
            self.update_thumbnail_config(thread_count=8)

            # Сброс портов
            PORTS.clear()
            PORTS.update(DEFAULT_PORTS)

            # Сброс expand
            self._config.app.expand = False

            # Сброс веб-сервера
            self._config.app.web.enabled = False
            self._config.app.web.host = "0.0.0.0"
            self._config.app.web.port = 8765
            self._config.app.web.username = "admin"
            self._config.app.web.password = ""
            self._config.app.web.thread_count = 8
            self._config.app.web.command_thread_count = 8
            self._config.app.web.rfb_thread_count = 4

            self._logger.info("ConfigService: Configuration reset to defaults")
            return True

        except Exception as e:
            self._logger.error(f"ConfigService: Error resetting configuration: {e}")
            return False
