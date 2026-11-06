"""Минимальное шифрование паролей для связок учётных данных.

AES-128-CBC с захардкоженным ключом и IV. Это НЕ криптографическая защита от
целевого атакующего — только обфускация, чтобы пароли не лежали в settings.json
открытым текстом. Защищает от случайного просмотра файла.

Ключ и IV захардкожены в коде (в т.ч. в собранном PyInstaller-бинаре). Для
настоящей защиты нужен master-password или OS keyring — см. TODO ниже.

TODO(security): заменить захардкоженный ключ на machine-bound key file
  (~/.dno-tool/secret.key, permissions 0600) или OS keyring (DPAPI /
  SecretService) после первой итерации. Текущая реализация — минимальный
  обфускатор, совместимый с требованием «захардкоженный ключ».
"""

import base64
import hashlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

# Захардкоженный 16-байтовый ключ (AES-128). Не меняйте после первого сохранения
# связок — иначе существующие пароли перестанут расшифровываться.
_HARDCODED_SECRET = b"dno-tool-credential-bundles-2024"

_KEY = hashlib.sha256(_HARDCODED_SECRET).digest()[:16]  # 16 байт для AES-128
_IV = hashlib.sha256(_HARDCODED_SECRET + b"iv").digest()[:16]  # 16 байт IV

_CIPHER = lambda: Cipher(algorithms.AES(_KEY), modes.CBC(_IV))


def encrypt_password(plaintext: str) -> str:
    """Зашифровать пароль → base64 ciphertext. Пустая строка → пустая строка."""
    if not plaintext:
        return ""
    try:
        padder = PKCS7(128).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        enc = _CIPHER().encryptor()
        ct = enc.update(padded) + enc.finalize()
        return base64.b64encode(ct).decode("ascii")
    except Exception:
        return ""


def decrypt_password(ciphertext_b64: str, *, strict: bool = False) -> str:
    """Расшифровать пароль; строгий режим явно сообщает о повреждении данных."""
    if not ciphertext_b64:
        if strict:
            raise ValueError("Не удалось расшифровать пароль")
        return ""
    try:
        ct = base64.b64decode(ciphertext_b64, validate=strict)
        dec = _CIPHER().decryptor()
        padded = dec.update(ct) + dec.finalize()
        unpadder = PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        return plain.decode("utf-8")
    except Exception as exc:
        if strict:
            raise ValueError("Не удалось расшифровать пароль") from exc
        # Невалидный ciphertext или повреждённые данные — возвращаем пусто.
        return ""
