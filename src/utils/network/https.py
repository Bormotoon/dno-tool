"""Доверенные сертификаты для HTTPS в исходниках и автономных сборках."""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import certifi

_LINUX_CA_BUNDLES = (
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
)


def create_https_context() -> ssl.SSLContext:
    """Дополнить доверие ОС переносимым набором CA, сохранив проверку TLS."""
    context = ssl.create_default_context()
    if sys.platform.startswith("linux"):
        # OpenSSL из сборочного окружения может искать CA в отсутствующем /opt.
        for bundle in _LINUX_CA_BUNDLES:
            if bundle.is_file():
                context.load_verify_locations(cafile=str(bundle))
                break
    context.load_verify_locations(cafile=certifi.where())
    return context
