"""Unit tests for PoolCoordinator heartbeat-driven worker count."""

import json
import time
import unittest
from unittest.mock import MagicMock

from broker.messages import ResultMessage
from pool.pool import PoolCoordinator


def _heartbeat_json(worker_id: str, ts: float | None = None) -> bytes:
    return json.dumps({
        "worker_id": worker_id,
        "action": "heartbeat",
        "timestamp": ts if ts is not None else time.time(),
    }).encode()


def _result_msg(block_index: int = 1, nonce: int = 42,
                worker_id: str = "w1", hash_val: str = "0000dead") -> ResultMessage:
    return ResultMessage(
        task_id="t1", block_index=block_index,
        worker_id=worker_id, nonce=nonce, hash=hash_val,
    )


# ---------------------------------------------------------------------------
# Heartbeat tracking (unit — no RabbitMQ needed)
# ---------------------------------------------------------------------------


class TestHeartbeatTracking(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = PoolCoordinator(
            pool_id="test-pool",
            rmq_url="amqp://fake",
            worker_count=2,
            heartbeat_timeout=0.3,
        )

    def test_heartbeat_registers_worker(self):
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        self.assertEqual(self.pool._get_active_worker_count(), 1)

    def test_multiple_workers(self):
        for wid in ("w1", "w2", "w3"):
            self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json(wid))
        self.assertEqual(self.pool._get_active_worker_count(), 3)

    def test_worker_expiry(self):
        # Timestamps from workers are now ignored (pool uses its own clock).
        # To test expiry we must actually wait past the heartbeat_timeout.
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        self.assertEqual(self.pool._get_active_worker_count(), 1)
        time.sleep(0.35)  # heartbeat_timeout is 0.3 s
        self.assertEqual(self.pool._get_active_worker_count(), 0)

    def test_worker_stays_with_recent_heartbeat(self):
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        time.sleep(0.1)
        self.assertEqual(self.pool._get_active_worker_count(), 1)

    def test_new_heartbeat_resets_expiry(self):
        # Send first heartbeat, wait a bit, send another to reset the timer.
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        time.sleep(0.15)  # still well within 0.3 s timeout
        # Second heartbeat resets the expiry clock
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        # First heartbeat would be ~0.35 s old, but second is only ~0.2 s
        time.sleep(0.2)
        self.assertEqual(self.pool._get_active_worker_count(), 1)

    def test_fallback_when_no_heartbeats(self):
        self.assertEqual(self.pool._get_active_worker_count(), 0)
        self.assertEqual(self.pool._worker_count_fallback, 2)


# ---------------------------------------------------------------------------
# Result handling (stale check)
# ---------------------------------------------------------------------------


class TestResultStaleCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = PoolCoordinator(pool_id="test-pool", rmq_url="amqp://fake", worker_count=2)
        self.channel = MagicMock()
        self.pool._channel = self.channel
        self.pool._current_block_index = 1

    def test_drops_stale_result(self):
        msg = _result_msg(block_index=99).to_json().encode()
        self.pool._on_worker_result(None, None, None, msg)
        self.channel.basic_publish.assert_not_called()


class TestPoolNctHeartbeat(unittest.TestCase):
    """Audit H2 corrected: pool registers as a worker with the NCT."""

    def setUp(self):
        self.pool = PoolCoordinator(
            pool_id="pool-corrected",
            rmq_url="amqp://fake",
            worker_count=2,
        )
        self.pool._channel = MagicMock()
        # Shorter interval for testing
        self.pool._nct_heartbeat_interval = 0.01
        self.pool._shutdown.clear()

    def test_heartbeat_publishes_to_worker_routing_key(self):
        """Pool heartbeat must use routing key worker.{pool_id}, not worker.*.*"""
        # Run one heartbeat iteration (kill after first publish)
        self.pool._channel.basic_publish.side_effect = (
            lambda **kw: self.pool._shutdown.set() or None  # stop after first
        )
        self.pool._nct_heartbeat_loop()

        self.pool._channel.basic_publish.assert_called_once()
        call_kw = self.pool._channel.basic_publish.call_args[1]
        self.assertEqual(call_kw["exchange"], "blockchain")
        self.assertEqual(call_kw["routing_key"], "worker.pool-corrected")

        body = json.loads(call_kw["body"])
        self.assertEqual(body["worker_id"], "pool-corrected")
        self.assertEqual(body["role"], "pool")
        self.assertIn("timestamp", body)

    def test_heartbeat_survives_channel_error(self):
        """Heartbeat loop must not crash when channel throws — it should
        catch and continue (debug-level log only)."""
        call_count = 0

        def _fail_once(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("simulated channel error")
            self.pool._shutdown.set()  # stop after second attempt
            return None

        self.pool._channel.basic_publish.side_effect = _fail_once
        self.pool._nct_heartbeat_loop()  # must not raise

        # Second call succeeded
        self.assertGreaterEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
