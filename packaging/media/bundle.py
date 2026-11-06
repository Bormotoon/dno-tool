"""Общий комплект FFmpeg для Qt и расширений PyAV в PyInstaller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import PySide6


def media_files() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    pyside = Path(PySide6.__file__).resolve().parent
    root = pyside if os.name == "nt" else pyside / "Qt/lib"
    manifest = root / "media-runtime.json"
    if not manifest.is_file():
        raise RuntimeError(
            "Перед сборкой подготовьте полный общий runtime PyAV/Qt и его манифест."
        )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    libraries = dict(data["libraries"])
    if os.name != "nt":
        libraries.update(data["xkb"]["libraries"])
    binaries = [(str(root / name), ".") for name in libraries]
    datas = [(str(manifest), ".")]
    if (root / "media-licenses").is_dir():
        datas.append((str(root / "media-licenses"), "media-licenses"))
    if (root / "media-fonts").is_dir():
        datas.append((str(root / "media-fonts"), "media-fonts"))
    return binaries, datas


def unify_media_binaries(
    binaries: list[tuple[str, str, str]],
    *,
    include_media: bool = False,
) -> list[tuple[str, str, str]]:
    source_files, _ = media_files()
    libraries = {Path(path).name: path for path, _ in source_files}
    result = []
    for destination, source, kind in binaries:
        name = Path(destination).name
        if name in libraries:
            continue
        if "av.libs" in destination:
            raise RuntimeError(
                "В пакет пытаются попасть дублирующие библиотеки PyAV wheel."
            )
        result.append((destination, source, kind))
    if include_media:
        result.extend((name, source, "BINARY") for name, source in libraries.items())
    return result


def clean_media_aliases(
    datas: list[tuple[str, str, str]],
    *,
    keep_resources: bool = False,
) -> list[tuple[str, str, str]]:
    """Analysis может положить прежние SONAME-ссылки также в таблицу данных."""
    files, _ = media_files()
    names = {Path(path).name for path, _ in files}
    return [
        entry
        for entry in datas
        if Path(entry[0]).name not in names
        and (
            keep_resources
            or (
                Path(entry[0]).name != "media-runtime.json"
                and not entry[0]
                .replace("\\", "/")
                .startswith(("media-licenses/", "media-fonts/"))
            )
        )
    ]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_manifest() -> None:
    import PySide6

    pyside = Path(PySide6.__file__).resolve().parent
    root = pyside if os.name == "nt" else pyside / "Qt/lib"
    handle = os.add_dll_directory(str(root)) if os.name == "nt" else None
    import av

    names = (
        "avcodec",
        "avformat",
        "avutil",
        "avfilter",
        "avdevice",
        "swresample",
        "swscale",
    )
    libraries = []
    for name in names:
        files = (
            list(root.glob(name + "-*.dll"))
            if os.name == "nt"
            else [
                p
                for p in root.glob("lib" + name + ".so.*")
                if p.name.split(".so.", 1)[1].isdigit()
            ]
        )
        if len(files) != 1:
            raise RuntimeError(f"Неоднозначный состав поставки: {name}")
        libraries.append(files[0])
    manifest_file = root / "media-runtime.json"
    old = (
        json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest_file.exists()
        else {}
    )
    if os.name != "nt":
        # Манифест создаётся после strip и других преобразований сборочного окружения.
        xkb = old["xkb"]
        if set(xkb["libraries"]) != {"libxkbcommon.so.0", "libxkbcommon-x11.so.0"}:
            raise RuntimeError("Не записана согласованная пара XKB из SDK.")
        xkb["libraries"] = {name: digest(root / name) for name in xkb["libraries"]}
    encoders = []
    for name in sorted(av.codecs_available):
        try:
            if av.Codec(name, "w").is_encoder:
                encoders.append(name)
        except ValueError:
            pass
    package = Path(av.__file__).resolve().parent
    extensions = {
        p.relative_to(package).as_posix(): digest(p)
        for p in sorted(package.rglob("*"))
        if p.suffix in (".so", ".pyd")
    }
    manifest = {
        **old,
        "schema": 1,
        "pyav": av.__version__,
        "ffmpeg": av.ffmpeg_version_info,
        "abi": av.library_versions,
        "libraries": {p.name: digest(p) for p in libraries},
        "pyav_extensions": extensions,
        "encoders": encoders,
        "filters": sorted(av.filter.filters_available),
        "formats": sorted(av.formats_available),
        "license": old.get("license", "FFmpeg GPL-3.0-or-later; PyAV BSD-3-Clause"),
    }
    if os.name != "nt":
        records = list(Path("/opt/manifest").glob("*.lock")) + list(
            Path("/opt/manifest").glob("*packages*.txt")
        )
        native_lock = Path("/opt/native-packages.txt")
        if native_lock.exists():
            records.append(native_lock)
        manifest["package_locks"] = {
            p.name: p.read_text(encoding="utf-8") for p in records
        }
        manifest["sources"] = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(Path("/opt/manifest").glob("*-source.json"))
        ]
    manifest["assets"] = {
        path.relative_to(root).as_posix(): digest(path)
        for group in ("media-fonts", "media-licenses")
        if (root / group).exists()
        for path in sorted((root / group).rglob("*"))
        if path.is_file()
    }
    manifest_file.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Манифест: PyAV {av.__version__}, FFmpeg {av.ffmpeg_version_info}, {len(libraries)} библиотек, {len(extensions)} расширений."
    )
    if handle is not None:
        handle.close()


def stage(destination: Path) -> None:
    binaries, resources = media_files()
    destination.mkdir(parents=True, exist_ok=True)
    for source, _ in binaries + resources:
        path = Path(source)
        target = destination / path.name
        if path.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)
    manifest = json.loads(
        (destination / "media-runtime.json").read_text(encoding="utf-8")
    )
    # Устаревшие версии библиотек не должны сосуществовать с текущими SONAME/DLL.
    for pattern in ("av*.dll", "sw*.dll", "libav*.so.*", "libsw*.so.*"):
        for path in destination.glob(pattern):
            if path.name not in manifest["libraries"]:
                path.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("manifest", "stage"))
    parser.add_argument("destination", type=Path, nargs="?")
    args = parser.parse_args()
    if args.command == "manifest":
        write_manifest()
    elif args.destination is not None:
        stage(args.destination)
    else:
        parser.error("destination is required")
