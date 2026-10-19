"""API keys, HMAC request signing, JWTs and PDPA column encryption."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings
from app.core.exceptions import Unauthorized

ROLE_RANK = {"customer": 0, "staff": 1, "manager": 2, "owner": 3}


# --- API keys ---------------------------------------------------------------
def generate_api_key(kind: str) -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, sha256_hex)``; ``kind`` is publishable or secret.

    Keys carry 256 bits of entropy, so an unsalted SHA-256 is sufficient at rest.
    """
    prefix = "pk" if kind == "publishable" else "sk"
    plaintext = f"{prefix}_{secrets.token_urlsafe(32)}"
    return plaintext, plaintext[:8], hash_api_key(plaintext)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_otp() -> str:
    """A uniformly random 6-digit one-time code."""
    return f"{secrets.randbelow(10**6):06d}"


def generate_magic_token() -> str:
    """The secret in an emailed sign-in link: 256 random bits, URL-safe."""
    return secrets.token_urlsafe(32)


# --- HMAC (WordPress sync) ---------------------------------------------------
def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over ``"{timestamp}.{body}"``, hex encoded."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str) -> None:
    """Raise :class:`Unauthorized` on a bad signature or a stale timestamp (replay guard)."""
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError as exc:
        raise Unauthorized("Invalid timestamp") from exc
    if age > get_settings().sync_signature_tolerance_seconds:
        raise Unauthorized("Stale signature")
    if not hmac.compare_digest(sign_payload(secret, timestamp, body), signature):
        raise Unauthorized("Invalid signature")


# --- JWT ---------------------------------------------------------------------
def issue_jwt(*, tenant_id: uuid.UUID, subject: str, role: str, resource_id: uuid.UUID | None = None, email: str | None = None) -> str:
    """Issue a short-lived token. Roles: owner | manager | staff | customer.

    ``email`` (customers only) limits the session to the bookings made with that address: it is what an emailed sign-in link gives for an email nobody has confirmed."""
    s = get_settings()
    now = int(time.time())
    ttl = s.customer_jwt_ttl_seconds if role == "customer" else s.jwt_ttl_seconds
    claims: dict[str, Any] = {"sub": subject, "tid": str(tenant_id), "role": role, "iat": now, "exp": now + ttl}
    if resource_id:
        claims["rid"] = str(resource_id)
    if email:
        claims["em"] = email
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_jwt(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        # (PyJWT checks ``exp`` only if the token has one: a signed token without it would never expire)
        claims: dict[str, Any] = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "iat", "sub", "tid", "role"]})
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid or expired token") from exc
    if claims.get("role") not in ROLE_RANK:
        raise Unauthorized("Invalid role")
    return claims


# --- review invitation tokens ------------------------------------------------
# (bandit B105 takes "review" for a password: it is the name of a kind of token)
REVIEW_TOKEN_TYPE = "review"  # nosec B105
_BAD_REVIEW_LINK = "This review link is invalid or has expired"


def issue_review_token(*, tenant_id: uuid.UUID, booking_id: uuid.UUID) -> str:
    """The signed token in a completed-visit message's review link: it lets whoever holds the link review that one booking, once, until it expires.

    It carries no ``role``, so :func:`decode_jwt` refuses it as a login token, and :func:`decode_review_token` refuses every other kind of token.
    """
    s = get_settings()
    now = int(time.time())
    claims = {"typ": REVIEW_TOKEN_TYPE, "sub": str(booking_id), "tid": str(tenant_id), "iat": now, "exp": now + s.review_link_days * 86400}
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_review_token(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """``(tenant id, booking id)`` of a valid review token; :class:`Unauthorized` for anything else (expired, forged, or not a review token)."""
    s = get_settings()
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub", "tid", "typ"]})
        if claims["typ"] != REVIEW_TOKEN_TYPE:
            raise Unauthorized(_BAD_REVIEW_LINK)
        return uuid.UUID(claims["tid"]), uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise Unauthorized(_BAD_REVIEW_LINK) from exc


# --- PDPA column encryption (AES-256-GCM) -------------------------------------
def _key() -> bytes:
    key = base64.b64decode(get_settings().pdpa_encryption_key)
    if len(key) != 32:
        raise ValueError("PDPA_ENCRYPTION_KEY must decode to exactly 32 bytes (AES-256)")
    return key


def encrypt_field(plaintext: str, tenant_id: uuid.UUID) -> bytes:
    """AES-256-GCM; random 96-bit nonce prepended. The tenant id is bound as AAD so a
    ciphertext copied between tenants fails authentication."""
    nonce = os.urandom(12)
    return nonce + AESGCM(_key()).encrypt(nonce, plaintext.encode(), tenant_id.bytes)


def decrypt_field(blob: bytes, tenant_id: uuid.UUID) -> str:
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], tenant_id.bytes).decode()
