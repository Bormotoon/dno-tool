"""Собрать расширения conda/source PyAV без импорта DLL в изолированном сканере."""

from pathlib import Path

from PyInstaller.utils.hooks import get_package_paths

_, package = get_package_paths("av")
root = Path(package)
hiddenimports = ["fractions", "dataclasses", "uuid"]
for path in root.rglob("*"):
    if path.suffix not in (".py", ".pyd", ".so"):
        continue
    parts = list(path.relative_to(root).parts)
    parts[-1] = parts[-1].split(".")[0]
    if parts[-1] == "__init__":
        parts.pop()
    hiddenimports.append(".".join(["av", *parts]))
