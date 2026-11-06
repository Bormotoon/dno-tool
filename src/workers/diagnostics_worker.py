"""Параллельная диагностика через аренду общего пула.

Координатор QThread держит ограниченное окно задач и выполняет drain перед
finished. Проверки хоста используют одно SSH-соединение; сетевые ресурсы
принадлежат запуску и закрываются при отмене. Типизированные результаты
содержат идентификатор запуска, состояние, причину и время проверки.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from PySide6.QtCore import Signal

from src.config import config
from src.config.settings import DEFAULT_VNC_SERVER
from src.domain.models.device import DeviceModel
from src.logger import logger
from src.services.hosts.diagnostics_checks import (
    ACTIVE_MAP,
    check_rfb_banner,
    run_ssh_cmd,
    tcp_probe,
    vnc_auth_check,
)
from src.services.hosts.diagnostics_state import (
    CHECKS,
    CheckResult,
    CheckState,
    OperationControl,
)
from src.workers.application_pools import get_application_pool
from src.workers.command.executor_base import get_credentials

from .base import BaseWorker

# Иконки результатов — строковые ключи в ICONS (см. src.config.settings.ICONS).
ICON_OK = "result_success"
ICON_ERR = "result_failure"
ICON_WARN = "result_warning"
# Используются ui-слоем (diagnostics.py): pending-иконка при старте,
# abort-иконка при прерывании.
ICON_ABORT = "result_cancelled"
ICON_PENDING = "result_pending"

# Способ SSH-авторизации: код → текстовая метка для лога/tooltip.
# Иконка ячейки ssh.auth определяется результатом подключения (OK/ERR),
# а не способом авторизации — поэтому здесь только текстовая метка.
_AUTH_METHOD_LABEL = {
    "key": "ключ",
    "password": "пароль",
    "unknown": "способ не уточнён",
}


class DiagnosticsWorker(BaseWorker):
    """Параллельный воркер диагностики SSH/Veyon/VNC на хостах.

    Сигналы:
      check_result(device, check_key, icon, text, detail) — результат одной проверки.
      host_log(host, component, message)                   — короткое сообщение в общий лог.
      error(message)                                       — критическая ошибка (исключение future).
      started()                                            — начало выполнения.
      finished()                                           — унаследован от BaseWorker.
    """

    result_ready = Signal(object)
    message_ready = Signal(str, str, str, str)
    error_ready = Signal(str, str)
    progress = Signal(str, int, int, int)
    check_result = Signal(object, str, str, str, str)
    host_log = Signal(str, str, str)
    error = Signal(str)
    started = Signal()

    def __init__(
        self,
        devices: List[DeviceModel],
        components: List[str],
        vnc_password: str = "",
        veyon_server_port: int = 11100,
        vnc_port: int = 5900,
        ssh_port: int = 22,
        vnc_server: str = DEFAULT_VNC_SERVER,
        parent=None,
    ) -> None:
        super().__init__()
        self.devices = list(devices)
        # Только "vnc" (оба бекенда x11vnc/krfb в одной группе). Порт ОДИН
        # на оба бекенда (vnc_port, ключ конфига veyon.vnc_port), пароль
        # тоже один (vnc_password, ключ конфига veyon.vnc_password).
        self.components = [c for c in components if c in ("ssh", "veyon", "vnc")]
        self.vnc_password = vnc_password or ""
        self.veyon_server_port = veyon_server_port or 11100
        self.vnc_port = vnc_port or 5900
        self.ssh_port = ssh_port or 22
        self.vnc_server = vnc_server or DEFAULT_VNC_SERVER
        self.run_id = uuid4().hex
        self.control = OperationControl()
        self._local_abort = self.control.cancelled
        self._lease = None
        self._lease_lock = threading.RLock()
        self._results_lock = threading.RLock()
        self._results = {}
        self._timing = threading.local()
        self.failed = False
        self._veyon_service: Optional[object] = None

    # ------------------------------------------------------------ public API

    def execute(self) -> None:
        """Скользящее окно задач с обязательным drain своей аренды."""
        pending = {}
        remaining = iter(self.devices)
        exhausted = False
        completed = 0
        self.started.emit()
        try:
            with self._lease_lock:
                if self._local_abort.is_set():
                    return
                self._lease = get_application_pool("services").lease(source="application")
            window = max(1, min(16, self._lease._max_workers))
            while pending or not exhausted:
                while (
                    not exhausted
                    and not self._local_abort.is_set()
                    and len(pending) < window
                ):
                    device = next(remaining, None)
                    if device is None:
                        exhausted = True
                        break
                    try:
                        with self._lease_lock:
                            if self._local_abort.is_set():
                                break
                            future = self._lease.submit(
                                self._run_owned,
                                device,
                                metadata={
                                    "host": device.host,
                                    "type": "diagnostics",
                                    "batch_id": self.run_id,
                                },
                            )
                        pending[future] = device
                    except RuntimeError:
                        self.failed = True
                        self._complete_missing(
                            device,
                            CheckState.UNAVAILABLE,
                            "Пул перегружен или остановлен; повторите проверку",
                        )
                        completed += 1
                active = sum(future.running() for future in pending)
                self.progress.emit(
                    self.run_id,
                    completed,
                    active,
                    max(0, len(self.devices) - completed - active),
                )
                if self._local_abort.is_set():
                    exhausted = True
                if not pending:
                    continue
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    device = pending.pop(future)
                    if not future.cancelled():
                        try:
                            future.result()
                        except Exception:
                            self.failed = True
                            self._complete_missing(
                                device,
                                CheckState.UNAVAILABLE,
                                "Ошибка выполнения проверки; повторите запуск",
                            )
                    completed += 1
        except Exception as error:
            self.failed = True
            self.error_ready.emit(self.run_id, f"Не удалось запустить диагностику: {type(error).__name__}")
        finally:
            try:
                if self._lease is not None:
                    self._lease.shutdown(wait=True, cancel_futures=True)
            finally:
                state = (
                    CheckState.CANCELLED
                    if self._local_abort.is_set()
                    else CheckState.UNAVAILABLE
                )
                reason = (
                    "Отменено пользователем"
                    if self._local_abort.is_set()
                    else "Проверка не выполнена из-за ошибки запуска"
                )
                for device in self.devices:
                    self._complete_missing(device, state, reason)
                self.progress.emit(self.run_id, completed, 0, 0)
                self.finished.emit()

    def abort(self) -> None:
        """Отменить свою очередь и закрыть активные сетевые ресурсы."""
        self._abort_event.set()
        self.control.abort()
        with self._lease_lock:
            if self._lease is not None:
                self._lease.shutdown(wait=False, cancel_futures=True)

    def _complete_missing(self, device, state: CheckState, reason: str) -> None:
        for component in self.components:
            for key in CHECKS[component]:
                identity = (getattr(device, "iid", None) or device.host, key)
                with self._results_lock:
                    previous = self._results.get(identity)
                if previous is None or previous.state in (
                    CheckState.PENDING,
                    CheckState.RUNNING,
                ):
                    self._emit(
                        device,
                        key,
                        "result_cancelled"
                        if state == CheckState.CANCELLED
                        else "result_skip",
                        state.value,
                        reason,
                        state,
                    )

    def _run_owned(self, device) -> None:
        if self._local_abort.is_set():
            return
        self._timing.services = None
        try:
            self._run_for_device(device)
        finally:
            self._complete_missing(
                device,
                CheckState.CANCELLED
                if self._local_abort.is_set()
                else CheckState.UNAVAILABLE,
                "Отменено"
                if self._local_abort.is_set()
                else "Зависимый этап недоступен",
            )

    def _perform(self, device, keys, operation) -> None:
        if self._local_abort.is_set():
            return
        self._timing.start = time.monotonic()
        for key in keys:
            self._emit(
                device,
                key,
                "result_executing",
                "Выполняется",
                "Проверка выполняется",
                CheckState.RUNNING,
            )
        operation()

    def _port_ok(self, device, key: str) -> bool:
        with self._results_lock:
            result = self._results.get(
                (getattr(device, "iid", None) or device.host, key)
            )
        return result is not None and result.state == CheckState.SUCCESS

    # ------------------------------------------------------------ per-host

    def _run_for_device(self, device: DeviceModel) -> None:
        """Независимые сетевые проверки и один SSH-сеанс на хост."""
        host = device.host
        if "ssh" in self.components:
            self._perform(
                device,
                ("ssh.port", "ssh.ping"),
                lambda: self._check_ssh_port(device, host),
            )
        if "veyon" in self.components:
            self._perform(
                device,
                ("veyon.server_port",),
                lambda: self._check_veyon_server_port(device, host),
            )
            if self._port_ok(device, "veyon.server_port"):
                self._perform(
                    device,
                    ("veyon.handshake",),
                    lambda: self._check_veyon_handshake(device, host),
                )
        if "vnc" in self.components:
            self._perform(
                device, ("vnc.port",), lambda: self._check_vnc_port(device, host)
            )
            if self._port_ok(device, "vnc.port"):
                self._perform(
                    device, ("vnc.rfb",), lambda: self._check_vnc_rfb(device, host)
                )
                if self._port_ok(device, "vnc.rfb"):
                    self._perform(
                        device,
                        ("vnc.auth",),
                        lambda: self._check_vnc_auth(device, host),
                    )
        if self._local_abort.is_set():
            return
        if "ssh" in self.components and not self._port_ok(device, "ssh.port"):
            self._complete_missing(
                device,
                CheckState.UNAVAILABLE,
                "Не удалось проверить: SSH-порт недоступен",
            )
            return
        ssh = self._open_ssh(device, host, emit_auth="ssh" in self.components)
        if ssh is None:
            self._complete_missing(
                device,
                CheckState.UNAVAILABLE,
                "Не удалось проверить: нет SSH-соединения; проверьте учётную запись, ключ и доступность порта",
            )
            return
        try:
            if "ssh" in self.components:
                self._perform(
                    device,
                    ("ssh.echo",),
                    lambda: self._check_ssh_echo(device, host, ssh),
                )
                self._perform(
                    device,
                    ("ssh.sudo",),
                    lambda: self._check_ssh_sudo(device, host, ssh),
                )
            if "veyon" in self.components:
                self._perform(
                    device,
                    ("veyon.service", "veyon.autostart"),
                    lambda: self._service(
                        device, ssh, "veyon", "veyon.service", "veyon.autostart"
                    ),
                )
            if "vnc" in self.components:
                self._perform(
                    device,
                    ("vnc.backend",),
                    lambda: self._check_vnc_backend(device, host, ssh),
                )
                if self._vnc_backend_for_device(device) == "krfb":
                    self._perform(
                        device,
                        ("vnc.activity", "vnc.autostart"),
                        lambda: self._check_krfb_autostart(device, host, ssh),
                    )
                else:
                    unit = (
                        "kmsvnc"
                        if self._vnc_backend_for_device(device) == "kmsvnc"
                        else "x11vnc"
                    )
                    self._perform(
                        device,
                        ("vnc.activity", "vnc.autostart"),
                        lambda: self._service(
                            device, ssh, unit, "vnc.activity", "vnc.autostart"
                        ),
                    )
        finally:
            self.control.remove(ssh.close)
            ssh.close()

    def _emit(
        self,
        device,
        check_key: str,
        icon: str,
        text: str,
        detail: str,
        state: CheckState | None = None,
    ) -> None:
        """Состояние хранится независимо от оформления и привязано к запуску."""
        if state is None:
            state = {
                ICON_ERR: CheckState.ERROR,
                ICON_WARN: CheckState.WARNING,
                "result_skip": CheckState.SKIPPED,
                "result_cancelled": CheckState.CANCELLED,
            }.get(icon, CheckState.SUCCESS)
        if self._local_abort.is_set():
            if state == CheckState.RUNNING:
                return
            state, icon, text, detail = (
                CheckState.CANCELLED,
                "result_cancelled",
                "Отменено",
                "Отменено пользователем",
            )
        result = CheckResult(
            self.run_id,
            device,
            check_key,
            state,
            icon,
            text,
            detail,
            max(0, time.monotonic() - getattr(self._timing, "start", time.monotonic())),
            time.time(),
        )
        with self._results_lock:
            self._results[(getattr(device, "iid", None) or device.host, check_key)] = (
                result
            )
        self.result_ready.emit(result)
        self.check_result.emit(device, check_key, icon, text, detail)

    def _service(
        self, device, ssh, service: str, activity_key: str, autostart_key: str
    ) -> None:
        snapshot = getattr(self._timing, "services", None)
        if snapshot is None:
            code, out = run_ssh_cmd(
                ssh,
                'for s in veyon x11vnc kmsvnc; do a=$(systemctl is-active $s); ar=$?; e=$(systemctl is-enabled $s); er=$?; printf \'DNO|%s|active|%s|%s\\nDNO|%s|enabled|%s|%s\\n\' "$s" "$ar" "$a" "$s" "$er" "$e"; done',
            )
            records = {}
            for line in out.splitlines():
                parts = line.split("|")
                if len(parts) == 5 and parts[0] == "DNO":
                    records[(parts[1], parts[2])] = (parts[3], parts[4])
            snapshot = (code, records)
            self._timing.services = snapshot
        code, records = snapshot
        for key, field, expected in (
            (activity_key, "active", "active"),
            (autostart_key, "enabled", "enabled"),
        ):
            status = records.get((service, field))
            if code != 0 or not status:
                self._emit(
                    device,
                    key,
                    "result_skip",
                    "Не удалось проверить",
                    "systemctl: ошибка выполнения или доступа",
                    CheckState.UNAVAILABLE,
                )
            elif status == ("0", expected):
                self._emit(
                    device,
                    key,
                    ICON_OK,
                    "Активен" if field == "active" else "Включён",
                    f"{service}: {field}={expected}",
                )
            elif status[1] in (
                "inactive",
                "disabled",
                "static",
                "indirect",
                "masked",
                "not-found",
                "failed",
                "enabled-runtime",
            ):
                self._emit(
                    device,
                    key,
                    ICON_ERR if status[1] == "failed" else ICON_WARN,
                    ACTIVE_MAP.get(status[1], status[1]),
                    f"{service}: {field}={status[1]}; проверьте настройку службы",
                )
            else:
                self._emit(
                    device,
                    key,
                    "result_skip",
                    "Неизвестно",
                    f"{service}: {field}: неподтверждённый ответ",
                    CheckState.UNAVAILABLE,
                )

    def _open_ssh(self, device: DeviceModel, host: str, emit_auth: bool = True):
        """Открыть SSH-соединение. Возвращает SSHClient или None.

        emit_auth: если True — эмитит ssh.auth при успехе/ошибке (когда компонент ssh выбран).
        """
        import paramiko

        self._timing.start = time.monotonic()
        if emit_auth:
            self._emit(
                device,
                "ssh.auth",
                "result_executing",
                "Подключение",
                "Подключение SSH",
                CheckState.RUNNING,
            )
        port_ssh = device.port or self.ssh_port
        try:
            creds = get_credentials(device)
            ssh = paramiko.SSHClient()
            self.control.add(ssh.close)
            timer = threading.Timer(20.0, ssh.close)
            timer.daemon = True
            timer.start()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            t0 = time.monotonic()
            ssh.connect(
                hostname=host,
                username=creds.username,
                password=creds.password,
                pkey=creds.private_key,
                port=port_ssh,
                timeout=config.app.ssh.connect_timeout,
                banner_timeout=config.app.ssh.connect_timeout,
                auth_timeout=config.app.ssh.ssh_connect_timeout,
            )
            timer.cancel()
            latency = (time.monotonic() - t0) * 1000.0
            method_code = self._detect_auth_method(ssh, creds)
            method_label = _AUTH_METHOD_LABEL.get(
                method_code, _AUTH_METHOD_LABEL["unknown"]
            )
            if emit_auth:
                auth_icon = {
                    "key": "key_exists",
                    "password": "veyon_login",
                    "unknown": ICON_OK,
                }.get(method_code, ICON_OK)
                self._emit(
                    device,
                    "ssh.auth",
                    auth_icon,
                    method_label.capitalize(),
                    f"SSH-подключение успешно: {creds.username}@{host}:{port_ssh}, "
                    f"авторизация: {method_label}, задержка {latency:.0f} мс",
                )
            self.message_ready.emit(
                self.run_id, host, "ssh", f"подключено ({method_label}, {latency:.0f} мс)"
            )
            return ssh
        except Exception as error:
            if "timer" in locals():
                timer.cancel()
            if "ssh" in locals():
                self.control.remove(ssh.close)
                ssh.close()
            import socket

            if isinstance(error, paramiko.AuthenticationException):
                reason = "Отказ авторизации"
            elif isinstance(error, (TimeoutError, socket.timeout)):
                reason = "Таймаут SSH"
            elif isinstance(error, paramiko.ssh_exception.NoValidConnectionsError):
                reason = "SSH-порт недоступен"
            elif isinstance(error, socket.gaierror):
                reason = "Ошибка разрешения имени"
            else:
                reason = "Ошибка протокола SSH"
            if emit_auth:
                self._emit(
                    device,
                    "ssh.auth",
                    ICON_ERR,
                    reason,
                    f"{reason}: {host}:{port_ssh} ({type(error).__name__})",
                )
            self.message_ready.emit(self.run_id, host, "ssh", reason)
            return None

    @staticmethod
    def _detect_auth_method(ssh, creds) -> str:
        """Определить способ SSH-авторизации.

        Читает публичный атрибут paramiko ``Transport.auth_handler.auth_method``
        (значения: "publickey", "password", "keyboard-interactive") — точно
        отражает сработавший метод. Если атрибут недоступен — fallback по creds.

        Возвращает код: "key" (ключ), "password" (пароль), "unknown" (не уточнено).
        """
        # 1. Публичное API paramiko (точно)
        try:
            transport = ssh.get_transport() if hasattr(ssh, "get_transport") else None
            handler = (
                getattr(transport, "auth_handler", None)
                if transport is not None
                else None
            )
            raw = getattr(handler, "auth_method", None) if handler is not None else None
            mapping = {
                "publickey": "key",
                "password": "password",
                "keyboard-interactive": "password",
            }
            if raw and raw in mapping:
                return mapping[raw]
        except Exception:
            pass
        # 2. Fallback по creds (логически)
        has_key = creds.private_key is not None
        has_pwd = bool(getattr(creds, "password", None))
        if has_key and has_pwd:
            return "unknown"
        if has_key:
            return "key"
        if has_pwd:
            return "password"
        return "unknown"

    # --- SSH checks ---

    def _check_ssh_port(self, device: DeviceModel, host: str) -> None:
        """Один TCP-ping — результат для двух столбцов: Пинг (задержка) и Порт (открыт/закрыт)."""
        port = device.port or self.ssh_port
        ok, latency, reason = tcp_probe(host, port, timeout=2.0, control=self.control)
        if ok:
            self._emit(
                device,
                "ssh.ping",
                ICON_OK,
                f"{latency:.0f} мс",
                f"Задержка {latency:.0f} мс",
            )
            self._emit(
                device,
                "ssh.port",
                ICON_OK,
                "Открыт",
                f"SSH-порт {port} открыт",
            )
        else:
            self._emit(
                device,
                "ssh.ping",
                ICON_ERR,
                reason,
                f"SSH-порт недоступен: {reason}",
            )
            self._emit(
                device,
                "ssh.port",
                ICON_ERR,
                reason,
                f"SSH-порт {port} недоступен: {reason}",
            )

    def _check_ssh_echo(self, device: DeviceModel, host: str, ssh) -> None:
        code, out = run_ssh_cmd(ssh, "echo OK", timeout=10)
        if code == 0 and out == "OK":
            self._emit(
                device,
                "ssh.echo",
                ICON_OK,
                "OK",
                f"Команда `echo OK` выполнена успешно (exit=0, stdout={out!r})",
            )
        else:
            self._emit(
                device,
                "ssh.echo",
                ICON_ERR,
                f"Ошибка (код {code})",
                f"Команда `echo OK`: exit={code}, stdout={out!r}",
            )

    def _check_ssh_sudo(self, device: DeviceModel, host: str, ssh) -> None:
        code, out = run_ssh_cmd(ssh, "id -u", timeout=10)
        if code != 0:
            self._emit(
                device,
                "ssh.sudo",
                "result_skip",
                "Не удалось проверить",
                f"id -u: код {code}; {out}",
                CheckState.UNAVAILABLE,
            )
            return
        if code == 0 and out.strip() == "0":
            self._emit(
                device, "ssh.sudo", ICON_OK, "Root", "Пользователь — root (id -u == 0)"
            )
            return
        # Проверка sudo без пароля (NOPASSWD)
        if self._local_abort.is_set():
            return
        code2, out2 = run_ssh_cmd(ssh, "sudo -n true", timeout=10)
        if code2 == 0:
            self._emit(
                device,
                "ssh.sudo",
                ICON_OK,
                "NOPASSWD",
                "Пользователь не root, но sudo без пароля работает",
            )
            return
        # Проверка sudo с паролем через sudo -S (пароль из связки учётных данных)
        from src.workers.command.executor_base import get_credentials
        from src.workers.command.sudo_utils import should_use_sudo

        creds = get_credentials(device)
        if self._local_abort.is_set():
            return
        if creds.password:
            # sudo -S -p '' — пароль из stdin, пустой prompt (не путает вывод)
            code3, out3 = run_ssh_cmd(
                ssh,
                "sudo -S -p '' true",
                timeout=10,
                stdin_data=creds.password + "\n",
            )
            if code3 == 0:
                hint = (
                    "Sudo с паролем работает. Включите «Использовать sudo» в настройках "
                    "устройства или глобально для автоматической подачи пароля."
                )
                if should_use_sudo(device):
                    hint = "Sudo с паролем работает. Пароль будет подан автоматически."
                self._emit(device, "ssh.sudo", ICON_WARN, "Пароль", hint)
                return
        # sudo не доступно или пароль не подходит
        err = f"код {code3}: {out3}" if creds.password else f"код {code2}: {out2}"
        self._emit(
            device,
            "ssh.sudo",
            ICON_ERR,
            "Нет sudo",
            f"Пользователь не root и sudo недоступно: {err}",
        )

    # --- Veyon checks ---

    def _check_veyon_server_port(self, device: DeviceModel, host: str) -> None:
        port = self.veyon_server_port
        ok, latency, reason = tcp_probe(host, port, timeout=2.0, control=self.control)
        if ok:
            self._emit(
                device,
                "veyon.server_port",
                ICON_OK,
                f"Открыт ({latency:.0f} мс)",
                f"Veyon-порт {host}:{port} — открыт, задержка {latency:.0f} мс",
            )
        else:
            self._emit(
                device,
                "veyon.server_port",
                ICON_ERR,
                reason,
                f"Veyon-порт {host}:{port} — {reason}",
            )

    def _check_veyon_handshake(self, device: DeviceModel, host: str) -> None:
        """Использовать общий handshake, сохраняя владение отменой и дедлайн."""
        from src.services.remote.veyon_rfb_bridge import VeyonRFBHandshake

        handshake = None
        timer = None
        try:
            service = self._get_veyon_service()
            key_file = (
                service._find_key_file()
                if service and service.rfb_bridge_enabled
                else None
            )
            if not key_file:
                self._emit(
                    device,
                    "veyon.handshake",
                    "result_skip",
                    "Нет локального ключа",
                    "Настройте ключ Veyon и включите RFB bridge",
                    CheckState.UNAVAILABLE,
                )
                return
            path = Path(key_file)
            handshake = VeyonRFBHandshake(
                host=host,
                port=self.veyon_server_port,
                key_data=path.read_text(),
                key_name=path.parent.name,
                connect_timeout=5,
                access_control_timeout=60,
            )
            self.control.add(handshake.cancel)
            timer = threading.Timer(70, handshake.cancel)
            timer.daemon = True
            timer.start()
            if self._local_abort.is_set():
                return
            sock, _ = handshake.connect()
            sock.close()
            self._emit(
                device,
                "veyon.handshake",
                ICON_OK,
                "OK",
                "Veyon: авторизация и access-control пройдены",
            )
        except Exception as error:
            self._emit(
                device,
                "veyon.handshake",
                ICON_ERR,
                "Ошибка handshake",
                f"Veyon: {type(error).__name__}; проверьте ключ, права доступа и подтверждение на хосте",
            )
        finally:
            if timer:
                timer.cancel()
            if handshake:
                self.control.remove(handshake.cancel)
                handshake.cancel()

    def _get_veyon_service(self):
        """Лениво получить VeyonService из DI-контейнера."""
        if self._veyon_service is not None:
            return self._veyon_service
        try:
            from src.di import get_container
            from src.services import VeyonService

            container = get_container()
            svc = container.resolve(VeyonService)
            self._veyon_service = svc
            return svc
        except Exception as e:
            logger.error(f"DiagnosticsWorker: cannot resolve VeyonService: {e}")
            return None

    # --- VNC checks ---

    def _vnc_backend_for_device(self, device: DeviceModel) -> str:
        """Бекенд для проверок: per-host override или выбранный в настройках."""
        return getattr(device, "vnc_server", None) or self.vnc_server or DEFAULT_VNC_SERVER

    def _vnc_port_for_device(self, device: DeviceModel) -> int:
        """Общий VNC-порт: per-host vnc_port или глобальный."""
        return int(getattr(device, "vnc_port", None) or self.vnc_port)

    def _check_vnc_port(self, device: DeviceModel, host: str) -> None:
        port = self._vnc_port_for_device(device)
        backend = self._vnc_backend_for_device(device)
        ok, latency, reason = tcp_probe(host, port, timeout=2.0, control=self.control)
        if ok:
            self._emit(
                device,
                "vnc.port",
                ICON_OK,
                f"Открыт ({latency:.0f} мс)",
                f"Порт {backend} {host}:{port} — открыт, задержка {latency:.0f} мс",
            )
        else:
            self._emit(
                device,
                "vnc.port",
                ICON_ERR,
                reason,
                f"Порт {backend} {host}:{port} — {reason}",
            )

    def _check_vnc_rfb(self, device: DeviceModel, host: str) -> None:
        port = self._vnc_port_for_device(device)
        backend = self._vnc_backend_for_device(device)
        ok, banner = check_rfb_banner(host, port, timeout=5.0, control=self.control)
        if ok:
            self._emit(device, "vnc.rfb", ICON_OK, banner, f"RFB-баннер: {banner}")
        else:
            self._emit(
                device,
                "vnc.rfb",
                ICON_ERR,
                banner,
                f"Чтение RFB-баннера с {host}:{port} ({backend}): {banner}",
            )

    def _check_vnc_auth(self, device: DeviceModel, host: str) -> None:
        port = self._vnc_port_for_device(device)
        backend = self._vnc_backend_for_device(device)
        pwd = self.vnc_password or getattr(config.app.veyon, "vnc_password", "") or ""
        icon, text = vnc_auth_check(host, port, pwd, control=self.control)
        self._emit(
            device,
            "vnc.auth",
            icon,
            text,
            f"VNC Auth (security type 2) для {host}:{port} ({backend}): {text}",
        )

    def _check_vnc_backend(self, device: DeviceModel, host: str, ssh) -> None:
        """Определить, какой VNC-бекенд ФАКТИЧЕСКИ активен (x11vnc/kmsvnc/krfb).

        Мастер НЕ удаляет пакет чужого бекенда (только стоп + disable),
        поэтому «command -v» всегда покажет оба бинарника. Детект по
        активности: x11vnc - системный юнит/процесс; krfb - user unit
        krfb-dno/krfb в X-сессии или процесс. Первая колонка VNC-группы:
        оператор сразу видит, слушает ли хост именно тот бекенд, который
        выбран в dno-tool.
        """
        code, out = run_ssh_cmd(
            ssh,
            "x11=0; krfb=0; kms=0; x11bin=0; krfb_bin=0; kms_bin=0; "
            # kmsvnc: системная служба или процесс.
            "systemctl is-active kmsvnc >/dev/null 2>&1 && kms=1; "
            "pgrep -x kmsvnc >/dev/null 2>&1 && kms=1; "
            "command -v kmsvnc >/dev/null 2>&1 && kms_bin=1; "
            # x11vnc: юнит активен ИЛИ процесс жив.
            "systemctl is-active x11vnc >/dev/null 2>&1 && x11=1; "
            "pgrep -x x11vnc >/dev/null 2>&1 && x11=1; "
            # krfb: user unit krfb-dno/krfb активен ИЛИ процесс жив.
            "XU=$(loginctl list-sessions --no-legend 2>/dev/null "
            "| awk '$4==\"seat0\" {print $3; exit}'); "
            'if [ -n "$XU" ]; then '
            "XUID=$(id -u $XU 2>/dev/null); "
            # Смена пользователя без PAM (setpriv; school guest рушит runuser).
            "ASU=$(command -v setpriv >/dev/null 2>&1 && echo S || echo R); "
            'asuser() { if [ "$ASU" = S ]; then '
            "setpriv --reuid=$XUID --regid=$(id -g $XU) --init-groups "
            "env XDG_RUNTIME_DIR=/run/user/$XUID "
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$XUID/bus "$@"; '
            "else "
            "runuser -u $XU -- env XDG_RUNTIME_DIR=/run/user/$XUID "
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$XUID/bus "$@"; '
            "fi; }; "
            'if [ "$(id -un)" = "$XU" ]; then '
            "systemctl --user is-active app-org.kde.krfb.service >/dev/null 2>&1 && krfb=1; "
            "systemctl --user is-active krfb-dno.service >/dev/null 2>&1 && krfb=1; "
            "systemctl --user is-active krfb.service >/dev/null 2>&1 && krfb=1; "
            "else "
            "asuser systemctl --user is-active app-org.kde.krfb.service >/dev/null 2>&1 && krfb=1; "
            "asuser systemctl --user is-active krfb-dno.service >/dev/null 2>&1 && krfb=1; "
            "asuser systemctl --user is-active krfb.service >/dev/null 2>&1 && krfb=1; "
            "fi; fi; "
            "pgrep -x krfb >/dev/null 2>&1 && krfb=1; "
            # Установлен ли пакет вообще (для пояснений).
            "command -v x11vnc >/dev/null 2>&1 && x11bin=1; "
            "command -v krfb >/dev/null 2>&1 && krfb_bin=1; "
            'echo "V:$x11:$krfb:$x11bin:$krfb_bin:$kms:$kms_bin"',
            timeout=15,
        )
        if code != 0 or "V:" not in (out or ""):
            self._emit(
                device,
                "vnc.backend",
                ICON_ERR,
                "ошибка",
                f"Не удалось определить VNC-бекенд: код {code}; проверьте права SSH",
            )
            return
        try:
            part = (out or "").split("V:")[-1].strip()
            vals = part.split(":")[:6]
            if len(vals) != 6 or any(v not in ("0", "1") for v in vals):
                raise ValueError("Некорректные флаги")
            x11_run, krfb_run, x11_bin, krfb_bin, kms_run, kms_bin = (
                bool(int(v)) for v in vals
            )
        except Exception:
            self._emit(
                device,
                "vnc.backend",
                ICON_ERR,
                "ошибка",
                "Некорректный формат ответа определения VNC-бекенда",
            )
            return

        selected = self._vnc_backend_for_device(device)
        running = [
            name
            for name, active in (
                ("x11vnc", x11_run),
                ("kmsvnc", kms_run),
                ("krfb", krfb_run),
            )
            if active
        ]
        if len(running) > 1:
            icon, text = ICON_WARN, " + ".join(running)
            detail = (
                "Обнаружено несколько VNC-серверов; конфликт порта не подтверждён. "
                "Проверьте слушающие сокеты через ss -ltnp. Повторите "
                "установку через мастер, чтобы лишний бекенд был остановлен. "
                f"Выбран в dno-tool: {selected}."
            )
        elif running:
            active = running[0]
            icon, text = ICON_OK, active
            detail = f"Активен {active}. Выбран в dno-tool: {selected}"
            if active != selected:
                icon = ICON_WARN
                detail += (
                    f" - НЕ СОВПАДАЕТ: в dno-tool выбран {selected}, но работает "
                    f"{active}. Повторно запустите установку с {selected}."
                )
        else:
            # Ничего не активно: различить "установлен, не запущен" и "ничего".
            installed = [
                name
                for name, present in (
                    ("x11vnc", x11_bin),
                    ("kmsvnc", kms_bin),
                    ("krfb", krfb_bin),
                )
                if present
            ]
            if installed:
                icon = ICON_WARN
                text = " + ".join(installed) + " (не зап.)"
                detail = (
                    f"Установлены: {', '.join(installed)}, но ни один не активен. "
                    "x11vnc и kmsvnc - системные службы, krfb требует сеанс "
                    "пользователя (без входа не стартует). Проверьте автозапуск "
                    "или повторно запустите установку."
                )
            else:
                icon, text = ICON_ERR, "не найден"
                detail = (
                    "VNC-сервер не установлен. Установите через мастер "
                    "настройки (шаг «Установка» - сервис VNC)."
                )
        self._emit(device, "vnc.backend", icon, text, detail)

    def _check_krfb_autostart(self, device: DeviceModel, host: str, ssh) -> None:
        """Проверить активность и автозапуск отдельно для каждого user unit."""
        script = (
            "XU=$(loginctl list-sessions --no-legend 2>/dev/null | awk '$4==\"seat0\" {print $3; exit}'); "
            'if [ -z "$XU" ]; then echo no-x-session; exit 0; fi; '
            'XUID=$(id -u "$XU"); XHOME=$(getent passwd "$XU" | cut -d: -f6); '
            'asuser() { if [ "$(id -u)" = "$XUID" ]; then '
            'env XDG_RUNTIME_DIR=/run/user/$XUID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$XUID/bus "$@"; '
            "elif command -v setpriv >/dev/null 2>&1; then "
            'setpriv --reuid=$XUID --regid=$(id -g "$XU") --init-groups env XDG_RUNTIME_DIR=/run/user/$XUID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$XUID/bus "$@"; '
            'else runuser -u "$XU" -- env XDG_RUNTIME_DIR=/run/user/$XUID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$XUID/bus "$@"; fi; }; '
            "for u in app-org.kde.krfb.service krfb-dno.service krfb.service; do "
            'a=$(asuser systemctl --user is-active "$u"); ar=$?; '
            'e=$(asuser systemctl --user is-enabled "$u"); er=$?; '
            'printf "DNO|%s|%s|%s|%s|%s\\n" "$u" "$ar" "$a" "$er" "$e"; done; '
            "pgrep -x krfb >/dev/null && echo DNO-PROCESS; "
            'for f in "$XHOME/.config/autostart/krfb.desktop" "$XHOME/.config/autostart/org.kde.krfb.desktop" /etc/xdg/autostart/org.kde.krfb.desktop; do '
            'if [ -f "$f" ]; then echo DNO-DESKTOP-UNCERTAIN; fi; done; true'
        )
        code, out = run_ssh_cmd(ssh, script, timeout=15)
        if code != 0 or "no-x-session" in out:
            reason = (
                "Нет пользовательской сессии"
                if "no-x-session" in out
                else "Ошибка выполнения или доступа к user systemd"
            )
            for key in ("vnc.activity", "vnc.autostart"):
                self._emit(
                    device, key, "result_skip", reason, reason, CheckState.UNAVAILABLE
                )
            return
        records = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) == 6 and parts[0] == "DNO":
                records.append(parts)
        active = any(p[2:4] == ["0", "active"] for p in records)
        enabled = any(p[4:6] == ["0", "enabled"] for p in records)
        process = "DNO-PROCESS" in out
        access_denied = any(
            marker in out.casefold()
            for marker in (
                "permission denied",
                "access denied",
                "operation not permitted",
                "отказано в доступе",
            )
        )
        no_bus = "failed to connect to bus" in out.casefold()
        valid = len(records) == 3 and all(
            p[3] in ("active", "inactive", "failed", "unknown")
            and p[5]
            in (
                "enabled",
                "disabled",
                "static",
                "indirect",
                "masked",
                "not-found",
                "enabled-runtime",
            )
            for p in records
        )
        if not valid and not active:
            self._emit(
                device,
                "vnc.activity",
                "result_skip",
                "Отказ доступа"
                if access_denied
                else "Нет пользовательской шины"
                if no_bus
                else "Ошибка выполнения",
                "Не удалось достоверно прочитать user units; проверьте доступ к пользовательской шине и права SSH",
                CheckState.UNAVAILABLE,
            )
        else:
            self._emit(
                device,
                "vnc.activity",
                ICON_OK if active or process else ICON_WARN,
                "Активен" if active or process else "Неактивен",
                "krfb: user unit или процесс"
                if active or process
                else "krfb: все проверенные юниты неактивны",
            )
        if enabled:
            self._emit(
                device,
                "vnc.autostart",
                ICON_OK,
                "Включён",
                "krfb: найден enabled user unit",
            )
        elif "DNO-DESKTOP-UNCERTAIN" in out or not valid:
            self._emit(
                device,
                "vnc.autostart",
                "result_skip",
                "Не подтверждён",
                "Проверьте desktop-автозапуск krfb, Hidden, OnlyShowIn и пользовательские переопределения",
                CheckState.UNAVAILABLE,
            )
        else:
            self._emit(
                device,
                "vnc.autostart",
                ICON_WARN,
                "Не включён",
                "User units не enabled; наличие процесса не подтверждает автозапуск",
            )
