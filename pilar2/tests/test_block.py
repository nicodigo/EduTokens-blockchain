"""Unit tests for Transaction and Block schemas (PKI-aware)."""

import hashlib
import json
import unittest

from shared.block import Block, Transaction

from tests._crypto_fixtures import make_keypair, sign


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_earn_tx(receiver_pub: str, authority_priv: str, authority_pub: str,
                  amount: float = 10.0, concept: str = "TP1") -> Transaction:
    """Create a signed EARN transaction from authority → student."""
    tx = Transaction(
        sender_pubkey=authority_pub,
        receiver_pubkey=receiver_pub,
        amount=amount,
        tx_type="EARN",
        concept=concept,
    )
    tx.signature = sign(authority_priv, tx.tx_id.encode())
    return tx


def _make_spend_tx(sender_priv: str, sender_pub: str, vendor_pub: str,
                   amount: float = 5.0, concept: str = "COMEDOR") -> Transaction:
    """Create a signed SPEND transaction from student → vendor."""
    tx = Transaction(
        sender_pubkey=sender_pub,
        receiver_pubkey=vendor_pub,
        amount=amount,
        tx_type="SPEND",
        concept=concept,
    )
    tx.signature = sign(sender_priv, tx.tx_id.encode())
    return tx


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class TestTransaction(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, self.student_addr = make_keypair()
        self.uni_priv, self.uni_pub, self.uni_addr = make_keypair()
        self.vendor_priv, self.vendor_pub, self.vendor_addr = make_keypair()

    # -- Creation & determinism --------------------------------------------

    def test_creation_with_pubkeys(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        self.assertEqual(tx.sender_pubkey, self.uni_pub)
        self.assertEqual(tx.receiver_pubkey, self.student_pub)
        self.assertEqual(tx.amount, 10.0)
        self.assertEqual(tx.tx_type, "EARN")
        self.assertEqual(len(tx.signature), 128)

    def test_tx_id_is_deterministic(self):
        tx1 = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                            amount=10.0, concept="TP1")
        # Manually set timestamp so the test is deterministic
        tx1.timestamp = 1000.0
        tx1.signature = sign(self.uni_priv, tx1.tx_id.encode())

        tx2 = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10.0,
            tx_type="EARN",
            concept="TP1",
            timestamp=1000.0,
        )
        tx2.signature = sign(self.uni_priv, tx2.tx_id.encode())

        self.assertEqual(tx1.tx_id, tx2.tx_id)
        self.assertEqual(len(tx1.tx_id), 64)

    def test_tx_id_excludes_signature(self):
        """Signing a tx does not change its tx_id."""
        tx = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10.0,
            tx_type="EARN",
            concept="TP1",
            timestamp=1000.0,
        )
        before = tx.tx_id
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        after = tx.tx_id
        self.assertEqual(before, after)

    # -- Serialisation roundtrip -------------------------------------------

    def test_serde_roundtrip(self):
        tx = Transaction(
            sender_pubkey=self.student_pub,
            receiver_pubkey=self.vendor_pub,
            amount=5.0,
            tx_type="SPEND",
            concept="COMEDOR",
            timestamp=1000.0,
            signature="a" * 128,
        )
        restored = Transaction.from_dict(tx.to_dict())
        self.assertEqual(restored.sender_pubkey, tx.sender_pubkey)
        self.assertEqual(restored.receiver_pubkey, tx.receiver_pubkey)
        self.assertEqual(restored.amount, tx.amount)
        self.assertEqual(restored.tx_type, tx.tx_type)
        self.assertEqual(restored.concept, tx.concept)
        self.assertEqual(restored.signature, tx.signature)
        self.assertEqual(restored.timestamp, tx.timestamp)
        self.assertEqual(restored.tx_id, tx.tx_id)

    # -- Validation (structural) -------------------------------------------

    def test_valid_earn_passes_structural(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        self.assertEqual(tx.validate(), [])

    def test_valid_spend_passes_structural(self):
        tx = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub)
        self.assertEqual(tx.validate(), [])

    def test_empty_sender_pubkey_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.sender_pubkey = ""
        errors = tx.validate()
        self.assertTrue(any("sender_pubkey" in e for e in errors))

    def test_short_sender_pubkey_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.sender_pubkey = "ab" * 10
        errors = tx.validate()
        self.assertTrue(any("sender_pubkey" in e for e in errors))

    def test_empty_receiver_pubkey_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.receiver_pubkey = ""
        errors = tx.validate()
        self.assertTrue(any("receiver_pubkey" in e for e in errors))

    def test_same_sender_and_receiver_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.receiver_pubkey = tx.sender_pubkey
        errors = tx.validate()
        self.assertTrue(any("different" in e for e in errors))

    def test_non_positive_amount_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.amount = 0
        self.assertTrue(tx.validate())
        tx.amount = -5
        self.assertTrue(tx.validate())

    def test_empty_concept_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.concept = ""
        self.assertTrue(any("concept" in e for e in tx.validate()))

    def test_invalid_tx_type_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.tx_type = "TRANSFER"
        self.assertTrue(any("tx_type" in e for e in tx.validate()))

    def test_empty_signature_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.signature = ""
        self.assertTrue(any("signature" in e for e in tx.validate()))

    def test_short_signature_fails(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.signature = "a" * 64
        errors = tx.validate()
        self.assertTrue(any("signature" in e for e in errors))

    # -- Signature actually verifies ---------------------------------------

    def test_earn_signature_verifies(self):
        from shared.crypto import verify as crypto_verify
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        self.assertTrue(crypto_verify(tx.sender_pubkey, tx.tx_id.encode(), tx.signature))

    def test_spend_signature_verifies(self):
        from shared.crypto import verify as crypto_verify
        tx = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub)
        self.assertTrue(crypto_verify(tx.sender_pubkey, tx.tx_id.encode(), tx.signature))

    def test_tampered_concept_invalidates_signature(self):
        from shared.crypto import verify as crypto_verify
        tx = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub,
                            concept="COMEDOR")
        tx.concept = "FRAUDE"
        self.assertFalse(crypto_verify(tx.sender_pubkey, tx.tx_id.encode(), tx.signature))

    # -- Nonce ---------------------------------------------------------------

    def test_nonce_affects_tx_id(self):
        """Two identical transactions with different nonces have different tx_ids."""
        tx1 = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10.0,
            tx_type="EARN",
            concept="TP1",
            timestamp=1000.0,
            nonce=0,
        )
        tx2 = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10.0,
            tx_type="EARN",
            concept="TP1",
            timestamp=1000.0,
            nonce=1,
        )
        self.assertNotEqual(tx1.tx_id, tx2.tx_id)

    def test_nonce_survives_roundtrip(self):
        """Nonce is preserved through serialisation/deserialisation."""
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 5
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        restored = Transaction.from_dict(tx.to_dict())
        self.assertEqual(restored.nonce, 5)

    def test_nonce_is_in_signing_dict(self):
        """Tampering with the nonce invalidates the signature."""
        from shared.crypto import verify as crypto_verify
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 0
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        self.assertTrue(crypto_verify(tx.sender_pubkey, tx.tx_id.encode(), tx.signature))

        # Tamper with nonce
        tx.nonce = 99
        self.assertFalse(crypto_verify(tx.sender_pubkey, tx.tx_id.encode(), tx.signature))


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class TestBlock(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()
        self.vendor_priv, self.vendor_pub, _ = make_keypair()

    def test_genesis_block(self):
        genesis = Block.create_genesis()
        self.assertEqual(genesis.index, 0)
        self.assertEqual(genesis.previous_hash, "0" * 64)
        self.assertEqual(genesis.transactions, [])
        self.assertEqual(genesis.hash, genesis.compute_hash())
        self.assertEqual(genesis.validate(), [])

    def test_genesis_serialisation_roundtrip(self):
        genesis = Block.create_genesis()
        restored = Block.from_dict(genesis.to_dict())
        self.assertEqual(restored.index, genesis.index)
        self.assertEqual(restored.hash, genesis.hash)
        self.assertEqual(restored.previous_hash, "0" * 64)

    def test_block_with_signed_transactions(self):
        genesis = Block.create_genesis()

        tx1 = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                            amount=10.0, concept="TP1")
        tx2 = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub,
                             amount=5.0, concept="COMEDOR")

        block1 = Block(
            index=1,
            timestamp=2000.0,
            transactions=[tx1, tx2],
            previous_hash=genesis.hash,
            difficulty=4,
            nonce=0,
        )
        block1.hash = block1.compute_hash()

        self.assertEqual(block1.index, 1)
        self.assertEqual(block1.previous_hash, genesis.hash)
        self.assertEqual(len(block1.transactions), 2)
        self.assertEqual(len(block1.hash), 64)
        self.assertTrue(block1.fingerprint)
        self.assertEqual(len(block1.fingerprint), 64)

        # Chaining validation
        errors = block1.validate(previous_block=genesis)
        self.assertEqual(errors, [], f"validation failed: {errors}")

    def test_validate_detects_chain_break(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)

        block1 = Block(
            index=1,
            timestamp=2000.0,
            transactions=[tx],
            previous_hash="0" * 64,  # wrong — should be genesis.hash
            difficulty=4,
        )
        block1.hash = block1.compute_hash()

        errors = block1.validate(previous_block=genesis)
        self.assertTrue(any("previous_hash" in e for e in errors),
                        f"expected hash error, got {errors}")

    def test_verify_pow_rejects_unsatisfied_nonce(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)

        block1 = Block(
            index=1,
            timestamp=2000.0,
            transactions=[tx],
            previous_hash=genesis.hash,
            difficulty=4,
            nonce=0,
        )
        block1.hash = block1.compute_hash()
        self.assertFalse(Block.verify_pow(block1))

    def test_block_serialisation_includes_signatures(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.timestamp = 1000.0

        block1 = Block(
            index=1, timestamp=2000.0, transactions=[tx],
            previous_hash=genesis.hash, difficulty=4,
        )
        block1.hash = block1.compute_hash()

        d = block1.to_dict()
        self.assertIn("signature", d["transactions"][0])
        self.assertEqual(len(d["transactions"][0]["signature"]), 128)

    def test_json_output(self):
        """Produce indent JSON output for manual inspection."""
        genesis = Block.create_genesis()
        tx1 = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                            amount=10.0, concept="TP1")
        tx2 = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub,
                             amount=5.0, concept="COMEDOR")

        block1 = Block(
            index=1, timestamp=2000.0, transactions=[tx1, tx2],
            previous_hash=genesis.hash, difficulty=4,
        )
        block1.hash = block1.compute_hash()

        print("\n=== Genesis Block ===")
        print(json.dumps(genesis.to_dict(), indent=2))
        print("\n=== Block 1 ===")
        print(json.dumps(block1.to_dict(), indent=2))

        self.assertEqual(genesis.index, 0)
        self.assertEqual(block1.index, 1)
        self.assertEqual(block1.previous_hash, genesis.hash)


if __name__ == "__main__":
    unittest.main()
