"""Установить фиксированный общий runtime PyAV/Qt без установки программ FFmpeg."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PYAV_PACKAGES = {
    (3, 12): (
        "av-19.0.2-py312hb2f1342_0.conda",
        "8f730b7c7c4efc661286c6a2a214744a0c43e46a06224140468554b7a218759e",
    ),
    (3, 13): (
        "av-19.0.2-py313h414490b_0.conda",
        "77bc179ed159f184a7759b73585d650b294eccc39fab374d700c9d15142ddd62",
    ),
    (3, 14): (
        "av-19.0.2-py314hc59ccdc_0.conda",
        "f90e80ca26934d7e01bc7421569e41edf13e6c50e207316cb741e16d93eeecd2",
    ),
    (3, 15): (
        "av-19.0.2-py315h98868ba_0.conda",
        "2d4652dc5b445bb51e14c02001be2f7c02b3789b1cd205ce0b2120e248c8ea4a",
    ),
}
FFMPEG_URL = (
    "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-full_build-shared.7z"
)
FFMPEG_SHA256 = "4d2060a8b34a940aa47d785142055bb92a63053781e55f2ace4546edd519a8f5"


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url: str, expected: str, cache: Path) -> Path:
    target = cache / url.rsplit("/", 1)[-1]
    if not target.is_file() or checksum(target) != expected:
        received = 0
        started = time.monotonic()

        def format_size(size: int) -> str:
            value = float(size)
            for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
                if value < 1024 or unit == "ГиБ":
                    return f"{value:.1f} {unit}"
                value /= 1024
            return f"{value:.1f} ГиБ"

        name = target.name
        print(f"Загрузка {name}", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as response, target.open(
                "wb"
            ) as output:
                raw_length = response.headers.get("Content-Length")
                try:
                    total = int(raw_length) if raw_length is not None else None
                    if total is not None and total <= 0:
                        total = None
                except ValueError:
                    total = None
                last_update = 0.0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    received += len(block)
                    now = time.monotonic()
                    if now - last_update >= 0.25:
                        elapsed = max(now - started, 0.001)
                        speed = received / elapsed
                        progress = (
                            f"{received * 100 / total:5.1f}% {format_size(received)} / {format_size(total)}"
                            if total is not None
                            else format_size(received)
                        )
                        print(
                            f"\r  {progress} · {format_size(int(speed))}/с",
                            end="",
                            flush=True,
                        )
                        last_update = now
            elapsed = max(time.monotonic() - started, 0.001)
            speed = received / elapsed
            progress = (
                f"100.0% {format_size(received)} / {format_size(total)}"
                if total is not None
                else format_size(received)
            )
            print(
                f"\r  {progress} · {format_size(int(speed))}/с — загрузка завершена",
                flush=True,
            )
        except Exception as exc:
            print(flush=True)
            raise RuntimeError(
                f"Не удалось загрузить {url}; получено {format_size(received)}: {exc}"
            ) from exc
    if checksum(target) != expected:
        raise RuntimeError(f"Контрольная сумма поставки не совпадает: {target.name}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(".cache/media-packages"))
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] not in PYAV_PACKAGES:
        raise RuntimeError("Требуется Windows x64 и обычный CPython 3.12–3.15.")
    import sysconfig

    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise RuntimeError("Комплект предназначен для CPython с GIL.")
    import PySide6

    site = Path(PySide6.__file__).resolve().parent.parent
    qt_root = site / "PySide6"
    args.cache.mkdir(parents=True, exist_ok=True)
    package, digest = PYAV_PACKAGES[sys.version_info[:2]]
    pyav_url = "https://conda.anaconda.org/conda-forge/win-64/" + package
    pyav_archive = download(pyav_url, digest, args.cache)
    ffmpeg_archive = download(FFMPEG_URL, FFMPEG_SHA256, args.cache)
    with tempfile.TemporaryDirectory(prefix="media-", dir=args.cache) as directory:
        staging = Path(directory)
        # Из поставки извлекаются исключительно библиотеки и лицензия.
        subprocess.run(
            [
                "tar",
                "-xf",
                str(ffmpeg_archive.resolve()),
                "-C",
                str(staging),
                "--strip-components=2",
                "ffmpeg-9.0.2-full_build-shared/bin/*.dll",
            ],
            check=True,
        )
        subprocess.run(
            [
                "tar",
                "-xf",
                str(ffmpeg_archive.resolve()),
                "-C",
                str(staging),
                "--strip-components=1",
                "ffmpeg-9.0.2-full_build-shared/LICENSE",
                "ffmpeg-9.0.2-full_build-shared/README.txt",
            ],
            check=True,
        )
        with zipfile.ZipFile(pyav_archive) as archive:
            name = next(
                n
                for n in archive.namelist()
                if n.startswith("pkg-") and n.endswith(".tar.zst")
            )
            compressed = staging / "pyav.tar.zst"
            compressed.write_bytes(archive.read(name))
        unpacked = staging / "pyav"
        unpacked.mkdir()
        subprocess.run(
            [
                "tar",
                "-xf",
                str(compressed),
                "-C",
                str(unpacked),
                "Lib/site-packages/av",
                "Lib/site-packages/av-19.0.2.dist-info",
            ],
            check=True,
        )
        libraries = sorted(staging.glob("*.dll"))
        if len(libraries) != 7:
            raise RuntimeError("Ожидались семь библиотек общего FFmpeg-комплекта.")
        # Резервная копия сохраняется один раз до согласованной замены всего набора.
        backup = args.cache / "original"
        for library in libraries:
            previous = qt_root / library.name
            if previous.is_file() and not (backup / library.name).exists():
                backup.mkdir(exist_ok=True)
                shutil.copy2(previous, backup / library.name)
            shutil.copy2(library, previous)
        for name in ("av", "av-19.0.2.dist-info"):
            shutil.copytree(
                unpacked / "Lib/site-packages" / name, site / name, dirs_exist_ok=True
            )
        # Старые abi3-расширения wheel имеют приоритет над conda cp3xx.
        for extension in (site / "av").rglob("*.pyd"):
            if not (
                unpacked / "Lib/site-packages" / extension.relative_to(site)
            ).exists():
                extension.unlink()
        for name in ("av.libs", ".av.libs"):
            duplicate = site / name
            if duplicate.is_dir():
                shutil.rmtree(duplicate)
        notices = qt_root / "media-licenses"
        notices.mkdir(exist_ok=True)
        for name in ("LICENSE", "README.txt"):
            shutil.copy2(staging / name, notices / name)
        for license_file in (site / "av-19.0.2.dist-info/licenses").glob("LICENSE*"):
            shutil.copy2(license_file, notices / ("PyAV-" + license_file.name))
        manifest = {
            "schema": 1,
            "pyav": "19.0.2",
            "ffmpeg": "9.0.2-full_build-www.gyan.dev",
            "sources": [
                {"url": pyav_url, "sha256": digest},
                {"url": FFMPEG_URL, "sha256": FFMPEG_SHA256},
            ],
            "license": "GPL-3.0-or-later; PyAV BSD-3-Clause",
            "libraries": {library.name: checksum(library) for library in libraries},
            "build_information": "media-licenses/README.txt",
        }
        (qt_root / "media-runtime.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        "Установлен единый комплект FFmpeg 9.0.2 и PyAV 19.0.2 для Qt и локального движка."
    )


if __name__ == "__main__":
    main()
