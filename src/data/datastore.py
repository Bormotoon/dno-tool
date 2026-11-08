"""
Потокобезопасное хранилище данных с интеграцией EventBus.

Предоставляет:
- Потокобезопасный доступ через RLock
- Валидацию данных
- События об изменениях для интеграции с EventBus
- Кэширование последних операций
"""

import copy
import json
import os
import re
import tempfile
import time
import uuid
import zlib
from collections import deque
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from src.architecture import EventBus, EventType
from src.config import (
    DEFAULT_COMMANDS_FILE,
    DEFAULT_HOSTS_FILE,
    normalize_thumbnail_backend,
    validate_thumbnail_backend,
)
from src.logger import logger
from src.utils.filesystem.fs_utils import ensure_user_owned


def _fast_clone(value: Any) -> Any:
    """Быстрое глубокое копирование JSON-структур (dict/list/примитивы) без оверхеда _fast_clone."""
    t = type(value)
    if t is dict:
        return {k: _fast_clone(v) for k, v in value.items()}
    if t is list:
        return [_fast_clone(item) for item in value]
    return value


def _pack_history_entry(entry: dict[str, Any]) -> bytes:
    raw = json.dumps(entry, ensure_ascii=False).encode("utf-8")
    return zlib.compress(raw, level=1)


def _unpack_history_entry(blob: bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(blob, dict):
        return blob
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _same_path(first: str, second: str) -> bool:
    """Сравнить пути файлов с учётом регистра и разделителей ОС."""
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second)
    )

class DataStore(QObject):
    """
    Потокобезопасное хранилище данных хостов и команд.

    Обеспечивает CRUD-операции над хостами и командами с поддержкой:
    - Валидации данных перед записью;
    - Истории операций для undo;
    - Публикации событий через EventBus и Qt-сигналы.

    Атрибуты:
        hosts_data_changed: Qt-сигнал, излучаемый при изменении данных хостов.
        commands_data_changed: Qt-сигнал, излучаемый при изменении данных команд.
    """

    # Сигналы для Qt интеграции
    hosts_data_changed = Signal(dict)  # {action, data}
    commands_data_changed = Signal(dict)  # {action, data}

    # Статистика для отладки
    stats_signal = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._hosts_data: List[Dict] = []  # Плоский список устройств
        self._hosts_data_raw: List[Dict] = []  # Оригинальная структура с группами
        self._commands_data: List[Dict] = []
        # Индексы по iid (персистентны — сохраняются в JSON-файлах)
        self._hosts_by_iid: Dict[str, Dict] = {}
        self._host_revisions: dict[str, int] = {}
        self._host_revision_serial = 0
        self._tasks_by_iid: Dict[str, Dict] = {}
        # Ошибка загрузки рабочего commands.json: пока она есть, файл не перезаписывается.
        self._commands_load_error: str | None = None
        # Текст последней ошибки проверки или загрузки задач для показа пользователю.
        self._commands_error = ""
        self._lock = RLock()  # Потокобезопасность

        # Кэш последних операций для undo/redo
        self._operation_history: deque = deque(maxlen=50)

        # Статистика операций
        self._stats = {"reads": 0, "writes": 0, "validations": 0, "errors": 0}

        # EventBus для интеграции с архитектурой (устанавливается через set_event_bus)
        self._event_bus: Optional[EventBus] = None

        # Загружаем начальные данные
        self._load_initial_data()

    def _load_initial_data(self):
        """Загрузка начальных данных из файлов"""
        with self._lock:
            self._hosts_data = self._load_hosts_from_file_internal()
            # Сохраняем и оригинальную структуру
            self._hosts_data_raw = self._load_hosts_raw_from_file_internal()
            try:
                self._commands_data = self._load_commands_from_file_internal()
            except FileNotFoundError:
                self._commands_data = []
            except (OSError, ValueError) as error:
                # Испорченный файл не подменяется пустым списком при сохранении.
                self._commands_data = []
                self._commands_load_error = f"{DEFAULT_COMMANDS_FILE}: {error}"
                logger.error(f"DataStore: Commands file not loaded: {error}")
            # Присваиваем iid отсутствующим записям и строим индексы
            self._rebuild_iid_indexes()
            logger.info(
                f"DataStore loaded: {len(self._hosts_data)} hosts, {len(self._commands_data)} commands"
            )

    def _load_hosts_raw_from_file_internal(
        self, filename: str = DEFAULT_HOSTS_FILE
    ) -> List[Dict]:
        """Загрузка оригинальной структуры хостов с группами"""
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self._normalize_host_thumbnail_backends(data)
                    return data
                return []
        except:
            return []

    def _rebuild_iid_indexes(self) -> None:
        """Присвоить iid каждой записи-листу (host/task) без него и перестроить индексы.

        iid — персистентный идентификатор, сохраняется в JSON-файлах.
        При загрузке старых файлов без iid генерируется новый (тихая миграция),
        который будет записан при следующем сохранении.
        """
        self._rebuild_hosts_iid_index()
        # Задачи: iid присваивается рекурсивно в иерархии commands
        self._assign_tasks_iids_recursive(self._commands_data)

    def _rebuild_hosts_iid_index(self) -> None:
        """Заполнить отсутствующие iid, исправить дубли и перестроить индекс хостов."""
        previous = self._hosts_by_iid
        self._hosts_by_iid = {}
        for h in self._hosts_data:
            if "iid" not in h or not h["iid"]:
                h["iid"] = uuid.uuid4().hex
            elif h["iid"] in self._hosts_by_iid:
                logger.warning(
                    "DataStore: повторяющийся iid хоста %s — назначен новый iid",
                    h["iid"],
                )
                h["iid"] = uuid.uuid4().hex
            self._hosts_by_iid[h["iid"]] = h
            if previous.get(h["iid"]) != h or h["iid"] not in self._host_revisions:
                self._host_revision_serial += 1
                self._host_revisions[h["iid"]] = self._host_revision_serial
        self._host_revisions = {
            iid: revision
            for iid, revision in self._host_revisions.items()
            if iid in self._hosts_by_iid
        }
        # Синхронизировать iid в raw-структуре
        self._sync_hosts_raw_iids()

    def _sync_hosts_raw_iids(self) -> None:
        """Сохранить известные iid, сопоставить остальные записи по адресу и имени."""
        raw_hosts: list[dict] = []
        self._extract_hosts_recursive(self._hosts_data_raw, raw_hosts)
        existing_iids = {
            host["iid"] for host in raw_hosts if host.get("iid") in self._hosts_by_iid
        }
        # FIFO fallback prevents collapsing legacy records with identical names
        # and addresses when flat/raw trees were loaded independently.
        mapping: dict[tuple[Any, Any], deque[str]] = {}
        for h in self._hosts_data:
            if h.get("iid") not in existing_iids:
                mapping.setdefault((h.get("host"), h.get("name")), deque()).append(
                    h["iid"]
                )
        used_iids: set[str] = set()

        def _walk(node):
            if isinstance(node, list):
                for item in node:
                    _walk(item)
            elif isinstance(node, dict):
                if "host" in node and "name" in node:
                    # Каждый известный iid сохраняется только у первой записи.
                    # Остальные получают исправленные iid из плоского списка.
                    iid = node.get("iid")
                    if iid in self._hosts_by_iid and iid not in used_iids:
                        used_iids.add(iid)
                        return
                    candidates = mapping.get((node.get("host"), node.get("name")))
                    while candidates and candidates[0] in used_iids:
                        candidates.popleft()
                    if candidates:
                        node["iid"] = candidates.popleft()
                        used_iids.add(node["iid"])
                else:
                    for value in node.values():
                        _walk(value)

        _walk(self._hosts_data_raw)

    def _sync_hosts_raw_data(self, op_data: Dict, action: str) -> None:
        """Зеркалировать изменения плоского списка в raw-структуру (по iid).

        update: обновляет поля существующего листа raw-структуры по iid
        (host может измениться — ищем строго по iid).
        add: добавляет новый лист в указанную папку либо в корень.
        Вызывается из update_host/delete_host_by_iid после изменения
        _hosts_data — иначе hosts.json и /api/node/hosts возвращают
        устаревшие данные.
        """
        try:
            existing_iids = set()

            def _collect(node):
                """Собрать iid существующих листов raw-структуры."""
                if isinstance(node, list):
                    for item in node:
                        _collect(item)
                elif isinstance(node, dict):
                    if "host" in node and "name" in node:
                        existing_iids.add(node.get("iid"))
                    else:
                        for value in node.values():
                            if isinstance(value, list):
                                _collect(value)

            def _find_update(node) -> bool:
                """True если внутри найден и обновлён лист op_data.iid."""
                if isinstance(node, list):
                    return any(_find_update(item) for item in node)
                if isinstance(node, dict):
                    if "host" in node and "name" in node:
                        if node.get("iid") == op_data.get("iid"):
                            node.clear()
                            node.update(_fast_clone(op_data))
                            return True
                        return False
                    for value in node.values():
                        if isinstance(value, list) and _find_update(value):
                            return True
                return False

            if action == "add":
                _collect(self._hosts_data_raw)
                iid = op_data.get("iid")
                if iid and iid not in existing_iids:
                    folder = op_data.get("folder")

                    def _folder_items(nodes: List[Dict]) -> Optional[List[Dict]]:
                        """Найти папку, не принимая поля устройства за группы."""
                        for node in nodes:
                            if not isinstance(node, dict) or "host" in node:
                                continue
                            for name, children in node.items():
                                if not isinstance(children, list):
                                    continue
                                if name == folder:
                                    return children
                                found = _folder_items(children)
                                if found is not None:
                                    return found
                        return None

                    destination = self._hosts_data_raw
                    if folder:
                        destination = _folder_items(self._hosts_data_raw)
                        if destination is None:
                            destination = []
                            self._hosts_data_raw.append({folder: destination})
                    destination.append(_fast_clone(op_data))
            else:
                _find_update(self._hosts_data_raw)
        except Exception as e:
            logger.error(f"DataStore: _sync_hosts_raw_data error: {e}")

    def _sync_hosts_raw_delete(self, iid: str) -> None:
        """Удалить лист с данным iid из raw-структуры (по образцу delete)."""
        try:

            def _walk(node):
                if isinstance(node, list):
                    for i in range(len(node) - 1, -1, -1):
                        item = node[i]
                        if (
                            isinstance(item, dict)
                            and item.get("iid") == iid
                            and "host" in item
                        ):
                            del node[i]
                        elif isinstance(item, (dict, list)):
                            _walk(item)
                elif isinstance(node, dict):
                    for value in node.values():
                        if isinstance(value, list):
                            _walk(value)

            _walk(self._hosts_data_raw)
        except Exception as e:
            logger.error(f"DataStore: _sync_hosts_raw_delete error: {e}")

    def _assign_tasks_iids_recursive(
        self, items: List[Dict], _seen: Optional[set] = None
    ) -> None:
        """Рекурсивно присвоить iid каждой задаче (Task) без него (тихая миграция) и построить индекс.

        Дубли iid (копии с тем же iid) получают новый iid с предупреждением
        в лог - иначе merge/связи считают их одной задачей.
        """
        if _seen is None:
            _seen = set()
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            # Task (имеет name и commands)
            if "name" in item and "commands" in item:
                iid = item.get("iid")
                if not iid:
                    item["iid"] = uuid.uuid4().hex
                elif iid in _seen:
                    logger.warning(
                        f"DataStore: duplicate task iid {iid} "
                        f"({item.get('name')}) - assigning new iid"
                    )
                    item["iid"] = uuid.uuid4().hex
                _seen.add(item["iid"])
                self._tasks_by_iid[item["iid"]] = item
            # Категория (один ключ -> список)
            elif len(item) == 1:
                children = next(iter(item.values()))
                if isinstance(children, list):
                    self._assign_tasks_iids_recursive(children, _seen)

    # ========== HOSTS OPERATIONS ==========

    def get_hosts_data(self) -> List[Dict]:
        """
        Получить копию данных хостов (потокобезопасно).

        Returns:
            Плоский список всех хостов (без групп)
        """
        with self._lock:
            self._stats["reads"] += 1
            return [dict(h) for h in self._hosts_data]

    def get_hosts_data_raw(self) -> List[Dict]:
        """
        Получить оригинальные данные хостов с группами.

        Returns:
            Оригинальные данные с группами
        """
        with self._lock:
            self._stats["reads"] += 1
            # Возвращаем сохранённую структуру или читаем из файла
            if self._hosts_data_raw:
                return [
                    dict(h) if isinstance(h, dict) else h for h in self._hosts_data_raw
                ]
            try:
                with open(DEFAULT_HOSTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return []

    def get_hosts_count(self) -> int:
        """Получить количество хостов (быстрая операция)"""
        with self._lock:
            return len(self._hosts_data)

    def set_hosts_data(self, new_data: List[Dict]) -> bool:
        """
        Установить новые данные хостов.

        Args:
            new_data: Новые данные (список хостов или структура с группами)

        Returns:
            True если успешно
        """
        with self._lock:
            try:
                # API/bulk updates are strict; only file loads normalize values.
                hosts: list[dict] = []
                self._extract_hosts_recursive(new_data, hosts)
                for host in hosts:
                    if "thumbnail_backend" in host:
                        validate_thumbnail_backend(
                            host["thumbnail_backend"], allow_none=True
                        )
                # Проверяем это структура с группами или плоский список
                # Структура с группами: [{"Group": [...]}, {"Group2": [...]}]
                # Плоский список: [{"name": "...", "host": "..."}]
                has_groups = False
                if new_data:
                    for item in new_data:
                        if isinstance(item, dict) and len(item) == 1:
                            # Это группа (один ключ со списком)
                            has_groups = True
                            break
                        elif isinstance(item, dict) and "host" in item:
                            # Это устройство (есть ключ 'host')
                            pass

                if has_groups:
                    # Это структура с группами
                    self._hosts_data_raw = _fast_clone(new_data)
                    # Извлекаем плоский список
                    self._hosts_data = []
                    self._extract_hosts_recursive(
                        self._hosts_data_raw, self._hosts_data
                    )
                else:
                    # Это плоский список
                    self._hosts_data = _fast_clone(new_data)
                    self._hosts_data_raw = []  # Очищаем raw если данные плоские

                # Перестроить индексы по iid (присвоить iid новым записям без него)
                self._rebuild_hosts_iid_index()

                logger.debug(
                    f"DataStore: Hosts data set ({len(self._hosts_data)} items)"
                )
                return True

            except Exception as e:
                logger.error(f"DataStore: Error setting hosts data: {e}")
                self._stats["errors"] += 1
                return False

    def setup_snapshot(self):
        """Independent in-memory baseline; never re-read potentially stale files."""
        with self._lock:
            return (_fast_clone(self._hosts_data_raw or self._hosts_data),
                    _fast_clone(self._commands_data))

    def commit_setup_snapshot(self, baseline, hosts, commands, persist):
        """Validate and persist before publishing either new model."""
        from src.services.configuration.setup_draft import normalize_tree

        with self._lock:
            if self.setup_snapshot() != baseline:
                raise RuntimeError("Хосты или команды изменились. Откройте мастер заново.")
            hosts = normalize_tree(hosts, "hosts")
            commands = normalize_tree(commands, "commands")
            flat = []
            self._extract_hosts_recursive(hosts, flat)
            for host in flat:
                validate_thumbnail_backend(host.get("thumbnail_backend"), allow_none=True)
            tasks = {}

            def index(nodes):
                for node in nodes:
                    if "commands" in node:
                        tasks[node["iid"]] = node
                    else:
                        index(next(iter(node.values())))
            index(commands)
            persist(hosts, commands)
            self._hosts_data_raw, self._hosts_data = hosts, flat
            self._commands_data, self._tasks_by_iid = commands, tasks
            self._rebuild_hosts_iid_index()
            self._stats["writes"] += 1
        self._notify_change("hosts", "set", _fast_clone(hosts))
        self._notify_change("commands", "set", _fast_clone(commands))

    def update_host(
        self, host_data: Dict, *, persist: bool = False
    ) -> Tuple[bool, str]:
        """
        Обновить или добавить хост.

        Args:
            host_data: Данные хоста (обязательное поле 'host').
            persist: Save the updated tree before publishing; rollback on failure.

        Returns:
            Кортеж (успех, сообщение).
        """
        with self._lock:
            self._stats["validations"] += 1

            is_valid, error_msg = self._validate_host_internal(host_data)
            if not is_valid:
                self._stats["errors"] += 1
                return False, error_msg

            # Поиск по iid (первичный), затем по host+port (legacy fallback)
            existing_idx = None
            target_iid = host_data.get("iid")
            if target_iid and target_iid in self._hosts_by_iid:
                # Найти индекс в _hosts_data по iid
                for i, h in enumerate(self._hosts_data):
                    if h.get("iid") == target_iid:
                        existing_idx = i
                        break
            if existing_idx is None:
                existing_idx = self._find_host_index_internal(
                    host_data.get("host"), host_data.get("port")
                )

            operation = "update" if existing_idx is not None else "add"
            if persist:
                previous_hosts = _fast_clone(self._hosts_data)
                previous_raw = _fast_clone(self._hosts_data_raw)

            # После set_hosts_data плоский список может не иметь raw-зеркала.
            # Восстанавливаем его ДО изменения, иначе первая новая папка
            # вытеснит прежние хосты из hosts.json и снимков дерева.
            if not self._hosts_data_raw and self._hosts_data:
                self._hosts_data_raw = _fast_clone(self._hosts_data)

            if existing_idx is not None:
                old_data = _fast_clone(self._hosts_data[existing_idx])
                # Сохраняем iid существующей записи при update
                if "iid" not in host_data and "iid" in old_data:
                    host_data["iid"] = old_data["iid"]
                self._hosts_data[existing_idx].update(host_data)
                new_data = _fast_clone(self._hosts_data[existing_idx])
                # Обновить индекс
                self._hosts_by_iid[new_data.get("iid")] = self._hosts_data[existing_idx]
                logger.debug(f"DataStore: Updated host {host_data.get('host')}")
            else:
                # Новая запись — генерируем iid если нет
                if "iid" not in host_data or not host_data["iid"]:
                    host_data["iid"] = uuid.uuid4().hex
                self._hosts_data.append(_fast_clone(host_data))
                old_data = {}
                new_data = _fast_clone(host_data)
                self._hosts_by_iid[new_data["iid"]] = self._hosts_data[-1]
                logger.debug(f"DataStore: Added host {host_data.get('host')}")

            # Синхронизировать iid в raw-структуре
            self._sync_hosts_raw_iids()
            # Зеркалировать данные в raw-структуру (add/update) — иначе
            # hosts.json и /api/node/hosts возвращают устаревшие поля.
            self._sync_hosts_raw_data(new_data, operation)
            if persist:
                try:
                    saved = self.save_hosts_to_file()
                except Exception as e:  # noqa: BLE001 - rollback any persistence failure
                    logger.error(f"DataStore: Error persisting host update: {e}")
                    saved = False
                if not saved:
                    self._hosts_data = previous_hosts
                    self._hosts_data_raw = previous_raw
                    self._hosts_by_iid = {h["iid"]: h for h in previous_hosts}
                    return False, "Could not save host update"

            self._stats["writes"] += 1
            if new_data != old_data:
                self._host_revision_serial += 1
                self._host_revisions[new_data["iid"]] = self._host_revision_serial
            self._add_operation(
                "hosts", operation, old_data, new_data, index=existing_idx
            )

        self._notify_change("hosts", operation, new_data)

        return True, f"Host {operation}ed successfully"

    def delete_host(self, host: str, port: Optional[int] = None) -> bool:
        """
        Удалить хост по адресу и опционально порту (DEPRECATED: используйте delete_host_by_iid).

        Args:
            host: hostname или IP.
            port: Порт (None — удалить все записи с данным host).

        Returns:
            True если х найден и удалён.
        """
        with self._lock:
            indices_to_remove = []

            for i, h in enumerate(self._hosts_data):
                if h.get("host") == host:
                    if port is None or h.get("port") == port:
                        indices_to_remove.append(i)

            if not indices_to_remove:
                logger.debug(f"DataStore: Host {host} not found for deletion")
                return False

            removed = []
            for i in reversed(indices_to_remove):
                removed.append(_fast_clone(self._hosts_data[i]))
                # Удалить из индекса
                iid = self._hosts_data[i].get("iid")
                if iid and iid in self._hosts_by_iid:
                    del self._hosts_by_iid[iid]
                    self._host_revisions.pop(iid, None)
                del self._hosts_data[i]

            self._stats["writes"] += 1

            self._add_operation("hosts", "delete", removed, {})
            self._sync_hosts_raw_iids()

        self._notify_change(
            "hosts", "delete", {"host": host, "port": port, "removed": removed}
        )

        logger.debug(f"DataStore: Deleted {len(removed)} host(s) {host}")
        return True

    def delete_host_by_iid(self, iid: str, *, persist: bool = False) -> bool:
        """Удалить хост по iid (первичный метод)."""
        with self._lock:
            idx = None
            for i, h in enumerate(self._hosts_data):
                if h.get("iid") == iid:
                    idx = i
                    break
            if idx is None:
                logger.debug(f"DataStore: Host iid={iid} not found for deletion")
                return False
            if persist:
                previous_hosts = _fast_clone(self._hosts_data)
                previous_raw = _fast_clone(self._hosts_data_raw)
            removed = _fast_clone(self._hosts_data[idx])
            del self._hosts_data[idx]
            self._hosts_by_iid.pop(iid, None)
            self._sync_hosts_raw_iids()
            self._sync_hosts_raw_delete(iid)
            if persist:
                try:
                    saved = self.save_hosts_to_file()
                except Exception as error:
                    logger.error(f"DataStore: ошибка сохранения удаления хоста: {error}")
                    saved = False
                if not saved:
                    self._hosts_data = previous_hosts
                    self._hosts_data_raw = previous_raw
                    self._hosts_by_iid = {h["iid"]: h for h in previous_hosts}
                    return False
            self._host_revisions.pop(iid, None)
            self._stats["writes"] += 1
            self._add_operation("hosts", "delete", removed, {})
        self._notify_change("hosts", "delete", {"iid": iid, "removed": [removed]})
        logger.debug(f"DataStore: Deleted host iid={iid}")
        return True

    def find_host(self, host: str, port: Optional[int] = None) -> Optional[Dict]:
        """Найти хост по host и опционально port (DEPRECATED: используйте find_host_by_iid)"""
        with self._lock:
            self._stats["reads"] += 1
            for h in self._hosts_data:
                if h.get("host") == host:
                    if port is None or h.get("port") == port:
                        return _fast_clone(h)
            return None

    def find_host_by_iid(self, iid: str) -> Optional[Dict]:
        """Найти хост по iid (первичный метод поиска)."""
        with self._lock:
            self._stats["reads"] += 1
            h = self._hosts_by_iid.get(iid)
            return _fast_clone(h) if h is not None else None

    def find_task_by_iid(self, iid: str) -> Optional[Dict]:
        """Найти задачу по iid (первичный метод поиска)."""
        with self._lock:
            self._stats["reads"] += 1
            t = self._tasks_by_iid.get(iid)
            return _fast_clone(t) if t is not None else None

    def get_hosts_by_folder(self, folder_path: str) -> List[Dict]:
        """Получить все хосты в папке"""
        with self._lock:
            self._stats["reads"] += 1
            return [
                _fast_clone(h)
                for h in self._hosts_data
                if h.get("folder", "") == folder_path
            ]

    # ========== COMMANDS OPERATIONS ==========

    def get_commands_data(self) -> List[Dict]:
        """Получить независимую копию дерева задач (потокобезопасно)."""
        with self._lock:
            self._stats["reads"] += 1
            return _fast_clone(self._commands_data)

    def get_commands_count(self) -> int:
        """Получить количество элементов верхнего уровня дерева задач."""
        with self._lock:
            return len(self._commands_data)

    def get_tasks_count(self) -> int:
        """
        Подсчитать общее количество задач (Task) в иерархии команд.

        Task — это элемент с полями ``name`` и ``commands``. Категории
        (папки) не учитываются, учитываются только сами задачи на всех
        уровнях вложенности.
        """
        with self._lock:
            return self._count_tasks_recursive(self._commands_data)

    @staticmethod
    def _count_tasks_recursive(items: List[Dict]) -> int:
        """Рекурсивно посчитать задачи (Task) в структуре команд."""
        count = 0
        if not isinstance(items, list):
            return count
        for item in items:
            if not isinstance(item, dict):
                continue
            if "name" in item and "commands" in item:
                count += 1
            elif len(item) == 1:
                children = next(iter(item.values()))
                if isinstance(children, list):
                    count += DataStore._count_tasks_recursive(children)
        return count

    @property
    def commands_error(self) -> str:
        """Текст последней ошибки проверки или загрузки задач."""
        with self._lock:
            return self._commands_error

    @property
    def commands_load_error(self) -> str | None:
        """Ошибка загрузки рабочего файла задач; пока она есть, файл не перезаписывается."""
        with self._lock:
            return self._commands_load_error

    def set_commands_data(self, new_data: List[Dict]) -> bool:
        """
        Заменить дерево задач после полной проверки.

        Args:
            new_data: Новое дерево категорий и задач.

        Returns:
            True если данные прошли проверку и установлены; текст ошибки
            доступен в ``commands_error``.
        """
        from src.domain.command_schema import validate_command_tree

        with self._lock:
            try:
                validate_command_tree(new_data)
            except ValueError as error:
                self._commands_error = str(error)
                logger.error(f"DataStore: Invalid commands data: {error}")
                self._stats["errors"] += 1
                return False
            self._commands_error = ""
            self._commands_data = _fast_clone(new_data)
            self._stats["writes"] += 1

            # Перестроить индекс tasks по iid (присвоить iid новым записям)
            self._tasks_by_iid = {}
            self._assign_tasks_iids_recursive(self._commands_data)

            self._notify_change("commands", "set", new_data)
            logger.debug(f"DataStore: Commands data set ({len(new_data)} items)")
            return True

    def merge_commands_data(
        self, imported_data: List[Dict], resolution: Optional[Dict] = None
    ) -> Tuple[bool, Dict]:
        """Влить импортированные команды в существующие (режим Обновить).

        Совпадение только по iid. Знакомые задачи обновляются по resolution:
        {"name": "mine"|"theirs", "parent": "mine"|"theirs"}.
        Новые задачи добавляются в совпавшую категорию, иначе создаются
        недостающие категории. Ничего не удаляет. Задачи с ошибками формата
        пропускаются и учитываются в статистике.

        Args:
            imported_data: Дерево команд из файла импорта.
            resolution: Решение конфликтов (None = прежнее поведение).

        Returns:
            (success, stats) - статистика merge_command_trees.
        """
        from src.services.commands.command_import import merge_command_trees

        with self._lock:
            try:
                if not isinstance(imported_data, list):
                    self._stats["errors"] += 1
                    return False, {}
                merged = _fast_clone(self._commands_data)
                stats = merge_command_trees(merged, imported_data, resolution=resolution)
                self._commands_data = merged
                self._stats["writes"] += 1
                self._tasks_by_iid = {}
                self._assign_tasks_iids_recursive(self._commands_data)
                self._notify_change("commands", "merged", stats)
                logger.debug(f"DataStore: Commands merged: {stats}")
                return True, stats
            except Exception as e:
                logger.error(f"DataStore: Error merging commands data: {e}")
                self._stats["errors"] += 1
                return False, {}

    # ========== FILE OPERATIONS ==========

    def save_hosts_to_file(self, filename: str = DEFAULT_HOSTS_FILE) -> bool:
        """Сохранить хосты в файл с сохранением иерархии (с iid — персистентный идентификатор)"""
        with self._lock:
            temporary_file = None
            try:
                # Сохраняем оригинальную структуру с группами, включая iid
                data_to_save = (
                    self._hosts_data_raw if self._hosts_data_raw else self._hosts_data
                )

                directory = os.path.dirname(os.path.abspath(filename))
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=directory,
                    prefix=".hosts-",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    temporary_file = f.name
                    json.dump(
                        data_to_save, f, ensure_ascii=False, indent=4, sort_keys=False
                    )
                    f.flush()
                    os.fsync(f.fileno())
                ensure_user_owned(temporary_file)
                os.replace(temporary_file, filename)
                temporary_file = None
                logger.debug(f"DataStore: Hosts saved to {filename}")
                self._notify_change("hosts", "saved", {"file": filename})
                return True
            except Exception as e:
                logger.error(f"DataStore: Error saving hosts: {e}")
                self._stats["errors"] += 1
                return False
            finally:
                if temporary_file is not None:
                    try:
                        os.unlink(temporary_file)
                    except OSError:
                        logger.warning(
                            "DataStore: Could not remove temporary hosts file"
                        )

    def load_hosts_from_file(self, filename: str = DEFAULT_HOSTS_FILE) -> bool:
        """Загрузить хосты из файла"""
        with self._lock:
            try:
                data = self._load_hosts_from_file_internal(filename)
                raw = self._load_hosts_raw_from_file_internal(filename)
                self._hosts_data = data
                self._hosts_data_raw = raw
                self._rebuild_hosts_iid_index()
                logger.debug(f"DataStore: Hosts loaded from {filename}")
                self._notify_change("hosts", "loaded", {"file": filename})
                return True
            except Exception as e:
                logger.error(f"DataStore: Error loading hosts: {e}")
                self._stats["errors"] += 1
                return False

    def save_commands_to_file(self, filename: str = DEFAULT_COMMANDS_FILE) -> bool:
        """Сохранить команды в файл (с iid — персистентный идентификатор).

        Рабочий файл, который не удалось загрузить, не перезаписывается:
        иначе испорченный, но восстановимый файл заменился бы пустым списком.
        """
        with self._lock:
            if self._commands_load_error and _same_path(filename, DEFAULT_COMMANDS_FILE):
                self._commands_error = (
                    f"Файл задач не был загружен ({self._commands_load_error}). "
                    "Исправьте его или замените импортом: сохранение поверх него отключено."
                )
                logger.error(f"DataStore: {self._commands_error}")
                self._stats["errors"] += 1
                return False
            try:
                data_to_save = self._commands_data
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(
                        data_to_save, f, ensure_ascii=False, indent=4, sort_keys=True
                    )
                ensure_user_owned(filename)
                logger.debug(f"DataStore: Commands saved to {filename}")
                self._notify_change("commands", "saved", {"file": filename})
                return True
            except Exception as e:
                self._commands_error = f"Не удалось записать {filename}: {e}"
                logger.error(f"DataStore: Error saving commands: {e}")
                self._stats["errors"] += 1
                return False

    def load_commands_from_file(self, filename: str = DEFAULT_COMMANDS_FILE) -> bool:
        """Заменить задачи содержимым файла.

        Отсутствующий, нечитаемый или неверный по формату файл не меняет
        текущие данные; текст ошибки доступен в ``commands_error``.
        """
        with self._lock:
            try:
                data = self._load_commands_from_file_internal(filename)
            except FileNotFoundError:
                self._commands_error = f"Файл не найден: {filename}"
            except (OSError, ValueError) as error:
                self._commands_error = f"{filename}: {error}"
            else:
                self._commands_data = data
                self._commands_error = ""
                # Данные заменены целиком: прежний файл можно перезаписать.
                self._commands_load_error = None
                self._tasks_by_iid = {}
                self._assign_tasks_iids_recursive(self._commands_data)
                logger.debug(f"DataStore: Commands loaded from {filename}")
                self._notify_change("commands", "loaded", {"file": filename})
                return True
            if _same_path(filename, DEFAULT_COMMANDS_FILE) and os.path.exists(filename):
                self._commands_load_error = self._commands_error
            logger.error(f"DataStore: Error loading commands: {self._commands_error}")
            self._stats["errors"] += 1
            return False

    # ========== HISTORY & UNDO ==========

    def undo(self) -> Optional[Dict]:
        """
        Отменить последнюю операцию (если есть в истории).

        Returns:
            Словарь с описанием отменённой операции или None.
        """
        with self._lock:
            if not self._operation_history:
                return None

            operation = _unpack_history_entry(self._operation_history.pop())

            if operation["type"] == "hosts":
                if operation["action"] == "add":
                    idx = operation.get("index")
                    new_data = operation.get("new_data")
                    if idx is not None and idx < len(self._hosts_data):
                        if self._hosts_data[idx] == new_data or (
                            isinstance(new_data, dict)
                            and self._hosts_data[idx].get("host")
                            == new_data.get("host")
                        ):
                            del self._hosts_data[idx]
                        else:
                            for i, h in enumerate(self._hosts_data):
                                if (
                                    isinstance(h, dict)
                                    and isinstance(new_data, dict)
                                    and h.get("host") == new_data.get("host")
                                ):
                                    del self._hosts_data[i]
                                    break
                    elif new_data and isinstance(new_data, dict):
                        host_key = new_data.get("host")
                        self._hosts_data = [
                            h
                            for h in self._hosts_data
                            if not (isinstance(h, dict) and h.get("host") == host_key)
                        ]
                    self._hosts_data_raw = []
                elif operation["action"] == "update":
                    old_data = operation.get("old_data")
                    idx = operation.get("index")
                    if old_data and isinstance(old_data, dict):
                        host_key = old_data.get("host")
                        found = False
                        for i, h in enumerate(self._hosts_data):
                            if isinstance(h, dict) and h.get("host") == host_key:
                                self._hosts_data[i] = old_data
                                found = True
                                break
                        if (
                            not found
                            and idx is not None
                            and idx < len(self._hosts_data)
                        ):
                            self._hosts_data[idx] = old_data
                    self._hosts_data_raw = []
                elif operation["action"] == "delete":
                    removed = operation.get("old_data")
                    if isinstance(removed, list):
                        self._hosts_data.extend(removed)
                    elif removed:
                        self._hosts_data.append(removed)
                    self._hosts_data_raw = []
                elif operation["action"] == "set":
                    old_data = operation.get("old_data")
                    if old_data is not None:
                        self._hosts_data = (
                            [dict(h) for h in old_data]
                            if isinstance(old_data, list)
                            else old_data
                        )
                    self._hosts_data_raw = []

        # После любых undo — перестроить индексы iid (данные изменились)
        self._rebuild_iid_indexes()
        self._notify_change(operation["type"], "undo", operation)
        logger.debug(f"DataStore: Undone {operation['action']} on {operation['type']}")

        return operation

    def get_history(self, limit: int = 10) -> List[Dict]:
        """Получить последние ``limit`` операций из истории."""
        with self._lock:
            return [_unpack_history_entry(item) for item in list(self._operation_history)[-limit:]]

    def clear_history(self) -> None:
        """Очистить историю операций."""
        with self._lock:
            self._operation_history.clear()
            logger.debug("DataStore: History cleared")

    # ========== STATISTICS ==========

    def get_stats(self) -> Dict[str, int]:
        """Получить статистику хранилища (чтения, записи, ошибки, количество записей)."""
        with self._lock:
            return {
                **self._stats,
                "hosts_count": len(self._hosts_data),
                "commands_count": len(self._commands_data),
                "history_size": len(self._operation_history),
            }

    def reset_stats(self) -> None:
        """Сбросить счётчики статистики."""
        with self._lock:
            self._stats = {"reads": 0, "writes": 0, "validations": 0, "errors": 0}

    # ========== INTERNAL METHODS ==========

    def _load_hosts_from_file_internal(
        self, filename: str = DEFAULT_HOSTS_FILE
    ) -> List[Dict]:
        """
        Внутренняя загрузка хостов (без блокировки).

        Извлекает все устройства из вложенной структуры с группами.
        """
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Извлекаем все устройства рекурсивно
                all_hosts: list[dict] = []
                self._extract_hosts_recursive(data, all_hosts)
                self._normalize_host_thumbnail_backends(data)

                logger.debug(f"DataStore: Loaded {len(all_hosts)} hosts from file")
                return all_hosts

        except FileNotFoundError:
            logger.debug(f"DataStore: Hosts file not found: {filename}")
            return []
        except Exception as e:
            logger.error(f"DataStore: Error loading hosts from file: {e}")
            return []

    def _normalize_host_thumbnail_backends(self, data: Any) -> None:
        """Normalize persisted overrides without changing the source file."""
        hosts: list[dict] = []
        self._extract_hosts_recursive(data, hosts)
        for host in hosts:
            if "thumbnail_backend" in host:
                host["thumbnail_backend"] = normalize_thumbnail_backend(
                    host["thumbnail_backend"], allow_none=True
                )

    def _extract_hosts_recursive(self, data, hosts_list: List[Dict]):
        """Рекурсивное извлечение хостов из вложенной структуры"""
        if isinstance(data, list):
            for item in data:
                self._extract_hosts_recursive(item, hosts_list)
        elif isinstance(data, dict):
            # Проверяем это устройство (есть name и host)
            if "name" in data and "host" in data:
                hosts_list.append(data)
            else:
                # Это группа — извлекаем устройства из значений
                for value in data.values():
                    self._extract_hosts_recursive(value, hosts_list)

    def _load_commands_from_file_internal(
        self, filename: str = DEFAULT_COMMANDS_FILE
    ) -> List[Dict]:
        """Прочитать и проверить файл задач (без блокировки).

        Raises:
            FileNotFoundError: файла нет.
            OSError, ValueError: файл не читается или не соответствует формату.
        """
        from src.domain.command_schema import validate_command_tree

        with open(filename, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"ошибка JSON в строке {error.lineno}: {error.msg}"
                ) from None
        validate_command_tree(data)
        return data

    def _validate_host_internal(
        self, host_data: Dict, strict: bool = True
    ) -> Tuple[bool, str]:
        """
        Валидация данных хоста.

        Args:
            host_data: Данные для проверки
            strict: Если True, требуется name; если False, только host

        Returns:
            (is_valid, error_message)
        """
        if not isinstance(host_data, dict):
            return False, "Host data must be a dictionary"
        if "thumbnail_backend" in host_data:
            try:
                validate_thumbnail_backend(
                    host_data["thumbnail_backend"], allow_none=True
                )
            except ValueError as e:
                return False, str(e)

        # Обязательное поле: host
        if "host" not in host_data:
            return False, "Host field is required"

        # Проверка формата host (IP или hostname)
        host = host_data.get("host", "")
        if not host:
            return False, "Host cannot be empty"

        # Опционально: name (требуется в strict режиме)
        if strict and "name" not in host_data:
            return False, "Name field is required"

        # Проверка port если указан
        port = host_data.get("port")
        if port is not None:
            if not isinstance(port, int) or not (1 <= port <= 65535):
                return False, f"Invalid port: {port}"

        # Проверка MAC-адреса если указан (опционально)
        mac_address = host_data.get("mac_address")
        if mac_address:
            mac_pattern = r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$|^([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$"
            if not re.match(mac_pattern, mac_address):
                return False, f"Invalid MAC address: {mac_address}"

        return True, ""

    def _find_host_index_internal(
        self, host: str, port: Optional[int] = None
    ) -> Optional[int]:
        """Найти индекс хоста (без блокировки)"""
        for i, h in enumerate(self._hosts_data):
            if h.get("host") == host:
                if port is None or h.get("port") == port:
                    return i
        return None

    def _add_operation(
        self,
        op_type: str,
        action: str,
        old_data: Any,
        new_data: Any,
        index: Optional[int] = None,
    ):
        """Добавить операцию в историю"""
        self._operation_history.append(
            _pack_history_entry(
                {
                    "type": op_type,
                    "action": action,
                    "old_data": old_data,
                    "new_data": new_data,
                    "index": index,
                    "timestamp": time.time(),
                }
            )
        )

    def get_host_revision(self, iid: str) -> int | None:
        """Return a runtime-only commit generation, including restored updates."""
        with self._lock:
            return self._host_revisions.get(iid) if iid in self._hosts_by_iid else None

    def set_event_bus(self, event_bus: "EventBus") -> None:
        """Установить EventBus для интеграции с архитектурой"""
        self._event_bus = event_bus

    def _notify_change(self, data_type: str, action: str, data: Any):
        try:
            if data_type == "hosts":
                self.hosts_data_changed.emit({"action": action, "data": data})
            elif data_type == "commands":
                self.commands_data_changed.emit({"action": action, "data": data})

            if self._event_bus:
                event_type = EventType.DATA_CHANGED
                self._event_bus.publish_typed(
                    event_type=event_type,
                    source="DataStore",
                    data={
                        "type": data_type,
                        "action": action,
                        "data": data,
                        "timestamp": time.time(),
                    },
                )

        except Exception as e:
            logger.error(f"DataStore: Error notifying change: {e}")


# Глобальный экземпляр
datastore = DataStore()
