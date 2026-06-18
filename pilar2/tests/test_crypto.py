"""Unit tests for shared/crypto.py — Ed25519 verification."""

import unittest

from shared.crypto import (
    ED25519_PUBKEY_HEX_LEN,
    ED25519_SIG_HEX_LEN,
    pubkey_to_address,
    verify,
)


# ---------------------------------------------------------------------------
# Helpers — generate fresh keys for test fixtures
# ---------------------------------------------------------------------------


def _make_keypair() -> tuple[str, str, str]:
    """Return ``(private_hex, public_hex, address)`` for a fresh Ed25519 keypair."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_hex = private_key.private_bytes_raw().hex()
    pub_hex = public_key.public_bytes_raw().hex()
    return priv_hex, pub_hex, pubkey_to_address(pub_hex)


def _sign(private_hex: str, message: bytes) -> str:
    """Sign *message* and return the hex-encoded signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return private_key.sign(message).hex()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.priv, self.pub, self.addr = _make_keypair()
        self.message = b"hello blockchain"

    def test_valid_signature_returns_true(self):
        sig = _sign(self.priv, self.message)
        self.assertTrue(verify(self.pub, self.message, sig))

    def test_wrong_public_key_returns_false(self):
        sig = _sign(self.priv, self.message)
        _, other_pub, _ = _make_keypair()
        self.assertFalse(verify(other_pub, self.message, sig))

    def test_tampered_message_returns_false(self):
        sig = _sign(self.priv, self.message)
        self.assertFalse(verify(self.pub, b"tampered message", sig))

    def test_wrong_signature_returns_false(self):
        sig = _sign(self.priv, self.message)
        # Flip one bit in the signature
        sig_bytes = bytearray(bytes.fromhex(sig))
        sig_bytes[0] ^= 0x01
        bad_sig = sig_bytes.hex()
        self.assertFalse(verify(self.pub, self.message, bad_sig))

    def test_signature_deterministic(self):
        """Ed25519 signatures are deterministic — same key + message = same sig."""
        sig1 = _sign(self.priv, self.message)
        sig2 = _sign(self.priv, self.message)
        self.assertEqual(sig1, sig2)

    def test_verify_tx_id_roundtrip(self):
        """Simulate the full flow: sign(tx_id) → verify(pubkey, tx_id, sig)."""
        import hashlib, json

        tx_body = {
            "sender": self.pub,
            "receiver": "00" * 32,
            "amount": 10,
            "concept": "TP1",
        }
        tx_id = hashlib.sha256(json.dumps(tx_body, sort_keys=True).encode()).digest()
        sig = _sign(self.priv, tx_id)
        self.assertTrue(verify(self.pub, tx_id, sig))

    # -- Error handling ---------------------------------------------------

    def test_public_key_too_short_raises(self):
        with self.assertRaises(ValueError):
            verify("ab" * 31, b"msg", "c0" * 64)

    def test_public_key_non_hex_raises(self):
        with self.assertRaises(ValueError):
            verify("gg" * 32, b"msg", "c0" * 64)

    def test_public_key_wrong_type_raises(self):
        with self.assertRaises(TypeError):
            verify(12345, b"msg", "c0" * 64)  # type: ignore[arg-type]

    def test_signature_too_short_raises(self):
        pub = "ab" * 32
        with self.assertRaises(ValueError):
            verify(pub, b"msg", "c0" * 31)

    def test_signature_non_hex_raises(self):
        pub = "ab" * 32
        with self.assertRaises(ValueError):
            verify(pub, b"msg", "zz" * 64)

    def test_invalid_curve_point_returns_false_not_crash(self):
        """C1 fix: all-zeros is valid hex but not a curve point → must return False."""
        invalid_pub = "00" * 32  # all-zeros is never a valid Ed25519 point
        valid_sig = "c0" * 64   # any 128 hex chars, structurally valid
        result = verify(invalid_pub, b"test", valid_sig)
        self.assertFalse(result, "Invalid curve point should return False, not crash")


class TestPubkeyToAddress(unittest.TestCase):
    def test_returns_24_hex_chars(self):
        _, pub, _ = _make_keypair()
        addr = pubkey_to_address(pub)
        self.assertEqual(len(addr), 24)
        self.assertTrue(all(c in "0123456789abcdef" for c in addr))

    def test_deterministic(self):
        _, pub, _ = _make_keypair()
        self.assertEqual(pubkey_to_address(pub), pubkey_to_address(pub))

    def test_different_keys_produce_different_addresses(self):
        _, pub1, _ = _make_keypair()
        _, pub2, _ = _make_keypair()
        self.assertNotEqual(pubkey_to_address(pub1), pubkey_to_address(pub2))

    def test_invalid_hex_raises(self):
        with self.assertRaises(ValueError):
            pubkey_to_address("zz" * 32)

    def test_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            pubkey_to_address("ab" * 10)


class TestConstants(unittest.TestCase):
    def test_pubkey_hex_len_is_64(self):
        self.assertEqual(ED25519_PUBKEY_HEX_LEN, 64)

    def test_sig_hex_len_is_128(self):
        self.assertEqual(ED25519_SIG_HEX_LEN, 128)


if __name__ == "__main__":
    unittest.main()
