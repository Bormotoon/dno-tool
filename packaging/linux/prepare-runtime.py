"""Оставить установленное окружение упаковки без SDK и кэшей его сборки."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def strip_debug_symbols(roots: tuple[Path, ...]) -> None:
    """Убрать отладочные секции, сохранив динамические символы и загруженные файлы."""
    for root in roots:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            target = path.with_name(path.name + ".stripped")
            if target.exists():
                raise RuntimeError(f"Временный файл уже существует: {target}")
            subprocess.run(
                ["/usr/bin/strip", "--strip-debug", "-o", str(target), str(path)],
                check=True,
            )
            shutil.copystat(path, target)
            target.replace(path)


def main() -> None:
    """Сохранить Python, инструменты упаковки и замыкание нативных зависимостей."""
    native = Path("/opt/native")
    venv = Path("/opt/dno/.venv")
    stdlib = native / "lib/python3.15"
    qt_lib = venv / "lib/python3.15/site-packages/PySide6/Qt/lib"
    keep = {
        native / "bin" / name
        for name in ("python", "python3", "python3.15", "patchelf")
    }
    keep.update(native.glob("lib/libpython3.15.so*"))
    for directory in (
        stdlib,
        native / "ssl",
        native / "etc",
        native / "fonts",
        native / "share/fontconfig",
    ):
        if directory.exists():
            keep.update(
                path
                for path in directory.rglob("*")
                if path.is_file() or path.is_symlink()
            )
    # Базовый site-packages не используется виртуальным окружением приложения.
    keep = {
        path
        for path in keep
        if not path.is_relative_to(stdlib / "site-packages")
        and "__pycache__" not in path.parts
    }
    roots = set(keep)
    roots.update(
        path for path in venv.rglob("*") if path.is_file() and not path.is_symlink()
    )
    environment = dict(os.environ, LD_LIBRARY_PATH=f'{qt_lib}:{native / "lib"}')
    for path in sorted(roots):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        result = subprocess.run(
            ["ldd", str(path)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
            check=False,
        )
        for dependency in re.findall(
            r"(?:=>\s+|^\s*)(/\S+)", result.stdout, re.MULTILINE
        ):
            library = Path(os.path.normpath(dependency))
            if library.is_relative_to(native) and library.exists():
                keep.add(library)
                keep.add(library.resolve())
    # Сохраняем алиасы SONAME и symlink интерпретатора вместе с их целями.
    keep.update(
        path.resolve()
        for path in tuple(keep)
        if path.is_symlink() and path.exists() and path.resolve().is_relative_to(native)
    )
    for path in native.rglob("*"):
        if path.is_symlink() and path.exists() and path.resolve() in keep:
            keep.add(path)
    target = Path("/opt/native-runtime")
    if target.exists():
        raise RuntimeError(f"Временный каталог уже существует: {target}")
    target.mkdir()
    for path in sorted(keep):
        if not path.is_relative_to(native):
            raise RuntimeError(f"Зависимость за пределами окружения: {path}")
        destination = target / path.relative_to(native)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination, follow_symlinks=False)
    # OverlayFS не поддерживает rename каталога из нижнего слоя.
    shutil.rmtree(native)
    target.rename(native)
    for cache in ("/opt/dno/wheels", "/root/.cache", "/root/.conda"):
        shutil.rmtree(cache, ignore_errors=True)
    # Переменные кэшей в готовом образе не нужны; .pyc между сборками не накапливаем.
    for directory in venv.rglob("__pycache__"):
        shutil.rmtree(directory)
    # Готовые PyPI wheels уже обработаны auditwheel; повторный strip может нарушить ELF.
    packages = venv / "lib/python3.15/site-packages"
    strip_debug_symbols((native, packages / "PySide6", packages / "shiboken6"))
    print(
        f"Окружение упаковки подготовлено: {len(keep)} файлов Python и нативных зависимостей"
    )


if __name__ == "__main__":
    main()
