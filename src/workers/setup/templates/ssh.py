"""Remote ssh scripts and templates."""

AUTHORIZED_KEYS_SCRIPT = r"""
import os
import pathlib
import sys
import tempfile

directory = pathlib.Path.home() / ".ssh"
directory.mkdir(mode=0o700, exist_ok=True)
directory.chmod(0o700)
target = directory / "authorized_keys"
old = target.read_text(encoding="utf-8") if target.exists() else ""
key = sys.stdin.read().strip()
identity = key.split()[:2]
if len(identity) != 2 or not identity[0].startswith(("ssh-", "ecdsa-")):
    raise ValueError("Invalid public key")
present = False
for line in old.splitlines():
    fields = line.split()
    for index in range(len(fields) - 1):
        if fields[index:index+2] == identity:
            present = True
            break
if not present:
    fd, temporary = tempfile.mkstemp(prefix=".dno-key-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(old)
            if old and not old.endswith("\n"):
                stream.write("\n")
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
target.chmod(0o600)
print("PRESENT" if present else "ADDED")
"""
