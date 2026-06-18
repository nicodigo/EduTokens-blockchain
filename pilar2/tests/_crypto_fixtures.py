"""Shared cryptographic fixtures for the test suite.

Import this from any test file that needs to create keypairs or sign
transactions.  All helpers use ``cryptography`` lazily so the module
is importable even without it installed (useful for syntax-check gates).
"""

from __future__ import annotations


def make_keypair() -> tuple[str, str, str]:
    """Generate a fresh Ed25519 keypair.

    Returns ``(private_hex, public_hex, address)``.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    from shared.crypto import pubkey_to_address

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_hex = private_key.private_bytes_raw().hex()
    pub_hex = public_key.public_bytes_raw().hex()
    return priv_hex, pub_hex, pubkey_to_address(pub_hex)


def sign(private_hex: str, message: bytes) -> str:
    """Sign *message* with *private_hex* and return the hex-encoded signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return private_key.sign(message).hex()
