"""kmsvnc: VNC-сервер, читающий кадр напрямую из DRM/KMS.

Пакета нет в репозиториях МОС: RPM лежит в ``assets/remote`` и передаётся
на хост по SSH. Пакет содержит только программу, служба и пароль — от
мастера, как у x11vnc. Сборка пакета: ``scripts/build_kmsvnc.ps1``
(``packaging/kmsvnc``), окружение МОС 12, пакет подходит МОС 12 и 13.
"""

import re
from dataclasses import dataclass
from typing import Optional

from src.workers.setup.installers.system_vnc import (
    BundledRpm,
    SystemVncBackend,
    VncPackage,
)
from src.workers.setup.templates import _KMSVNC_SERVICE_TEMPLATE

RPM = BundledRpm(
    file_name="kmsvnc-0.0.6-9.20240625git190db24.dno-rosa2021.1.x86_64.rpm",
    sha256="c39e03a6adb7615b27d30f3089c89f090d7681252dabea11885c0f8085486806",
    evr="0.0.6-9.20240625git190db24.dno",
)
RPM_ARCH = "x86_64"
# Старшая версия символов glibc в программе (objdump -T): пакет собран
# в окружении МОС 12 и ставится на МОС 12 и новее.
MIN_GLIBC = (2, 17)

PASSWORD_FILE = "/etc/kmsvnc/passwd"

INCOMPATIBLE_HINT = "Выберите x11vnc (сеанс X11) или KRFB (Wayland KDE)."


# Видеодрайверы, для которых в kmsvnc есть способ прочитать кадр
# (drm_vendors в drm.c). NVIDIA читает AB30/BLOCK_LINEAR через EGL DMA-BUF;
# импорт и чтение проверяются до открытия VNC-порта.
SUPPORTED_DRIVERS = ("i915", "amdgpu", "vmwgfx", "vboxvideo", "virtio_gpu", "nvidia")

# Видеокарты: имя, драйвер, подключён ли монитор, устойчивый путь
# (/dev/dri/by-path: номер cardN может смениться после перезагрузки).
_GPU_SCRIPT = (
    "for c in /sys/class/drm/card[0-9]*; do n=${c##*/}; "
    "case $n in *-*) continue;; esac; "
    'drv=$(basename "$(readlink -f $c/device/driver)" 2>/dev/null); '
    "con=0; for s in $c/$n-*/status; do "
    '[ "$(cat $s 2>/dev/null)" = connected ] && con=1; done; '
    "path=; for l in /dev/dri/by-path/*-card; do "
    '[ "$(readlink -f "$l")" = "/dev/dri/$n" ] && path=$l; done; '
    'echo "GPU=$n $drv $con ${path:-/dev/dri/$n}"; done; '
)


@dataclass(frozen=True)
class HostFacts:
    arch: str = ""
    glibc: Optional[tuple] = None
    has_rpm: bool = False
    # (имя, драйвер, монитор подключён, путь устройства)
    gpus: tuple = ()


@dataclass(frozen=True)
class HostCheck:
    ok: bool
    reason: str = ""
    # Видеокарта для kmsvnc (-d), если хост подходит.
    card: str = ""


def parse_host_facts(output: str) -> HostFacts:
    """Разобрать вывод проверки хоста."""
    arch, glibc, has_rpm, gpus = "", None, False, []
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("ARCH="):
            arch = line[5:]
        elif line.startswith("GLIBC="):
            match = re.search(r"(\d+)\.(\d+)", line)
            if match:
                glibc = (int(match.group(1)), int(match.group(2)))
        elif line == "RPM=1":
            has_rpm = True
        elif line.startswith("GPU="):
            part = line[4:].split()
            if len(part) == 4:
                gpus.append((part[0], part[1], part[2] == "1", part[3]))
    return HostFacts(arch, glibc, has_rpm, tuple(gpus))


def choose_card(gpus) -> HostCheck:
    """Выбрать видеокарту с монитором, которую kmsvnc умеет читать."""
    if not gpus:
        return HostCheck(False, "На хосте не найдена видеокарта DRM")
    displaying = [gpu for gpu in gpus if gpu[2]] or list(gpus)
    for _name, driver, _connected, path in displaying:
        if driver in SUPPORTED_DRIVERS:
            return HostCheck(True, card=path)
    drivers = sorted({driver or "неизвестен" for _n, driver, _c, _p in displaying})
    return HostCheck(
        False, f"Видеодрайвер {', '.join(drivers)} kmsvnc не поддерживает"
    )


def inspect_host(run) -> HostCheck:
    """Подходит ли хосту kmsvnc: пакет, система, видеокарта."""
    code, out, _ = run(
        "echo ARCH=$(uname -m); echo GLIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null); "
        "command -v rpm >/dev/null 2>&1 && echo RPM=1; " + _GPU_SCRIPT + "true",
        timeout=15,
    )
    if code != 0:
        return HostCheck(False, "Не удалось определить систему хоста")
    facts = parse_host_facts(out)
    if not facts.has_rpm:
        return HostCheck(False, "kmsvnc поставляется пакетом RPM, а на хосте нет rpm")
    if facts.arch != RPM_ARCH:
        return HostCheck(
            False,
            f"Пакет kmsvnc собран для {RPM_ARCH}, на хосте {facts.arch or 'неизвестно'}",
        )
    if facts.glibc is None or facts.glibc < MIN_GLIBC:
        found = ".".join(map(str, facts.glibc)) if facts.glibc else "неизвестна"
        need = ".".join(map(str, MIN_GLIBC))
        return HostCheck(False, f"Для kmsvnc нужна glibc {need} и новее, на хосте {found}")
    return choose_card(facts.gpus)


class KmsvncBackend(SystemVncBackend):
    NAME = "kmsvnc"
    TITLE = "kmsvnc"
    PACKAGE = VncPackage(name="kmsvnc", binary="kmsvnc", bundled=RPM)
    UNIT_TEMPLATE = _KMSVNC_SERVICE_TEMPLATE
    # Без пароля kmsvnc принимал бы подключения без аутентификации.
    PASSWORD_REQUIRED = True
    # Видеокарта, выбранная проверкой хоста (host_problem).
    _card = ""
    FAILURE_HINT = (
        "kmsvnc читает экран из видеокарты: если формат кадра или драйвер "
        "не поддерживается, выберите x11vnc (X11) или KRFB (Wayland KDE)."
    )

    def password_content(self, password: str):
        # Простой текст, одна строка: kmsvnc берёт первые 8 символов.
        return password + "\n"

    def host_problem(self, run_cmd) -> Optional[str]:
        check = inspect_host(run_cmd)
        self._card = check.card
        return None if check.ok else f"{check.reason}. {INCOMPATIBLE_HINT}"

    def unit_text(self, port: int) -> str:
        # Видеокарта с монитором: по умолчанию kmsvnc берёт первую
        # /dev/dri/cardN, на машинах с двумя видеокартами это может быть не та.
        text = self.UNIT_TEMPLATE.format(port=port, card=self._card)
        return text if self._card else text.replace(" -d  ", " ")
