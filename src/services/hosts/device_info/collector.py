"""Сбор сведений об устройстве поверх SSH или TLS Node API без зависимости от Qt."""

from __future__ import annotations

import codecs
import copy
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import paramiko

from src.config import config
from src.domain.device_info import CategoryResult, DeviceInfoSnapshot, InfoStatus
from src.domain.models.device import DeviceModel
from src.logger import logger
from src.services.hosts.device_info.protocol import (
    MAX_SNAPSHOT_BYTES,
    InfoFrameParser,
    build_node_info_script,
)
from src.services.hosts.device_info.results import parse_category

CategoryCallback = Callable[[str, CategoryResult], None]
ProgressCallback = Callable[[str, int, int, str], None]


class DeviceInfoCollector:
    """Владеет I/O одного запроса; колбэки вызываются в потоке, выполняющем run."""

    def __init__(
        self,
        device: DeviceModel,
        categories: dict[str, Any],
        snapshot: DeviceInfoSnapshot,
        on_category: CategoryCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.device = copy.copy(device)
        self.categories = categories
        self.snapshot = copy.deepcopy(snapshot)
        self.on_category = on_category
        self.on_progress = on_progress
        self._cancel = threading.Event()
        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None
        self._io_lock = threading.Lock()
        self._snapshot_lock = threading.RLock()
        self._closer: threading.Thread | None = None
        self._started_at = 0.0
        self._deadline = 0.0
        self._parser: InfoFrameParser | None = None

    def snapshot_copy(self) -> DeviceInfoSnapshot:
        with self._snapshot_lock:
            return copy.deepcopy(self.snapshot)

    def abort(self) -> None:
        """Вызывающий поток только выставляет флаг; сокет закрывается отдельно."""
        self._cancel.set()
        with self._io_lock:
            if self._closer is None:
                self._closer = threading.Thread(
                    target=self._close_io, name="device-info-cancel", daemon=True
                )
                self._closer.start()

    def _close_io(self) -> None:
        channel, client = self._channel, self._client
        for resource in (channel, client):
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:  # noqa: BLE001
                    # Очистка отменённого транспорта должна завершить остальные ресурсы.
                    logger.debug(f"DeviceInfo close: {type(error).__name__}")

    def _progress(self, done: int, total: int, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(self.snapshot.request_id, done, total, message)

    def _command_ready(self, name: str, _result: object) -> None:
        assert self._parser is not None
        results = self._parser.results[name]
        if len(results) == len(self.categories[name]["commands"]):
            start = time.monotonic()
            category = parse_category(name, results, self.categories)
            with self._snapshot_lock:
                self.snapshot.metrics["parse_sec"] = (
                    self.snapshot.metrics.get("parse_sec", 0.0)
                    + time.monotonic()
                    - start
                )
                self.snapshot.categories[name] = category
            if self.on_category is not None:
                self.on_category(self.snapshot.request_id, category)
            done = len(self.snapshot.categories)
            self._progress(done, len(self.categories), f"Готово: {name}")
            if "first_result_sec" not in self.snapshot.metrics:
                with self._snapshot_lock:
                    self.snapshot.metrics["first_result_sec"] = (
                        time.monotonic() - self._started_at
                    )

    def run(self) -> DeviceInfoSnapshot:
        """Выполнить сбор и вернуть независимую копию итогового снимка."""
        self._started_at = time.monotonic()
        snapshot = self.snapshot
        self._parser = InfoFrameParser(
            self.categories, snapshot.request_id, self._command_ready
        )
        total_timeout = min(600, max(30, int(config.app.ssh.max_as_completed_timeout)))
        if snapshot.profile == "brief":
            total_timeout = min(total_timeout, 120)
        else:
            total_timeout = min(total_timeout, 180)
        self._deadline = self._started_at + total_timeout
        transport_ok = False
        try:
            if self._cancel.is_set():
                return self._finish(transport_ok)
            snapshot.status = InfoStatus.CONNECTING
            self._progress(0, len(self.categories), "Подключение…")
            if snapshot.transport == "node":
                transport_ok = self._run_node(total_timeout)
            else:
                transport_ok = self._run_ssh(total_timeout)
        except Exception as error:  # noqa: BLE001
            # Не выводим исключение с потенциальными секретами или содержимым команд.
            snapshot.diagnostic = self._safe_error(error)
            logger.debug(
                f"DeviceInfo[{snapshot.request_id}] transport={snapshot.transport} error={type(error).__name__}"
            )
        return self._finish(transport_ok)

    def _finish(self, transport_ok: bool) -> DeviceInfoSnapshot:
        """Закрыть ресурсы и привести снимок к итоговому виду без сырого вывода."""
        assert self._parser is not None
        snapshot = self.snapshot
        self._close_io()
        if self._closer is not None:
            self._closer.join()
        snapshot.categories = {
            name: parse_category(name, commands, self.categories)
            for name, commands in self._parser.results.items()
        }
        if self._parser.unsupported:
            snapshot.diagnostic = "На целевой системе отсутствуют bash/base64/timeout или другие средства ограниченного сбора"
        elif self._parser.limited:
            snapshot.diagnostic = (
                "Истекло время сбора"
                if self._parser.limit_reason == "time"
                else "Достигнут лимит объёма раздела или снимка"
            ) + "; сохранены полные фреймы до ограничения"
        elif not self._parser.complete and not snapshot.diagnostic:
            snapshot.diagnostic = "Ответ неполон: отсутствует часть команд, манифест или завершающий маркер"
        snapshot.finish(
            cancelled=self._cancel.is_set(),
            transport_ok=transport_ok and self._parser.complete,
        )
        with self._snapshot_lock:
            snapshot.metrics["total_sec"] = time.monotonic() - self._started_at
            snapshot.metrics["wire_bytes"] = self._parser.bytes_received
            snapshot.metrics["channels"] = 1 if snapshot.transport == "ssh" else 0
        # Снимок не удерживает сырые passwd, историю и stderr в кэше/GUI.
        snapshot.categories = {
            name: replace(
                cat,
                commands=tuple(
                    replace(cmd, stdout="", stderr="") for cmd in cat.commands
                ),
            )
            for name, cat in snapshot.categories.items()
        }
        logger.debug(
            f"DeviceInfo[{snapshot.request_id}] transport={snapshot.transport} status={snapshot.status} "
            f"categories={len(snapshot.categories)}/{len(self.categories)} metrics={snapshot.metrics}"
        )
        return copy.deepcopy(snapshot)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, paramiko.AuthenticationException):
            return "SSH: отказ авторизации. Проверьте связку учетных данных на владельце устройства."
        if isinstance(error, paramiko.BadHostKeyException):
            return "SSH: ключ сервера изменился. Проверьте known_hosts."
        if isinstance(error, TimeoutError):
            return "Истекло время сбора; полученные разделы сохранены."
        if isinstance(error, paramiko.SSHException):
            return (
                "SSH: подключение отклонено, проверьте ключ сервера и доступность SSH."
            )
        return f"Сбой сбора ({type(error).__name__}); проверьте доступность владельца и хоста."

    def _run_ssh(self, timeout: int) -> bool:
        # Пакет src.services импортирует src.workers только внутри функций.
        from src.workers.command.executor_base import create_ssh_client, get_credentials
        from src.workers.command.sudo_utils import should_use_sudo

        assert self._parser is not None
        client = paramiko.SSHClient()
        self._client = client
        if self._cancel.is_set():
            return False
        started = time.monotonic()
        create_ssh_client(self.device, client=client)
        with self._snapshot_lock:
            self.snapshot.metrics["connect_sec"] = time.monotonic() - started
        if self._cancel.is_set():
            return False
        creds = get_credentials(self.device)
        use_sudo = should_use_sudo(self.device) and creds.username != "root"
        script = build_node_info_script(
            self.categories,
            request_id=self.snapshot.request_id,
            command_timeout=config.app.ssh.command_timeout,
            total_timeout=max(1, int(self._deadline - time.monotonic())),
            local_sudo=use_sudo,
        )
        stdin, stdout, _stderr = client.exec_command(script, timeout=min(10, timeout))
        channel = stdout.channel
        self._channel = channel
        if self._cancel.is_set():
            return False
        stdin.write(((creds.password or "") if use_sudo else "") + "\n")
        stdin.flush()
        channel.shutdown_write()
        self.snapshot.status = InfoStatus.COLLECTING
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        stderr_size = 0
        while True:
            if self._cancel.is_set():
                return False
            if time.monotonic() >= self._deadline:
                raise TimeoutError()
            got_data = False
            # По одной порции каждого потока: непрерывный stderr не вытесняет stdout.
            if channel.recv_ready():
                self._parser.feed(decoder.decode(channel.recv(32768)))
                got_data = True
            if channel.recv_stderr_ready():
                stderr_size += len(channel.recv_stderr(32768))
                got_data = True
                if stderr_size > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Превышен лимит stderr")
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                self._parser.feed(decoder.decode(b"", final=True))
                return channel.recv_exit_status() == 0
            if not got_data:
                self._cancel.wait(0.02)

    def _run_node(self, timeout: int) -> bool:
        from src.di import get_container
        from src.services.remote.node_service import NodeService

        assert self._parser is not None
        niid = getattr(self.device, "node_iid", None)
        service = get_container().resolve_optional(NodeService)
        client = service.get_client(niid) if service is not None else None
        if client is None:
            self.snapshot.diagnostic = (
                "Нода недоступна или скрыта. Включите связь с нодой-владельцем."
            )
            return False
        if self._cancel.is_set():
            return False
        streamed = False
        parser_error = False

        def abort_check() -> bool:
            return (
                self._cancel.is_set()
                or parser_error
                or time.monotonic() >= self._deadline
            )

        def on_event(event: dict[str, Any]) -> None:
            nonlocal streamed, parser_error
            assert self._parser is not None
            if event.get("device_iid") != self.device.iid:
                return
            if event.get("type") == "progress":
                streamed = True
                try:
                    self._parser.feed(str(event.get("chunk", "")))
                except ValueError:
                    parser_error = True

        script = build_node_info_script(
            self.categories,
            request_id=self.snapshot.request_id,
            command_timeout=config.app.ssh.command_timeout,
            total_timeout=timeout,
        )
        result = client.execute_commands(
            [{"type": "ssh", "text": script}],
            [self.device.iid],
            timeout=timeout,
            abort_check=abort_check,
            on_event=on_event,
        )
        ok, output = result.get(self.device.iid, (False, ""))
        # Итоговый output авторитетен при пропуске progress в истории старой ноды.
        final_parser = InfoFrameParser(self.categories, self.snapshot.request_id)
        try:
            final_parser.feed(output)
            if sum(map(len, final_parser.results.values())) >= sum(
                map(len, self._parser.results.values())
            ):
                self._parser = final_parser
        except ValueError:
            parser_error = True
        if not ok:
            low = output.lower()
            if "отменено до отправки" in low:
                self.snapshot.diagnostic = "Отменено до отправки задания ноде"
            elif "отмена запрошена" in low:
                self.snapshot.diagnostic = "Нода приняла запрос отмены; завершение удалённых команд не подтверждено"
            elif "отменено" in low:
                self.snapshot.diagnostic = "Нода сообщила об отмене задания"
            elif self._cancel.is_set() or time.monotonic() >= self._deadline:
                self.snapshot.diagnostic = (
                    "Подтверждение отмены нодой не получено; исход задания неизвестен"
                )
            elif any(
                word in low
                for word in ("access denied", "авторизац", "довер", "401", "403")
            ):
                self.snapshot.diagnostic = "Нода: отказ доступа или авторизации"
            elif self._parser.results:
                self.snapshot.diagnostic = (
                    "Задание ноды завершилось неуспешно; сохранён частичный ответ"
                )
            else:
                self.snapshot.diagnostic = "Нода не выполнила задание: проверьте связь с нодой, доступность хоста и SSH на владельце"
        if time.monotonic() >= self._deadline and not self._cancel.is_set():
            self.snapshot.diagnostic = "Истекло время ожидания задания ноды; исход незавершённых команд неизвестен"
        return ok and not parser_error and time.monotonic() < self._deadline
