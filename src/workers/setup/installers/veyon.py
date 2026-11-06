"""Установка Veyon с проверкой доступа и захвата рабочего стола."""

import re
import shlex
import time
from collections.abc import Callable

from paramiko import SSHClient

from src.workers.setup.phases import (
    CommandRunner,
    InstallRequest,
    InstallTarget,
    PhasedInstaller,
    PhaseResult,
)
from src.workers.setup.types import (
    RESULT_ABORT,
    RESULT_ERROR,
    RESULT_SUCCESS,
    RESULT_WAITING,
)
from src.workers.setup.veyon_capture import (
    HEADLESS_PLUGIN,
    CaptureConfigurationError,
    capture_target,
    configure_capture,
)


class VeyonInstaller(PhasedInstaller):
    service = "veyon"
    _verification_waiting: bool = False
    recovery_limit = "Пакеты, настройки и импортированный ключ Veyon автоматически не откатываются; проверьте результат перед повтором."

    def _apply_install(
        self,
        device: InstallTarget,
        ssh: SSHClient,
        run: CommandRunner,
        write: Callable[[str, str | bytes], tuple[int, str]],
        append: Callable[[str, str], tuple[bool, str]],
    ) -> None:
        self._install_veyon(device, run, write)

    def _install_veyon(
        self,
        device: InstallTarget,
        run_cmd: CommandRunner,
        write_file: Callable[[str, str | bytes], tuple[int, str]],
    ) -> None:
        svc = "veyon"
        output_lines: list[str] = []

        def log(msg: str) -> None:
            output_lines.append(msg)

        self.context.log(device.host, svc, "Начало установки и настройки Veyon")
        if (
            not self.parameters.veyon_public_key_pem
            or not self.parameters.veyon_key_name
        ):
            self.context.log(device.host, svc, "Veyon: ключ не подготовлен")
            self._report(
                device, svc, RESULT_ERROR, "Публичный ключ Veyon не передан воркеру"
            )
            return

        key_name = self.parameters.veyon_key_name
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}", key_name):
            self._report(device, svc, RESULT_ERROR, "Недопустимое имя ключа Veyon")
            return
        key_ref = shlex.quote(key_name + "/public")
        access_group = self.parameters.veyon_access_group
        if access_group is None:
            for candidate in ("wheel", "sudo", "root"):
                code, _, _ = run_cmd(
                    "getent group " + shlex.quote(candidate), timeout=10
                )
                if code == 0:
                    access_group = candidate
                    break
            if access_group is None:
                self._report(
                    device,
                    svc,
                    RESULT_ERROR,
                    "Не найдена группа доступа для ключа Veyon",
                )
                return
        group_arg = shlex.quote(access_group)
        server_port = self.parameters.veyon_server_port

        # 1. Проверка veyon-cli
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        code, out, err = run_cmd("command -v veyon-cli")
        if code != 0 or not out:
            log("veyon-cli не найден — устанавливаю пакет...")
            from src.workers.setup.packages import PackageManager

            code, out, err = PackageManager(run_cmd, self._local_abort).install(
                ("veyon", "veyon-core")
            )
            if code != 0:
                msg = f"Не удалось установить veyon: {err}"
                log(msg)
                self.context.log(
                    device.host, svc, "Veyon: ошибка — пакет не установлен"
                )
                self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
                return
            log("Пакет veyon установлен")
        else:
            log(f"veyon-cli найден: {out}")

        # Выбор захвата выполняется до импорта ключа и перезапуска сервиса.
        try:
            capture = configure_capture(run_cmd, log)
            self.context.log(
                device.host,
                svc,
                f"Графическая сессия: {capture.session or 'не обнаружена'}; модуль захвата Veyon: {capture.name}.",
            )
        except CaptureConfigurationError as error:
            log(str(error))
            self.context.log(device.host, svc, f"Не удалось настроить захват Veyon: {error}")
            self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
            return

        # 2. Проверка: ключ уже есть? Только для лога, не для ветвления.
        # Перезапись идемпотентна: существующий ключ удаляем и импортируем заново.
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        code, out, err = run_cmd("veyon-cli authkeys list")
        if code == 0 and key_name in (out or ""):
            log(f"Ключ '{key_name}/public' уже существует - перезаписываю")

        # 3. Удалить существующий ключ (если его нет - не ошибка).
        code, out, err = run_cmd(f"veyon-cli authkeys delete {key_ref}")
        if code == 0:
            log(f"Старый ключ '{key_name}/public' удалён (или отсутствовал)")
        else:
            log(f"delete старого ключа: {err or out} (продолжаю импорт)")

        # 4. Запись PEM во временный файл и импорт оттуда
        #    (не в /etc/veyon/keys/public/ — туда пишет сам veyon-cli authkeys import,
        #     ручная запись конфликтует при повторных запусках)
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        code, tmp_key_path, err = run_cmd("umask 077; mktemp /tmp/dno-veyon.XXXXXXXXXX")
        if code != 0 or not re.fullmatch(
            r"/tmp/dno-veyon\.[A-Za-z0-9]+", tmp_key_path or ""
        ):
            self._report(
                device, svc, RESULT_ERROR, "Не удалось создать временный файл Veyon"
            )
            return
        tmp_arg = shlex.quote(tmp_key_path)
        try:
            code, err = write_file(tmp_key_path, self.parameters.veyon_public_key_pem)
            if code != 0:
                self._report(
                    device,
                    svc,
                    RESULT_ERROR,
                    "Не удалось записать временный ключ Veyon",
                )
                return
            if self._local_abort.is_set():
                self._report(device, svc, RESULT_ABORT, "Прервано")
                return
            code, out, err = run_cmd(f"veyon-cli authkeys import {key_ref} {tmp_arg}")
            if code != 0:
                log(f"import не удался, повторяю delete+import: {err or out}")
                run_cmd(f"veyon-cli authkeys delete {key_ref}")
                code, out, err = run_cmd(
                    f"veyon-cli authkeys import {key_ref} {tmp_arg}"
                )
        finally:
            run_cmd(f"rm -f -- {tmp_arg}")
        if code != 0:
            msg = f"authkeys import failed: {err or out}"
            log(msg)
            self.context.log(device.host, svc, "Veyon: ошибка импорта ключа")
            self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
            return
        # Verify: ключ должен быть в списке.
        code, out, err = run_cmd("veyon-cli authkeys list")
        if code != 0 or key_name not in (out or ""):
            msg = (
                f"verify failed: ключ '{key_name}' не найден после import: {err or out}"
            )
            log(msg)
            self.context.log(device.host, svc, "Veyon: ключ не подтверждён")
            self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
            return
        log("Ключ импортирован в Veyon (перезаписан)")

        # 6. Группа доступа (опционально)
        if access_group:
            code, _, _ = run_cmd(
                f"veyon-cli authkeys setaccessgroup {key_ref} {group_arg}"
            )
            group_code, details, _ = run_cmd("veyon-cli authkeys list details")
            verified = False
            for line in details.splitlines():
                parts = [part.strip() for part in line.split("|") if part.strip()]
                if len(parts) >= 4 and parts[0] == key_name and parts[1] == "public":
                    verified = parts[3] == access_group
            if code or group_code or not verified:
                self._report(
                    device, svc, RESULT_ERROR, "Группа доступа Veyon не подтверждена"
                )
                return
            log(f"Группа доступа: {access_group}")

        # 7. Auth method = key file
        code, out, err = run_cmd("veyon-cli config set Authentication/Method 1")
        if code != 0:
            self._report(
                device, svc, RESULT_ERROR, "Не удалось настроить аутентификацию Veyon"
            )
            return
        else:
            log("Метод аутентификации: key file")

        for setting, expected in (
            ("Authentication/Method", "1"),
            ("Network/VeyonServerPort", str(server_port)),
        ):
            if setting != "Authentication/Method":
                code, _, _ = run_cmd(f"veyon-cli config set {setting} {expected}")
                if code:
                    self._report(
                        device, svc, RESULT_ERROR, "Не удалось применить порт Veyon"
                    )
                    return
            code, actual, _ = run_cmd(f"veyon-cli config get {setting}")
            if code or actual.strip().strip('"') != expected:
                self._report(
                    device, svc, RESULT_ERROR, "Настройки Veyon не подтверждены"
                )
                return

        # 7.1 Сервисные настройки (как в команде «Настройка» из commands.json)
        for cfg_key, cfg_val in (
            ("Service/HideTrayIcon", "true"),
            ("Service/RemoteConnectionNotifications", "false"),
            ("Service/FailedAuthenticationNotifications", "false"),
            ("Service/ClipboardSynchronizationDisabled", "true"),
            ("Service/MultiSession", "true"),
        ):
            code, out, err = run_cmd(f"veyon-cli config set {cfg_key} {cfg_val}")
            if code != 0:
                log(f"WARNING: set {cfg_key} failed: {err}")
                if cfg_key == "Service/HideTrayIcon":
                    self.context.log(
                        device.host,
                        svc,
                        f"Не удалось скрыть значок Veyon Server: {err or out or 'ошибка настройки'}",
                    )
            elif cfg_key == "Service/HideTrayIcon":
                read_code, actual, _ = run_cmd(
                    "veyon-cli config get Service/HideTrayIcon"
                )
                if read_code == 0 and actual.strip().strip('"').lower() == "true":
                    message = "Значок Veyon Server скрыт настройкой Service/HideTrayIcon"
                    log(message)
                    self.context.log(device.host, svc, message)
                else:
                    message = (
                        "WARNING: значок Veyon Server не подтверждён как скрытый "
                        f"(текущее значение: {actual.strip() or 'не прочитано'})"
                    )
                    log(message)
                    self.context.log(device.host, svc, message)

        # 8. Регистрация + перезапуск сервиса
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        code, out, err = run_cmd("veyon-cli service register")
        if code != 0:
            log(f"service register failed ({err}), пробую systemctl enable...")
            run_cmd("systemctl enable veyon")
        else:
            log("Сервис зарегистрирован")
        code, out, err = run_cmd("veyon-cli service restart")
        if code != 0:
            log(f"service restart failed ({err}), пробую systemctl restart...")
            run_cmd("systemctl restart veyon")
        else:
            log("Сервис перезапущен")

        # 8. Firewall
        self._open_firewall_v4(run_cmd, server_port, log)

        self._report(device, svc, RESULT_SUCCESS, "\n".join(output_lines))

    def verify(self, request: InstallRequest, applied: PhaseResult) -> PhaseResult:
        messages = [applied.message]
        if self._verify_veyon(
            request.run,
            self.parameters.veyon_server_port,
            messages.append,
            request.device,
        ):
            return PhaseResult(RESULT_SUCCESS, "\n".join(messages))
        return PhaseResult(
            RESULT_WAITING if self._verification_waiting else RESULT_ERROR,
            "\n".join(messages),
        )

    def _verify_veyon(
        self,
        run_cmd: CommandRunner,
        server_port: int,
        log: Callable[[str], None],
        device: InstallTarget | None = None,
    ) -> bool:
        self._verification_waiting = False
        try:
            target = capture_target(run_cmd)
        except CaptureConfigurationError as error:
            log(str(error))
            return False
        if target.current_uid == HEADLESS_PLUGIN:
            log(
                "Выбран HeadlessVncServer: доступен искусственный экран, захват рабочего стола не настроен"
            )
            return False
        if target.current_uid != target.uid:
            log(f"Модуль захвата не соответствует сессии; требуется {target.name}")
            return False
        code, out, _ = run_cmd("systemctl is-active veyon")
        if code != 0 or (out or "").strip() != "active":
            log(f"Сервис не активен (статус: {out or 'unknown'})")
            _, status_out, _ = run_cmd(
                "systemctl status veyon --no-pager -l 2>/dev/null"
            )
            if status_out:
                log("--- systemctl status veyon ---")
                log(status_out[:1500])
            return False
        log("Сервис активен")
        if target.session is None:
            self._verification_waiting = True
            log(
                "Нет активной графической сессии пользователя; войдите в систему и повторите настройку для проверки модуля и кадра"
            )
            return False
        code, out, _ = run_cmd(f"ss -tln 2>/dev/null | grep ':{server_port} '")
        if code != 0 or not (out or "").strip():
            log(f"Порт {server_port} не слушается")
            return False
        else:
            log(f"Порт {server_port} слушается")
        if device is not None:
            from src.services.remote.veyon_rfb_bridge import (
                VeyonRFBError,
                VeyonRFBHandshake,
            )
            from src.services.thumbnails.rfb import RfbError, RfbSession

            if not self.parameters.veyon_private_key:
                log("Нет приватного ключа для проверки доступа")
                return False
            handshake = VeyonRFBHandshake(
                device.host,
                server_port,
                self.parameters.veyon_private_key,
                self.parameters.veyon_key_name,
                connect_timeout=5,
                access_control_timeout=15,
            )
            self.context.register_connection(handshake)
            session = RfbSession(device.host, server_port, "", read_timeout=0.5)
            authenticated = False
            try:
                connection, server_init = handshake.connect()
                authenticated = True
                log("Подключение и аутентификация с машины оператора подтверждены")
                deadline = time.monotonic() + 15.0
                session.adopt_authenticated_socket(connection, server_init, deadline)
                while time.monotonic() < deadline:
                    if self._local_abort.is_set():
                        return False
                    session.poll(min(deadline, time.monotonic() + 0.5))
                    if session.complete:
                        log(
                            f"Кадр рабочего стола получен: {session.width}×{session.height}"
                        )
                        break
                else:
                    self._verification_waiting = True
                    log(
                        "Сервис доступен, но кадр не получен за 15 с; проверьте разрешение захвата экрана, PipeWire и портал пользовательской сессии, затем повторите проверку"
                    )
                    return False
            except RfbError as error:
                log(f"Сервис доступен, но захват кадра не подтверждён ({error.code})")
                return False
            except (VeyonRFBError, OSError, ValueError):
                log(
                    "Сервис доступен, но получение кадра завершилось ошибкой"
                    if authenticated
                    else "Подключение или аутентификация с машины оператора не подтверждены"
                )
                return False
            finally:
                session.close()
                handshake.cancel()
                self.context.unregister_connection(handshake)
        else:
            self._verification_waiting = True
            log("Сервис доступен; для проверки кадра нужен адрес хоста")
            return False
        log("Veyon настроен успешно")
        return True

    def _open_firewall_v4(
        self, run_cmd: CommandRunner, port: int, log: Callable[[str], None]
    ) -> None:
        code, out, err = run_cmd(
            f"firewall-cmd --permanent --add-port={port}/tcp && firewall-cmd --reload"
        )
        if code == 0:
            log(f"Порт {port} открыт в брандмауэре")
            return
        fw_err = (err or out or "").strip()
        if "command not found" in fw_err:
            fw_err = "не установлен"
        code2, out2, err2 = run_cmd(f"ufw allow {port}/tcp")
        if code2 == 0:
            log(f"Порт {port} открыт в брандмауэре")
        else:
            ufw_err = (err2 or out2 or "").strip()
            if "command not found" in ufw_err:
                ufw_err = "не установлен"
            if fw_err == "не установлен" and ufw_err == "не установлен":
                log(
                    "WARNING: нет firewall-cmd/ufw; состояние фильтрации неизвестно, доступ проверяется с машины оператора"
                )
            else:
                log(
                    f"WARNING: не удалось открыть порт {port} в брандмауэре "
                    f"(firewall-cmd: {fw_err}; ufw: {ufw_err})"
                )
