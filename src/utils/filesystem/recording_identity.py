"""Признаки файла записи: не удалять чужой файл вместо своей пустой записи."""

from __future__ import annotations

import stat
from pathlib import Path


def recording_file_identity(path: str | Path | None) -> dict[str, int] | None:
    """Вернуть признаки обычного файла, не переходя по символической ссылке."""
    if path is None:
        return None
    try:
        info = Path(path).stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "modified_ns": info.st_mtime_ns,
    }
