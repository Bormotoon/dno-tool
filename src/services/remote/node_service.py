"""Node Service — управление федерацией нод.

Хранит список нод (``config.app.nodes.nodes``), служебный ключ сервера
(Ed25519, ``config.app.nodes.private_key``), секрет ключа доступа к этой
ноде (``access_secret``); загруженный служебный мастер-ключ держит только
в памяти до выхода (на диск не пишется); отвечает за handshake между
нодами и хранит актуальное состояние удалённых нод: дерево хостов,
онлайн-статусы, флаг «нода доступна/недоступна».

Деревья хостов и статусы живут только в памяти сервиса (не в hosts.json
оператора и не в DataStore) — они принадлежат ноде-владельцу.

Идентификация:
- ``NodeRecord.iid`` — локальный стабильный идентификатор записи ноды.
- ``DeviceModel.node_iid`` — ссылка на ноду-владельца (только в памяти).

Доступ односторонний: для подключения к ноде достаточно её ключа доступа
(открытый ключ TLS + секрет). Своего ключа другой стороне передавать не нужно.

Хендшейк:
- ``hello()`` — открытый: отдаёт открытый ключ TLS нашей ноды.
- ``start_handshake(node)`` — клиентская сторона: получает hello от пира,
  запрашивает challenge и отвечает HMAC секрета (или подписью мастер-ключом,
  если секрет не подошёл или его нет), получает токен.
- Server-сторона handshake реализована в ``src/web/node_server.py``.
"""

import threading
import uuid
import weakref
from dataclasses import replace
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot

from src.architecture import EventType
from src.config import NodeRecord, config
from src.logger import logger
from src.utils.security.node_crypto import (
    fingerprint_from_pubkey,
    format_access_key,
    generate_access_secret,
    generate_keypair,
    parse_access_key,
    private_key_from_base64,
    private_key_to_base64,
    public_key_to_base64,
)

from src.services.base import BaseService


class _NodeClientReaper(QObject):
    """Marshal QThread ownership changes onto the application's GUI thread."""

    retire_requested = Signal(object)

    def __init__(self, service: "NodeService") -> None:
        super().__init__()
        self._service = weakref.ref(service)
        self.retire_requested.connect(self._watch_client)
        application = QCoreApplication.instance()
        if application is not None and self.thread() is not application.thread():
            self.moveToThread(application.thread())

    @Slot(object)
    def _watch_client(self, client: QThread) -> None:
        service = self._service()
        if service is None:
            return
        # QThread objects (unlike their run() methods) normally live in the GUI
        # thread. Never change their Qt parent from the running worker thread.
        if client.thread() is not self.thread():
            logger.error("NodeService: node client has unexpected QObject affinity")
            return
        client.setParent(None)
        client.finished.connect(
            self._on_client_finished, Qt.ConnectionType.QueuedConnection
        )
        # Finish may race signal hookup. Never-started QThread objects also
        # need no wait and can be disposed on their owning thread immediately.
        if client.isFinished() or not client.isRunning():
            service._release_retired_client(client)

    @Slot()
    def _on_client_finished(self) -> None:
        client = self.sender()
        service = self._service()
        if service is not None and isinstance(client, QThread):
            service._release_retired_client(client)


class NodeService(BaseService):
    """Сервис управления нодами (федерация)."""

    def __init__(self, event_bus=None) -> None:
        super().__init__(event_bus)
        self._config = config
        self._diagnostics = {}
        self._priv_key = None  # Ed25519PrivateKey, лениво
        # Закрытая часть служебного мастер-ключа: только в памяти процесса.
        self._master_key = None
        self._lock = threading.RLock()
        # Клиентские состояния: node_iid -> {"client": NodeClientWorker, ...}
        self._clients: Dict[str, Any] = {}
        # Деревья хостов: node_iid -> list (формат get_devices_raw)
        self._hosts: Dict[str, list] = {}
        # Доступность ноды: node_iid -> bool
        self._reachable: Dict[str, bool] = {}
        # Имя ноды (из hello, если не задано локально)
        self._names: Dict[str, str] = {}
        # Токены сессий (client side): node_iid -> token
        self._tokens: Dict[str, str] = {}
        # Capability нод из hello-ответа (client side): node_iid -> dict.
        # Старая нода без capabilities = без session-поддержки (D3).
        self._capabilities: Dict[str, dict] = {}
        self._thumbnail_backends: dict[str, list[str]] = {}
        # Stopping QThreads remain owned until their real finished signal.
        self._zombie_clients: list[QThread] = []
        self._client_reaper = _NodeClientReaper(self)
        self._stop = False

    # ========== КЛЮЧИ ==========

    def _reset_private_key(self, priv) -> None:
        """Подменить приватный ключ в памяти (тест-хук, без записи в config)."""
        with self._lock:
            self._priv_key = priv

    def _load_priv_from_config(self):
        """Приватный ключ из config (base64-строка). None если пусто/битый."""
        text = getattr(self._config.app.nodes, "private_key", "") or ""
        if not text.strip():
            return None
        try:
            return private_key_from_base64(text)
        except Exception as e:
            logger.error(f"NodeService: неверный приватный ключ в настройках: {e}")
            return None

    def _save_priv_to_config(self, priv) -> bool:
        """Сохранить приватный ключ (base64) в config и записать файл."""
        with self._lock:
            previous = self._config.app.nodes.private_key
            self._config.app.nodes.private_key = private_key_to_base64(priv)
            if not self._save():
                self._config.app.nodes.private_key = previous
                return False
            return True

    def ensure_private_key(self):
        """Получить приватный ключ (ленивая загрузка/генерация).

        Ключ хранится в settings.json; при первом запуске генерируется
        новая пара и сразу сохраняется.
        """
        with self._lock:
            if self._priv_key is None:
                priv = self._load_priv_from_config()
                if priv is None:
                    if self._config.app.nodes.private_key:
                        raise ValueError("Повреждён ключ ноды: восстановите или явно замените его")
                    priv, _ = generate_keypair()
                    if not self._save_priv_to_config(priv):
                        raise OSError("Не удалось сохранить новый ключ ноды")
                    logger.info(
                        "NodeService: сгенерирована новая пара ключей ноды "
                        "(сохранена в settings.json)"
                    )
                self._priv_key = priv
            return self._priv_key

    def get_public_key_str(self) -> str:
        """Публичный ключ ноды (base64-строка). Лениво создаёт пару ключей."""
        priv = self.ensure_private_key()
        return public_key_to_base64(priv.public_key())

    def get_fingerprint(self) -> str:
        """Fingerprint публичного ключа ноды. Лениво создаёт пару ключей."""
        return fingerprint_from_pubkey(self.get_public_key_str())

    def get_access_secret(self) -> str:
        """Секрет ключа доступа к этой ноде (создаётся при первом обращении)."""
        with self._lock:
            secret = self._config.app.nodes.access_secret
            if secret:
                return secret
            secret = generate_access_secret()
            self._config.app.nodes.access_secret = secret
            if not self._save():
                self._config.app.nodes.access_secret = ""
                raise OSError("Не удалось сохранить ключ доступа ноды")
            logger.info("NodeService: создан ключ доступа ноды")
            return secret

    def get_access_key(self) -> str:
        """Ключ доступа для передачи операторам других нод."""
        return format_access_key(self.get_public_key_str(), self.get_access_secret())

    def regenerate_access_key(self) -> bool:
        """Создать новый ключ доступа и отозвать все подключения по старому.

        Служебный ключ TLS не меняется: записи у владельцев мастер-ключа
        остаются рабочими. Подключения по мастер-ключу не отзываются.
        """
        with self._lock:
            previous = self._config.app.nodes.access_secret
            self._config.app.nodes.access_secret = generate_access_secret()
            if not self._save():
                self._config.app.nodes.access_secret = previous
                return False
        self._revoke_peer_access("key")
        self._publish(
            EventType.NODE_CONFIG_CHANGED, {"type": "access_key_regenerated"}
        )
        return True

    def get_instance_id(self) -> str:
        """Идентификатор этой копии для сессий на удалённых нодах."""
        with self._lock:
            value = self._config.app.nodes.instance_id
            if value:
                return value
            value = uuid.uuid4().hex
            self._config.app.nodes.instance_id = value
            if not self._save():
                logger.warning("NodeService: идентификатор копии не сохранён")
            return value

    # ========== СЛУЖЕБНЫЙ МАСТЕР-КЛЮЧ ==========

    @staticmethod
    def master_key_supported() -> bool:
        """Есть ли в сборке открытая часть мастер-ключа (иначе механизм выключен)."""
        from src.utils.security.node_master import master_key_enabled

        return master_key_enabled()

    def has_master_key(self) -> bool:
        return self._master_key is not None and self.master_key_supported()

    def _master_private(self):
        """Закрытая часть мастер-ключа или None (не раскрывать наружу)."""
        return self._master_key if self.master_key_supported() else None

    def import_master_key(self, text: str) -> "tuple[bool, str]":
        """Загрузить служебный мастер-ключ в память до выхода из программы.

        Ключ проверяется по открытой части из сборки и на диск не пишется.
        """
        from src.utils.security.node_master import load_master_key

        try:
            key = load_master_key(text)
        except ValueError as error:
            return False, str(error)
        with self._lock:
            self._master_key = key
        self._publish(EventType.NODE_CONFIG_CHANGED, {"type": "master_key"})
        return True, "Служебный ключ загружен до закрытия программы"

    def remove_master_key(self) -> bool:
        """Забыть загруженный служебный ключ."""
        with self._lock:
            if self._master_key is None:
                return True
            self._master_key = None
        self._publish(EventType.NODE_CONFIG_CHANGED, {"type": "master_key"})
        return True

    def lift_ban(self, text: str = "") -> "tuple[bool, str]":
        """Снять блокировку копии: ключом из ``text`` или загруженным служебным."""
        from src.utils.security.ban import lift_ban, remove_ban
        from src.utils.security.node_master import DISABLED_MESSAGE

        if text:
            return lift_ban(text)
        if self._master_private() is None:
            if not self.master_key_supported():
                return False, DISABLED_MESSAGE
            return False, "Загрузите служебный ключ"
        # Загруженный ключ уже сверен с открытой частью при импорте.
        return remove_ban()

    def _revoke_peer_access(self, kind: str | None = None) -> None:
        """Отозвать API/VNC-доступ, не останавливая уже запущенные задания.

        ``kind``: None — все сессии, "key" — подключения по ключу доступа.
        """
        from src.web.node_server import (
            _registry, _node_thumbnail_consumers, _node_thumbnail_lock,
            _node_thumbnail_epochs, _node_thumbnail_ownership,
        )
        from src.web.vnc_access import revoke_all_vnc_access, revoke_vnc_owner

        owners = _registry.clear(kind)
        if kind is None:
            revoke_all_vnc_access()
        else:
            for owner in owners:
                revoke_vnc_owner(owner)
        consumers_to_drop = {"node:" + owner for owner in owners}
        with _node_thumbnail_lock:
            for key in list(_node_thumbnail_epochs):
                if kind is None or key[0] in consumers_to_drop:
                    _node_thumbnail_epochs[key] += 1
                    _node_thumbnail_ownership.pop(key, None)
            consumers = [
                (owner, service) for owner, service in _node_thumbnail_consumers.items()
                if kind is None or owner in consumers_to_drop
            ]
            for owner, service in consumers:
                _node_thumbnail_consumers.pop(owner, None)
                service.release_consumer(owner)

    # ========== CRUD НОД ==========

    def get_nodes(self) -> List[NodeRecord]:
        """Список записей нод."""
        return list(self._config.app.nodes.nodes or [])

    def get_node_by_iid(self, iid: str) -> Optional[NodeRecord]:
        """Запись ноды по iid."""
        if not iid:
            return None
        for r in self._config.app.nodes.nodes or []:
            if isinstance(r, NodeRecord) and r.iid == iid:
                return r
        return None

    def get_node_by_pubkey(self, pubkey: str) -> Optional[NodeRecord]:
        """Запись ноды по публичному ключу (точное совпадение)."""
        for r in self._config.app.nodes.nodes or []:
            if isinstance(r, NodeRecord) and r.pubkey == pubkey:
                return r
        return None

    def add_node(
        self, name: str, host: str, port: int, access_key: str = "", *, pubkey: str = ""
    ) -> "tuple[bool, str, Optional[NodeRecord]]":
        """Добавить ноду по её ключу доступа.

        Без ключа доступа нужен загруженный служебный мастер-ключ и
        открытый ключ TLS, полученный ``probe_master_node``. Такая нода
        подключается только пока ключ загружен (он не сохраняется).
        Возвращает (успех, сообщение, NodeRecord).
        """
        name, host = name.strip(), host.strip()
        if not name or not host:
            return False, "Заполните имя и хост", None
        if type(port) is not int or not 1 <= port <= 65535:
            return False, "Порт должен быть целым числом от 1 до 65535", None
        from src.utils.security.node_transport import node_base_url

        try:
            node_base_url(host, port)
        except ValueError:
            return False, "Укажите имя хоста или IP без схемы, пути и порта", None
        secret = ""
        if access_key.strip():
            try:
                pubkey, secret = parse_access_key(access_key)
            except ValueError as error:
                return False, str(error), None
        elif not (pubkey and self.has_master_key()):
            return False, "Укажите ключ доступа ноды", None
        if not fingerprint_from_pubkey(pubkey):
            return False, "Ключ ноды невалиден", None
        if self.get_node_by_pubkey(pubkey):
            return False, "Эта нода уже добавлена", None
        rec = NodeRecord(
            iid=uuid.uuid4().hex,
            name=name,
            host=host,
            port=int(port or 8801),
            pubkey=pubkey,
            secret=secret,
            autoconnect=True,
        )
        with self._lock:
            previous = self._config.app.nodes.nodes
            self._config.app.nodes.nodes = [*previous, rec]
            if not self._save():
                self._config.app.nodes.nodes = previous
                return False, "Не удалось сохранить настройки нод", None
        self._publish(EventType.NODE_CONFIG_CHANGED, {"type": "added", "iid": rec.iid})
        return True, "Нода добавлена", rec

    def update_node(
        self,
        iid: str,
        name: str = None,
        host: str = None,
        port: int = None,
        access_key: str = None,
        autoconnect: bool = None,
    ) -> "tuple[bool, str]":
        """Обновить ноду. None = не менять поле.

        ``access_key`` — новый ключ доступа (после его перегенерации на ноде).
        """
        rec = self.get_node_by_iid(iid)
        if rec is None:
            return False, "Нода не найдена"
        changes = {}
        if name is not None:
            changes["name"] = name.strip()
        if host is not None:
            from src.utils.security.node_transport import node_base_url

            try:
                node_base_url(host, port or rec.port)
            except ValueError:
                return False, "Укажите имя хоста или IP без схемы, пути и порта"
            changes["host"] = host.strip()
        if port is not None:
            if type(port) is not int or not 1 <= port <= 65535:
                return False, "Порт должен быть целым числом от 1 до 65535"
            changes["port"] = port
        if access_key is not None:
            try:
                pubkey, secret = parse_access_key(access_key)
            except ValueError as error:
                return False, str(error)
            duplicate = self.get_node_by_pubkey(pubkey)
            if duplicate is not None and duplicate.iid != iid:
                return False, "Эта нода уже добавлена"
            changes["pubkey"] = pubkey
            changes["secret"] = secret
        if autoconnect is not None:
            changes["autoconnect"] = bool(autoconnect)
        updated = replace(rec, **changes)
        if not updated.host:
            return False, "Укажите хост"
        if updated == rec:
            return True, "Настройки не изменились"
        with self._lock:
            previous = self._config.app.nodes.nodes
            self._config.app.nodes.nodes = [updated if r.iid == iid else r for r in previous]
            if not self._save():
                self._config.app.nodes.nodes = previous
                return False, "Не удалось сохранить настройки нод"
            if (rec.pubkey, rec.secret) != (updated.pubkey, updated.secret):
                self._tokens.pop(iid, None)
        self._publish(EventType.NODE_CONFIG_CHANGED, {"type": "updated", "iid": iid})
        return True, "Нода обновлена"

    def delete_node(self, iid: str) -> "tuple[bool, str]":
        """Удалить ноду. Останавливает клиент и очищает память."""
        with self._lock:
            lst = self._config.app.nodes.nodes
            before = len(lst)
            self._config.app.nodes.nodes = [
                r for r in lst if not (isinstance(r, NodeRecord) and r.iid == iid)
            ]
            if len(self._config.app.nodes.nodes) == before:
                return False, "Нода не найдена"
            if not self._save():
                self._config.app.nodes.nodes = lst
                return False, "Не удалось сохранить настройки нод"
        self.stop_client(iid)
        with self._lock:
            self._hosts.pop(iid, None)
            self._reachable.pop(iid, None)
            self._names.pop(iid, None)
            self._tokens.pop(iid, None)
            self._capabilities.pop(iid, None)
            self._thumbnail_backends.pop(iid, None)
            self._diagnostics.pop(iid, None)
        self._publish(EventType.NODE_CONFIG_CHANGED, {"type": "deleted", "iid": iid})
        return True, "Нода удалена"

    # ========== СОСТОЯНИЕ КЛИЕНТОВ ==========

    def set_client(self, iid: str, client) -> None:
        with self._lock:
            previous = self._clients.get(iid)
            if previous is client:
                return
            self._clients[iid] = client
        if previous is not None:
            self._retire_clients([previous])

    def get_client(self, iid: str):
        with self._lock:
            return self._clients.get(iid)

    def _retire_clients(self, clients: list[Any]) -> None:
        """Request every stop without waiting, retaining real Qt threads first."""
        retiring: list[QThread] = []
        with self._lock:
            for client in clients:
                if isinstance(client, QThread) and not any(
                    owned is client for owned in self._zombie_clients
                ):
                    self._zombie_clients.append(client)
                    retiring.append(client)
        # stop() is a thread-safe, prompt cancellation hook on NodeClientWorker.
        # Request all stops before doing any cleanup; never wait per client.
        for client in clients:
            try:
                client.stop()
            except Exception as e:
                logger.debug(f"NodeService: ошибка остановки клиента: {e}")
        for client in retiring:
            self._client_reaper.retire_requested.emit(client)

    def retire_background_worker(self, worker: QThread) -> None:
        """Удерживать отменённую проверку диалога до реального finished."""
        self._retire_clients([worker])

    def _release_retired_client(self, client: QThread) -> None:
        """Drop a stopped QThread on its owning thread, never a replacement."""
        with self._lock:
            if not any(owned is client for owned in self._zombie_clients):
                return
            self._zombie_clients[:] = [
                owned for owned in self._zombie_clients if owned is not client
            ]
        client.deleteLater()

    def stop_client(self, iid: str) -> None:
        """Remove an active client and request its stop without blocking."""
        with self._lock:
            client = self._clients.pop(iid, None)
        if client is not None:
            self._retire_clients([client])

    def stop_all_clients(self) -> None:
        """Clear active clients and request all stops without waiting."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        self._retire_clients(clients)

    def has_pending_clients(self) -> bool:
        """Return whether active or stopping clients are still owned.

        After shutdown(), keep this service and the Qt event loop alive until
        False. Finished cleanup and parent detachment run on the GUI thread.
        """
        with self._lock:
            return bool(self._clients or self._zombie_clients)

    # ========== ДАННЫЕ НОД (память) ==========

    def set_hosts(self, iid: str, tree: list, *, client: object = None) -> bool:
        with self._lock:
            if client is not None and self._clients.get(iid) is not client:
                return False
            self._hosts[iid] = tree or []
        self._publish(EventType.NODE_HOSTS_UPDATED, {"iid": iid})
        return True

    def get_hosts(self, iid: str) -> list:
        with self._lock:
            return self._hosts.get(iid, [])

    def set_reachable(
        self, iid: str, reachable: bool, *, client: object = None
    ) -> bool:
        with self._lock:
            if client is not None and self._clients.get(iid) is not client:
                return False
            prev = self._reachable.get(iid)
            self._reachable[iid] = reachable
        if prev != reachable:
            self._publish(
                EventType.NODE_CONNECTED if reachable else EventType.NODE_DISCONNECTED,
                {"iid": iid},
            )
        return True

    def is_reachable(self, iid: str) -> bool:
        with self._lock:
            return self._reachable.get(iid, False)

    def set_name(self, iid: str, name: str) -> None:
        with self._lock:
            self._names[iid] = name

    def get_display_name(self, iid: str) -> str:
        """Имя ноды для UI: локальное из записи, иначе из hello-ответа."""
        rec = self.get_node_by_iid(iid)
        if rec is not None and rec.name:
            return rec.name
        with self._lock:
            return self._names.get(iid) or (rec.host if rec else iid[:8])

    def set_token(self, iid: str, token: str) -> None:
        with self._lock:
            self._tokens[iid] = token

    def get_token(self, iid: str) -> Optional[str]:
        with self._lock:
            return self._tokens.get(iid)

    def set_capabilities(
        self, iid: str, caps: dict, thumbnail_backends: list[str] | None = None
    ) -> None:
        """Сохранить capabilities ноды из hello-ответа."""
        with self._lock:
            self._capabilities[iid] = dict(caps or {})
            backends = (
                thumbnail_backends
                if thumbnail_backends is not None
                else (caps or {}).get("supported_thumbnail_backends", [])
            )
            self._thumbnail_backends[iid] = (
                [name for name in backends if name in ("veyon", "vnc")]
                if isinstance(backends, list)
                else []
            )

    def supports_status_batch(self, iid: str) -> bool:
        """Нода принимает статусы нескольких хостов одним запросом."""
        with self._lock:
            return (self._capabilities.get(iid) or {}).get("status_batch") is True

    def supports_background_commands(self, iid: str) -> bool:
        """Нода подтверждает отдельный протокол фонового запуска SSH-шагов."""
        with self._lock:
            return (self._capabilities.get(iid) or {}).get("background_commands") is True

    def supports_desktop_session(self, iid: str) -> bool:
        """Поддерживает ли нода выполнение в окружении рабочего стола."""
        with self._lock:
            return (self._capabilities.get(iid) or {}).get("desktop_session") is True

    def supports_desktop_session_steps(self, iid: str) -> bool:
        """Поддерживает ли нода выбор прав отдельно для каждого SSH-шага."""
        with self._lock:
            return (self._capabilities.get(iid) or {}).get("desktop_session_steps") is True

    def supports_session(self, iid: str) -> bool:
        """Поддерживает ли нода session-режим (capability из hello)."""
        with self._lock:
            return bool((self._capabilities.get(iid) or {}).get("session", False))

    def supports_thumbnail_backends(self, iid: str) -> bool:
        """Report source-selection support; old/unknown nodes require an upgrade."""
        with self._lock:
            return (self._capabilities.get(iid) or {}).get("thumbnail_backends") is True

    def get_thumbnail_backends(self, iid: str) -> list[str]:
        with self._lock:
            return list(self._thumbnail_backends.get(iid, []))

    # ========== НАСТРОЙКИ ==========

    def get_listen_enabled(self) -> bool:
        return bool(self._config.app.nodes.listen_enabled)

    def set_listen_enabled(self, enabled: bool) -> bool:
        return self.set_listen_settings(self.get_listen_port(), bool(enabled))

    def get_listen_port(self) -> int:
        return int(self._config.app.nodes.listen_port or 8801)

    def set_listen_port(self, port: int) -> bool:
        return self.set_listen_settings(port, self.get_listen_enabled())

    def set_listen_settings(self, port: int, enabled: bool) -> bool:
        """Согласованно сохранить параметры сервера или вернуть прежние."""
        if type(port) is not int or not 1 <= port <= 65535:
            return False
        with self._lock:
            previous = self._config.app.nodes
            self._config.app.nodes = replace(
                previous, listen_port=port, listen_enabled=bool(enabled)
            )
            if not self._save():
                self._config.app.nodes = previous
                return False
        self._publish(
            EventType.NODE_CONFIG_CHANGED,
            {"type": "listen_settings", "port": port, "enabled": bool(enabled)},
        )
        return True

    def get_show_in_tree(self) -> bool:
        return bool(getattr(self._config.app.nodes, "show_in_tree", True))

    def set_show_in_tree(self, show: bool) -> bool:
        with self._lock:
            previous = self._config.app.nodes.show_in_tree
            self._config.app.nodes.show_in_tree = bool(show)
            if not self._save():
                self._config.app.nodes.show_in_tree = previous
                return False
        self._publish(
            EventType.NODE_CONFIG_CHANGED, {"type": "show_in_tree", "show": bool(show)}
        )
        return True

    def get_poll_interval(self) -> int:
        return int(getattr(self._config.app.nodes, "poll_interval_sec", 5) or 5)

    # ========== HANDSHAKE (клиентская сторона) ==========

    def _authenticate(
        self, transport, base: str, pubkey: str, secret: str, timeout: float,
        *, master_only: bool = False,
    ) -> "tuple[str, str]":
        """Получить токен: ключом доступа, при отказе — мастер-ключом.

        ``master_only`` — не пробовать ключ доступа (нужно для команд,
        которые сервер разрешает только сессии мастер-ключа).
        Возвращает (token, ошибка); ровно одно из значений непустое.
        """
        import socket

        from src.utils.security.node_crypto import access_proof
        from src.utils.security.node_master import sign_master_challenge

        master = self._master_private()
        attempts = []
        if secret and not master_only:
            attempts.append(
                ("proof", lambda challenge: access_proof(secret, pubkey, challenge))
            )
        if master is not None:
            attempts.append(
                (
                    "master_signature",
                    lambda challenge: sign_master_challenge(master, pubkey, challenge),
                )
            )
        if not attempts:
            if master_only:
                return "", "Нужен служебный ключ: загрузите его"
            error = (
                "Открытый ключ ноды сохранён, но отсутствует секрет доступа "
                "для текущего протокола. Укажите полный ключ доступа из настроек удалённой ноды"
            )
            if self.master_key_supported():
                error += " либо загрузите служебный ключ"
            return "", error
        error = ""
        for field_name, respond in attempts:
            r = transport.post(f"{base}/api/node/challenge", json={}, timeout=timeout)
            if r.status_code != 200:
                return "", f"challenge: HTTP {r.status_code}"
            challenge_hex = (r.json() or {}).get("challenge", "")
            if not challenge_hex:
                return "", "challenge: пустой ответ"
            r = transport.post(
                f"{base}/api/node/verify",
                json={
                    "challenge": challenge_hex,
                    "client_id": self.get_instance_id(),
                    "client_name": socket.gethostname()[:128],
                    field_name: respond(bytes.fromhex(challenge_hex)),
                },
                timeout=timeout,
            )
            if r.status_code == 403:
                error = "Проверьте ключ доступа или обновите ноду"
                continue
            if r.status_code != 200:
                return "", f"verify: HTTP {r.status_code}"
            token = (r.json() or {}).get("token", "")
            return (token, "") if token else ("", "verify: нет токена")
        return "", error

    def start_handshake(
        self, iid: str, *, client: object = None
    ) -> "tuple[bool, str, Optional[dict]]":
        """Выполнить handshake с нодой: GET hello → POST challenge → POST verify.

        Возвращает (успех, сообщение, client_info|None).
        client_info: {"token": str, "name": str, "fingerprint": str, ...}
        """
        rec = self.get_node_by_iid(iid)
        if rec is None:
            return False, "Нода не найдена", None
        if client is not None and (
            self.get_client(iid) is not client
            or (client._host, client._port, client._pubkey)
            != (rec.host, rec.port, rec.pubkey)
        ):
            return False, "Параметры подключения изменились", None
        if not rec.pubkey:
            return False, "Не сохранён открытый ключ ноды. Укажите полный ключ доступа", None
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            self.record_diagnostic(iid, stage="blocked", error="Копия программы заблокирована")
            return False, BAN_MESSAGE, None

        import requests

        from src.utils.security.node_transport import NodeHTTPSession, node_base_url

        base = node_base_url(rec.host, rec.port)
        transport = NodeHTTPSession(rec.pubkey)
        self.record_diagnostic(iid, stage="handshake")
        timeout = 5

        try:
            # 1. hello — имя, версия и возможности; ключ TLS уже сверен.
            r = transport.get(f"{base}/api/node/hello", timeout=timeout)
            if r.status_code != 200:
                self.record_diagnostic(iid, stage="hello", error=f"HTTP {r.status_code}")
                return False, f"HTTP {r.status_code}", None
            hello = r.json()
            remote_fp = hello.get("fingerprint", "")
            if hello.get("pubkey") != rec.pubkey:
                self.record_diagnostic(iid, stage="hello", error="Несогласованный ключ сервера")
                return (
                    False,
                    "Ключ ноды не совпадает с доверенным (возможна подмена)",
                    None,
                )
            if (hello.get("capabilities") or {}).get("auth") != "access-key-v1":
                self.record_diagnostic(iid, stage="hello", error="Старый протокол доступа")
                return False, "Обновите dno-tool на ноде: изменился протокол доступа", None

            # 2-3. challenge → verify
            token, error = self._authenticate(
                transport, base, rec.pubkey, rec.secret, timeout
            )
            if not token:
                self.record_diagnostic(iid, stage="verify", error=error)
                return False, error, None

            with self._lock:
                if self.get_node_by_iid(iid) != rec or (
                    client is not None and self._clients.get(iid) is not client
                ):
                    return False, "Подключение отменено", None
                self.set_token(iid, token)
                self.set_name(iid, hello.get("name", ""))
                self.set_capabilities(
                    iid, hello.get("capabilities", {}), hello.get("thumbnail_backends", [])
                )
            self.record_diagnostic(
                iid, stage="ready", error="", version=hello.get("version", ""),
                capabilities=[key for key, value in hello.get("capabilities", {}).items() if value],
            )
            self.set_reachable(iid, True, client=client)
            return (
                True,
                "Соединено",
                {
                    "token": token,
                    "name": hello.get("name", ""),
                    "fingerprint": remote_fp,
                    "capabilities": hello.get("capabilities", {}),
                    "thumbnail_backends": self.get_thumbnail_backends(iid),
                },
            )
        except requests.exceptions.SSLError:
            self.record_diagnostic(iid, stage="tls", error="TLS: ключ не подтверждён или удалённая нода требует обновления")
            self.set_reachable(iid, False, client=client)
            return False, "TLS не подтверждён: проверьте ключ и обновите удалённую ноду", None
        except requests.exceptions.ConnectionError:
            self.record_diagnostic(iid, stage="connect", error="Нет соединения")
            self.set_reachable(iid, False, client=client)
            return False, "Нода недоступна (нет соединения)", None
        except requests.exceptions.Timeout:
            self.record_diagnostic(iid, stage="connect", error="Таймаут")
            self.set_reachable(iid, False, client=client)
            return False, "Нода не отвечает (таймаут)", None
        except Exception as e:
            logger.debug(f"NodeService: handshake с {iid}: {type(e).__name__}")
            self.record_diagnostic(iid, stage="handshake", error=type(e).__name__)
            self.set_reachable(iid, False, client=client)
            return False, f"Некорректный ответ ноды: {type(e).__name__}", None
        finally:
            transport.close()

    def probe_master_node(self, host: str, port: int) -> "tuple[bool, str, str]":
        """Подключиться к ноде мастер-ключом без её ключа доступа (блокирующий I/O).

        Ключ TLS принимается при первом подключении и возвращается для
        закрепления в записи. Вызывать только из рабочего потока.
        Возвращает (успех, сообщение, открытый ключ TLS).
        """
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            return False, BAN_MESSAGE, ""
        if self._master_private() is None:
            return False, "Укажите ключ доступа ноды", ""
        import requests

        from src.utils.security.node_transport import (
            NodeHTTPSession, fetch_tls_pubkey, node_base_url,
        )

        try:
            base = node_base_url(host, port)
            pubkey = fetch_tls_pubkey(host, port)
        except ValueError:
            return False, "Укажите имя хоста или IP без схемы, пути и порта", ""
        except OSError:
            return False, "Нода недоступна (нет соединения)", ""
        except Exception:
            return False, "Сервер не похож на ноду dno-tool", ""
        transport = NodeHTTPSession(pubkey)
        try:
            r = transport.get(f"{base}/api/node/hello", timeout=5)
            hello = r.json() if r.status_code == 200 else {}
            if hello.get("pubkey") != pubkey:
                return False, "Сервер не похож на ноду dno-tool", ""
            if (hello.get("capabilities") or {}).get("auth") != "access-key-v1":
                return False, "Обновите dno-tool на ноде: изменился протокол доступа", ""
            token, error = self._authenticate(transport, base, pubkey, "", 5)
            if not token:
                return False, error, ""
            return True, "Соединено", pubkey
        except requests.exceptions.RequestException:
            return False, "Нода недоступна (нет соединения)", ""
        except Exception as error:
            return False, f"Некорректный ответ ноды: {type(error).__name__}", ""
        finally:
            transport.close()

    # ========== KILL-SWITCH ==========

    def _service_request(
        self, iid: str, path: str, payload: dict, *, master_only: bool = False
    ) -> "tuple[bool, str, dict]":
        """Открыть собственную сессию к ноде и выполнить служебную команду.

        Токен не кэшируется в состоянии сервиса (это разовое действие,
        не опрос). Работает и для скрытых нод без клиента.
        Возвращает (успех, сообщение, тело ответа).
        """
        rec = self.get_node_by_iid(iid)
        if rec is None:
            return False, "Нода не найдена", {}
        if not rec.pubkey:
            return False, "Не сохранён открытый ключ ноды. Укажите полный ключ доступа", {}

        from src.utils.security.node_transport import NodeHTTPSession, node_base_url

        base = node_base_url(rec.host, rec.port)
        transport = NodeHTTPSession(rec.pubkey)
        try:
            hello = transport.get(f"{base}/api/node/hello", timeout=5)
            if hello.status_code != 200 or (hello.json() or {}).get("pubkey") != rec.pubkey:
                return False, "Ключ ноды не совпадает с доверенным", {}
            token, error = self._authenticate(
                transport, base, rec.pubkey, rec.secret, 5, master_only=master_only
            )
            if not token:
                return False, error, {}
            response = transport.post(
                f"{base}{path}",
                json=payload,
                headers={"Authorization": f"Node {token}"},
                timeout=(5, 15),
                allow_redirects=False,
            )
            if response.status_code != 200:
                if response.status_code == 403:
                    return False, "Нода отклонила команду: нужен служебный ключ", {}
                return False, f"HTTP {response.status_code}", {}
            body = response.json() if response.content else {}
            if not body.get("ok"):
                return False, "Нода отклонила команду", {}
            return True, "", body
        except Exception as error:
            return False, f"Ответ ноды не получен: {type(error).__name__}", {}
        finally:
            transport.close()

    def wipe_remote_node(self, iid: str) -> "tuple[bool, str]":
        """Полная очистка выбранной ноды и её блокировка (блокирующий I/O)."""
        ok, message, _ = self._service_request(
            iid, "/api/node/killswitch", {"confirm": "WIPE"}, master_only=True
        )
        if not ok:
            return False, message
        self.stop_client(iid)
        self.set_reachable(iid, False)
        return True, "Нода очищена и завершает работу"

    def block_remote_node(self, iid: str) -> "tuple[bool, str]":
        """Заблокировать выбранную ноду без удаления данных (блокирующий I/O)."""
        ok, message, _ = self._service_request(
            iid, "/api/node/block", {}, master_only=True
        )
        if not ok:
            return False, message
        return True, "Нода заблокирована"

    def unblock_remote_node(self, iid: str) -> "tuple[bool, str]":
        """Снять блокировку ноды. Требуется служебный мастер-ключ."""
        if not self.master_key_supported():
            from src.utils.security.node_master import DISABLED_MESSAGE

            return False, DISABLED_MESSAGE
        if not self.has_master_key():
            return False, "Снять блокировку можно только служебным ключом"
        ok, message, body = self._service_request(
            iid, "/api/node/unblock", {}, master_only=True
        )
        if not ok:
            return False, message
        return True, str(body.get("message") or "Блокировка снята")

    # ========== СЛУЖЕБНЫЕ ==========

    def record_diagnostic(self, iid: str, **values) -> None:
        """Обновить обезличенное состояние подключения."""
        import time

        with self._lock:
            state = self._diagnostics.setdefault(iid, {})
            state.update(values)
            if values.get("stage") == "ready":
                state["last_success"] = time.time()

    def get_diagnostics(self, iid: str) -> dict:
        with self._lock:
            return dict(self._diagnostics.get(iid, {}))

    def shutdown(self) -> None:
        """Mark shutdown and request cancellation without waiting for clients."""
        with self._lock:
            self._stop = True
        self.stop_all_clients()

    def _save(self) -> bool:
        try:
            self._config.save()
            return True
        except Exception as e:
            logger.error(f"NodeService: ошибка сохранения: {e}")
            return False

    def _publish(self, event_type: EventType, data: dict) -> None:
        if self._event_bus:
            try:
                self._event_bus.publish_typed(
                    event_type, source=self.__class__.__name__, data=data
                )
            except Exception as e:
                logger.debug(f"NodeService: ошибка публикации события: {e}")
