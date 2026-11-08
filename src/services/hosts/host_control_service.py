"""
Host Control Service - Сервис управления хостами через SSH/systemd/UDP.

Содержит операции, не зависящие от Veyon:
- Power: powerdown (systemctl poweroff), reboot (systemctl reboot), power_on (WoL UDP)
- Session: logoff_user (KDE DBus / loginctl), login_user_ssh (DBus org.mos.auth)

Veyon-зависимые операции (lock/unlock/WebAPI-фичи, RFB-мост, миниатюры)
остаются в VeyonService. Этот сервис владеет только SSH/systemd/UDP-транспортом.

При убийстве сессии хоста (powerdown/reboot/logoff) закрывает WebAPI-соединения
VeyonService, чтобы не осталось сокетов завершённой графической сессии.
Зависимость однонаправленная: HostControl → VeyonService (cleanup), не наоборот.
"""

import re
import socket
import shlex
from typing import Optional, Tuple

from src.utils.network.net_utils import get_broadcast_addresses
from src.services.base import BaseService
from src.services.operation_errors import ScopedLastError


class HostControlService(BaseService):
    """SSH/systemd/UDP-операции управления хостами, не зависящие от Veyon.

    Power-операции (powerdown/reboot/power_on) и управление сеансами
    (logoff/login через DBus). Veyon-зависимые операции — в VeyonService.
    """

    _last_error = ScopedLastError()
    _last_error_detail = ScopedLastError()

    # Карта токенов host-control-фичей → (метод, нужны_аргументы).
    # Используется GUI (MainWindow) и веб-сервером (REST endpoint).
    FEATURE_MAP = {
        "power_on": ("power_on", True),  # WoL UDP, args=(mac,)
        "reboot": ("reboot", False),  # SSH systemctl reboot
        "powerdown": ("powerdown", False),  # SSH systemctl poweroff
        "logoff": ("logoff_user", False),  # SSH: штатный выход KDE / loginctl
        "login_ssh": ("login_user_ssh", True),  # DBus org.mos.auth, args=(user, pass)
        # Блокировка без Veyon: временные службы systemd + python3 на хосте.
        "ssh_lock": ("lock_screen_ssh", False),
        "ssh_unlock": ("unlock_screen_ssh", False),
        "ssh_lock_input": ("lock_input_ssh", False),
        "ssh_unlock_input": ("unlock_input_ssh", False),
        # Режим «Экзамен» (ограничение интернета и USB)
        "exam_mode_on": ("enable_exam_mode", False),
        "exam_mode_off": ("disable_exam_mode", False),
    }

    # Токен режима удалённого скрипта для каждого метода блокировки.
    SESSION_LOCK_MODES = ("lock-screen", "unlock-screen", "lock-input", "unlock-input")

    def __init__(self, event_bus: Optional["EventBus"] = None) -> None:
        super().__init__(event_bus)
        self._last_error = ""
        # Подробное описание ошибки (для tooltip статус-строки); пустая строка,
        # если не требуется. Краткий текст — в ``_last_error``.
        self._last_error_detail = ""

    # ========== Lazy resolve VeyonService (для cleanup close_connection) ==========

    def _veyon_service(self):
        """Получить VeyonService из DI-контейнера (lazy).

        Вызывается внутри power/session-методов перед убийством сессии хоста,
        чтобы закрыть WebAPI/RFB-соединения и не оставить битые сокеты.
        Lazy-resolve (не в __init__) — избегает циклов при регистрации сервисов.
        """
        try:
            from src.di import get_container
            from src.services import VeyonService

            return get_container().resolve(VeyonService)
        except Exception as e:
            self._logger.debug(f"HostControl: cannot resolve VeyonService: {e}")
            return None

    def _close_veyon_connections(self, host: str) -> None:
        """Закрыть WebAPI-соединения VeyonService для хоста (best-effort)."""
        if not host:
            return
        try:
            vs = self._veyon_service()
            if vs is not None:
                vs.close_connection(host)
        except Exception as e:
            self._logger.debug(f"HostControl: close_connection for {host} failed: {e}")

    # ========== SSH-хелперы ==========

    def _exec_ssh(
        self, host: str, command_text: str, timeout: int = 15, *, worker=None
    ) -> Tuple[str, bool]:
        """Выполнить SSH-команду на хосте (через SSHWorker).

        Резолвит DeviceModel по host через DeviceService. При
        ``should_use_sudo(device) == True`` и отсутствии `sudo` в тексте команды
        автоматически оборачивает команду в `sudo <cmd>` — SSHWorker подаёт пароль
        через PTY-expect. Для root-логина sudo = no-op, для NOPASSWD — пароль
        не запрашивается, для password-sudo — expect перехватывает prompt.

        ``worker`` — свой SSHWorker, если вызывающему нужны его признаки
        после выполнения (например, обрыв связи после отправки).

        Returns:
            (output, ok) — вывод команды и флаг успеха.
        """
        try:
            from src.di import get_container
            from src.services import DeviceService
            from src.workers.command.ssh import SSHWorker
            from src.workers.command.sudo_utils import has_sudo, should_use_sudo

            container = get_container()
            ds = container.resolve(DeviceService)
            dev = ds.get_device(host)
            if dev is None:
                self._logger.error(f"HostControl: _exec_ssh — host {host} не найден")
                return ("host not found", False)

            # При use_sudo и отсутствии sudo в тексте — оборачиваем.
            # PTY-expect в SSHWorker подаёт пароль при появлении prompt.
            if should_use_sudo(dev) and not has_sudo(command_text):
                command_text = f"sudo {command_text}"

            if worker is None:
                worker = SSHWorker()
            output, ok = worker.execute(dev, command_text, timeout=timeout)
            if not ok:
                self._logger.debug(
                    f"HostControl: _exec_ssh failed for {host}: {output}"
                )
            return (output, ok)
        except Exception as e:
            self._logger.debug(f"HostControl: _exec_ssh error for {host}: {e}")
            return (str(e), False)

    # ========== Power-операции (SSH/systemd) ==========

    @staticmethod
    def build_power_command(action: str) -> str:
        """Команда выключения или перезагрузки без ожидания её выполнения.

        ``--no-block`` только ставит задание systemd и сразу возвращает код:
        отказ (права, polkit) виден, а SSH успевает закрыться до остановки sshd.
        """
        verb = {"reboot": "reboot", "powerdown": "poweroff"}[action]
        return f"systemctl --no-block {verb}"

    def send_power_command(self, host: str, action: str) -> bool:
        """Отправить выключение или перезагрузку; успех — «команда отправлена».

        Хост сам рвёт SSH при выключении, поэтому обрыв связи после отправки
        команды ошибкой не считается.
        """
        from src.workers.command.ssh import SSHWorker

        if not host:
            return False
        worker = SSHWorker()
        _output, ok = self._exec_ssh(
            host, self.build_power_command(action), timeout=10, worker=worker
        )
        self._close_veyon_connections(host)
        return ok or worker.connection_lost_after_dispatch

    def powerdown(self, host: str) -> bool:
        """Выключить питание удалённого хоста через systemctl poweroff.

        SSH-путь на уровне systemd убивает все процессы, включая Veyon-
        сервер сессии — нет гонки с удержанием WebAPI/RFB-соединений
        (проблема чёрного экрана вместо DM при WebAPI-пути). После
        команды закрываем все WebAPI-соединения хоста — Veyon-сервис
        на хосте всё равно умрёт, наши сокеты станут битыми.
        """
        return self.send_power_command(host, "powerdown")

    def reboot(self, host: str) -> bool:
        """Перезагрузить удалённый хост через systemctl reboot.

        Аналогично powerdown — SSH/systemd обрабатывает shutdown чисто,
        без зависимости от состояния Veyon WebAPI-сессий.
        """
        return self.send_power_command(host, "reboot")

    @staticmethod
    def build_logoff_command() -> str:
        """Сформировать выход из пользовательских X11/Wayland-сеансов.

        Сначала проверяется полный список, включая неактивные сеансы.
        Ошибка обнаружения запрещает завершение любых сеансов. SSH, TTY
        и greeter не затрагиваются; отсутствие целей считается успехом.
        KDE завершает сеанс через свой DBus: terminate-session убивает
        sddm-helper и обходит остановку пользовательских служб Plasma.
        При ошибке или отмене штатного выхода принудительного fallback нет.
        Явный sh -c позволяет внешнему sudo охватить весь скрипт.
        """
        script = '''set -f
sessions=$(loginctl list-sessions --no-legend --no-pager --no-ask-password) || exit 1
targets=
while read -r sid rest; do
    [ -n "$sid" ] || continue
    case "$sid" in
        *[!a-zA-Z0-9_-]*) exit 1 ;;
    esac
    class=$(loginctl show-session "$sid" --property=Class --value --no-pager --no-ask-password) || exit 1
    type=$(loginctl show-session "$sid" --property=Type --value --no-pager --no-ask-password) || exit 1
    [ -n "$class" ] && [ -n "$type" ] || exit 1
    case "$class:$type" in
        user:x11|user:wayland|user-early:x11|user-early:wayland)
            desktop=$(loginctl show-session "$sid" --property=Desktop --value --no-pager --no-ask-password) || exit 1
            mode=logind
            uid=0
            name=-
            case "$desktop" in
                [Kk][Dd][Ee]*|[Pp][Ll][Aa][Ss][Mm][Aa]*)
                    mode=kde
                    uid=$(loginctl show-session "$sid" --property=User --value --no-pager --no-ask-password) || exit 1
                    name=$(loginctl show-session "$sid" --property=Name --value --no-pager --no-ask-password) || exit 1
                    case "$uid" in ''|*[!0-9]*) exit 1 ;; esac
                    [ -n "$name" ] || exit 1
                    command -v runuser >/dev/null && command -v busctl >/dev/null || exit 1
                    runuser -u "$name" -- busctl --address="unix:path=/run/user/$uid/bus" --timeout=5 introspect org.kde.Shutdown /Shutdown org.kde.Shutdown >/dev/null || exit 1
                    ;;
            esac
            targets="$targets
$sid $mode $uid $name" ;;
    esac
done <<EOF
$sessions
EOF
result=0
kde_users=
while read -r sid mode uid name; do
    [ -n "$sid" ] || continue
    if [ "$mode" = kde ]; then
        case " $kde_users " in *" $uid "*) continue ;; esac
        kde_users="$kde_users $uid"
        runuser -u "$name" -- busctl --address="unix:path=/run/user/$uid/bus" --timeout=5 call org.kde.Shutdown /Shutdown org.kde.Shutdown logout || result=1
    else
        loginctl terminate-session "$sid" --no-ask-password || result=1
    fi
done <<EOF
$targets
EOF
[ "$result" -eq 0 ] || exit "$result"
[ -n "$targets" ] || exit 0
attempt=0
while [ "$attempt" -lt 20 ]; do
    sessions=$(loginctl list-sessions --no-legend --no-pager --no-ask-password) || exit 1
    ids=
    while read -r sid rest; do
        [ -n "$sid" ] && ids="$ids $sid"
    done <<EOF
$sessions
EOF
    pending=0
    while read -r sid rest; do
        [ -n "$sid" ] || continue
        case " $ids " in *" $sid "*) pending=1 ;; esac
    done <<EOF
$targets
EOF
    [ "$pending" -eq 0 ] && exit 0
    sleep 1
    attempt=$((attempt + 1))
done
printf '%s\\n' 'Графический сеанс не завершился: выход отменён или ещё выполняется.' >&2
result=1
exit "$result"
'''
        return f"sh -c {shlex.quote(script)}"

    def logoff_user(self, host: str) -> bool:
        """Завершить графические сеансы без перезапуска DM.

        Для KDE используется штатный DBus-выход с ожиданием завершения,
        для остальных окружений — loginctl terminate-session.
        Настройки автологина не меняются. SDDM с Relogin=true может снова
        выполнить автовход даже после обычного завершения сеанса.
        После команды закрываем WebAPI/RFB-соединения хоста.
        """
        if not host:
            return False
        output, ok = self._exec_ssh(host, self.build_logoff_command(), timeout=45)
        self._close_veyon_connections(host)
        return ok

    # ========== Блокировка экрана и ввода (SSH, без Veyon) ==========

    @classmethod
    def build_session_lock_command(cls, mode: str) -> str:
        """Команда запуска ``remote_session_lock.py`` в нужном режиме.

        Скрипт передаётся в base64 внутри аргумента ``python3 -c``: на хосте
        ничего не нужно заранее, сам скрипт копирует себя в ``/run/dno-tool``
        для временной службы. sudo, если он нужен, ставит ``_exec_ssh``.
        """
        import base64

        from src.workers.remote_recording import resource_text

        if mode not in cls.SESSION_LOCK_MODES:
            raise ValueError(f"unknown session lock mode: {mode}")
        payload = base64.b64encode(
            resource_text("remote_session_lock.py").encode("utf-8")
        ).decode("ascii")
        loader = (
            "import base64;s=base64.b64decode('" + payload + "');"
            "exec(compile(s,'dno-session-lock','exec'),"
            "{'__name__':'__main__','SOURCE':s})"
        )
        return f"python3 -I -c {shlex.quote(loader)} {mode}"

    @staticmethod
    def _session_lock_message(output: str) -> tuple[str, str]:
        """Краткий текст ошибки и хвост вывода для подсказки."""
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        marked = [line[len("dno-lock:") :].strip() for line in lines if line.startswith("dno-lock:")]
        if marked:
            short = marked[-1]
        elif any("python3" in line and "not found" in line for line in lines):
            short = "На хосте нет python3"
        else:
            short = lines[-1] if lines else "нет ответа от хоста"
        return short, "\n".join(lines[-20:])

    def _run_session_lock(self, host: str, mode: str) -> bool:
        """Выполнить режим блокировки; итог хоста пишется в журнал."""
        self._last_error = ""
        self._last_error_detail = ""
        if not host:
            return False
        output, ok = self._exec_ssh(
            host, self.build_session_lock_command(mode), timeout=45
        )
        short, detail = self._session_lock_message(output)
        if ok:
            self._logger.info(f"HostControl: {mode} {host}: {short}")
            return True
        self._last_error = short
        self._last_error_detail = detail
        self._logger.warning(f"HostControl: {mode} {host} failed: {short}")
        return False

    def lock_screen_ssh(self, host: str) -> bool:
        """Окно блокировки поверх сеансов и захват ввода; держится до снятия."""
        return self._run_session_lock(host, "lock-screen")

    def unlock_screen_ssh(self, host: str) -> bool:
        """Снять блокировку экрана (вместе с её захватом ввода)."""
        return self._run_session_lock(host, "unlock-screen")

    def lock_input_ssh(self, host: str) -> bool:
        """Захватить физические клавиатуры и мыши (EVIOCGRAB)."""
        return self._run_session_lock(host, "lock-input")

    def unlock_input_ssh(self, host: str) -> bool:
        """Снять блокировку ввода; заблокированный экран продолжает держать ввод."""
        return self._run_session_lock(host, "unlock-input")

    # ========== Режим экзамена (ограничение интернета и USB) ==========

    @classmethod
    def build_exam_mode_command(
        cls, action: str, extra_ips: list[str] | None = None
    ) -> str:
        """Команда запуска ``remote_exam_mode.py`` в нужном режиме."""
        import base64

        from src.workers.remote_recording import resource_text

        payload = base64.b64encode(
            resource_text("remote_exam_mode.py").encode("utf-8")
        ).decode("ascii")
        ips_arg = ",".join(extra_ips or [])
        loader = (
            "import base64;s=base64.b64decode('" + payload + "');"
            "exec(compile(s,'dno-exam-mode','exec'),"
            "{'__name__':'__main__'})"
        )
        return f"python3 -I -c {shlex.quote(loader)} {action} {shlex.quote(ips_arg)}"

    def _run_exam_mode(
        self, host: str, action: str, extra_ips: list[str] | None = None
    ) -> bool:
        self._last_error = ""
        self._last_error_detail = ""
        if not host:
            return False
        cmd = self.build_exam_mode_command(action, extra_ips)
        output, ok = self._exec_ssh(host, cmd, timeout=30)
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        marked = [
            line[len("dno-exam:") :].strip()
            for line in lines
            if line.startswith("dno-exam:")
        ]
        short = marked[-1] if marked else (lines[-1] if lines else "нет ответа")
        if ok and "ошибка" not in short.lower():
            self._logger.info(f"HostControl: exam {action} {host}: {short}")
            return True
        self._last_error = short
        self._last_error_detail = "\n".join(lines[-20:])
        self._logger.warning(f"HostControl: exam {action} {host} failed: {short}")
        return False

    def enable_exam_mode(
        self, host: str, extra_ips: list[str] | None = None
    ) -> bool:
        """Включить режим экзамена на хосте (изоляция интернета учеников + блокировка USB)."""
        return self._run_exam_mode(host, "enable", extra_ips)

    def disable_exam_mode(self, host: str) -> bool:
        """Отключить режим экзамена на хосте (восстановить полный доступ)."""
        return self._run_exam_mode(host, "disable")

    # ========== Вход через DBus (org.mos.auth, MOS/ROSA) ==========

    def login_user_ssh(self, host: str, username: str, password: str) -> bool:
        """Выполнить вход пользователя через DBus (org.mos.auth) по SSH.

        Требует пакет `mos-auth-dbus-conf` на хосте (MOS/ROSA-специфичный).
        После входа перезапускает veyon.service, чтобы Veyon подхватил
        новую сессию (иначе миниатюры/фичи не работают до перезапуска Veyon).

        Реализация по образцу comp-cab-setup (workers/users.py:LoginStudents):
        - проверка `rpm -q mos-auth-dbus-conf` (fail → ошибка, не выполняем)
        - `qdbus --system org.mos.auth / login <user> '<pass>'`
        - `sleep 5 && systemctl restart veyon.service` (подхват новой сессии)

        Сервис `org.mos.auth` регистрируется в системной шине DBus SDDM-
        плагином **только пока активен экран входа** (sddm-greeter запущен,
        никто не залогинен). Дополнительно требуется `mos-auth-config enable`
        и SDDM тема = mos-auth. При невыполнении этих условий метод `login`
        отсутствует на объекте `/` → ошибка "Can't find '.login'".

        Args:
            host: адрес удалённой машины.
            username: имя пользователя для входа.
            password: пароль пользователя.

        Returns:
            True при успехе, False при ошибке (краткая причина в `_last_error`,
            подробный диагноз в `_last_error_detail`).
        """
        self._last_error_detail = ""
        if not host:
            return False
        if not username:
            self._last_error = "Не указано имя пользователя для входа"
            self._logger.warning(
                f"HostControl: login_user_ssh — no username for {host}"
            )
            return False

        # 1. Проверка пакета mos-auth-dbus-conf на хосте.
        out, ok = self._exec_ssh(host, "rpm -q mos-auth-dbus-conf", timeout=5)
        if not ok or not out.strip().startswith("mos-auth-dbus-conf"):
            self._last_error = "На хосте не установлен пакет mos-auth-dbus-conf"
            self._last_error_detail = (
                "Удалённый вход через DBus недоступен — отсутствует пакет "
                "mos-auth-dbus-conf.\nУстановите: dnf install mos-auth-dbus-conf"
            )
            self._logger.warning(
                f"HostControl: login_user_ssh — mos-auth-dbus-conf missing on {host}"
            )
            return False

        # 2. Проверка доступности DBus-сервиса org.mos.auth на хосте.
        #    Сервис активен только пока SDDM greeter отображает экран входа
        #    (никто не залогинен). Если вывод не содержит 'login' — сервис
        #    не зарегистрирован, диагностируем конкретную причину.
        svc_out, svc_ok = self._exec_ssh(
            host, "qdbus --system org.mos.auth / 2>&1", timeout=5
        )
        if not svc_ok or "login" not in svc_out:
            short, detail = self._diagnose_mos_auth(host, svc_out)
            self._last_error = short
            self._last_error_detail = detail
            self._logger.warning(
                f"HostControl: login_user_ssh — org.mos.auth unavailable on {host}: "
                f"{svc_out.strip()[:200]}"
            )
            return False

        # 3. Экранирование аргументов для shell (одинарные кавычки, спецсимволы).
        u = shlex.quote(username)
        p = shlex.quote(password)

        # 4. Команда: DBus-вход + ожидание + перезапуск Veyon для подхвата сессии.
        #    При sudo-режиме _exec_ssh добавит `sudo` только в начало (первая
        #    команда), а `systemctl restart veyon` после `&&` останется без sudo.
        #    Поэтому для non-root логина оборачиваем всю цепочку в `bash -c '...'`
        #    с явным `sudo` — PTY-expect в SSHWorker подаст пароль один раз.
        cmd = (
            f"qdbus --system org.mos.auth / login {u} {p} "
            f"&& sleep 5 && systemctl restart veyon.service"
        )
        use_sudo = self._device_use_sudo(host)
        if use_sudo:
            escaped = cmd.replace("'", "'\\''")
            cmd = f"sudo bash -c '{escaped}'"

        # 5. Выполнение. systemctl restart veyon требует root — при sudo-режиме
        #    вся цепочка уже под sudo; при root-логине sudo не нужен.
        #    PTY-expect в SSHWorker подаёт пароль при появлении prompt.
        output, ok = self._exec_ssh(host, cmd, timeout=30)
        if not ok:
            short, detail = self._format_login_error(output)
            self._last_error = short
            self._last_error_detail = detail
            self._logger.debug(
                f"HostControl: login_user_ssh failed for {host}: {output}"
            )
            return False

        # 6. Закрыть WebAPI-соединения — veyon.service на хосте перезапускается,
        #    старые сокеты станут битыми.
        self._close_veyon_connections(host)

        self._last_error = ""
        self._last_error_detail = ""
        self._logger.info(f"HostControl: login_user_ssh ok for {host} ({username})")
        return True

    # ========== Диагностика org.mos.auth ==========

    def _device_use_sudo(self, host: str) -> bool:
        """Резолв should_use_sudo для хоста (через DeviceService)."""
        try:
            from src.di import get_container
            from src.services import DeviceService
            from src.workers.command.sudo_utils import should_use_sudo

            container = get_container()
            ds = container.resolve(DeviceService)
            dev = ds.get_device(host)
            if dev is None:
                return False
            return should_use_sudo(dev)
        except Exception:
            return False

    def _diagnose_mos_auth(self, host: str, svc_out: str) -> Tuple[str, str]:
        """Диагностировать причину недоступности DBus-сервиса org.mos.auth.

        Сервис `org.mos.auth` регистрируется SDDM-плагином только пока активен
        `sddm-greeter` (экран входа). Проверяем последовательно:
        1. Активные локальные сессии (loginctl) — если кто-то залогинен,
           greeter не активен, сервис не зарегистрирован.
        2. `mos-auth-config status` — если подсистема отключена, SDDM-плагин
           не загружается.
        3. `systemctl is-active sddm` — если SDDM не активен, greeter не работает.
        4. SDDM тема — если не mos-auth, плагин не загружается.

        Returns:
            (short, detail) — краткая причина (для статус-строки) и подробный
            диагноз (для tooltip).
        """
        reasons = []

        # 1. Проверка активных локальных сессий (кто залогинен).
        #    loginctl list-sessions: SESSION UID USER SEAT TTY;
        #    нас интересуют сессии с привязанным SEAT (локальные графические).
        sess_out, _ = self._exec_ssh(
            host, "loginctl list-sessions --no-legend 2>/dev/null", timeout=5
        )
        local_users = []
        if sess_out:
            for line in sess_out.strip().splitlines():
                parts = line.split()
                # Колонки: SESSION UID USER SEAT TTY
                # SEAT — 4-я колонка (индекс 3); непустой SEAT = локальная сессия.
                if len(parts) >= 4 and parts[3] and parts[3] != "-":
                    local_users.append(parts[2] if len(parts) > 2 else "?")
        if local_users:
            users_str = ", ".join(local_users)
            reasons.append(
                f"На хосте уже выполнен вход: {users_str}. "
                "Выйдите из сессии — DBus-сервис org.mos.auth доступен "
                "только на экране входа SDDM (пока sddm-greeter активен)."
            )

        # 2. Проверка mos-auth-config (активация подсистемы).
        cfg_out, _ = self._exec_ssh(
            host,
            "command -v mos-auth-config >/dev/null 2>&1 && "
            "mos-auth-config status 2>&1 || echo '__no_mos_auth_config__'",
            timeout=5,
        )
        cfg_text = cfg_out.strip() if cfg_out else ""
        if "__no_mos_auth_config__" not in cfg_text and cfg_text:
            # mos-auth-config есть; проверяем включена ли подсистема.
            lowered = cfg_text.lower()
            if "disabled" in lowered or "выключ" in lowered or "отключ" in lowered:
                reasons.append(
                    "Подсистема mos-auth отключена. "
                    "Выполните на хосте: sudo mos-auth-config enable"
                )
        elif "__no_mos_auth_config__" in cfg_text:
            reasons.append(
                "Утилита mos-auth-config не найдена — подсистема авторизации "
                "mos-auth не установлена. Установите пакет и выполните "
                "sudo mos-auth-config enable."
            )

        # 3. Проверка активности SDDM.
        sddm_out, _ = self._exec_ssh(host, "systemctl is-active sddm 2>&1", timeout=5)
        sddm_state = sddm_out.strip().lower() if sddm_out else ""
        if sddm_state and sddm_state not in ("active", "activating"):
            reasons.append(
                f"SDDM не активен (состояние: {sddm_state}). "
                "Экран входа не отображается — DBus-сервис не загружен."
            )

        # 4. Проверка SDDM темы (должна быть mos-auth).
        theme_out, _ = self._exec_ssh(
            host,
            "grep -rh '^[[:space:]]*Theme=' /etc/sddm.conf /etc/sddm.conf.d/ "
            "2>/dev/null | head -1",
            timeout=5,
        )
        theme_text = theme_out.strip() if theme_out else ""
        if theme_text:
            theme_val = (
                theme_text.split("=", 1)[-1].strip() if "=" in theme_text else ""
            )
            if theme_val and "mos-auth" not in theme_val.lower():
                reasons.append(
                    f"SDDM тема не mos-auth (текущая: '{theme_val}'). "
                    "Плагин org.mos.auth загружается только с темой mos-auth."
                )
        elif not local_users and sddm_state in ("active", "activating"):
            reasons.append(
                "SDDM тема не определена (вероятно, тема по умолчанию). "
                "Плагин org.mos.auth требует тему mos-auth. "
                "Проверьте /etc/sddm.conf или /etc/sddm.conf.d/."
            )

        # Краткая причина для статус-строки.
        if reasons:
            short = reasons[0].split(".")[0] + "."
        else:
            short = "DBus-сервис org.mos.auth недоступен"

        # Подробный диагноз для tooltip.
        detail_header = (
            "DBus-сервис org.mos.auth не предоставляет метод login "
            "(объект / без метода)."
        )
        if reasons:
            detail = detail_header + "\n\nВозможные причины:\n• " + "\n• ".join(reasons)
        else:
            detail = (
                detail_header + "\n\nПроверьте:\n"
                "• На хосте никто не залогинен (SDDM greeter активен).\n"
                "• Выполнено `mos-auth-config enable`.\n"
                "• SDDM тема = mos-auth.\n"
                f"Вывод qdbus: {svc_out.strip()[:200]}"
            )
        return short, detail

    def _format_login_error(self, output: str) -> Tuple[str, str]:
        """Отформатировать ошибку выполнения qdbus-команды.

        Парсит вывод qdbus и формирует человекочитаемое сообщение
        со списком возможных причин при типичных ошибках.

        Returns:
            (short, detail) — краткая причина и подробный диагноз.
        """
        text = output.strip()
        lowered = text.lower()
        if "can't find" in lowered or "no such method" in lowered:
            short = "DBus-сервис org.mos.auth недоступен"
            detail = (
                "DBus-сервис org.mos.auth не предоставляет метод login.\n\n"
                "Проверьте:\n"
                "• На хосте никто не залогинен (SDDM greeter активен).\n"
                "• Выполнено `mos-auth-config enable`.\n"
                "• SDDM тема = mos-auth.\n"
                f"Вывод: {text[:300]}"
            )
            return short, detail
        if "service 'org.mos.auth' does not exist" in lowered or (
            "no such service" in lowered
        ):
            short = "DBus-сервис org.mos.auth не зарегистрирован"
            detail = (
                "DBus-сервис org.mos.auth не зарегистрирован в системной шине.\n"
                "Сервис активен только пока SDDM greeter отображает экран входа "
                "(никто не залогинен).\n\n"
                "Также проверьте `mos-auth-config enable` "
                "и SDDM тему = mos-auth.\n"
                f"Вывод: {text[:300]}"
            )
            return short, detail
        return (
            f"Ошибка входа через DBus: {text[:120]}",
            f"Ошибка входа через DBus:\n{text}",
        )

    # ========== Wake-on-LAN (UDP, не SSH) ==========

    def power_on(self, mac_address: str) -> bool:
        """Включить удалённую машину через Wake-on-LAN (magic packet).

        Не требует Veyon-соединения — отправляет UDP directed broadcast
        на каждый активный IPv4-интерфейс (per-interface). Это необходимо
        на multi-NIC машинах: ограниченный broadcast 255.255.255.255
        маршрутизируется ядром только через основной интерфейс, и хосты
        в других подсетях пакет не получают. Directed broadcast
        (например 192.168.1.255) отправляется через соответствующий NIC.

        Если psutil недоступен или не нашёл ни одного адреса — fallback
        на 255.255.255.255 (совместимость со single-NIC машинами).
        """
        if not mac_address:
            return False
        mac = re.sub(r"[:\-.]", "", mac_address).strip()
        if len(mac) != 12:
            self._logger.error(
                f"HostControl: invalid MAC address for WoL: {mac_address}"
            )
            return False
        try:
            mac_bytes = bytes.fromhex(mac)
            packet = b"\xff" * 6 + mac_bytes * 16

            broadcasts = get_broadcast_addresses()
            if not broadcasts:
                broadcasts = ["255.255.255.255"]

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sent = 0
            for bcast in broadcasts:
                try:
                    sock.sendto(packet, (bcast, 9))
                    sent += 1
                    self._logger.info(
                        f"HostControl: WoL magic packet sent to {bcast}:9 "
                        f"for MAC {mac_address}"
                    )
                except Exception as e:
                    self._logger.warning(
                        f"HostControl: WoL send to {bcast}:9 failed: {e}"
                    )
            sock.close()
            return sent > 0
        except Exception as e:
            self._logger.error(f"HostControl: WoL error for {mac_address}: {e}")
            return False
