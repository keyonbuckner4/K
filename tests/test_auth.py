"""Offline signature tests. A throwaway RSA key is generated; nothing touches the network."""

import base64
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot import auth


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def signer(key):
    return auth.KalshiSigner("test-key-id", key)


def test_header_names_and_shapes(signer):
    h = signer.headers("GET", "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance")
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
    assert h["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert re.fullmatch(r"\d{13}", h["KALSHI-ACCESS-TIMESTAMP"]), "timestamp must be unix milliseconds"
    raw = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"], validate=True)
    assert len(raw) == 256, "2048-bit RSA signature is 256 bytes"


def test_signature_verifies_against_timestamp_method_path(signer):
    ts = 1_700_000_000_000
    sig = signer.sign(ts, "get", "/trade-api/v2/portfolio/balance")
    pub = signer.public_key()
    assert auth.verify(pub, sig, ts, "GET", "/trade-api/v2/portfolio/balance")
    assert not auth.verify(pub, sig, ts + 1, "GET", "/trade-api/v2/portfolio/balance")
    assert not auth.verify(pub, sig, ts, "POST", "/trade-api/v2/portfolio/balance")
    assert not auth.verify(pub, sig, ts, "GET", "/trade-api/v2/portfolio/positions")


def test_query_string_is_excluded_from_signed_path(signer):
    ts = 1_700_000_000_000
    with_query = signer.headers("GET", "https://x.test/trade-api/v2/markets?limit=5&status=open", timestamp_ms=ts)
    pub = signer.public_key()
    assert auth.verify(pub, with_query["KALSHI-ACCESS-SIGNATURE"], ts, "GET", "/trade-api/v2/markets")
    assert not auth.verify(pub, with_query["KALSHI-ACCESS-SIGNATURE"], ts, "GET", "/trade-api/v2/markets?limit=5&status=open")


def test_signing_path_variants():
    assert auth.signing_path("https://h/trade-api/v2/markets?limit=5") == "/trade-api/v2/markets"
    assert auth.signing_path("/trade-api/v2/markets?limit=5#frag") == "/trade-api/v2/markets"
    assert auth.signing_path("trade-api/v2/markets") == "/trade-api/v2/markets"
    assert auth.signing_path("wss://h/trade-api/ws/v2") == "/trade-api/ws/v2"


def test_message_format():
    assert auth.message_to_sign(123, "post", "/trade-api/v2/portfolio/events/orders") == b"123POST/trade-api/v2/portfolio/events/orders"


def test_load_private_key_from_pem(tmp_path, key):
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    p = tmp_path / "k.pem"
    p.write_bytes(pem)
    s = auth.KalshiSigner.from_pem_file("kid", p)
    assert s.key_id == "kid"
    ts = 1
    assert auth.verify(s.public_key(), s.sign(ts, "GET", "/a"), ts, "GET", "/a")


def test_pss_signatures_are_randomized_but_both_verify(signer):
    ts = 5
    a = signer.sign(ts, "GET", "/x")
    b = signer.sign(ts, "GET", "/x")
    assert a != b
    assert auth.verify(signer.public_key(), a, ts, "GET", "/x")
    assert auth.verify(signer.public_key(), b, ts, "GET", "/x")


def test_empty_key_id_rejected(key):
    with pytest.raises(ValueError):
        auth.KalshiSigner("", key)
