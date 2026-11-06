"""Сохранить общий FFmpeg/PyAV до удаления SDK мультимедийного окружения."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

QT_LIBRARIES = ("libQt6Multimedia.so.6", "libQt6MultimediaWidgets.so.6")
XKB_LIBRARIES = ("libxkbcommon.so.0", "libxkbcommon-x11.so.0")
DEPENDENCY_RE = re.compile(r"(?:=>\s+|^\s*)(/\S+)\s+\(")


def copy_file(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source.resolve(), destination)
    return destination


def library_dependencies(roots: list[Path], prefix: Path, qt_lib: Path) -> set[Path]:
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = f"{qt_lib}:{prefix / 'lib'}"
    pending = list(roots)
    visited: set[Path] = set()
    dependencies: set[Path] = set()
    while pending:
        binary = pending.pop()
        resolved_binary = binary.resolve()
        if resolved_binary in visited:
            continue
        visited.add(resolved_binary)
        result = subprocess.run(
            ["ldd", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        if result.returncode != 0 or "not found" in result.stdout:
            raise RuntimeError(
                f"Не удалось разрешить зависимости {binary}: {result.stdout}{result.stderr}"
            )
        for match in DEPENDENCY_RE.finditer(result.stdout):
            dependency = Path(match.group(1))
            try:
                dependency.resolve().relative_to(prefix)
            except ValueError:
                continue
            real_path = dependency.resolve()
            if real_path not in dependencies:
                dependencies.add(real_path)
                pending.append(dependency)
    return dependencies


def copy_runtime_libraries(
    dependencies: set[Path], prefix: Path, runtime: Path
) -> None:
    runtime_lib = runtime / "lib"
    runtime_lib.mkdir(parents=True, exist_ok=True)
    for source in sorted(dependencies):
        destination = runtime_lib / source.name
        shutil.copy2(source, destination)
        for alias in source.parent.iterdir():
            if not alias.is_symlink():
                continue
            if alias.resolve() != source:
                continue
            alias_target = runtime_lib / alias.name
            if alias_target.exists() or alias_target.is_symlink():
                continue
            alias_target.symlink_to(source.name)


def replace_qt(prefix: Path, runtime: Path, pyside: Path) -> None:
    qt_root = pyside / "Qt"
    qt_lib = qt_root / "lib"
    plugin_dir = qt_root / "plugins" / "multimedia"

    copied: list[Path] = []
    for name in QT_LIBRARIES:
        source = prefix / "lib" / name
        if not source.is_file():
            raise FileNotFoundError(f"В conda-наборе отсутствует {source}")
        copied.append(copy_file(source, qt_lib / name))

    for name in LIBRARIES:
        matches = [
            path
            for path in (prefix / "lib").glob(name + ".so.*")
            if path.is_file() and path.name.removeprefix(name + ".so.").isdigit()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Ожидалась одна SONAME-библиотека {name}, найдено: {matches}"
            )
        copied.append(copy_file(matches[0], qt_lib / matches[0].name))

    plugin_matches = list(prefix.glob("**/plugins/multimedia/libffmpegmediaplugin.so"))
    if len(plugin_matches) != 1:
        raise RuntimeError(
            f"Не удалось однозначно найти FFmpeg plugin Qt: {plugin_matches}"
        )
    copied.append(copy_file(plugin_matches[0], plugin_dir / plugin_matches[0].name))

    dependencies = library_dependencies(copied, prefix, qt_lib)
    copy_runtime_libraries(dependencies, prefix, runtime)
    print(
        "Общий Qt Multimedia установлен: "
        f"{len(copied)} файлов заменено, {len(dependencies)} runtime-библиотек сохранено"
    )


LIBRARIES = (
    "libavcodec",
    "libavformat",
    "libavutil",
    "libavfilter",
    "libavdevice",
    "libswresample",
    "libswscale",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--pyside", type=Path, required=True)
    parser.add_argument("--copy-pyav", action="store_true")
    parser.add_argument("--replace-qt", action="store_true")
    args = parser.parse_args()
    prefix, runtime, pyside = (
        args.prefix.resolve(),
        args.runtime.resolve(),
        args.pyside.resolve(),
    )
    if args.replace_qt:
        replace_qt(prefix, runtime, pyside)
    site = pyside.parent
    qt_lib = pyside / "Qt/lib"
    # X11 использует внутренние структуры core: обе библиотеки нужны из одного пакета.
    records = list((prefix / "conda-meta").glob("libxkbcommon-*.json"))
    if len(records) != 1:
        raise RuntimeError("Не найден однозначный исходный пакет libxkbcommon.")
    xkb_record = json.loads(records[0].read_text(encoding="utf-8"))
    xkb_files = []
    for name in XKB_LIBRARIES:
        if "lib/" + name not in xkb_record["files"]:
            raise RuntimeError(f"Библиотека {name} не принадлежит общему пакету XKB.")
        target = copy_file(prefix / "lib" / name, qt_lib / name)
        subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", str(target)], check=True)
        xkb_files.append(target)
    manifest_file = qt_lib / "media-runtime.json"
    manifest = (
        json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest_file.exists()
        else {}
    )
    manifest["xkb"] = {
        "version": xkb_record["version"],
        "build": xkb_record["build"],
        "libraries": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in xkb_files
        },
    }
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.copy_pyav:
        sources = list(
            prefix.glob(
                f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/av"
            )
        )
        if len(sources) != 1:
            raise RuntimeError("В исходном prefix не найден PyAV для текущего Python.")
        destination = site / "av"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(sources[0], destination)
    # Переименованные SONAME стандартного wheel не должны попасть в frozen-пакет.
    for name in ("av.libs", ".av.libs"):
        duplicate = site / name
        if duplicate.is_dir():
            shutil.rmtree(duplicate)
    copied = list(xkb_files)
    for name in LIBRARIES:
        matches = [
            p
            for p in (prefix / "lib").glob(name + ".so.*")
            if p.is_file() and p.name.removeprefix(name + ".so.").isdigit()
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Неоднозначный комплект {name}: {matches}")
        copied.append(copy_file(matches[0], qt_lib / matches[0].name))
    for extension in (site / "av").rglob("*.so"):
        relative = os.path.relpath(qt_lib, extension.parent)
        subprocess.run(
            ["patchelf", "--set-rpath", "$ORIGIN/" + relative, str(extension)],
            check=True,
        )
        copied.append(extension)
    dependencies = library_dependencies(copied, prefix, qt_lib)
    if runtime != prefix:
        copy_runtime_libraries(dependencies, prefix, runtime)
    font = prefix / "fonts/DejaVuSans.ttf"
    if font.is_file():
        copy_file(font, qt_lib / "media-fonts/DejaVuSans.ttf")
    notices = qt_lib / "media-licenses"
    notices.mkdir(exist_ok=True)
    for license_file in site.glob("av-*.dist-info/licenses/LICENSE*"):
        shutil.copy2(license_file, notices / ("PyAV-" + license_file.name))
    package_cache = Path(
        os.environ.get("CONDA_PKGS_DIRS", "/cache/conda-pkgs").split(os.pathsep)[0]
    )
    for pattern in (
        "**/font-ttf-dejavu*/info/licenses/*",
        "**/ffmpeg-9.0.2*/info/licenses/*",
    ):
        for license_file in package_cache.glob(pattern):
            if license_file.is_file():
                shutil.copy2(
                    license_file,
                    notices / (license_file.parents[2].name + "-" + license_file.name),
                )
    print(
        f"Сохранены общий FFmpeg, расширения PyAV и {len(dependencies)} зависимостей."
    )


if __name__ == "__main__":
    main()
