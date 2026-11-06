"""Проверка происхождения, ABI и идентичности единого комплекта библиотек."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.utils.system.media_runtime import (
    load_pyav,
    media_runtime_root,
    numeric_locale_is_c,
)

ABI = {
    "libavcodec": 63,
    "libavformat": 63,
    "libavutil": 61,
    "libavfilter": 12,
    "libavdevice": 63,
    "libswscale": 10,
    "libswresample": 7,
}
REQUIRED_FILTERS = frozenset(
    ("drawtext", "overlay", "showwavespic", "amix", "sidechaincompress", "alimiter")
)
REQUIRED_ENCODERS = frozenset(
    ("libx264", "libx265", "aac", "libmp3lame", "png", "mjpeg")
)


@dataclass(frozen=True)
class MediaCapabilities:
    identity: str
    version: str
    encoders: frozenset[str]
    filters: frozenset[str]
    formats: frozenset[str]
    manifest: str


def inspect_runtime() -> MediaCapabilities:
    """Проверяется в рабочем потоке до первой локальной медиоперации."""
    if not numeric_locale_is_c():
        raise RuntimeError(
            "Числовая локаль процесса не C: FFmpeg не разберёт дробные параметры. "
            "После создания QApplication нужен ensure_c_numeric_locale()."
        )
    root = media_runtime_root()
    manifest_path = root / "media-runtime.json"
    if not manifest_path.is_file():
        raise RuntimeError(
            "Полный комплект PyAV/FFmpeg не подготовлен. Нужна поставка приложения с манифестом media-runtime.json."
        )
    raw = manifest_path.read_bytes()
    manifest: dict[str, Any] = json.loads(raw)
    if manifest.get("schema") != 1:
        raise RuntimeError("Неизвестная версия манифеста медиабиблиотек.")
    libraries = manifest.get("libraries")
    if not isinstance(libraries, dict) or len(libraries) < len(ABI):
        raise RuntimeError("В манифесте отсутствует полный комплект FFmpeg.")
    for name, digest in libraries.items():
        path = root / name
        if path.resolve().parent != root.resolve() or not path.is_file():
            raise RuntimeError("В поставке отсутствует библиотека из манифеста.")
        with path.open("rb") as library:
            if hashlib.file_digest(library, "sha256").hexdigest() != digest:
                raise RuntimeError(
                    f"Контрольная сумма медиабиблиотеки {path.name} не совпадает с поставкой."
                )
    av = load_pyav()
    package_root = Path(av.__file__).resolve().parent
    for name, expected in manifest.get("pyav_extensions", {}).items():
        extension = package_root / name
        if (
            not extension.resolve().is_relative_to(package_root)
            or not extension.is_file()
        ):
            raise RuntimeError("В поставке отсутствует расширение PyAV из манифеста.")
        with extension.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise RuntimeError(
                    "Контрольная сумма расширения PyAV не совпадает с поставкой."
                )
    if av.__version__ != manifest.get("pyav") or av.ffmpeg_version_info != manifest.get(
        "ffmpeg"
    ):
        raise RuntimeError("Версии загруженных PyAV/FFmpeg не совпадают с манифестом.")
    if any(
        av.library_versions.get(name, (0,))[0] != major for name, major in ABI.items()
    ):
        raise RuntimeError("ABI медиабиблиотек несовместим с Qt и локальным движком.")
    filters = frozenset(av.filter.filters_available)
    encoders: set[str] = set()
    for name in av.codecs_available:
        try:
            if av.Codec(name, "w").is_encoder:
                encoders.add(name)
        except ValueError:
            continue
    missing = (REQUIRED_FILTERS - filters) | (REQUIRED_ENCODERS - encoders)
    if missing:
        raise RuntimeError(
            "Комплект FFmpeg неполон: " + ", ".join(sorted(missing)) + "."
        )
    # SONAME/DLL проверяются по фактически загруженным файлам, а не по версии.
    import psutil

    for module in psutil.Process().memory_maps():
        path = Path(module.path)
        if any(
            path.name.startswith((name, name.removeprefix("lib") + "-")) for name in ABI
        ) and (
            path.name not in libraries or path.resolve() != (root / path.name).resolve()
        ):
            raise RuntimeError(
                "PyAV и Qt загрузили разные комплекты FFmpeg. Перезапустите приложение после исправления поставки."
            )
    return MediaCapabilities(
        hashlib.sha256(raw).hexdigest(),
        f"PyAV {av.__version__}; FFmpeg {av.ffmpeg_version_info}",
        frozenset(encoders),
        filters,
        frozenset(av.formats_available),
        str(manifest_path),
    )
