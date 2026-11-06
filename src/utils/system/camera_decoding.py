"""Выбор устройства просмотра и фиксация режима на время жизни приложения."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CameraDecoding:
    requested: str
    device_type: str = ""
    device: str | None = None

    @property
    def warning(self) -> str:
        """Сообщение при отсутствии подходящего аппаратного устройства."""
        if self.requested == "gpu" and not self.device_type:
            return "GPU недоступен; просмотр продолжен на CPU"
        return ""


_decoding: CameraDecoding | None = None


def select_camera_decoding(mode: str) -> CameraDecoding:
    """Windows использует D3D11VA; Linux выбирает драйвер по оборудованию."""
    if mode == "cpu":
        return CameraDecoding(mode)
    if sys.platform == "win32":
        return CameraDecoding(mode, "d3d11va")
    if sys.platform == "linux":
        adapters: list[tuple[str, Path]] = []
        for render in sorted(Path("/sys/class/drm").glob("renderD*")):
            device = Path("/dev/dri") / render.name
            try:
                vendor = (render / "device/vendor").read_text().strip().lower()
            except OSError:
                continue
            adapters.append((vendor, device))
        if (
            any(vendor == "0x10de" for vendor, _ in adapters)
            or Path("/proc/driver/nvidia/version").is_file()
        ):
            return CameraDecoding(mode, "cuda")
        for vendor, device in adapters:
            if vendor in ("0x8086", "0x1002") and os.access(device, os.R_OK | os.W_OK):
                # VAAPI допускается только после проверки контекста в том же комплекте FFmpeg.
                try:
                    from src.utils.system.media_runtime import load_pyav

                    av = load_pyav()
                    av.codec.hwaccel.HWDevice("vaapi", device=str(device))
                except (ImportError, OSError, RuntimeError, ValueError):
                    continue
                return CameraDecoding(mode, "vaapi", str(device))
    return CameraDecoding(mode)


def camera_decoding() -> CameraDecoding:
    """Снимок до первого плеера; сохранение настроек его не меняет."""
    global _decoding
    if _decoding is None:
        from src.config import config

        _decoding = select_camera_decoding(config.app.camera.preview_decoding)
    return _decoding


class CameraDecoder:
    """После отказа GPU все переподключения этой камеры используют CPU."""

    def __init__(self, policy: CameraDecoding | None = None) -> None:
        self.policy = policy or camera_decoding()
        self.warning = self.policy.warning
        self.failed = False

    def acceleration(self) -> Any:
        """Не выгружать аппаратные кадры до отбора планировщиком показа."""
        if self.failed or not self.policy.device_type:
            return None
        from src.utils.system.media_runtime import load_pyav

        av = load_pyav()
        return av.codec.hwaccel.HWAccel(
            self.policy.device_type,
            device=self.policy.device,
            allow_software_fallback=False,
            is_hw_owned=True,
        )

    def fallback(self) -> None:
        """Зафиксировать отказ без сохранения URL, секретов и текста драйвера."""
        self.failed = True
        self.warning = "GPU недоступен; просмотр продолжен на CPU"
