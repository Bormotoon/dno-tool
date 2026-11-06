"""Save wizard settings, hosts and commands together with crash recovery.

Temporary files here provide atomic replacement and rollback of configuration
files; they do not store temporary SSH keys.
"""

import json
import os
from pathlib import Path
import tempfile
import uuid


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".dno-tx-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        from src.utils.filesystem.fs_utils import ensure_user_owned

        ensure_user_owned(name)
        os.replace(name, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _writer_alive(record):
    if not record.get("pid"):
        return False
    import psutil

    try:
        process = psutil.Process(record["pid"])
        return process.is_running() and process.create_time() == record.get(
            "process_started"
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def recover_file_transaction(journal, allowed_paths, *, allow_owned=False):
    """Restore an interrupted prepare, or clean a committed transaction.

    The journal contains paths only; backup content stays in mode-0600 files.
    Reject unexpected paths rather than using a modified journal as authority.
    """
    journal = Path(journal)
    if not journal.exists():
        return
    record = json.loads(journal.read_text(encoding="utf-8"))
    allowed = {str(Path(p).resolve()) for p in allowed_paths}
    if record.get("state") not in ("prepared", "committed"):
        raise ValueError("Unknown setup transaction state")
    if record["state"] == "prepared" and _writer_alive(record):
        if not allow_owned or record["pid"] != os.getpid():
            raise RuntimeError(
                "Сохранение настроек другим экземпляром ещё выполняется."
            )
    entries = record["files"]
    for entry in entries:
        target, backup = Path(entry["target"]), Path(entry["backup"])
        if (
            str(target.resolve()) not in allowed
            or backup.parent != target.parent
            or not backup.name.startswith(".dno-wizard-")
            or backup.is_symlink()
        ):
            raise ValueError("Invalid setup transaction path")
    if record["state"] == "prepared":
        for entry in entries:
            target, backup = Path(entry["target"]), Path(entry["backup"])
            if entry["existed"]:
                _atomic(target, backup.read_bytes())
            elif target.exists():
                target.unlink()
    # Deleting the journal first makes cleanup repeatable after a crash.
    journal.unlink()
    for entry in entries:
        Path(entry["backup"]).unlink(missing_ok=True)


def replace_file_set(payloads, journal, expected=None):
    """Commit bytes with rollback and a recovery journal. Caller holds locks."""
    payloads = {Path(p).resolve(): value for p, value in payloads.items()}
    journal = Path(journal)
    recover_file_transaction(journal, payloads, allow_owned=True)
    if expected is not None:
        for path in payloads:
            before = path.read_bytes() if path.exists() else None
            if before != expected[str(path)]:
                raise RuntimeError(
                    "Файлы изменились во время работы мастера. Откройте мастер заново."
                )
    entries = []
    prepared = False
    try:
        for path in payloads:
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = path.with_name(".dno-wizard-" + uuid.uuid4().hex + ".bak")
            existed = path.exists()
            _atomic(backup, path.read_bytes() if existed else b"")
            entries.append(dict(target=str(path), backup=str(backup), existed=existed))
        import psutil

        record = dict(
            state="prepared",
            files=entries,
            pid=os.getpid(),
            process_started=psutil.Process().create_time(),
        )
        _atomic(journal, json.dumps(record).encode("utf-8"))
        prepared = True
        for path, data in payloads.items():
            _atomic(path, data)
        record["state"] = "committed"
        _atomic(journal, json.dumps(record).encode("utf-8"))
    except Exception as failure:
        if prepared:
            try:
                recover_file_transaction(journal, payloads, allow_owned=True)
            except Exception as rollback:
                raise OSError(
                    f"Ошибка записи и восстановления; журнал: {journal}"
                ) from rollback
        else:
            for entry in entries:
                Path(entry["backup"]).unlink(missing_ok=True)
        raise failure
    # A cleanup failure does not turn a completed commit into a failed save.
    try:
        recover_file_transaction(journal, payloads, allow_owned=True)
    except OSError:
        pass
