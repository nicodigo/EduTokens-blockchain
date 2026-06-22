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
                  amount: int = 10, concept: str = "TP1") -> Transaction:
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
                   amount: int = 5, concept: str = "COMEDOR") -> Transaction:
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
        self.assertEqual(tx.amount, 10)
        self.assertEqual(tx.tx_type, "EARN")
        self.assertEqual(len(tx.signature), 128)

    def test_tx_id_is_deterministic(self):
        tx1 = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                            amount=10, concept="TP1")
        # Manually set timestamp so the test is deterministic
        tx1.timestamp = 1000.0
        tx1.signature = sign(self.uni_priv, tx1.tx_id.encode())

        tx2 = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10,
            tx_type="EARN",
            concept="TP1",
            timestamp=1000.0,
        )
        tx2.signature = sign(self.uni_priv, tx2.tx_id.encode())

        self.assertEqual(tx1.tx_id, tx2.tx_id)
        self.assertEqual(len(tx1.tx_id), 64)

    def test_tx_id_survives_roundtrip_with_int_amount(self):
        """M1: tx_id must be identical after JSON roundtrip with int amounts."""
        import json

        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.timestamp = 1000.0
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        original_id = tx.tx_id

        # Simulate storage + retrieval
        raw = json.dumps(tx.to_dict(), sort_keys=True)
        restored = Transaction.from_dict(json.loads(raw))
        restored_id = restored.tx_id

        self.assertEqual(original_id, restored_id,
                         "tx_id changed after roundtrip — determinism broken")

    def test_amount_serializes_as_int_in_json(self):
        """M1: JSON must contain an integer amount, not a float (e.g. 10 not 10.0)."""
        import json

        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.timestamp = 1000.0
        raw = json.dumps(tx.to_dict(), sort_keys=True)

        self.assertIn('"amount": 10,', raw,
                      f"amount should be int 10 in JSON, got: {raw}")

    def test_tx_id_excludes_signature(self):
        """Signing a tx does not change its tx_id."""
        tx = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10,
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
            amount=5,
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

    def test_excessive_amount_fails(self):
        """Audit M3: amount must not exceed 1,000,000,000."""
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.amount = 1_000_000_000
        self.assertEqual(tx.validate(), [])

        tx.amount = 1_000_000_001
        errors = tx.validate()
        self.assertTrue(
            any("amount must not exceed" in e for e in errors),
            f"expected ceiling error, got {errors}",
        )

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
            amount=10,
            tx_type="EARN",
            concept="TP1",
            timestamp=1000.0,
            nonce=0,
        )
        tx2 = Transaction(
            sender_pubkey=self.uni_pub,
            receiver_pubkey=self.student_pub,
            amount=10,
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

    # -- M4: nonce validation -----------------------------------------------

    def test_nonce_negative_rejected(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = -1
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        errors = tx.validate()
        self.assertTrue(any("nonce must be >= 0" in e for e in errors),
                        f"expected nonce error, got {errors}")

    def test_nonce_zero_accepted(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 0
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        errors = tx.validate()
        self.assertFalse(any("nonce" in e for e in errors),
                         f"nonce=0 should pass, got {errors}")

    # -- L3: to_dict nonce roundtrip ----------------------------------------

    def test_to_dict_includes_nonce(self):
        """L3: nonce survives to_dict() roundtrip without redundant assignment."""
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 42
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        restored = Transaction.from_dict(tx.to_dict())
        self.assertEqual(restored.nonce, 42,
                         "nonce should survive roundtrip via _signing_dict")

    # -- L2: timestamp validation -------------------------------------------

    def test_timestamp_negative_rejected(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.timestamp = -1
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        errors = tx.validate()
        self.assertTrue(any("timestamp must be positive" in e for e in errors),
                        f"expected timestamp error, got {errors}")

    def test_timestamp_zero_rejected(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.timestamp = 0
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        errors = tx.validate()
        self.assertTrue(any("timestamp must be positive" in e for e in errors),
                        f"expected timestamp error, got {errors}")

    def test_timestamp_positive_accepted(self):
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.timestamp = 1000.0
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        errors = tx.validate()
        self.assertFalse(any("timestamp" in e for e in errors),
                         f"positive timestamp should pass, got {errors}")


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
        import hashlib

        genesis = Block.create_genesis()

        tx1 = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                            amount=10, concept="TP1")
        tx2 = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub,
                             amount=5, concept="COMEDOR")

        block1 = Block(
            index=1,
            timestamp=2000.0,
            transactions=[tx1, tx2],
            previous_hash=genesis.hash,
            difficulty=4,
            nonce=0,
        )
        block1.hash = block1.compute_hash()

        # Mine the block so PoW validation passes
        fingerprint = block1.fingerprint
        nonce = 0
        while nonce < 10_000_000:
            digest = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
            if digest.startswith("0000"):
                break
            nonce += 1
        block1.nonce = nonce
        block1.hash = block1.compute_hash()

        self.assertEqual(block1.index, 1)
        self.assertEqual(block1.previous_hash, genesis.hash)
        self.assertEqual(len(block1.transactions), 2)
        self.assertEqual(len(block1.hash), 64)
        self.assertTrue(block1.fingerprint)
        self.assertEqual(len(block1.fingerprint), 64)

        # Chaining validation (structural + PoW)
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

    # -- M3: previous_hash format validation --------------------------------

    def test_previous_hash_wrong_length_rejected(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block1 = Block(index=1, timestamp=2000.0, transactions=[tx],
                       previous_hash="a" * 63, difficulty=4)
        errors = block1.validate()
        self.assertTrue(any("previous_hash must be 64" in e for e in errors),
                        f"expected length error, got {errors}")

    def test_previous_hash_non_hex_rejected(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block1 = Block(index=1, timestamp=2000.0, transactions=[tx],
                       previous_hash="z" * 64, difficulty=4)
        errors = block1.validate()
        self.assertTrue(any("hex-encoded" in e for e in errors),
                        f"expected hex error, got {errors}")

    def test_previous_hash_genesis_format_accepted(self):
        genesis = Block.create_genesis()
        self.assertEqual(genesis.previous_hash, "0" * 64)
        # Genesis passes previous_hash check (special-cased elsewhere)
        errors = genesis.validate()
        self.assertFalse(any("previous_hash must" in e for e in errors))

    # -- M2: difficulty range validation ------------------------------------

    def test_difficulty_negative_rejected(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block1 = Block(index=1, timestamp=2000.0, transactions=[tx],
                       previous_hash=genesis.hash, difficulty=-1)
        errors = block1.validate()
        self.assertTrue(any("difficulty must be 0-32" in e for e in errors),
                        f"expected difficulty error, got {errors}")

    def test_difficulty_over_32_rejected(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block1 = Block(index=1, timestamp=2000.0, transactions=[tx],
                       previous_hash=genesis.hash, difficulty=33)
        errors = block1.validate()
        self.assertTrue(any("difficulty must be 0-32" in e for e in errors),
                        f"expected difficulty error, got {errors}")

    def test_difficulty_0_valid_for_genesis(self):
        genesis = Block.create_genesis()
        self.assertEqual(genesis.difficulty, 0)
        self.assertEqual(genesis.validate(), [])

    # -- H1: PoW now checked inside validate --------------------------------

    def test_validate_rejects_unmined_block(self):
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block1 = Block(index=1, timestamp=2000.0, transactions=[tx],
                       previous_hash=genesis.hash, difficulty=4, nonce=0)
        block1.hash = block1.compute_hash()
        errors = block1.validate(previous_block=genesis)
        self.assertTrue(any("Proof-of-Work" in e for e in errors),
                        f"expected PoW error for unmined block, got {errors}")

    def test_validate_accepts_mined_block(self):
        import hashlib
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block1 = Block(index=1, timestamp=2000.0, transactions=[tx],
                       previous_hash=genesis.hash, difficulty=4, nonce=0)
        block1.hash = block1.compute_hash()

        # Mine it
        fp = block1.fingerprint
        n = 0
        while n < 10_000_000:
            if hashlib.md5((fp + str(n)).encode()).hexdigest().startswith("0000"):
                break
            n += 1
        block1.nonce = n
        block1.hash = block1.compute_hash()

        errors = block1.validate(previous_block=genesis)
        self.assertEqual(errors, [], f"mined block should pass, got {errors}")

    # -- H2: uppercase hex accepted -----------------------------------------

    def test_transaction_with_uppercase_pubkeys_passes_validation(self):
        """H2: uppercase hex pubkeys should pass structural validation."""
        upper_pub = "A" * 64  # 64 uppercase hex chars
        upper_sig = "B" * 128
        tx = Transaction(
            sender_pubkey=upper_pub,
            receiver_pubkey=upper_pub[:32] + "C" * 32,
            amount=10,
            tx_type="SPEND",
            concept="TEST",
            signature=upper_sig,
        )
        errors = tx.validate()
        # Structural validation passes — hex-encoded, correct lengths
        self.assertFalse(any("hex chars" in e for e in errors),
                         f"uppercase hex should be accepted, got {errors}")
        # But sender == receiver is rejected
        tx.receiver_pubkey = "D" * 64
        errors = tx.validate()
        self.assertEqual(errors, [], f"valid uppercase tx should pass, got {errors}")

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

    # ------------------------------------------------------------------
    # Block.verify_result  (audit H3 — canonical worker-result check)
    # ------------------------------------------------------------------

    def test_verify_result_accepts_valid_pow(self):
        """Block.verify_result must return True for a correct hash that meets
        the difficulty prefix."""
        fingerprint = "dummy-fp"
        difficulty = 2
        # Brute-force a valid (nonce, hash) pair
        for nonce in range(100000):
            import hashlib
            h = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
            if h.startswith("0" * difficulty):
                break
        valid, actual = Block.verify_result(fingerprint, difficulty, nonce, h)
        self.assertTrue(valid, f"nonce={nonce} hash={actual}")
        self.assertEqual(actual, h)

    def test_verify_result_rejects_hash_mismatch(self):
        """Correct prefix but wrong hash — must reject."""
        valid, actual = Block.verify_result("abc", 4, 42, "0000deadbeef")
        self.assertFalse(valid)

    def test_verify_result_rejects_difficulty_not_met(self):
        """Hash matches but prefix doesn't — must reject."""
        import hashlib
        fingerprint = "xyz"
        h = hashlib.md5((fingerprint + "0").encode()).hexdigest()
        valid, actual = Block.verify_result(fingerprint, 4, 0, h)
        self.assertFalse(valid)

    def test_verify_result_returns_32_char_md5_hex(self):
        """Actual hash must always be a 32-char lowercase hex string."""
        valid, actual = Block.verify_result("abc", 1, 0, "dummy")
        self.assertFalse(valid)  # doesn't match, but format must be correct
        self.assertIsInstance(actual, str)
        self.assertEqual(len(actual), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in actual))

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
                            amount=10, concept="TP1")
        tx2 = _make_spend_tx(self.student_priv, self.student_pub, self.vendor_pub,
                             amount=5, concept="COMEDOR")

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


# ---------------------------------------------------------------------------
# L1: Pydantic output model validation
# ---------------------------------------------------------------------------


class TestSchemaValidation(unittest.TestCase):
    def test_balance_response_rejects_short_address(self):
        from pydantic import ValidationError
        from shared.schemas import BalanceResponse

        with self.assertRaises(ValidationError):
            BalanceResponse(address="ab" * 11, balance=100)  # 22 chars, not 24

    def test_balance_response_rejects_non_hex_address(self):
        from pydantic import ValidationError
        from shared.schemas import BalanceResponse

        with self.assertRaises(ValidationError):
            BalanceResponse(address="z" * 24, balance=100)

    def test_balance_response_accepts_valid_address(self):
        from shared.schemas import BalanceResponse

        resp = BalanceResponse(address="a" * 24, balance=100)
        self.assertEqual(resp.address, "a" * 24)
        self.assertEqual(resp.balance, 100)

    def test_account_response_accepts_valid_address(self):
        from shared.schemas import AccountResponse

        resp = AccountResponse(address="f" * 24, balance=50, nonce=3, pending_nonce=5)
        self.assertEqual(resp.address, "f" * 24)
        self.assertEqual(resp.balance, 50)
        self.assertEqual(resp.nonce, 3)


if __name__ == "__main__":
    unittest.main()
