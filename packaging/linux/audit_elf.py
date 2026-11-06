"""Проверить версии glibc приложения и всех ELF в каталоге lib/."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def required_versions(output: str) -> set[tuple[int, ...]]:
    """Читать только требования ELF, исключая экспортируемые версии символов."""
    in_needs = False
    found = set()
    for line in output.splitlines():
        if line.startswith("Version "):
            in_needs = line.startswith("Version needs section")
        if in_needs:
            for version in re.findall(r"Name: GLIBC_([^\s]+)", line):
                if re.fullmatch(r"\d+(?:\.\d+)+", version) is None:
                    raise ValueError(f"Неподдерживаемое требование: GLIBC_{version}")
                found.add(tuple(map(int, version.split("."))))
    return found


def inspect(
    path: Path, name: str, maximum: tuple[int, ...]
) -> dict[str, object] | None:
    """Проверить один файл через readelf в неизменённой локали C."""
    with path.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            return None
    result = subprocess.run(
        ["readelf", "--version-info", "--wide", str(path)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    versions = required_versions(result.stdout)
    high = max(versions, default=(0, 0))
    return {"name": name, "glibc": ".".join(map(str, high)), "ok": high <= maximum}


def audit(binary: Path, maximum: tuple[int, ...]) -> dict[str, object]:
    """Извлечь каждый элемент CArchive и проверить ELF вне зависимости от имени."""
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(binary))
    items = [inspect(binary, "<bootloader>", maximum)]
    with tempfile.TemporaryDirectory(prefix="dno-elf-") as folder:
        payload = Path(folder) / "payload"
        for name, entry in reader.toc.items():
            # Ссылки не содержат ELF; их целевые записи проверяются отдельно.
            if entry[-1] == "n":
                continue
            data = reader.extract(name)
            if data and data[:4] == b"\x7fELF":
                payload.write_bytes(data)
                items.append(inspect(payload, name, maximum))
    elfs = [item for item in items if item is not None]
    media_root = binary.parent / "lib"
    external_names = []
    for path in sorted(media_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.relative_to(media_root).as_posix()
        external_names.append(name)
        item = inspect(path, name, maximum)
        if item is not None:
            elfs.append(item)
    names = tuple(reader.toc) + tuple(external_names)
    resources = {
        "ffmpeg_plugin": any(
            "multimedia/" in name and "ffmpeg" in name and name.endswith(".so")
            for name in names
        ),
        "ffmpeg_libraries": all(
            any(lib in item["name"] for item in elfs)
            for lib in (
                "libavcodec.so",
                "libavformat.so",
                "libavutil.so",
                "libavfilter.so",
                "libavdevice.so",
                "libswresample.so",
                "libswscale.so",
            )
        ),
        "pyav_extensions": any(
            name.startswith("av/") and name.endswith(".so") for name in names
        ),
        "media_manifest": (media_root / "media-runtime.json").is_file(),
        "qtbase_ru": any(name.endswith("qtbase_ru.qm") for name in names),
    }
    high = max(
        (tuple(map(int, item["glibc"].split("."))) for item in elfs), default=(0, 0)
    )
    with binary.open("rb") as stream:
        sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "binary_sha256": sha256,
        "maximum_allowed": ".".join(map(str, maximum)),
        "maximum_required": ".".join(map(str, high)),
        "elf_count": len(elfs),
        "elf": elfs,
        "resources": resources,
        "ok": bool(elfs)
        and all(item["ok"] for item in elfs)
        and all(resources.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--maximum", default="2.33")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.binary, tuple(map(int, args.maximum.split("."))))
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"ELF: {report['elf_count']}, glibc: {report['maximum_required']}, результат: {report['ok']}"
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
