"""HTTPS федерации: TLS 1.2+ с закреплением полного Ed25519-ключа пира.

Центр сертификации и имя хоста не задают доверие: оно задаётся сохранённым
ключом NodeRecord. Проверка сертификата выполняется до отправки HTTP-данных.
"""

from __future__ import annotations

import hmac
import ipaddress
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from requests.adapters import BaseAdapter, HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import SSLError

from .node_crypto import public_key_from_base64, public_key_to_base64


def node_base_url(host: str, port: int) -> str:
    """Сформировать HTTPS authority, включая литерал IPv6."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if (
        not host
        or any(char in host for char in "/?#@\\[]")
        or any(char.isspace() for char in host)
    ):
        raise ValueError("Ожидается имя хоста или IP без схемы и пути")
    if ":" in host:
        ipaddress.IPv6Address(host)
    authority = f"[{host}]" if ":" in host else host
    return f"https://{authority}:{port}"


class _RejectHTTP(BaseAdapter):
    def send(self, request, **kwargs):
        raise requests.exceptions.SSLError(
            "Нодам требуется HTTPS; обновите удалённый экземпляр"
        )

    def close(self) -> None:
        pass


class _PinnedAdapter(HTTPAdapter):
    def __init__(self, pubkey: str):
        expected = public_key_to_base64(public_key_from_base64(pubkey))

        class PinnedConnection(HTTPSConnection):
            def connect(self) -> None:
                super().connect()
                try:
                    cert = x509.load_der_x509_certificate(
                        self.sock.getpeercert(binary_form=True)
                    )
                    actual = public_key_to_base64(cert.public_key())
                    now = datetime.now(timezone.utc)
                    if not hmac.compare_digest(actual, expected):
                        raise ValueError(
                            "Ключ TLS не совпадает с доверенным ключом ноды"
                        )
                    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
                        raise ValueError("Истёк срок сертификата ноды")
                    self.is_verified = True
                except Exception as error:
                    self.close()
                    raise SSLError("Не удалось удостоверить TLS-сервер ноды") from error

        class PinnedPool(HTTPSConnectionPool):
            ConnectionCls = PinnedConnection

        self._pool_class = PinnedPool
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().init_poolmanager(*args, ssl_context=context, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"https": self._pool_class}

    def send(self, request, **kwargs):
        # PKI заменена обязательной проверкой полного ключа в connect().
        kwargs["verify"] = False
        kwargs["proxies"] = {}
        return super().send(request, **kwargs)


class NodeHTTPSession(requests.Session):
    """Изолированная сессия без HTTP, proxy из окружения и редиректов."""

    def __init__(self, pubkey: str):
        super().__init__()
        self.trust_env = False
        self.mount("https://", _PinnedAdapter(pubkey))
        self.mount("http://", _RejectHTTP())

    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def fetch_tls_pubkey(host: str, port: int, timeout: float = 5.0) -> str:
    """Прочитать открытый Ed25519-ключ сертификата ноды без HTTP-запроса.

    Только для первого подключения служебным мастер-ключом, когда ключ
    доступа неизвестен: результат закрепляется в NodeRecord, дальше все
    запросы идут через NodeHTTPSession с проверкой этого ключа.
    """
    import socket

    node_base_url(host, port)
    host = host.strip().strip("[]")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw) as tls:
            der = tls.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return public_key_to_base64(cert.public_key())


def load_node_tls(config, private_key: Ed25519PrivateKey) -> ssl.SSLContext:
    """Загрузить TLS в uvicorn и удалить временный PEM до запуска сервера."""
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dno-tool node")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .sign(private_key, algorithm=None)
    )
    with tempfile.TemporaryDirectory(prefix="dno-node-tls-") as directory:
        key_path = Path(directory) / "key.pem"
        cert_path = Path(directory) / "cert.pem"
        # Каталог tempfile закрыт для других пользователей на POSIX.
        key_path.touch(mode=0o600)
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        config.ssl_keyfile = str(key_path)
        config.ssl_certfile = str(cert_path)
        config.load()
        config.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
        return config.ssl


def create_node_tls_context(private_key: Ed25519PrivateKey) -> ssl.SSLContext:
    """Новый независимый контекст для безопасной смены ключа сервера."""

    class ContextLoader:
        def load(self) -> None:
            self.ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl.load_cert_chain(self.ssl_certfile, self.ssl_keyfile)

    return load_node_tls(ContextLoader(), private_key)
