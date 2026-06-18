"""Cryptographic primitives for EduTokens (Ed25519).

This module contains only the **verification** side. Clients (frontend JS,
admin scripts) generate their own keys and sign transactions locally — the
blockchain never sees or generates private keys.

Usage::

    from shared.crypto import verify, pubkey_to_address

    ok = verify(student_pubkey_hex, tx_id_bytes, signature_hex)
    address = pubkey_to_address(student_pubkey_hex)  # → 24-char hex address
"""

from __future__ import annotations

import hashlib

# ---------------------------------------------------------------------------
# Ed25519 key lengths (hex-encoded)
# ---------------------------------------------------------------------------

ED25519_PUBKEY_HEX_LEN = 64   # 32 raw bytes → 64 hex chars
ED25519_SIG_HEX_LEN = 128     # 64 raw bytes → 128 hex chars


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """Verify an Ed25519 signature against a public key.

    Parameters
    ----------
    public_key_hex:
        64 hex chars representing a 32-byte Ed25519 public key.
    message:
        The exact bytes that were signed (typically ``tx_id.encode()``).
    signature_hex:
        128 hex chars representing a 64-byte Ed25519 signature.

    Returns
    -------
    bool
        ``True`` if the signature is valid for this public key and message.

    Raises
    ------
    ValueError
        If any hex argument is not the expected length or contains
        non-hex characters.
    """
    from cryptography.exceptions import InvalidSignature

    _validate_hex(public_key_hex, ED25519_PUBKEY_HEX_LEN, "public_key")
    _validate_hex(signature_hex, ED25519_SIG_HEX_LEN, "signature")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    except ValueError:
        return False  # invalid curve point → treat as failed verification

    sig = bytes.fromhex(signature_hex)

    try:
        pubkey.verify(sig, message)
        return True
    except InvalidSignature:
        return False


def pubkey_to_address(public_key_hex: str) -> str:
    """Derive a human-readable address from an Ed25519 public key.

    Computes ``SHA-256(public_key_raw)[:12]`` → 24 hex chars.

    The address is a deterministic, collision-resistant shorthand used
    as the Redis balance key and in API responses.

    **Address-space trade-off:**  Using only the first 12 bytes of the
    32-byte SHA-256 hash yields a 96-bit address.  By the birthday bound,
    an attacker would need to generate ~2^48 keys (≈ 281 trillion) to
    have a 50% chance of finding a collision.  This is acceptable for a
    proof-of-concept with < 10^6 students.

    **Production consideration:**  For production deployment, consider:
    - Using the **last** 20 bytes (like Ethereum addresses) → 160 bits,
      ``hexdigest()[-40:]``, for familiarity with existing tooling.
    - Using the full 32-byte SHA-256 output → 256 bits, eliminating the
      collision risk entirely at the cost of longer addresses.
    """
    _validate_hex(public_key_hex, ED25519_PUBKEY_HEX_LEN, "public_key")
    raw = bytes.fromhex(public_key_hex)
    return hashlib.sha256(raw).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_hex(value: str, expected_len: int, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a hex string, got {type(value).__name__}")
    if len(value) != expected_len:
        raise ValueError(
            f"{label} must be {expected_len} hex chars, got {len(value)}"
        )
    try:
        bytes.fromhex(value)
    except ValueError:
        raise ValueError(f"{label} contains non-hex characters") from None
