"""Shared dnf/apt operations. A repository failure is never package absence."""

import re
import shlex
from src.workers.setup.types import parse_dnf_available, parse_apt_candidate


def validate_package_names(packages):
    for package in packages:
        if not isinstance(package, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9+_.:\-]*", package
        ):
            raise ValueError("Недопустимое имя пакета")


class PackageManager:
    def __init__(self, run, cancelled=None, manager=None):
        self.run, self.cancelled = run, cancelled
        self.name = manager

    def detect(self):
        if self.name is None:
            for executable, name in (("dnf", "dnf"), ("apt-get", "apt")):
                if self.run("command -v " + executable, timeout=15)[0] == 0:
                    self.name = name
                    break
        if self.name not in ("dnf", "apt"):
            raise RuntimeError("Не найден пакетный менеджер (dnf/apt-get)")
        return self.name

    def installed(self, package):
        validate_package_names((package,))
        if self.detect() == "dnf":
            code, _, _ = self.run("rpm -q " + shlex.quote(package), timeout=15)
            return code == 0
        code, out, _ = self.run(
            "dpkg-query -W -f='${Status}' " + shlex.quote(package), timeout=15
        )
        return code == 0 and out.strip() == "install ok installed"

    def available(self, packages):
        validate_package_names(packages)
        if not packages:
            return [], []
        if self.detect() == "dnf":
            if self.run("dnf -q makecache --refresh", timeout=60)[0]:
                raise RuntimeError("Не удалось обновить сведения репозитория dnf")
            code, out, error = self.run(
                "dnf -q --cacheonly list available " + shlex.join(packages), timeout=60
            )
            # dnf returns 1 for no match as well as some failures. Only its
            # explicit no-match diagnostic is an allowed negative response.
            if code and (
                code != 1 or "no matching packages" not in (error + out).lower()
            ):
                raise RuntimeError("Не удалось прочитать репозиторий dnf")
            found = parse_dnf_available(out, packages)
        else:
            found = set()
            for package in packages:
                if self.cancelled is not None and self.cancelled.is_set():
                    raise InterruptedError("Setup cancelled")
                code, out, _ = self.run(
                    "apt-cache policy " + shlex.quote(package), timeout=30
                )
                if code:
                    raise RuntimeError("Не удалось прочитать репозиторий apt")
                if parse_apt_candidate(out):
                    found.add(package)
        return (
            [p for p in packages if p in found],
            [p for p in packages if p not in found],
        )

    def install(self, packages):
        validate_package_names(packages)
        if self.cancelled is not None and self.cancelled.is_set():
            raise InterruptedError("Setup cancelled")
        command = "dnf" if self.detect() == "dnf" else "apt-get"
        return self.run(command + " install -y " + shlex.join(packages), timeout=180)
