from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sys
import threading

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.padding import PKCS7

from src.logger import logger
from src.utils.filesystem.fs_utils import ensure_file_user_owned

# Legacy v1 константы (только для чтения старых конфигов и миграции в v2)
_LEGACY_SECRET = b"dno-tool-credential-bundles-2024"
_LEGACY_KEY = hashlib.sha256(_LEGACY_SECRET).digest()[:16]
_LEGACY_IV = hashlib.sha256(_LEGACY_SECRET + b"iv").digest()[:16]

_V2_PREFIX = "v2:"
_NONCE_LEN = 12
_KEY_LEN = 32

_key_lock = threading.Lock()
_cached_master_key: bytes | None = None


def _key_file_path() -> Path:
    return Path.home() / ".dno-tool" / ".master.key"


if sys.platform == "win32":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def _dpapi_protect(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()
        if not _crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
        ):
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)

    def _dpapi_unprotect(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
        ):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)
else:
    def _dpapi_protect(data: bytes) -> bytes:
        return data

    def _dpapi_unprotect(data: bytes) -> bytes:
        return data


def _load_or_create_master_key() -> bytes:
    global _cached_master_key
    with _key_lock:
        if _cached_master_key is not None:
            return _cached_master_key

        path = _key_file_path()
        if path.is_file():
            try:
                raw = base64.b64decode(path.read_bytes().strip(), validate=True)
                if sys.platform == "win32":
                    key = _dpapi_unprotect(raw)
                else:
                    key = raw
                if len(key) == _KEY_LEN:
                    _cached_master_key = key
                    return key
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не удалось прочитать локальный мастер-ключ ({exc}), создаётся новый")

        key = os.urandom(_KEY_LEN)
        stored = _dpapi_protect(key) if sys.platform == "win32" else key
        encoded = base64.b64encode(stored)

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
            ensure_file_user_owned(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(path))

        _cached_master_key = key
        return key


def encrypt_password(plaintext: str) -> str:
    """Зашифровать пароль с использованием AES-256-GCM и случайного nonce."""
    if not plaintext:
        return ""
    key = _load_or_create_master_key()
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = base64.b64encode(nonce + ct).decode("ascii")
    return f"{_V2_PREFIX}{payload}"


def _decrypt_legacy_v1(ciphertext_b64: str, *, strict: bool = False) -> str:
    ct = base64.b64decode(ciphertext_b64, validate=strict)
    cipher = Cipher(algorithms.AES(_LEGACY_KEY), modes.CBC(_LEGACY_IV))
    dec = cipher.decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8")


def decrypt_password(ciphertext_b64: str, *, strict: bool = False) -> str:
    """Расшифровать пароль (поддерживает новый формат v2 AES-GCM и старый v1 AES-CBC)."""
    if not ciphertext_b64:
        return ""
    try:
        if ciphertext_b64.startswith(_V2_PREFIX):
            raw = base64.b64decode(ciphertext_b64[len(_V2_PREFIX):], validate=strict)
            if len(raw) <= _NONCE_LEN:
                raise ValueError("Слишком короткий шифротекст v2")
            nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
            key = _load_or_create_master_key()
            plain = AESGCM(key).decrypt(nonce, ct, None)
            return plain.decode("utf-8")
        return _decrypt_legacy_v1(ciphertext_b64, strict=strict)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise ValueError("Не удалось расшифровать сохранённый секрет") from exc
        logger.warning(f"Ошибка расшифровки пароля: {exc}")
        return ""
