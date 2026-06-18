"""Unit tests for NCT components (PKI-aware)."""

import unittest
from unittest.mock import MagicMock

from broker.messages import ResultMessage
from nct.nct import (
    accumulate_transactions,
    handle_result,
    verify_pow_result,
)
from nct.state import NCTConfig, NCTState
from shared.block import Block, Transaction
from tests._crypto_fixtures import make_keypair, sign


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_earn_tx(receiver_pub: str, authority_priv: str, authority_pub: str,
                  amount: float = 1.0, concept: str = "TP1") -> Transaction:
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
                   amount: float = 1.0, concept: str = "COMEDOR") -> Transaction:
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
# verify_pow_result  (pure function — unchanged)
# ---------------------------------------------------------------------------


class TestVerifyPowResult(unittest.TestCase):
    def test_valid_pow(self):
        import hashlib
        fingerprint = "abc123"
        difficulty = 4
        nonce = 0
        while nonce < 1_000_000:
            claimed = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
            if claimed.startswith("0000"):
                break
            nonce += 1

        valid, actual = verify_pow_result(fingerprint, difficulty, nonce, claimed)
        self.assertTrue(valid, f"nonce={nonce} hash={actual}")

    def test_invalid_hash_mismatch(self):
        valid, actual = verify_pow_result("abc", 4, 42, "0000deadbeef")
        self.assertFalse(valid)

    def test_invalid_difficulty_not_met(self):
        valid, actual = verify_pow_result("abc", 4, 0, "1234abcd0000")
        self.assertFalse(valid)


# ---------------------------------------------------------------------------
# accumulate_transactions
# ---------------------------------------------------------------------------


class TestAccumulateTransactions(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()
        self.vendor_priv, self.vendor_pub, _ = make_keypair()

    def test_returns_txs_when_pool_full(self):
        state = NCTState()
        config = NCTConfig(block_size=2, block_timeout=60)
        state.add_transaction(_make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                                            amount=1.0))
        state.add_transaction(_make_earn_tx(self.vendor_pub, self.uni_priv, self.uni_pub,
                                            amount=2.0))

        redis_mock = MagicMock()
        txs = accumulate_transactions(state, redis_mock, config)
        self.assertEqual(len(txs), 2)
        self.assertEqual(state.pool_size(), 0)

    def test_returns_empty_on_shutdown(self):
        state = NCTState()
        redis_mock = MagicMock()
        config = NCTConfig(block_size=2, block_timeout=60)

        import threading, time

        def _shutdown():
            time.sleep(0.1)
            state.shutdown.set()

        t = threading.Thread(target=_shutdown, daemon=True)
        t.start()

        txs = accumulate_transactions(state, redis_mock, config)
        self.assertEqual(txs, [])


# ---------------------------------------------------------------------------
# handle_result
# ---------------------------------------------------------------------------


class TestHandleResult(unittest.TestCase):
    def setUp(self):
        self.state = NCTState()
        self.redis = MagicMock()
        self.channel = MagicMock()
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()

    def _make_block(self, index: int = 1, difficulty: int = 4) -> Block:
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block = Block(
            index=index,
            timestamp=2.0,
            transactions=[tx],
            previous_hash=genesis.hash,
            difficulty=difficulty,
        )
        return block

    def test_rejects_result_for_wrong_block(self):
        block = self._make_block(index=1)
        self.state.set_current_block(block, 1000)

        result = ResultMessage(
            task_id="t1", block_index=99, worker_id="w1", nonce=0, hash="dead",
        )
        ok = handle_result(self.state, self.redis, self.channel, result)
        self.assertFalse(ok)

    def test_accepts_valid_result_and_signals(self):
        import hashlib

        block = self._make_block(index=1, difficulty=4)
        self.state.set_current_block(block, 1_000_000)

        fingerprint = block.fingerprint
        nonce = 0
        while nonce < 10_000_000:
            claimed = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
            if claimed.startswith("0000"):
                break
            nonce += 1

        result = ResultMessage(
            task_id="t1", block_index=1, worker_id="w1", nonce=nonce, hash=claimed,
        )

        ok = handle_result(self.state, self.redis, self.channel, result)
        self.assertTrue(ok)

        self.assertEqual(block.nonce, nonce)
        self.assertEqual(block.hash, block.compute_hash())
        self.redis.rpush.assert_called_once()
        self.channel.basic_publish.assert_called()
        self.assertTrue(self.state.block_mined.is_set())

    def test_rejects_invalid_hash(self):
        block = self._make_block(index=1, difficulty=4)
        self.state.set_current_block(block, 1000)

        result = ResultMessage(
            task_id="t1", block_index=1, worker_id="w1", nonce=0, hash="0000deadbeef",
        )
        ok = handle_result(self.state, self.redis, self.channel, result)
        self.assertFalse(ok)
        self.assertFalse(self.state.block_mined.is_set())


class TestNCTState(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()

    def test_tx_pool_operations(self):
        state = NCTState()
        self.assertEqual(state.pool_size(), 0)

        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        state.add_transaction(tx)
        self.assertEqual(state.pool_size(), 1)

        drained = state.drain_pool(5)
        self.assertEqual(len(drained), 1)
        self.assertEqual(state.pool_size(), 0)

    def test_drain_respects_max_count(self):
        state = NCTState()
        for i in range(10):
            tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
            state.add_transaction(tx)
        drained = state.drain_pool(3)
        self.assertEqual(len(drained), 3)
        self.assertEqual(state.pool_size(), 7)

    def test_set_and_get_current_block(self):
        state = NCTState()
        block = self._make_block()
        state.set_current_block(block, 5000)

        cb, fingerprint, difficulty = state.get_current_for_verification()
        self.assertIs(cb, block)
        self.assertEqual(fingerprint, block.fingerprint)
        self.assertEqual(difficulty, block.difficulty)

    def _make_block(self) -> Block:
        genesis = Block.create_genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        block = Block(
            index=1, timestamp=2.0, transactions=[tx],
            previous_hash=genesis.hash, difficulty=4,
        )
        return block


if __name__ == "__main__":
    unittest.main()
