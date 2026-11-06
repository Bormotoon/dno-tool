"""Названия устройств для выбора декодирования, без запуска внешних процессов."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def _read(path: Path) -> str:
    """Прочитать необязательное системное свойство."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _pci_name(vendor: str, device: str) -> str:
    """Получить модель из локальной базы PCI; сеть для подписей не нужна."""
    for path in (
        "/usr/share/hwdata/pci.ids",
        "/usr/share/misc/pci.ids",
        "/var/lib/pciutils/pci.ids",
    ):
        try:
            with Path(path).open(encoding="utf-8", errors="replace") as stream:
                selected = False
                for line in stream:
                    if line.startswith(vendor + " "):
                        selected = True
                    elif selected and line and line[0] not in "\t#\n":
                        break
                    elif selected and line.startswith("\t" + device + " "):
                        return line[5:].strip()
        except OSError:
            continue
    return {"10de": "NVIDIA GPU", "8086": "Intel GPU", "1002": "AMD GPU"}.get(
        vendor, "GPU"
    )


def _linux_names() -> tuple[str, str]:
    cpu = "CPU"
    for line in _read(Path("/proc/cpuinfo")).splitlines():
        key, _, value = line.partition(":")
        if key.strip() in ("model name", "Hardware", "Processor") and value.strip():
            cpu = value.strip()
            break
    adapters: list[tuple[int, str]] = []
    for render in sorted(Path("/sys/class/drm").glob("renderD*")):
        if not os.access(Path("/dev/dri") / render.name, os.R_OK | os.W_OK):
            continue
        vendor = _read(render / "device/vendor").removeprefix("0x")
        device = _read(render / "device/device").removeprefix("0x")
        if vendor not in ("10de", "8086", "1002"):
            continue
        name = _read(render / "device/product_name") or _pci_name(vendor, device)
        adapters.append((0 if vendor == "10de" else 1, name))
    if adapters:
        return min(adapters)[1], cpu
    for path in Path("/proc/driver/nvidia/gpus").glob("*/information"):
        for line in _read(path).splitlines():
            if line.startswith("Model:"):
                return line.partition(":")[2].strip(), cpu
    return "GPU не обнаружен", cpu


def _windows_names() -> tuple[str, str]:
    import ctypes
    import winreg
    from ctypes import wintypes

    class DisplayDevice(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("DeviceName", wintypes.WCHAR * 32),
            ("DeviceString", wintypes.WCHAR * 128),
            ("StateFlags", wintypes.DWORD),
            ("DeviceID", wintypes.WCHAR * 128),
            ("DeviceKey", wintypes.WCHAR * 128),
        ]

    cpu = "CPU"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        pass
    adapters: list[tuple[int, str]] = []
    enum = ctypes.windll.user32.EnumDisplayDevicesW
    enum.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(DisplayDevice),
        wintypes.DWORD,
    ]
    enum.restype = wintypes.BOOL
    for index in range(64):
        display = DisplayDevice()
        display.cb = ctypes.sizeof(display)
        if not enum(None, index, ctypes.byref(display), 0):
            break
        if display.StateFlags & 1 and not display.StateFlags & 8:
            adapters.append((0 if display.StateFlags & 4 else 1, display.DeviceString))
    return (min(adapters)[1] if adapters else "GPU не обнаружен"), cpu


@lru_cache(maxsize=1)
def camera_hardware_names() -> tuple[str, str]:
    """Вернуть GPU и CPU; читать системные данные в рабочем потоке."""
    if sys.platform == "win32":
        return _windows_names()
    if sys.platform == "linux":
        return _linux_names()
    return "GPU", "CPU"
