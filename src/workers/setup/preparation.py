"""Operator-side preparation, separate from wizard widgets and SSH installers."""

import shutil
from PySide6.QtCore import QThread, Signal


def prepare_veyon_keys(service, name):
    import re

    if service is None or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}", name or ""
    ):
        raise ValueError("Выберите корректный ключ Veyon на локальном шаге")
    try:
        private = service.read_auth_key(name, "private")
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"Закрытый ключ Veyon «{name}» отсутствует или недоступен для чтения. "
            "Установка Veyon невозможна; проверьте существующий ключ и права доступа."
        ) from error
    try:
        public = service.read_auth_key(name, "public")
    except FileNotFoundError:
        if not service.extract_public_key(name):
            raise RuntimeError("Не удалось извлечь открытый ключ Veyon из существующего закрытого")
        public = service.read_auth_key(name, "public")
    return public, private


def install_local_krfb_tools(packages, status):
    from src.utils.system.subprocess_env import run_elevated

    manager = next(
        (shutil.which(name) for name in ("dnf", "apt-get") if shutil.which(name)), None
    )
    if manager is None:
        return False, "Не найден dnf/apt-get"
    status("Установка локальных пакетов: " + ", ".join(packages))
    return run_elevated([manager, "install", "-y", *packages], timeout=900)


class OperatorPreparationWorker(QThread):
    ready = Signal(object, str)
    status = Signal(str)
    service_failed = Signal(str, str)

    def __init__(self, plan, service, local_packages=(), parent=None):
        super().__init__(parent)
        self.plan, self.service = plan, service
        self.local_packages = tuple(local_packages)

    def run(self):
        from dataclasses import replace

        try:
            plan = self.plan
            if self.local_packages:
                ok, error = install_local_krfb_tools(
                    self.local_packages, self.status.emit
                )
                if not ok:
                    self.status.emit(
                        "Локальные пакеты KRFB не установлены; удалённая установка продолжится."
                    )
            if self.isInterruptionRequested():
                return
            if plan.services.get("veyon"):
                try:
                    public, private = prepare_veyon_keys(self.service, plan.veyon_key_name)
                    plan = replace(plan, veyon_pem=public, veyon_private_key=private)
                except Exception as error:
                    self.service_failed.emit("veyon", str(error))
                    plan = replace(plan, services={**plan.services, "veyon": False})
            if plan.services.get("vnc") and plan.vnc_server == "krfb":
                from src.services.remote.krfb_service import KRFBService
                import sys

                if sys.platform != "win32" and shutil.which("kwriteconfig5"):
                    ok, _ = KRFBService.apply_password_local(plan.vnc_password)
                    if not ok:
                        self.status.emit(
                            "Не удалось применить пароль локального KRFB; удалённая установка продолжится."
                        )
            if not self.isInterruptionRequested():
                self.ready.emit(plan, "")
        except Exception as error:
            if not self.isInterruptionRequested():
                self.ready.emit(None, str(error))
