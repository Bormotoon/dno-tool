"""Отбор кодировщиков по оборудованию, доступному текущей системе."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.utils.system.subprocess_env import clean_env_kwargs

from .process_control import ProcessControl


@dataclass(frozen=True)
class GpuAdapter:
    vendor: str
    name: str
    device: str = ""


@dataclass(frozen=True)
class EncoderHardware:
    adapters: tuple[GpuAdapter, ...] = ()
    platform: str = sys.platform
    error: str = ""

    @staticmethod
    def is_hardware(codec: str) -> bool:
        return codec.endswith(
            (
                "_nvenc",
                "_amf",
                "_qsv",
                "_vaapi",
                "_videotoolbox",
                "_v4l2m2m",
                "_omx",
                "_mediacodec",
                "_d3d12va",
            )
        )

    def devices_for(self, codec: str, device: str | None = None) -> tuple[str, ...]:
        """None перечисляет устройства; пустая строка сохраняет выбор по умолчанию."""
        if not self.is_hardware(codec):
            return ("",)
        if codec.endswith(("_vaapi", "_qsv")) and self.platform == "linux":
            paths = tuple(
                adapter.device
                for adapter in self.adapters
                if adapter.device
                and (not codec.endswith("_qsv") or adapter.vendor == "intel")
            )
            if device is None:
                return paths
            return (device,) if (device or "/dev/dri/renderD128") in paths else ()
        vendors = {adapter.vendor for adapter in self.adapters}
        if codec.endswith("_nvenc") and "nvidia" in vendors:
            return ("",)
        if codec.endswith("_amf") and "amd" in vendors:
            return ("",)
        if codec.endswith("_qsv") and self.platform == "win32" and "intel" in vendors:
            return ("",)
        if codec.endswith("_d3d12va") and self.platform == "win32" and vendors:
            return ("",)
        return ()

    @classmethod
    def detect(cls, cancel: threading.Event) -> EncoderHardware:
        """Не подменять ошибку обнаружения перебором всех драйверов FFmpeg."""
        ProcessControl(cancel).check()
        try:
            if sys.platform == "win32":
                return cls(cls._windows(cancel), platform=sys.platform)
            if sys.platform == "linux":
                return cls(cls._linux(cancel), platform=sys.platform)
            return cls(
                platform=sys.platform,
                error="Обнаружение GPU на этой платформе не поддерживается.",
            )
        except InterruptedError:
            raise
        except (OSError, ValueError, TypeError, TimeoutError) as error:
            return cls(
                platform=sys.platform,
                error=f"Не удалось определить доступные GPU: {error}",
            )

    @staticmethod
    def _vendor(value: str) -> str:
        match = re.search(r"(?:VEN_|0x)([0-9a-f]{4})", value, re.IGNORECASE)
        return {"1002": "amd", "10de": "nvidia", "8086": "intel"}.get(
            match[1].lower() if match else "", ""
        )

    @classmethod
    def _windows(cls, cancel: threading.Event) -> tuple[GpuAdapter, ...]:
        """CIM включает и вторую видеокарту без подключённого монитора."""
        control = ProcessControl(cancel)
        command = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "@(Get-CimInstance Win32_VideoController -ErrorAction Stop | "
            "Where-Object { $_.ConfigManagerErrorCode -eq 0 -and $_.Status -eq 'OK' } | "
            "Select-Object Name,PNPDeviceID) | ConvertTo-Json -Compress"
        )
        with tempfile.TemporaryFile() as output:
            process = control.spawn(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                **clean_env_kwargs(),
            )
            try:
                deadline = time.monotonic() + 8
                while True:
                    control.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время получения списка видеокарт.")
                    try:
                        code = process.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if code != 0:
                    raise ValueError("Windows не предоставила список видеокарт.")
                output.seek(0)
                data = output.read(65537)
                if len(data) > 65536:
                    raise ValueError("Слишком большой список видеокарт.")
                return cls._windows_adapters(data.decode("utf-8-sig"))
            finally:
                control.stop(process)

    @classmethod
    def _windows_adapters(cls, text: str) -> tuple[GpuAdapter, ...]:
        records = json.loads(text) if text.strip() else []
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise TypeError("Некорректный список видеокарт Windows.")
        adapters = []
        for record in records:
            if not isinstance(record, dict):
                continue
            vendor = cls._vendor(str(record.get("PNPDeviceID", "")))
            if vendor:
                adapters.append(GpuAdapter(vendor, str(record.get("Name", vendor))))
        return tuple(adapters)

    @classmethod
    def _linux(
        cls,
        cancel: threading.Event,
        root: Path = Path("/sys/class/drm"),
        device_root: Path = Path("/dev/dri"),
    ) -> tuple[GpuAdapter, ...]:
        adapters = []
        for path in sorted(root.glob("renderD*")):
            ProcessControl(cancel).check()
            device = device_root / path.name
            if not device.exists() or not os.access(device, os.R_OK | os.W_OK):
                continue
            try:
                vendor = cls._vendor((path / "device/vendor").read_text().strip())
            except OSError:
                vendor = ""
            adapters.append(
                GpuAdapter(vendor, f"{vendor or 'GPU'} ({path.name})", str(device))
            )
        return tuple(adapters)
