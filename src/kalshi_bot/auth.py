"""Kalshi request signing: RSA-PSS over ``{timestamp_ms}{METHOD}{path}``.

Facts this module encodes (confirmed against Kalshi's published SDK and independent clients):

* Headers: ``KALSHI-ACCESS-KEY`` (key id), ``KALSHI-ACCESS-TIMESTAMP`` (unix milliseconds),
  ``KALSHI-ACCESS-SIGNATURE`` (base64 RSA-PSS signature). There is no bearer token.
* Signed message is the millisecond timestamp, then the upper-cased HTTP method, then the URL
  path INCLUDING the ``/trade-api/v2`` prefix and EXCLUDING the query string.
* RSA-PSS with SHA-256, MGF1(SHA-256), salt length equal to the digest length (32 bytes).
* PSS is randomized, so two signatures of the same message differ. Verify, don't compare.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"

_PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    """Load a PEM private key (PKCS#8 or PKCS#1, unencrypted) from disk."""
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"{path} is not an RSA private key")
    return key


def signing_path(url_or_path: str) -> str:
    """Return only the path component: no scheme, host, query string, or fragment.

    ``https://host/trade-api/v2/markets?limit=5`` -> ``/trade-api/v2/markets``.
    """
    parts = urlsplit(url_or_path)
    path = parts.path if parts.scheme or parts.netloc else url_or_path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return path


def message_to_sign(timestamp_ms: int, method: str, path: str) -> bytes:
    return f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")


def now_ms() -> int:
    return int(time.time() * 1000)


class KalshiSigner:
    """Produces the three auth headers for a request. One instance per key."""

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey):
        if not key_id:
            raise ValueError("key_id is empty")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_pem_file(cls, key_id: str, path: str | Path) -> "KalshiSigner":
        return cls(key_id, load_private_key(path))

    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        sig = self._key.sign(message_to_sign(timestamp_ms, method, path), _PSS, hashes.SHA256())
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = now_ms() if timestamp_ms is None else timestamp_ms
        path = signing_path(url_or_path)
        return {
            HEADER_KEY: self.key_id,
            HEADER_TIMESTAMP: str(ts),
            HEADER_SIGNATURE: self.sign(ts, method, path),
        }

    def public_key(self) -> rsa.RSAPublicKey:
        return self._key.public_key()


def verify(public_key: rsa.RSAPublicKey, signature_b64: str, timestamp_ms: int, method: str, path: str) -> bool:
    """Verify a signature. Used by tests; Kalshi does the real verification server-side."""
    from cryptography.exceptions import InvalidSignature

    try:
        public_key.verify(base64.b64decode(signature_b64), message_to_sign(timestamp_ms, method, path), _PSS, hashes.SHA256())
        return True
    except InvalidSignature:
        return False
