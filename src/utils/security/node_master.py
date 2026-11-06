"""Служебный мастер-ключ федерации нод (Ed25519, без Qt).

Открытая часть лежит в файле ``master-key.pub`` в корне проекта;
файл не хранится в git и пакуется в корень бинарника при сборке, если есть
на машине сборки.
Нет файла, он пуст или повреждён — механизм выключен: нода не принимает
подпись мастер-ключом, а закрытая часть не загружается.

Владелец закрытой части подключается к любой ноде, даже после перегенерации
её ключа доступа, и снимает запрет выполнения команд. Программа закрытую
часть не сохраняет: оператор загружает её из файла, и она живёт в памяти до
выхода. Ключ нигде не показывается и не журналируется.

Подпись привязана к открытому ключу TLS сервера: перехватчик с другим
сертификатом не может переслать её настоящей ноде.
"""

from __future__ import annotations

import base64
import hmac
import sys
import threading
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from src.logger import logger

from .node_crypto import public_key_from_base64, public_key_to_base64

# Файл открытой части. Замена ключа в нём делает недействительными все
# выданные закрытые части.
PUBLIC_KEY_FILE = "master-key.pub"
# Файл — одна строка base64, допускаются строки-комментарии с «#».
_FILE_LIMIT = 4096

DISABLED_MESSAGE = "Эта сборка программы не поддерживает служебный ключ"

_MASTER_DOMAIN = b"dno-node-master-v1\0"

_lock = threading.Lock()
_loaded = False
_public_key: tuple[str, Ed25519PublicKey] | None = None


def _candidates() -> list[Path]:
    # Корень проекта; в сборке PyInstaller это корень распаковки.
    paths = [Path(__file__).resolve().parents[3] / PUBLIC_KEY_FILE]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        paths.append(Path(bundle) / PUBLIC_KEY_FILE)
    return paths


def _read_public_key() -> tuple[str, Ed25519PublicKey] | None:
    for path in _candidates():
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read(_FILE_LIMIT + 1)
        except (OSError, UnicodeDecodeError):
            logger.error("Служебный ключ: файл открытой части не читается, механизм выключен")
            return None
        lines = [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            logger.info("Служебный ключ: файл открытой части пуст, механизм выключен")
            return None
        if len(text) > _FILE_LIMIT or len(lines) != 1:
            logger.error("Служебный ключ: неверный формат открытой части, механизм выключен")
            return None
        try:
            key = public_key_from_base64(lines[0])
        except ValueError:
            logger.error("Служебный ключ: неверный формат открытой части, механизм выключен")
            return None
        return public_key_to_base64(key), key
    logger.info("Служебный ключ: открытой части нет в сборке, механизм выключен")
    return None


def _master_public() -> tuple[str, Ed25519PublicKey] | None:
    """Открытая часть (base64, объект) или None; файл читается один раз."""
    global _loaded, _public_key
    if not _loaded:
        with _lock:
            if not _loaded:
                _public_key = _read_public_key()
                _loaded = True
    return _public_key


def master_key_enabled() -> bool:
    """True, если в сборке есть действительная открытая часть мастер-ключа."""
    return _master_public() is not None


def load_master_key(text: str) -> Ed25519PrivateKey:
    """Разобрать закрытую часть и проверить, что она парна открытой.

    Raises:
        ValueError: механизм выключен, формат не распознан или ключ не
            служебный. Текст ошибки не содержит ключа.
    """
    public = _master_public()
    if public is None:
        raise ValueError(DISABLED_MESSAGE)
    value = "".join((text or "").split())
    try:
        raw = base64.b64decode(value, validate=True)
        key = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        raise ValueError("Файл не содержит служебного ключа") from None
    if not hmac.compare_digest(public_key_to_base64(key.public_key()), public[0]):
        raise ValueError("Ключ не является служебным ключом этой программы")
    return key


def _message(server_pubkey_b64: str, challenge: bytes) -> bytes:
    return _MASTER_DOMAIN + base64.b64decode(server_pubkey_b64, validate=True) + challenge


def sign_master_challenge(
    key: Ed25519PrivateKey, server_pubkey_b64: str, challenge: bytes
) -> str:
    """Подписать challenge сервера мастер-ключом (hex)."""
    return key.sign(_message(server_pubkey_b64, challenge)).hex()


def verify_master_challenge(
    server_pubkey_b64: str, challenge: bytes, signature_hex: str
) -> bool:
    """Проверить подпись открытой частью мастер-ключа; без неё — всегда False."""
    public = _master_public()
    if public is None:
        return False
    try:
        public[1].verify(
            bytes.fromhex(signature_hex or ""), _message(server_pubkey_b64, challenge)
        )
        return True
    except Exception:
        return False


def _reset_for_tests(public_key_b64: str | None = None) -> None:
    """Тест-хук: перечитать файл (None) или подставить открытую часть ("" — выключить)."""
    global _loaded, _public_key
    with _lock:
        if public_key_b64 is None:
            _loaded, _public_key = False, None
        elif not public_key_b64:
            _loaded, _public_key = True, None
        else:
            key = public_key_from_base64(public_key_b64)
            _loaded, _public_key = True, (public_key_to_base64(key), key)
