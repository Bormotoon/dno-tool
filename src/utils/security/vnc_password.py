"""Encode the eight-byte password file consumed by x11vnc/LibVNCServer."""

from cryptography.hazmat.primitives.ciphers import Cipher, modes

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES


def encode_vnc_password_file(password: str) -> bytes:
    """Match rfbEncryptAndStorePasswd, including RFB DES bit ordering.

    Format: https://github.com/LibVNC/libvncserver/blob/master/src/common/vncauth.c
    Bit order: src/common/crypto_openssl.c in the same repository.
    This is reversible storage encoding, not protection for a secret.
    """
    fixed = bytes((23, 82, 107, 6, 35, 78, 88, 7))
    key = bytes(int(f"{value:08b}"[::-1], 2) for value in fixed)
    data = password.encode("utf-8").split(b"\0", 1)[0][:8].ljust(8, b"\0")
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()
