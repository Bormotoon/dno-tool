"""Раннее разрешение единого комплекта PyAV и Qt из каталога приложения."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_handles: list[Any] = []
_configured = False
_native_libraries: list[Any] = []


def media_runtime_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    import PySide6

    root = Path(PySide6.__file__).resolve().parent
    return root if os.name == "nt" else root / "Qt" / "lib"


def ensure_c_numeric_locale() -> None:
    """Вернуть числовую C-локаль после создания QApplication.

    В Linux Qt вызывает ``setlocale(LC_ALL, "")``. При ``ru_RU`` десятичным
    разделителем становится запятая, и FFmpeg через ``strtod`` перестаёт
    разбирать дробные параметры фильтров и кодировщиков (EINVAL). Вызывать в
    GUI-потоке до запуска рабочих потоков: ``setlocale`` действует на весь
    процесс. ``LC_CTYPE`` не меняется, кодировка вывода команд остаётся прежней.
    """
    import locale

    locale.setlocale(locale.LC_NUMERIC, "C")


def numeric_locale_is_c() -> bool:
    import locale

    return locale.localeconv()["decimal_point"] == "."


def configure_media_runtime() -> None:
    """Сохранить дескриптор каталога DLL на весь срок жизни приложения.

    В Linux RPATH расширений PyAV и плагина Qt задаётся при упаковке.
    Переименованные библиотеки стандартных wheels сюда не подмешиваются.
    """
    global _configured
    with _lock:
        if _configured:
            return
        if os.name == "nt":
            _handles.append(os.add_dll_directory(str(media_runtime_root())))
        elif getattr(sys, "frozen", False):
            import ctypes
            import json

            root = media_runtime_root()
            manifest = json.loads(
                (root / "media-runtime.json").read_text(encoding="utf-8")
            )
            # SONAME заранее загружаются из поставки, зависимости доступны в _MEIPASS.
            for name in (
                "avutil",
                "swresample",
                "swscale",
                "avcodec",
                "avformat",
                "avfilter",
                "avdevice",
            ):
                library = next(
                    item
                    for item in manifest["libraries"]
                    if item.startswith("lib" + name + ".so.")
                )
                _native_libraries.append(
                    ctypes.CDLL(str(root / library), mode=ctypes.RTLD_GLOBAL)
                )
        _configured = True


def load_pyav() -> Any:
    configure_media_runtime()
    import av

    return av
