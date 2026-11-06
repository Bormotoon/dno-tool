"""Атомарная публикация файла с явной политикой перезаписи."""

from __future__ import annotations

import os
from pathlib import Path


class AtomicOutput:
    @staticmethod
    def publish(staging: str, target: Path, overwrite: bool) -> None:
        if overwrite:
            os.replace(staging, target)
        elif os.name == "nt":
            os.rename(staging, target)
        else:
            import ctypes
            import errno

            library = ctypes.CDLL(None, use_errno=True)
            rename = getattr(library, "renameat2", None)
            if rename is not None:
                rename.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                rename.restype = ctypes.c_int
                if (
                    rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1)
                    == 0
                ):
                    return
                error = ctypes.get_errno()
                if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                    raise OSError(error, os.strerror(error), str(target))
            os.link(staging, target)
            Path(staging).unlink()
