"""Временные файлы веб-задач с изоляцией владельцев и очисткой после выполнения."""

from __future__ import annotations

import ntpath
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.utils.filesystem.sftp_files import validate_name

MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
UPLOAD_TTL = 3600


@dataclass
class _Upload:
    owner: str
    task: str
    key: str
    path: Path
    touched: float
    size: int = 0
    ready: bool = False
    claimed: bool = False


class TaskUploadStore:
    """Файлы хранятся в системном tmp, клиент получает только случайный идентификатор."""

    def __init__(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="dno-task-", dir="/tmp" if Path("/tmp").is_dir() else None))
        self._lock = threading.RLock()
        self._entries: dict[str, _Upload] = {}
        self._closed = False

    def begin(self, owner: str, task: str, key: str, filename: str) -> str:
        """Зарезервировать отдельный каталог с исходным именем файла."""
        if (not filename or filename in (".", "..") or ntpath.basename(filename) != filename
                or any(c in filename for c in '/\\:\0') or len(filename.encode("utf-8")) > 240):
            raise ValueError("Некорректное имя файла")
        validate_name(filename, local=True)
        with self._lock:
            if self._closed:
                raise ValueError("Веб-сервер остановлен")
            self.sweep()
            if len(self._entries) >= 128:
                raise ValueError("Слишком много временных файлов; повторите позже")
            token = uuid.uuid4().hex
            folder = self._root / token
            folder.mkdir(mode=0o700)
            path = folder / filename
            try:
                path.touch(mode=0o600, exist_ok=False)
            except OSError:
                folder.rmdir()
                raise
            self._entries[token] = _Upload(owner, task, key, path, time.monotonic())
            return token

    def write(self, token: str, chunk: bytes) -> None:
        """Записать порцию с проверкой лимитов до выделения места."""
        with self._lock:
            entry = self._entries[token]
            if entry.ready or entry.claimed:
                raise ValueError("Загрузка уже завершена")
            if entry.size + len(chunk) > MAX_UPLOAD_BYTES:
                raise ValueError("Максимальный размер файла — 256 МиБ")
            if sum(e.size for e in self._entries.values()) + len(chunk) > MAX_TOTAL_BYTES:
                raise ValueError("Временное хранилище заполнено")
            with entry.path.open("ab") as stream:
                stream.write(chunk)
            entry.size += len(chunk)
            entry.touched = time.monotonic()

    def complete(self, token: str) -> None:
        """Разрешить использование только полностью принятого файла."""
        with self._lock:
            self._entries[token].ready = True

    def is_ready(self, token: str) -> bool:
        """Проверить, завершён ли приём файла."""
        with self._lock:
            entry = self._entries.get(token)
            return entry is not None and entry.ready

    def claim(self, owner: str, task: str, labels: list, tokens: dict) -> dict[str, str]:
        """Атомарно передать файлы единственному заданию того же владельца."""
        required = {f"param{i + 1}" for i, label in enumerate(labels) if str(label).startswith("file:")}
        if set(tokens) != required or any(not isinstance(t, str) for t in tokens.values()):
            raise ValueError("Выберите и загрузите файл для каждого файлового параметра")
        with self._lock:
            self.sweep()
            for key, token in tokens.items():
                entry = self._entries.get(token)
                if (entry is None or not entry.ready or entry.claimed
                        or (entry.owner, entry.task, entry.key) != (owner, task, key)):
                    raise ValueError("Файл недоступен или срок загрузки истёк; выберите его снова")
            paths = {}
            for key, token in tokens.items():
                self._entries[token].claimed = True
                paths[key] = str(self._entries[token].path)
            return paths

    def discard(self, token: str, owner: str | None = None) -> None:
        """Удалить файл после работы; клиенту запрещено удалять занятый файл."""
        with self._lock:
            entry = self._entries.get(token)
            if entry is None or (owner is not None and (entry.owner != owner or entry.claimed)):
                return
            try:
                shutil.rmtree(entry.path.parent)
            except OSError:
                # Антивирус или файловая система могут временно удерживать файл.
                entry.claimed = False
                entry.touched = time.monotonic() - UPLOAD_TTL
                return
            del self._entries[token]
            if self._closed and not self._entries:
                self._root.rmdir()

    def sweep(self) -> None:
        """Очистить брошенные загрузки, не затрагивая выполняющиеся задачи."""
        with self._lock:
            for token, entry in list(self._entries.items()):
                if not entry.claimed and time.monotonic() - entry.touched > UPLOAD_TTL:
                    self.discard(token)

    def close(self) -> None:
        """Удалить незанятые файлы при остановке; активные удалит завершение задач."""
        with self._lock:
            self._closed = True
            for token, entry in list(self._entries.items()):
                if not entry.claimed:
                    self.discard(token)
            if not self._entries and self._root.exists():
                self._root.rmdir()
