"""Unit tests for chain_store (mock Redis — PKI-aware)."""

import json
import unittest
from unittest.mock import MagicMock

from shared.block import Block, Transaction
from storage.chain_store import (
    BLOCKS_KEY,
    PENDING_TXS_KEY,
    add_discarded_tx,
    get_block,
    get_chain_height,
    get_discarded_txns,
    get_latest_block,
    restore_pending_txs,
    save_block,
    save_block_atomic,
    save_pending_tx,
    trim_pending_txs,
    validate_chain,
)
from tests._crypto_fixtures import make_keypair, sign


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _genesis() -> Block:
    return Block.create_genesis()


def _make_earn_tx(receiver_pub: str, authority_priv: str, authority_pub: str,
                  amount: int = 10, concept: str = "TP1") -> Transaction:
    tx = Transaction(
        sender_pubkey=authority_pub,
        receiver_pubkey=receiver_pub,
        amount=amount,
        tx_type="EARN",
        concept=concept,
        timestamp=1000.0,
    )
    tx.signature = sign(authority_priv, tx.tx_id.encode())
    return tx


def _block1(genesis_hash: str, student_pub: str, uni_priv: str, uni_pub: str) -> Block:
    import hashlib

    tx = _make_earn_tx(student_pub, uni_priv, uni_pub)
    b = Block(
        index=1,
        timestamp=2000.0,
        transactions=[tx],
        previous_hash=genesis_hash,
        difficulty=4,
        nonce=0,
    )
    b.hash = b.compute_hash()

    # Mine the block so PoW validation passes
    fingerprint = b.fingerprint
    nonce = 0
    while nonce < 10_000_000:
        digest = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
        if digest.startswith("0000"):
            break
        nonce += 1
    b.nonce = nonce
    b.hash = b.compute_hash()
    return b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveAndGetBlock(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()

    def test_save_appends_to_list(self):
        client = MagicMock()
        genesis = _genesis()
        save_block(client, genesis)
        client.rpush.assert_called_once_with(
            BLOCKS_KEY, json.dumps(genesis.to_dict(), sort_keys=True)
        )

    def test_get_block_returns_deserialised(self):
        genesis = _genesis()
        payload = json.dumps(genesis.to_dict(), sort_keys=True)

        client = MagicMock()
        client.lindex.return_value = payload

        block = get_block(client, 0)
        assert block is not None
        self.assertEqual(block.index, 0)
        self.assertEqual(block.previous_hash, "0" * 64)
        self.assertEqual(block.hash, genesis.hash)

    def test_get_block_missing_returns_none(self):
        client = MagicMock()
        client.lindex.return_value = None
        self.assertIsNone(get_block(client, 99))

    def test_save_and_retrieve_full_roundtrip(self):
        """End-to-end through a fake in-memory list (no mocking)."""
        storage: list[str] = []

        class FakeClient(MagicMock):
            def rpush(self, key, value):  # type: ignore[override]
                storage.append(value)
                return len(storage)

            def lindex(self, key, index):  # type: ignore[override]
                if 0 <= index < len(storage):
                    return storage[index]
                return None

            def llen(self, key):  # type: ignore[override]
                return len(storage)

        client = FakeClient()
        genesis = _genesis()

        save_block(client, genesis)
        self.assertEqual(get_chain_height(client), 1)

        b1 = _block1(genesis.hash, self.student_pub, self.uni_priv, self.uni_pub)
        save_block(client, b1)
        self.assertEqual(get_chain_height(client), 2)

        # Retrieve and verify
        g = get_block(client, 0)
        self.assertIsNotNone(g)
        assert g is not None
        self.assertEqual(g.index, 0)
        self.assertEqual(g.hash, genesis.hash)

        b = get_block(client, 1)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.index, 1)
        self.assertEqual(b.previous_hash, genesis.hash)

        latest = get_latest_block(client)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.index, 1)


class TestChainValidation(unittest.TestCase):
    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()

    def test_validate_empty_chain(self):
        client = MagicMock()
        client.llen.return_value = 0
        self.assertEqual(validate_chain(client), [])

    def test_validate_valid_two_block_chain(self):
        genesis = _genesis()
        b1 = _block1(genesis.hash, self.student_pub, self.uni_priv, self.uni_pub)

        storage: list[str] = [json.dumps(genesis.to_dict(), sort_keys=True),
                              json.dumps(b1.to_dict(), sort_keys=True)]

        class FakeClient(MagicMock):
            def lindex(self, key, index):  # type: ignore[override]
                if 0 <= index < len(storage):
                    return storage[index]
                return None

            def llen(self, key):  # type: ignore[override]
                return len(storage)

        client = FakeClient()
        errors = validate_chain(client)
        self.assertEqual(errors, [], f"unexpected errors: {errors}")

    def test_validate_detects_broken_chain(self):
        genesis = _genesis()
        tx = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                           amount=1)
        # Deliberately wrong previous_hash
        bad_block = Block(
            index=1,
            timestamp=2000.0,
            transactions=[tx],
            previous_hash="0" * 64,  # should be genesis.hash
            difficulty=4,
            nonce=0,
        )
        bad_block.hash = bad_block.compute_hash()

        storage: list[str] = [json.dumps(genesis.to_dict(), sort_keys=True),
                              json.dumps(bad_block.to_dict(), sort_keys=True)]

        class FakeClient(MagicMock):
            def lindex(self, key, index):  # type: ignore[override]
                if 0 <= index < len(storage):
                    return storage[index]
                return None

            def llen(self, key):  # type: ignore[override]
                return len(storage)

        client = FakeClient()
        errors = validate_chain(client)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["index"], 1)
        self.assertTrue(any("previous_hash" in e for e in errors[0]["errors"]))


# ---------------------------------------------------------------------------
# save_block_atomic  (audit H3, M4)
# ---------------------------------------------------------------------------


class TestSaveBlockAtomic(unittest.TestCase):
    """Verify that save_block_atomic uses a transactional pipeline."""

    def setUp(self):
        self.student_priv, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()
        self.vendor_priv, self.vendor_pub, _ = make_keypair()

    def test_uses_transactional_pipeline(self):
        """save_block_atomic must open pipeline(transaction=True)."""
        client = MagicMock()
        genesis = _genesis()
        # Genesis has no transactions — still must be transactional
        save_block_atomic(client, genesis)

        client.pipeline.assert_called_once_with(transaction=True)
        pipe = client.pipeline.return_value
        pipe.rpush.assert_called_once()
        pipe.execute.assert_called_once()

    def test_persists_block_and_updates_balances_and_nonces(self):
        """A block with EARN + SPEND must rpush + incrby + set."""
        client = MagicMock()
        genesis = _genesis()

        earn = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                             amount=10, concept="TP1")
        spend = Transaction(
            sender_pubkey=self.student_pub,
            receiver_pubkey=self.vendor_pub,
            amount=5,
            tx_type="SPEND",
            concept="COMEDOR",
            timestamp=2000.0,
            nonce=1,
        )
        spend.signature = sign(self.student_priv, spend.tx_id.encode())

        block = Block(
            index=1, timestamp=2000.0, transactions=[earn, spend],
            previous_hash=genesis.hash, difficulty=4, nonce=0,
        )
        block.hash = block.compute_hash()

        save_block_atomic(client, block)

        pipe = client.pipeline.return_value

        # rpush with block JSON
        pipe.rpush.assert_called_once()
        rpush_args = pipe.rpush.call_args[0]
        self.assertEqual(rpush_args[0], BLOCKS_KEY)
        block_dict = json.loads(rpush_args[1])
        self.assertEqual(block_dict["index"], 1)
        self.assertEqual(len(block_dict["transactions"]), 2)

        # EARN → incrby(receiver, +10)
        pipe.incrby.assert_any_call(
            f"balance:{self.student_pub}", 10,
        )
        # SPEND → incrby(sender, -5)
        pipe.incrby.assert_any_call(
            f"balance:{self.student_pub}", -5,
        )

        # Nonces: both senders get set
        pipe.set.assert_any_call(
            f"nonce:{self.uni_pub}", 1,  # EARN sender nonce 0 + 1
        )
        pipe.set.assert_any_call(
            f"nonce:{self.student_pub}", 2,  # SPEND sender nonce 1 + 1
        )

        pipe.execute.assert_called_once()

    def test_atomicity_crash_between_operations_does_not_persist_partial(self):
        """If execute() fails, nothing should be persisted.

        Since save_block_atomic uses MULTI/EXEC, a crash between the
        Python calls (before execute) doesn't matter — nothing is sent
        to Redis yet.  We simulate this by making execute() raise.
        """
        client = MagicMock()
        client.pipeline.return_value.execute.side_effect = ConnectionError("boom")

        genesis = _genesis()
        earn = _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub,
                             amount=10)
        block = Block(
            index=1, timestamp=2000.0, transactions=[earn],
            previous_hash=genesis.hash, difficulty=4, nonce=0,
        )
        block.hash = block.compute_hash()

        with self.assertRaises(ConnectionError):
            save_block_atomic(client, block)

        # The pipeline was created but execute() failed → no side effects
        # (In real Redis, MULTI commands are queued, not executed until EXEC)
        client.pipeline.assert_called_once_with(transaction=True)
        client.pipeline.return_value.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Pending transaction persistence (audit L2)
# ---------------------------------------------------------------------------


class TestPendingTxPersistence(unittest.TestCase):
    """L2: Pending transactions are persisted to Redis so they survive NCT restarts."""

    def setUp(self):
        _, self.student_pub, _ = make_keypair()
        self.uni_priv, self.uni_pub, _ = make_keypair()

    def _earn_tx(self, amount: int = 10) -> Transaction:
        return _make_earn_tx(self.student_pub, self.uni_priv, self.uni_pub, amount=amount)

    def test_save_pending_tx_rpushes_json(self):
        """L2: save_pending_tx must RPUSH the serialised transaction."""
        client = MagicMock()
        tx = self._earn_tx(5)
        save_pending_tx(client, tx)
        client.rpush.assert_called_once_with(
            PENDING_TXS_KEY, json.dumps(tx.to_dict(), sort_keys=True),
        )

    def test_trim_removes_first_n_items(self):
        """L2: trim_pending_txs must LTRIM from the given count, preserving
        items at index >= count."""
        client = MagicMock()
        trim_pending_txs(client, 3)
        client.ltrim.assert_called_once_with(PENDING_TXS_KEY, 3, -1)

    def test_trim_zero_does_nothing(self):
        """L2: trim_pending_txs with count=0 must be a no-op."""
        client = MagicMock()
        trim_pending_txs(client, 0)
        client.ltrim.assert_not_called()

    def test_restore_returns_deserialised_txs(self):
        """L2: restore_pending_txs must deserialise all items in insertion order."""
        tx1 = self._earn_tx(10)
        tx2 = self._earn_tx(20)
        client = MagicMock()
        client.lrange.return_value = [
            json.dumps(tx1.to_dict(), sort_keys=True),
            json.dumps(tx2.to_dict(), sort_keys=True),
        ]
        restored = restore_pending_txs(client)
        self.assertEqual(len(restored), 2)
        self.assertEqual(restored[0].tx_id, tx1.tx_id)
        self.assertEqual(restored[1].tx_id, tx2.tx_id)
        client.lrange.assert_called_once_with(PENDING_TXS_KEY, 0, -1)

    def test_restore_empty_redis_returns_empty_list(self):
        """L2: restore_pending_txs on empty Redis returns []."""
        client = MagicMock()
        client.lrange.return_value = []
        self.assertEqual(restore_pending_txs(client), [])

    def test_restore_skips_corrupt_entries(self):
        """L2: restore_pending_txs must skip unparseable entries without crashing."""
        tx = self._earn_tx(10)
        client = MagicMock()
        client.lrange.return_value = [
            b"not valid json",
            json.dumps(tx.to_dict(), sort_keys=True),
        ]
        restored = restore_pending_txs(client)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].tx_id, tx.tx_id)


# ---------------------------------------------------------------------------
# Discarded transaction tracking (audit M2)
# ---------------------------------------------------------------------------

DISCARDED_PREFIX = "discarded:"


class TestDiscardedTransactions(unittest.TestCase):
    """get_discarded_txns and add_discarded_tx work with decode_responses=True."""

    def setUp(self) -> None:
        self.pubkey = "aad94d792c20a07a4e7f338ba9e642cb890b94aa7faedabfe3f135a50f36fbfb"

    def test_add_and_get_discarded_txns_with_decode_responses_true(self):
        """When Redis returns str (decode_responses=True), get_discarded_txns
        must return list[str] without calling .decode()."""
        client = MagicMock()
        client.smembers.return_value = {"tx-abc123", "tx-def456"}

        result = get_discarded_txns(client, self.pubkey)

        self.assertEqual(set(result), {"tx-abc123", "tx-def456"})
        client.smembers.assert_called_once_with(
            f"{DISCARDED_PREFIX}{self.pubkey}"
        )
        # Must NOT call .decode() on any element
        for elem in result:
            self.assertIsInstance(elem, str)
            with self.assertRaises(AttributeError):
                elem.decode()  # str has no decode

    def test_get_discarded_txns_returns_empty_list_when_none(self):
        client = MagicMock()
        client.smembers.return_value = set()
        self.assertEqual(get_discarded_txns(client, self.pubkey), [])

    def test_add_discarded_tx_calls_sadd(self):
        client = MagicMock()
        add_discarded_tx(client, self.pubkey, "tx-ghi789")
        client.sadd.assert_called_once_with(
            f"{DISCARDED_PREFIX}{self.pubkey}", "tx-ghi789"
        )

    def test_get_discarded_txns_not_called_with_decode(self):
        """Regression: ensure .decode() is never called on smembers result."""
        client = MagicMock()
        client.smembers.return_value = {"valid_tx_id"}

        result = get_discarded_txns(client, self.pubkey)

        # str "valid_tx_id" has no .decode() method — if code called .decode()
        # it would have raised AttributeError before reaching this assertion.
        self.assertEqual(result, ["valid_tx_id"])


if __name__ == "__main__":
    unittest.main()
