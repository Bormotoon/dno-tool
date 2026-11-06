"""Independent installer using the operation context supplied by the worker."""

import shlex
from typing import List
from src.config import PACKAGE_POST_INSTALL
from src.workers.setup.types import (
    RESULT_SUCCESS,
    RESULT_ERROR,
    RESULT_IGNORE,
    RESULT_ABORT,
)
from src.workers.setup.phases import PhasedInstaller, PhaseResult


class PackagesInstaller(PhasedInstaller):
    service = "packages"
    recovery_limit = (
        "Установленные пакеты и выполненный post-install автоматически не откатываются."
    )

    def _apply_install(self, device, ssh, run, write, append):
        self._install_packages(device, run)

    def _check_repo_available(self, run_cmd, pkg_mgr, packages):
        """Разделить пакеты на доступные в репо и отсутствующие.

        Returns:
            (available, unavailable) - оба в исходном порядке.
        """
        from src.workers.setup.packages import PackageManager

        return PackageManager(run_cmd, self._local_abort, pkg_mgr).available(packages)

    def verify(self, request, applied):
        from src.workers.setup.packages import PackageManager

        manager = PackageManager(request.run, self._local_abort)
        absent = [
            p
            for p in getattr(self, "_expected_packages", ())
            if not manager.installed(p)
        ]
        if absent:
            return PhaseResult(RESULT_ERROR, "Не установились: " + ", ".join(absent))
        return applied

    def _post_install_packages(self, device, run_cmd, packages):
        for package in packages:
            command = PACKAGE_POST_INSTALL.get(package)
            if command:
                code, _, _ = run_cmd(command, timeout=60)
                if code:
                    self._report(
                        device,
                        "packages",
                        RESULT_ERROR,
                        f"Не удалось настроить установленный пакет {package}",
                    )
                    return False
        return True

    def _install_packages(self, device, run_cmd) -> None:
        """Установить дополнительные пакеты на хост (единая колонка «Пакеты»).

        Список пакетов ``self.parameters.extra_packages`` собирается мастером настройки
        (чекбоксы из ``EXTRA_PACKAGES`` + ручной ввод пользователя). Пакетный
        менеджер определяется на хосте (dnf → apt-get); проверка наличия —
        ``rpm -q`` (dnf) или ``dpkg -s`` (apt). Пакеты, отсутствующие в
        репозитории, пропускаются с предупреждением (не фатально).
        Недостающие доступные устанавливаются одной командой
        ``dnf/apt-get install -y``; при ее падении — добивка по одному пакету.
        """
        svc = "packages"
        output_lines: List[str] = []

        def log(msg: str):
            output_lines.append(msg)

        self.context.log(device.host, svc, "Начало установки пакетов")

        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return

        packages = self.parameters.extra_packages
        try:
            self._validate_package_names(packages)
        except ValueError:
            self._report(device, svc, RESULT_ERROR, "Недопустимое имя пакета")
            return
        if not packages:
            self.context.log(device.host, svc, "Пакеты не выбраны (пропуск)")
            self._report(device, svc, RESULT_IGNORE, "Пакеты не выбраны")
            return

        from src.workers.setup.packages import PackageManager

        manager = PackageManager(run_cmd, self._local_abort)
        try:
            pkg_mgr = manager.detect()
        except RuntimeError as error:
            self._report(device, svc, RESULT_ERROR, str(error))
            return
        log(f"Менеджер пакетов: {pkg_mgr}")

        # 1. Проверка наличия каждого пакета (rpm -q / dpkg -s).
        missing = []
        for pkg in packages:
            if self._local_abort.is_set():
                self._report(device, svc, RESULT_ABORT, "Прервано")
                return
            installed = manager.installed(pkg)
            if installed:
                log(f"{pkg} уже установлен")
            else:
                missing.append(pkg)

        self._expected_packages = tuple(packages)
        if not missing:
            if not self._post_install_packages(device, run_cmd, packages):
                return
            self.context.log(device.host, svc, "Все пакеты уже установлены")
            self._report(device, svc, RESULT_IGNORE, "\n".join(output_lines))
            return

        # 1b. Проверка наличия в репозитории: отсутствующие имена пропускаем
        # с предупреждением (не фатально), чтобы один неверный пакет не
        # ронял установку остальных.
        try:
            available, unavailable = (
                self._check_repo_available(run_cmd, pkg_mgr, missing)
                if missing
                else ([], [])
            )
        except RuntimeError as error:
            self._report(device, svc, RESULT_ERROR, str(error))
            return
        self._expected_packages = tuple(p for p in packages if p not in unavailable)
        for pkg in unavailable:
            log(f"{pkg} нет в репозитории - пропуск")
        if unavailable:
            self.context.log(
                device.host,
                svc,
                f"Пакеты: нет в репозитории: {', '.join(unavailable)}",
            )
        missing = available
        if not missing:
            if not self._post_install_packages(
                device, run_cmd, [p for p in packages if p not in unavailable]
            ):
                return
            log("Нет пакетов для установки")
            self._report(device, svc, RESULT_IGNORE, "\n".join(output_lines))
            return

        # 2. Установка недостающих пакетов (одной командой).
        pkgs_str = shlex.join(missing)
        log(f"Устанавливаю: {pkgs_str}")
        self.context.log(device.host, svc, f"Установка: {pkgs_str}")
        code, out, err = manager.install(missing)
        if code != 0:
            # Добивка по одному пакету: изолировать виновника и поставить
            # то, что ставится.
            log(f"Общая установка не удалась ({(err or out or '').strip()[:200]})")
            log("Пробую установить по одному пакету...")
            failed = []
            for pkg in list(missing):
                if self._local_abort.is_set():
                    self._report(device, svc, RESULT_ABORT, "Прервано")
                    return
                icode, iout, ierr = manager.install((pkg,))
                if icode != 0:
                    failed.append(pkg)
                    log(f"{pkg}: ошибка: {(ierr or iout or '').strip()[:200]}")
                else:
                    log(f"{pkg}: установлен")
            if failed:
                msg = f"Не установились: {', '.join(failed)}"
                log(msg)
                self.context.log(device.host, svc, f"Пакеты: {msg}")
                self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
                return
            log("Все пакеты установлены по одному")

        self.context.log(device.host, svc, f"Установлено: {', '.join(missing)}")

        if not self._post_install_packages(
            device, run_cmd, [p for p in packages if p not in unavailable]
        ):
            return

        self._report(device, svc, RESULT_SUCCESS, "\n".join(output_lines))
