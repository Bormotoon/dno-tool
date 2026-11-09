"""Установка VNC-серверов, работающих системной службой root (x11vnc, kmsvnc).

В отличие от KRFB (служба в сеансе пользователя, ``krfb.py``), эти серверы
не зависят от входа пользователя. Оба ставятся одинаково, отличаются только
источником пакета и форматом пароля:

1. пакет: из репозитория (x11vnc) или из RPM в составе программы (kmsvnc);
2. другие VNC-серверы останавливаются и снимаются с автозапуска;
3. пароль: ``/etc/<сервер>/passwd``, права 0600;
4. служба ``/etc/systemd/system/<сервер>.service`` пишется мастером;
5. включение службы, порт в firewall, проверка, что порт слушается.
"""

import hashlib
import re
import shlex
from dataclasses import dataclass
from typing import Callable, List, Optional

from src.workers.setup.installers.vnc_common import VncOperations
from src.workers.setup.types import RESULT_ABORT, RESULT_ERROR, RESULT_SUCCESS


# Строки журнала службы, которые объясняют сбой запуска.
_ERROR_LINE = re.compile(
    r"unsupported|failed|error|cannot|can't|no usable|not found|denied|already in use",
    re.IGNORECASE,
)


def short_evr(evr: str) -> str:
    """VERSION-RELEASE для людей: без даты и коммита в RELEASE (0.0.6-10)."""
    version, _, release = evr.partition("-")
    build = release.split(".", 1)[0]
    return f"{version}-{build}" if build else version


@dataclass(frozen=True)
class BundledRpm:
    """RPM в составе программы (``assets/remote``)."""

    file_name: str
    sha256: str
    # VERSION-RELEASE из пакета: совпадает с установленным — ставить не нужно.
    evr: str

    @property
    def version(self) -> str:
        """Версия пакета для журнала и окна мастера (0.0.6-10)."""
        return short_evr(self.evr)

    def load(self) -> bytes:
        """Прочитать пакет и сверить контрольную сумму."""
        from src.config.settings import get_asset_path

        with open(get_asset_path("remote/" + self.file_name), "rb") as stream:
            data = stream.read()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f"Пакет {self.file_name} в составе программы повреждён")
        return data


@dataclass(frozen=True)
class VncPackage:
    """Откуда берётся программа VNC-сервера."""

    name: str
    binary: str
    # None — пакет из репозитория хоста через dnf/apt.
    bundled: Optional[BundledRpm] = None


class SystemVncBackend(VncOperations):
    """VNC-сервер — системная служба root. Подкласс задаёт параметры сервера."""

    NAME = ""
    TITLE = ""
    PACKAGE: VncPackage
    UNIT_TEMPLATE = ""
    PASSWORD_REQUIRED = False
    # Скрипт запуска (путь, содержимое) или None.
    WRAPPER: Optional[tuple] = None
    # Журнал сервера помимо journalctl (x11vnc пишет в свой файл).
    LOG_FILE: Optional[str] = None
    FAILURE_HINT = ""

    @property
    def label(self) -> str:
        """Имя сервера для журнала мастера; у пакета из программы — с версией."""
        bundled = self.PACKAGE.bundled
        return f"{self.TITLE} {bundled.version}" if bundled is not None else self.TITLE

    @property
    def unit(self) -> str:
        return f"{self.NAME}.service"

    @property
    def unit_path(self) -> str:
        return f"/etc/systemd/system/{self.unit}"

    @property
    def password_path(self) -> str:
        return f"/etc/{self.NAME}/passwd"

    # --- Различия серверов -------------------------------------------------

    def password_content(self, password: str):
        """Содержимое файла пароля в формате сервера."""
        raise NotImplementedError

    def host_problem(self, run_cmd) -> Optional[str]:
        """Причина, по которой сервер нельзя ставить на хост, или None."""
        return None

    def unit_text(self, port: int) -> str:
        """Текст юнита службы для порта."""
        return self.UNIT_TEMPLATE.format(port=port)

    # --- Установка ---------------------------------------------------------

    def configure(self, device, ssh, run_cmd, write_file) -> None:
        svc = "vnc"
        port = int(device.vnc_port or self.parameters.vnc_port or 5900)
        password = self.parameters.vnc_password
        output_lines: List[str] = []
        log = output_lines.append

        def fail(message: str, short: str) -> None:
            log(message)
            self.context.log(device.host, svc, f"{self.TITLE}: {short}")
            self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))

        def aborted() -> bool:
            if self._local_abort.is_set():
                self._report(device, svc, RESULT_ABORT, "Прервано")
                return True
            return False

        self.context.log(
            device.host, svc, f"Начало установки общего экрана ({self.label})"
        )
        if self.PASSWORD_REQUIRED and not password:
            fail(f"Для {self.TITLE} нужен пароль VNC", "не задан пароль")
            return
        if aborted():
            return
        problem = self.host_problem(run_cmd)
        if problem:
            # Причина нужна и в журнале установки, не только в подробностях.
            fail(problem, f"хост не подходит. {problem}")
            return

        # 1. Пакет.
        if aborted():
            return
        def notice(message: str) -> None:
            self.context.log(device.host, svc, message)

        if not self._ensure_package(run_cmd, write_file, log, notice):
            reason = output_lines[-1] if output_lines else ""
            fail(
                f"Пакет {self.PACKAGE.name} не установлен",
                f"пакет не установлен. {reason}".strip(),
            )
            return

        # 2. Другие серверы занимают тот же порт.
        if aborted():
            return
        self._shutdown_other_backends(run_cmd, log)

        # 3. Пароль.
        if aborted():
            return
        if not self._write_password(run_cmd, write_file, password, log):
            fail("Не удалось записать пароль VNC", "ошибка записи пароля")
            return

        # 4. Скрипт запуска и служба.
        if aborted():
            return
        if self.WRAPPER:
            path, content = self.WRAPPER
            if not self._write_checked(
                run_cmd, write_file, path, content, 0o755, content.split("\n", 1)[0], log
            ):
                fail("Не удалось записать скрипт запуска", "ошибка скрипта запуска")
                return
        unit_text = self.unit_text(port)
        if not self._write_checked(
            run_cmd, write_file, self.unit_path, unit_text, 0o644, "[Unit]", log
        ):
            fail("Не удалось записать службу", "ошибка записи службы")
            return
        log(f"Служба {self.unit} записана (порт {port})")

        # 5. Включение.
        if aborted():
            return
        code, out, err = run_cmd(
            f"systemctl daemon-reload && systemctl enable {self.unit} "
            f"&& systemctl restart {self.unit}",
            timeout=60,
        )
        if code != 0:
            fail(
                f"Не удалось включить службу {self.unit}: {err or out}",
                "служба не включена",
            )
            return
        log(f"Служба {self.unit} включена и запущена")

        # 6. Firewall.
        self._open_firewall_v4(run_cmd, port, log)
        self._report(device, svc, RESULT_SUCCESS, "\n".join(output_lines))

    def _shutdown_other_backends(self, run_cmd, log) -> None:
        if self.NAME != "x11vnc":
            self._shutdown_legacy_x11vnc(run_cmd, log)
        if self.NAME != "kmsvnc":
            self._shutdown_kmsvnc(run_cmd, log)
        self._shutdown_legacy_krfb(run_cmd, log)

    def _write_password(self, run_cmd, write_file, password, log) -> bool:
        directory = shlex.quote(f"/etc/{self.NAME}")
        path = shlex.quote(self.password_path)
        if not password:
            run_cmd(f"rm -f -- {path}", timeout=15)
            log("VNC-пароль не задан — без аутентификации")
            return True
        code, _, err = run_cmd(f"install -d -m 700 {directory}", timeout=15)
        if code != 0:
            log(f"Не удалось создать каталог /etc/{self.NAME}: {err}")
            return False
        code, err = write_file(
            self.password_path, self.password_content(password), mode=0o600
        )
        if code != 0:
            log(f"Ошибка записи пароля: {err}")
            return False
        code, _, _ = run_cmd(f"test -s {path}", timeout=15)
        if code != 0:
            log("Файл пароля не создан или пустой")
            return False
        log("VNC-пароль установлен")
        return True

    def _write_checked(
        self, run_cmd, write_file, path, content, mode, first_line, log
    ) -> bool:
        """Записать файл и сверить первую строку: запись могла оборваться."""
        code, err = write_file(path, content, mode=mode)
        if code != 0:
            log(f"Ошибка записи {path}: {err}")
            return False
        _, out, _ = run_cmd(f"head -n1 {shlex.quote(path)}", timeout=15)
        if (out or "").strip() != first_line:
            log(f"Файл {path} повреждён (записался некорректно)")
            _, dump, _ = run_cmd(
                f"head -c 200 {shlex.quote(path)} 2>/dev/null", timeout=15
            )
            if dump:
                log("--- начало файла ---")
                log(dump[:500])
            return False
        return True

    # --- Пакет -------------------------------------------------------------

    def _ensure_package(self, run_cmd, write_file, log, notice=None) -> bool:
        """Поставить пакет; ``notice`` получает строку для общего журнала мастера."""
        package = self.PACKAGE
        if package.bundled is None:
            code, out, _ = run_cmd(f"command -v {shlex.quote(package.binary)}", timeout=15)
            if code == 0 and (out or "").strip():
                log(f"{package.name} уже установлен")
                return True
            log(f"Устанавливаю {package.name} из репозитория...")
            from src.workers.setup.packages import PackageManager

            code, out, err = PackageManager(run_cmd, self._local_abort).install(
                (package.name,)
            )
            if code != 0:
                log(f"Ошибка установки: {(err or out or '').strip()}")
                return False
            return True

        bundled = package.bundled
        installed = self._installed_evr(run_cmd, package.name)
        if installed == bundled.evr:
            log(f"{package.name} уже установлен ({bundled.version})")
            return True
        if installed:
            message = f"{package.name}: обновление {short_evr(installed)} → {bundled.version}"
            log(message)
            if notice is not None:
                notice(message)
        return self._install_bundled(run_cmd, write_file, package, log)

    @staticmethod
    def _installed_evr(run_cmd, name) -> str:
        code, out, _ = run_cmd(
            f"rpm -q --qf '%{{VERSION}}-%{{RELEASE}}' {shlex.quote(name)}", timeout=15
        )
        return (out or "").strip() if code == 0 else ""

    def _install_bundled(self, run_cmd, write_file, package, log) -> bool:
        """Передать RPM во временный каталог хоста и установить."""
        rpm = package.bundled
        try:
            data = rpm.load()
        except (OSError, ValueError) as error:
            log(f"Пакет {package.name} недоступен: {error}")
            return False
        code, tmpdir, _ = run_cmd("umask 077; mktemp -d /tmp/dno-pkg.XXXXXXXXXX")
        tmpdir = (tmpdir or "").strip()
        if code != 0 or not re.fullmatch(r"/tmp/dno-pkg\.[A-Za-z0-9]+", tmpdir):
            log("Не удалось создать временный каталог на хосте")
            return False
        remote = f"{tmpdir}/{rpm.file_name}"
        quoted = shlex.quote(remote)
        try:
            log(f"Передаю пакет {package.name} на хост...")
            code, _ = write_file(remote, data, mode=0o644)
            if code != 0:
                log(f"Не удалось передать пакет {package.name}")
                return False
            code, out, _ = run_cmd(f"sha256sum {quoted}", timeout=30)
            if code != 0 or not (out or "").startswith(rpm.sha256):
                log(f"Пакет {package.name} повреждён при передаче")
                return False
            log(f"Устанавливаю {package.name} {rpm.version} ({rpm.evr})...")
            # dnf подтягивает недостающие библиотеки из репозитория. Без
            # доступа к репозиторию dnf отказывает даже при всех
            # зависимостях на месте, тогда ставит rpm.
            code, out, err = run_cmd(f"dnf -y -q install {quoted}", timeout=300)
            if code != 0:
                code, out, err = run_cmd(f"rpm -U --replacepkgs {quoted}", timeout=120)
            if code != 0:
                log(f"Ошибка установки: {(err or out or '').strip()}")
                return False
        finally:
            run_cmd(f"rm -rf -- {shlex.quote(tmpdir)}", timeout=15)
        if self._installed_evr(run_cmd, package.name) != rpm.evr:
            log(f"После установки пакет {package.name} не найден")
            return False
        log(f"Пакет {package.name} {rpm.version} установлен ({rpm.evr})")
        return True

    # --- Проверка ----------------------------------------------------------

    def check(self, device, run_cmd):
        svc = "vnc"
        output_lines: List[str] = []
        log = output_lines.append
        port = int(device.vnc_port or self.parameters.vnc_port or 5900)
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        # После сбоя служба перезапускается через несколько секунд: ждём до 10 с.
        for _ in range(5):
            if self._local_abort.wait(2):
                self._report(device, svc, RESULT_ABORT, "Прервано")
                return
            code, out, _ = run_cmd(
                f"ss -tlnp 2>/dev/null | grep ':{port} ' | grep -w {self.NAME}",
                timeout=15,
            )
            if code == 0 and (out or "").strip():
                log(f"Порт {port} слушается - общий экран {self.TITLE} готов")
                self.context.log(
                    device.host, svc, f"{self.label} установлен и готов к работе"
                )
                self._report(device, svc, RESULT_SUCCESS, "\n".join(output_lines))
                return
        cause = self._report_not_listening(device, run_cmd, port, log)
        self.context.log(
            device.host,
            svc,
            f"{self.TITLE}: порт {port} не открылся"
            + (f". {cause}" if cause else "")
            + (f" {self.FAILURE_HINT}" if self.FAILURE_HINT else ""),
        )
        self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))

    def _report_not_listening(self, device, run_cmd, port, log: Callable) -> str:
        """Записать подробности в результат, вернуть короткую причину для журнала."""
        _, state, _ = run_cmd(f"systemctl is-active {self.unit}", timeout=15)
        log(
            f"{self.TITLE} не открыл порт {port} "
            f"(служба: {(state or 'неизвестно').strip()})"
        )
        _, journal, _ = run_cmd(
            f"journalctl -u {self.unit} -n 15 --no-pager -q -o cat 2>/dev/null",
            timeout=15,
        )
        cause = ""
        if journal:
            log(f"--- журнал службы {self.unit} ---")
            log(journal[-1500:])
            # Последняя строка с ошибкой сервера: её видно в журнале установки.
            for line in reversed(journal.splitlines()):
                # Строки самого systemd («Failed with result…») причину не объясняют.
                if self.unit in line or "systemd" in line:
                    continue
                if _ERROR_LINE.search(line):
                    cause = f"Сервер сообщил: {line.strip()[:200]}"
                    break
        if self.LOG_FILE:
            _, tail, _ = run_cmd(
                f"tail -n 20 {shlex.quote(self.LOG_FILE)} 2>/dev/null", timeout=15
            )
            if tail:
                log(f"--- {self.LOG_FILE} ---")
                log(tail)
        if self.FAILURE_HINT:
            log(self.FAILURE_HINT)
        return cause
