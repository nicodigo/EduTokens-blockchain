"""Unit tests for NCT components (PKI-aware)."""

import unittest
from unittest.mock import MagicMock

from broker.messages import ResultMessage
from nct.nct import (
    accumulate_transactions,
    drain_pool_validated,
    ensure_genesis,
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
                  amount: int = 1, concept: str = "TP1",
                  nonce: int = 0) -> Transaction:
    tx = Transaction(
        sender_pubkey=authority_pub,
        receiver_pubkey=receiver_pub,
        amount=amount,
        tx_type="EARN",
        concept=concept,
        nonce=nonce,
    )
    tx.signature = sign(authority_priv, tx.tx_id.encode())
    return tx


def _make_spend_tx(sender_priv: str, sender_pub: str, vendor_pub: str,
                   amount: int = 1, concept: str = "COMEDOR",
                   nonce: int = 0) -> Transaction:
    tx = Transaction(
        sender_pubkey=sender_pub,
        receiver_pubkey=vendor_pub,
        amount=amount,
        tx_type="SPEND",
        concept=concept,
        nonce=nonce,
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
                                            amount=1, nonce=0))
        state.add_transaction(_make_earn_tx(self.vendor_pub, self.uni_priv, self.uni_pub,
                                            amount=2, nonce=1))

        redis_mock = MagicMock()
        # get_nonce calls redis_client.get("nonce:...") — return None for missing keys
        redis_mock.get.return_value = None
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
        # save_block_atomic uses pipeline(transaction=True) → pipe.rpush
        self.redis.pipeline.assert_called_once_with(transaction=True)
        self.redis.pipeline.return_value.rpush.assert_called_once()
        self.redis.pipeline.return_value.execute.assert_called_once()
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


# ---------------------------------------------------------------------------
# Nonce validation (anti-replay)
# ---------------------------------------------------------------------------


class TestNonceValidation(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()
        self.vendor_priv, self.vendor_pub, _ = make_keypair()

    def test_drain_pool_rejects_stale_nonce(self):
        """Transactions with nonce < current are discarded (replay)."""
        state = NCTState()
        redis_mock = MagicMock()

        # Simulate current nonce = 3 for this sender
        redis_mock.get.return_value = "3"

        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 2  # stale — already consumed nonce
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        state.add_transaction(tx)

        result = drain_pool_validated(state, redis_mock, 5)
        self.assertEqual(result, [], "stale nonce should be rejected")

    def test_drain_pool_accepts_correct_nonce(self):
        """Transactions with nonce == current are accepted."""
        state = NCTState()
        redis_mock = MagicMock()

        # get_nonce uses redis_client.get(), which returns a string
        redis_mock.get.return_value = "3"

        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 3  # correct — matches expected nonce
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        state.add_transaction(tx)

        result = drain_pool_validated(state, redis_mock, 5)
        self.assertEqual(len(result), 1)

    def test_drain_pool_accepts_default_nonce_zero(self):
        """New account with no nonce record has expected nonce = 0."""
        state = NCTState()
        redis_mock = MagicMock()

        # No nonce key set → get_nonce returns 0
        redis_mock.get.return_value = None

        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 0  # first transaction from this sender
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        state.add_transaction(tx)

        result = drain_pool_validated(state, redis_mock, 5)
        self.assertEqual(len(result), 1)

    def test_handle_result_updates_nonces(self):
        """After a block is mined, sender nonces are incremented."""
        import hashlib

        state = NCTState()
        redis_mock = MagicMock()
        channel_mock = MagicMock()

        block = Block(
            index=1,
            timestamp=2.0,
            transactions=[],
            previous_hash=Block.create_genesis().hash,
            difficulty=4,
        )
        # Create tx with nonce=3
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub)
        tx.nonce = 3
        tx.signature = sign(self.uni_priv, tx.tx_id.encode())
        block.transactions = [tx]

        state.set_current_block(block, 1_000_000)

        # Find valid PoW
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

        ok = handle_result(state, redis_mock, channel_mock, result)
        self.assertTrue(ok)

        # Verify that update_nonces_from_block was called
        # redis_mock.set should have been called for nonce increment
        # (through the pipeline)
        self.assertTrue(redis_mock.pipeline.called,
                        "pipeline should be called to update nonces")


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


# ---------------------------------------------------------------------------
# ensure_genesis  (audit L3, M3)
# ---------------------------------------------------------------------------


class TestEnsureGenesis(unittest.TestCase):
    """Verify ensure_genesis creates genesis and runs rebuild + validate."""

    @staticmethod
    def _valid_genesis_json() -> str:
        import json as _json
        genesis = Block.create_genesis()
        return _json.dumps(genesis.to_dict(), sort_keys=True)

    def test_creates_genesis_when_chain_empty(self):
        client = MagicMock()
        # get_latest_block → None (chain empty)
        client.lindex.return_value = None
        client.llen.return_value = 0

        ensure_genesis(client)

        # Genesis block should have been created and saved via rpush
        client.rpush.assert_called_once()
        # pipeline NOT called (genesis path uses save_block directly)
        client.pipeline.assert_not_called()

    def test_rebuilds_state_and_validates_when_chain_exists(self):
        """When chain is non-empty, ensure_genesis must:
        1. Call rebuild_state_from_chain
        2. Call validate_chain
        """
        genesis_json = self._valid_genesis_json()

        client = MagicMock()
        # get_latest_block: return genesis
        client.lindex.return_value = genesis_json
        client.llen.return_value = 1  # 1 block

        ensure_genesis(client)

        # rebuild_state_from_chain walks all blocks via get_block → lindex
        self.assertTrue(client.lindex.called,
                        "should read blocks to rebuild state")
        # validate_chain uses llen + lindex
        self.assertTrue(client.llen.called,
                        "should check chain height for validate_chain")
        # Must NOT have saved genesis again
        rpush_calls = [c for c in client.method_calls if c[0] == "rpush"]
        self.assertEqual(len(rpush_calls), 0,
                         "should not re-save genesis")

    def test_does_not_double_create_genesis(self):
        """If genesis already exists, it should NOT try to create it again."""
        genesis_json = self._valid_genesis_json()

        client = MagicMock()
        client.lindex.return_value = genesis_json
        client.llen.return_value = 1

        ensure_genesis(client)

        # rpush should NOT be called (no genesis creation)
        rpush_calls = [c for c in client.method_calls if c[0] == "rpush"]
        self.assertEqual(len(rpush_calls), 0,
                         "should not save genesis when chain already exists")


if __name__ == "__main__":
    unittest.main()
