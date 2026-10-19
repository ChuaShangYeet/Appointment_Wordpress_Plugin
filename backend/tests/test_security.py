"""Crypto, HMAC and JWT unit tests (no database)."""
from __future__ import annotations

import time
import uuid

import pytest

from app.core.exceptions import Unauthorized
from app.core.security import (
    decode_jwt,
    decrypt_field,
    encrypt_field,
    generate_api_key,
    hash_api_key,
    issue_jwt,
    sign_payload,
    verify_signature,
)


def test_pdpa_roundtrip_and_ciphertext_is_not_plaintext() -> None:
    tid = uuid.uuid4()
    blob = encrypt_field("diabetic, allergic to latex", tid)
    assert b"diabetic" not in blob
    assert decrypt_field(blob, tid) == "diabetic, allergic to latex"
    assert encrypt_field("x", tid) != encrypt_field("x", tid)  # random nonce


def test_ciphertext_is_bound_to_its_tenant() -> None:
    blob = encrypt_field("secret", uuid.uuid4())
    with pytest.raises(Exception):  # cryptography.exceptions.InvalidTag
        decrypt_field(blob, uuid.uuid4())


def test_api_key_hashing() -> None:
    plain, prefix, digest = generate_api_key("publishable")
    assert plain.startswith("pk_") and prefix == plain[:8] and digest == hash_api_key(plain)
    assert generate_api_key("secret")[0].startswith("sk_")


def test_hmac_signature_accepts_valid_and_rejects_tampered_or_stale() -> None:
    body, ts = b'{"event":"site.updated"}', str(int(time.time()))
    sig = sign_payload("s3cret", ts, body)
    verify_signature("s3cret", ts, body, sig)
    with pytest.raises(Unauthorized):
        verify_signature("s3cret", ts, body + b" ", sig)
    with pytest.raises(Unauthorized):
        verify_signature("other", ts, body, sig)
    old = str(int(time.time()) - 3600)
    with pytest.raises(Unauthorized):
        verify_signature("s3cret", old, body, sign_payload("s3cret", old, body))


def test_jwt_roundtrip_and_tamper_detection() -> None:
    tid = uuid.uuid4()
    claims = decode_jwt(issue_jwt(tenant_id=tid, subject="u1", role="manager"))
    assert claims["tid"] == str(tid) and claims["role"] == "manager"
    with pytest.raises(Unauthorized):
        decode_jwt(issue_jwt(tenant_id=tid, subject="u1", role="manager")[:-2] + "xx")
