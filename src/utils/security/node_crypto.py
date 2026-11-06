"""Криптография для федерации нод (Ed25519 + ключ доступа).

Каждый экземпляр dno-tool (нода) хранит в settings.json:

- служебную пару Ed25519 (``nodes.private_key``): ею подписан TLS-сертификат
  сервера нод, клиент сверяет открытую часть до отправки HTTP;
- секрет доступа (``nodes.access_secret``, 32 байта): кто его знает, тот
  получает доступ к ноде.

Доступ односторонний: оператор передаёт другим ключ доступа своей ноды
(``format_access_key``: открытый ключ TLS + секрет). Принимающей стороне
ничего не нужно знать о подключающихся. Перегенерация секрета отзывает
все выданные ключи. Секрет по сети не передаётся: клиент отвечает на
одноразовый challenge значением HMAC, привязанным к ключу TLS сервера.

Отпечаток ключа (SHA256[:8]) — для визуальной сверки операторами.
"""

import base64
import hashlib
import hmac
import secrets
from typing import Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Размер raw-ключей Ed25519
_KEY_RAW_LEN = 32


def generate_keypair() -> Tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Сгенерировать новую пару Ed25519."""
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


# ---------------------------------------------------------------------------
# Сериализация: raw 32 байта <-> base64
# ---------------------------------------------------------------------------


def private_key_to_base64(priv: Ed25519PrivateKey) -> str:
    """Приватный ключ как base64-строка (raw 32 байта)."""
    raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def private_key_from_base64(text: str) -> Ed25519PrivateKey:
    """Загрузить приватный ключ из base64-строки (raw 32 байта).

    Raises:
        ValueError: при неверном формате или длине.
    """
    try:
        raw = base64.b64decode((text or "").strip(), validate=True)
    except Exception as e:
        raise ValueError(f"Неверный base64 в приватном ключе: {e}") from e
    if len(raw) != _KEY_RAW_LEN:
        raise ValueError(
            f"Неверная длина приватного ключа: {len(raw)} (ожидается {_KEY_RAW_LEN})"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_to_base64(pub: Ed25519PublicKey) -> str:
    """Публичный ключ как base64-строка (raw 32 байта)."""
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def public_key_from_base64(text: str) -> Ed25519PublicKey:
    """Загрузить публичный ключ из base64-строки.

    Raises:
        ValueError: при неверном формате или длине.
    """
    try:
        raw = base64.b64decode((text or "").strip(), validate=True)
    except Exception as e:
        raise ValueError(f"Неверный base64 в публичном ключе: {e}") from e
    if len(raw) != _KEY_RAW_LEN:
        raise ValueError(
            f"Неверная длина публичного ключа: {len(raw)} (ожидается {_KEY_RAW_LEN})"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def fingerprint_from_pubkey(pubkey_b64: str) -> str:
    """Короткий отпечаток публичного ключа (SHA256, hex, первые 8 байт).

    Для отображения в UI: ``ab:12:cd:34:56:78:90:ef``.
    При ошибке разбора возвращает пустую строку.
    """
    try:
        pub = public_key_from_base64(pubkey_b64)
        raw = pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        digest = hashlib.sha256(raw).digest()
        return ":".join(f"{b:02x}" for b in digest[:8])
    except Exception:
        return ""


def sign_challenge(priv: Ed25519PrivateKey, challenge: bytes) -> str:
    """Подписать challenge приватным ключом. Возвращает hex-подпись."""
    return priv.sign(challenge).hex()


def verify_signature(
    pub: Ed25519PublicKey, challenge: bytes, signature_hex: str
) -> bool:
    """Проверить подпись challenge. True если подпись верна."""
    try:
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    try:
        pub.verify(sig, challenge)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ключ доступа ноды: открытый ключ TLS (32 байта) + секрет (32 байта)
# ---------------------------------------------------------------------------

_ACCESS_PROOF_DOMAIN = b"dno-node-access-v1\0"


def generate_access_secret() -> str:
    """Новый секрет доступа (base64, 32 случайных байта)."""
    return base64.b64encode(secrets.token_bytes(_KEY_RAW_LEN)).decode("ascii")


def _secret_bytes(secret_b64: str) -> bytes:
    try:
        raw = base64.b64decode((secret_b64 or "").strip(), validate=True)
    except Exception as e:
        raise ValueError("Неверный формат секрета доступа") from e
    if len(raw) != _KEY_RAW_LEN:
        raise ValueError("Неверная длина секрета доступа")
    return raw


def format_access_key(pubkey_b64: str, secret_b64: str) -> str:
    """Ключ доступа для передачи операторам других нод."""
    pub = public_key_from_base64(pubkey_b64).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    body = base64.urlsafe_b64encode(pub + _secret_bytes(secret_b64))
    return body.decode("ascii").rstrip("=")


def parse_access_key(text: str) -> Tuple[str, str]:
    """Разобрать ключ доступа: (открытый ключ TLS, секрет), оба base64.

    Raises:
        ValueError: ключ не распознан.
    """
    body = "".join((text or "").split())
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception as e:
        raise ValueError("Ключ доступа не распознан") from e
    if len(raw) != 2 * _KEY_RAW_LEN:
        raise ValueError("Ключ доступа не распознан")
    pubkey = base64.b64encode(raw[:_KEY_RAW_LEN]).decode("ascii")
    public_key_from_base64(pubkey)
    secret = base64.b64encode(raw[_KEY_RAW_LEN:]).decode("ascii")
    return pubkey, secret


def access_proof(secret_b64: str, server_pubkey_b64: str, challenge: bytes) -> str:
    """Ответ на challenge: HMAC-SHA256 секрета, привязанный к ключу сервера.

    Привязка к ключу TLS не даёт переслать ответ другому серверу.
    """
    message = (
        _ACCESS_PROOF_DOMAIN
        + base64.b64decode(server_pubkey_b64, validate=True)
        + challenge
    )
    return hmac.new(_secret_bytes(secret_b64), message, hashlib.sha256).hexdigest()


def verify_access_proof(
    secret_b64: str, server_pubkey_b64: str, challenge: bytes, proof_hex: str
) -> bool:
    """Проверить ответ клиента за постоянное время."""
    try:
        expected = access_proof(secret_b64, server_pubkey_b64, challenge)
    except Exception:
        return False
    return hmac.compare_digest(expected, (proof_hex or "").lower())
