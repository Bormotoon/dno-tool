"""Разрешение выбранного семейства в конкретный файл в рабочем потоке."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from src.utils.system.subprocess_env import clean_env_kwargs


class VideoFontResolver:
    @staticmethod
    @lru_cache(maxsize=128)
    def resolve(family: str) -> str:
        if not family.strip():
            from .ffmpeg_utils import default_fontfile

            path = default_fontfile()
            if path and Path(path).is_file():
                return path
            raise ValueError(
                "Шрифт по умолчанию не найден. Выберите файл TTF/OTF для текста."
            )
        if os.name == "nt":
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
                    ) as key:
                        for index in range(winreg.QueryInfoKey(key)[1]):
                            name, filename, _ = winreg.EnumValue(key, index)
                            clean = name.rsplit(" (", 1)[0].casefold()
                            if family.casefold() in (clean, *clean.split(" & ")):
                                path = Path(filename)
                                if not path.is_absolute():
                                    path = (
                                        Path(os.environ.get("WINDIR", "C:/Windows"))
                                        / "Fonts"
                                        / path
                                    )
                                if path.is_file():
                                    return str(path)
                except OSError:
                    continue
        else:
            try:
                result = subprocess.run(
                    ["fc-match", "-f", "%{family}\n%{file}", family],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    **clean_env_kwargs(),
                )
                names, path = result.stdout.split("\n", 1)
                if (
                    result.returncode == 0
                    and family.casefold()
                    in {name.strip().casefold() for name in names.split(",")}
                    and Path(path).is_file()
                ):
                    return path
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        raise ValueError(
            f"Не удалось найти файл шрифта «{family}». Выберите TTF/OTF явно; замена не выполнена."
        )
