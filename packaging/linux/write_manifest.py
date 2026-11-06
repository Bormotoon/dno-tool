"""Записать версии, хеши и параметры фактически собранного окружения."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def command(*args: str) -> str:
    """Прочитать версию инструмента с проверкой успешного завершения."""
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def main() -> None:
    """Сохранить манифест вместе с wheels и полным pip freeze."""
    root = Path("/opt/manifest")
    wheels = {}
    for path in sorted(Path("/opt/dno/wheels").glob("*.whl")):
        with path.open("rb") as stream:
            wheels[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    import av

    data = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "glibc": command("getconf", "GNU_LIBC_VERSION"),
        "native_packages": Path("/opt/native-packages.txt").read_text(),
        "cmake": command("cmake", "--version").splitlines()[0],
        "ninja": command("ninja", "--version"),
        "ffmpeg": av.ffmpeg_version_info,
        "pyav": av.__version__,
        "ffmpeg_abi": av.library_versions,
        "qt": command(sys.executable, "-c", "from PySide6.QtCore import qVersion; print(qVersion())"),
        "jobs": os.environ.get("BUILD_JOBS", "4"),
        "sources": {path.stem: json.loads(path.read_text()) for path in root.glob("*-source.json")},
        "wheels_sha256": wheels,
        "pip_freeze": (root / "requirements.lock").read_text(),
        "debian_packages": command("dpkg-query", "-W", "-f=${Package}=${Version}\n"),
        "recipe_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in Path("/opt/recipe").iterdir() if path.is_file()},
    }
    (root / "environment-manifest.json").write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
