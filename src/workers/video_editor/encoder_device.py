"""Единые параметры GPU для пробы и рабочего кодирования."""

from __future__ import annotations

import os
import re


class EncoderDevice:
    @staticmethod
    def settings(codec: str, device: str = "") -> tuple[list[str], str]:
        if codec.endswith("_d3d12va"):
            if os.name != "nt":
                raise ValueError("Кодирование D3D12 доступно только на Windows.")
            if device:
                raise ValueError("Путь DRM-устройства не применяется к D3D12.")
            return [
                "-init_hw_device",
                "d3d12va=hw",
                "-filter_hw_device",
                "hw",
            ], "format=nv12,hwupload"
        if not codec.endswith(("_vaapi", "_qsv")):
            return [], ""
        if os.name == "nt":
            if device:
                raise ValueError("Путь DRM-устройства применяется только на Linux.")
            return [], ""
        path = device or "/dev/dri/renderD128"
        if not re.fullmatch(r"/dev/dri/renderD[0-9]+", path):
            raise ValueError("Укажите DRM render-устройство вида /dev/dri/renderD128.")
        if not os.path.exists(path) or not os.access(path, os.R_OK | os.W_OK):
            raise ValueError(
                "DRM-устройство отсутствует или недоступно для чтения/записи. Выберите доступное устройство или libx264."
            )
        if codec.endswith("_vaapi"):
            return ["-vaapi_device", path], "format=nv12,hwupload"
        return [
            "-init_hw_device",
            f"vaapi=va:{path}",
            "-init_hw_device",
            "qsv=hw@va",
            "-filter_hw_device",
            "hw",
        ], "format=nv12,hwupload=extra_hw_frames=64"
