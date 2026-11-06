"""Сертификаты встроенного веб-сервера: выпуск, проверка и подключение к uvicorn."""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TLS_MODES = ("off", "generated", "custom")
GENERATED_VALID_DAYS = 825
GENERATED_CERT_NAME = "server.crt"
GENERATED_KEY_NAME = "server.key"
MAX_NAMES = 64


class WebTlsError(ValueError):
    """Текст исключения показывается пользователю; секретов в нём нет."""


@dataclass(frozen=True)
class CertificateInfo:
    """Сведения для окна настроек."""

    subject: str
    names: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    fingerprint_sha256: str

    @property
    def expired(self) -> bool:
        """Сертификат вне срока действия браузер не примет без предупреждения."""
        now = datetime.now(timezone.utc)
        return now >= self.not_after or now < self.not_before


def generated_directory() -> Path:
    """Каталог выпущенного сертификата в профиле приложения."""
    from src.config.settings import LOGS_PATH

    return Path(LOGS_PATH) / "web-tls"


def default_names() -> list[str]:
    """Имена и адреса, по которым к серверу обычно обращаются."""
    names = ["localhost", "127.0.0.1", "::1"]
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""
    if hostname:
        names.append(hostname)
    try:
        import psutil

        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family != socket.AF_INET:
                    continue
                parsed = ipaddress.IPv4Address(address.address)
                if not parsed.is_loopback and not parsed.is_link_local:
                    names.append(str(parsed))
    except (ImportError, OSError, ValueError):
        pass
    return list(dict.fromkeys(names))


def parse_names(text: str) -> list[str]:
    """Разобрать список из поля настроек: запятые, пробелы и переводы строк."""
    if not isinstance(text, str):
        raise WebTlsError("Имена сертификата должны быть строкой")
    names = list(dict.fromkeys(text.replace(",", " ").split()))
    if not names:
        raise WebTlsError("Укажите хотя бы одно имя или адрес сервера")
    if len(names) > MAX_NAMES:
        raise WebTlsError(f"В сертификате может быть не больше {MAX_NAMES} имён")
    for name in names:
        _san_entry(name)
    return names


def _san_entry(name: str) -> x509.GeneralName:
    """IP-адрес и DNS-имя кодируются в SAN разными типами."""
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        pass
    try:
        encoded = name.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise WebTlsError(f"Недопустимое имя: {name}") from error
    labels = encoded.rstrip(".").split(".")
    valid = all(
        0 < len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and all(char.isascii() and (char.isalnum() or char in "-_") for char in label)
        for label in labels
    )
    if not valid or len(encoded) > 253:
        raise WebTlsError(f"Недопустимое имя: {name}")
    return x509.DNSName(encoded)


def _write_atomic(path: Path, data: bytes, *, private: bool) -> None:
    """Предыдущий файл остаётся целым при ошибке записи."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if private and os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            elif not private and os.name != "nt":
                os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def generate_certificate(
    names: list[str], directory: Path | None = None
) -> tuple[Path, Path]:
    """Выпустить самоподписанный сертификат; браузеры не принимают ключи Ed25519."""
    if not names:
        raise WebTlsError("Укажите хотя бы одно имя или адрес сервера")
    entries = [_san_entry(name) for name in names]
    target = directory if directory is not None else generated_directory()
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"DNO Tool {names[0]}"[:64])]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=GENERATED_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
        cert_path = target / GENERATED_CERT_NAME
        key_path = target / GENERATED_KEY_NAME
        # Ключ пишется первым: сертификат без своего ключа не должен казаться готовым.
        _write_atomic(
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            private=True,
        )
        _write_atomic(
            cert_path,
            certificate.public_bytes(serialization.Encoding.PEM),
            private=False,
        )
    except OSError as error:
        raise WebTlsError(f"Не удалось сохранить сертификат: {error}") from error
    return cert_path, key_path


def _reject_key_password() -> str:
    """Без колбэка OpenSSL спросил бы пароль в консоли и завис бы запуск."""
    raise WebTlsError("Ключ защищён паролем. Укажите ключ без пароля")


def inspect_certificate(cert_path: str | Path, key_path: str | Path) -> CertificateInfo:
    """Проверить, что пара загружается, и прочитать сведения о сертификате."""
    cert_file, key_file = Path(cert_path), Path(key_path)
    for label, path in (("сертификата", cert_file), ("ключа", key_file)):
        if not str(path) or not path.is_file():
            raise WebTlsError(f"Файл {label} не найден: {path}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert_file, key_file, password=_reject_key_password)
    except WebTlsError:
        raise
    except ssl.SSLError as error:
        raise WebTlsError(
            "Сертификат и ключ не подходят друг другу или повреждены "
            f"({error.reason or error.__class__.__name__})"
        ) from error
    except OSError as error:
        raise WebTlsError(f"Не удалось прочитать файлы: {error}") from error
    try:
        certificate = x509.load_pem_x509_certificate(cert_file.read_bytes())
    except (OSError, ValueError) as error:
        raise WebTlsError("Сертификат должен быть в формате PEM") from error
    names: list[str] = []
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        names += extension.value.get_values_for_type(x509.DNSName)
        names += [
            str(value)
            for value in extension.value.get_values_for_type(x509.IPAddress)
        ]
    except x509.ExtensionNotFound:
        pass
    return CertificateInfo(
        certificate.subject.rfc4514_string(),
        tuple(names),
        certificate.not_valid_before_utc,
        certificate.not_valid_after_utc,
        certificate.fingerprint(hashes.SHA256()).hex(),
    )


def resolve_paths(web: Any) -> tuple[Path, Path] | None:
    """Пути выбранного режима; `None` только для явно выключенного TLS."""
    mode = getattr(web, "tls_mode", "off")
    if mode == "off":
        return None
    if mode == "generated":
        directory = generated_directory()
        return directory / GENERATED_CERT_NAME, directory / GENERATED_KEY_NAME
    if mode == "custom":
        return Path(web.tls_cert_path or ""), Path(web.tls_key_path or "")
    # Неизвестный режим не должен тихо превращаться в HTTP.
    raise WebTlsError(f"Неизвестный режим HTTPS: {mode}")


def configure_uvicorn(cfg: Any, web: Any) -> bool:
    """Подключить TLS к uvicorn; ошибка останавливает запуск, HTTP не подставляется."""
    paths = resolve_paths(web)
    if paths is None:
        return False
    cert_path, key_path = paths
    if getattr(web, "tls_mode", "") == "generated" and not cert_path.is_file():
        raise WebTlsError(
            "Сертификат не выпущен. Откройте настройки веб-сервера и выпустите его"
        )
    # Проверка раньше config.load: зашифрованный ключ запросил бы пароль в консоли.
    inspect_certificate(cert_path, key_path)
    cfg.ssl_certfile = str(cert_path)
    cfg.ssl_keyfile = str(key_path)
    try:
        cfg.load()
    except (ssl.SSLError, OSError) as error:
        raise WebTlsError(f"Не удалось загрузить сертификат: {error}") from error
    cfg.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    return True
