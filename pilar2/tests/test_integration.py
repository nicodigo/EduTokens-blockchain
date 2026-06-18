"""Minimal integration smoke tests (audit M4 — test gaps).

These tests wire up the full NCT FastAPI app with mocked Redis and
RabbitMQ to verify that the HTTP endpoints work together correctly.

They do NOT require Docker, a real Redis instance, or a real RabbitMQ
broker — all external dependencies are mocked at the Python level.

.. note::

    ``POST /transaction`` with a *valid* Ed25519 signature cannot be
    tested end-to-end through the HTTP layer because
    ``Transaction.timestamp`` is regenerated server-side (different
    ``time.time()`` → different ``tx_id`` → signature mismatch).
    This is a pre-existing architectural issue (transaction timestamps
    must travel through the API) that is outside the scope of audit-05.
"""

import json
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from nct.nct import create_health_app
from nct.state import NCTConfig, NCTState
from tests._crypto_fixtures import make_keypair


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app_with_blocks(n_blocks: int = 5) -> TestClient:
    """Create a test app with *n_blocks* of chain in the Redis mock."""
    from shared.block import Block

    client = MagicMock()
    client.llen.return_value = n_blocks
    client.get.return_value = b"1000"  # balance / nonce placeholder

    block_template = Block.create_genesis()
    genesis_json = json.dumps(block_template.to_dict(), sort_keys=True)

    def _lindex(key: str, index: int) -> bytes | None:
        if key == "blockchain:blocks" and 0 <= index < n_blocks:
            return genesis_json.encode()
        return None

    client.lindex.side_effect = _lindex

    state = NCTState()
    state.chain_height = n_blocks

    config = NCTConfig(rate_limit="1000/minute")
    config.authority_pubkey = "A" * 64

    app = create_health_app(state, client, config)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestAppIntegration(unittest.TestCase):
    """End-to-end smoke tests for the FastAPI app (mocked infrastructure)."""

    def setUp(self):
        self.tc = _build_app_with_blocks(n_blocks=5)

    def test_health_returns_ok(self):
        resp = self.tc.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_status_returns_chain_height(self):
        resp = self.tc.get("/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["chain_height"], 5)
        self.assertEqual(data["pending_transactions"], 0)
        self.assertIsNone(data["current_block"])

    def test_chain_returns_blocks(self):
        resp = self.tc.get("/chain")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertLessEqual(len(data), 20)
        self.assertGreater(len(data), 0)

    def test_chain_pagination_start_count(self):
        resp = self.tc.get("/chain", params={"start": 1, "count": 2})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertLessEqual(len(data), 2)

    def test_chain_count_zero_returns_empty(self):
        resp = self.tc.get("/chain", params={"count": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


class TestTransactionSubmission(unittest.TestCase):
    """Test the POST /transaction flow with a full app."""

    def setUp(self):
        self.authority_priv, self.authority_pub, _ = make_keypair()
        self.alice_priv, self.alice_pub, _ = make_keypair()

    def _build_app(self, authority_pubkey: str) -> TestClient:
        client = MagicMock()
        client.llen.return_value = 1  # genesis block
        client.get.return_value = b"0"  # nonce = 0

        state = NCTState()
        state.chain_height = 1

        config = NCTConfig(rate_limit="1000/minute")
        config.authority_pubkey = authority_pubkey

        app = create_health_app(state, client, config)
        return TestClient(app)

    def test_invalid_signature_rejected_400(self):
        """A transaction with a bad signature is rejected (400)."""
        tc = self._build_app(self.authority_pub)

        tx_data = {
            "sender_pubkey": self.authority_pub,
            "receiver_pubkey": self.alice_pub,
            "amount": 5,
            "tx_type": "EARN",
            "concept": "TP1",
            "signature": "B" * 128,  # obviously wrong
            "nonce": 0,
        }

        resp = tc.post("/transaction", json=tx_data)
        self.assertEqual(resp.status_code, 400,
                         f"Expected 400, got {resp.status_code}: {resp.json()}")
        self.assertIn("signature", resp.json()["error"].lower())

    def test_balance_endpoint_returns_structure(self):
        """GET /balance/{address} returns expected JSON shape."""
        from shared.crypto import pubkey_to_address

        client = MagicMock()
        client.llen.return_value = 1
        client.get.return_value = b"5000"  # balance key

        state = NCTState()
        state.chain_height = 1

        config = NCTConfig(rate_limit="1000/minute")
        config.authority_pubkey = self.authority_pub

        app = create_health_app(state, client, config)
        with TestClient(app) as tc:
            addr = pubkey_to_address(self.alice_pub)
            resp = tc.get(f"/balance/{addr}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("balance", data)
            self.assertIn("address", data)
            self.assertEqual(data["balance"], 5000)

    def test_account_endpoint_returns_nonce_and_discarded(self):
        """GET /account/{pubkey} returns nonce and discarded_transactions."""
        from shared.crypto import pubkey_to_address

        client = MagicMock()
        client.llen.return_value = 1
        client.get.return_value = b"0"  # nonce

        state = NCTState()
        state.chain_height = 1

        config = NCTConfig(rate_limit="1000/minute")
        config.authority_pubkey = self.authority_pub

        app = create_health_app(state, client, config)
        with TestClient(app) as tc:
            addr = pubkey_to_address(self.alice_pub)
            resp = tc.get(f"/account/{addr}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("nonce", data)
            self.assertIn("discarded_transactions", data)
            self.assertIsInstance(data["discarded_transactions"], list)


if __name__ == "__main__":
    unittest.main()
