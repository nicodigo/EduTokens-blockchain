"""Unit tests for PoolCoordinator heartbeat-driven worker count."""

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from broker.messages import TaskMessage

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
        mock_method = MagicMock()
        mock_method.delivery_tag = 1
        self.pool._on_worker_result(self.channel, mock_method, None, msg)
        self.channel.basic_publish.assert_not_called()
        # Stale result should be acked (processed, just not forwarded)
        self.channel.basic_ack.assert_called_once_with(delivery_tag=1)


# ---------------------------------------------------------------------------
# Worker result ack/nack handling (audit H2)
# ---------------------------------------------------------------------------


def _fake_method(delivery_tag: int = 42) -> MagicMock:
    m = MagicMock()
    m.delivery_tag = delivery_tag
    return m


class TestWorkerResultAckHandling(unittest.TestCase):
    """H2: pool _on_worker_result must use auto_ack=False and explicitly
    ack/nack based on processing outcome."""

    def setUp(self) -> None:
        self.pool = PoolCoordinator(
            pool_id="test-pool", rmq_url="amqp://fake", worker_count=2,
        )
        self.channel = MagicMock()
        self.pool._channel = self.channel
        # Set up mining context so result passes stale check
        self.pool._current_block_index = 1
        self.pool._current_fingerprint = "abc123"
        self.pool._current_difficulty = 4
        self.pool._current_task_id = "t1"

    def _valid_result_body(self) -> bytes:
        """Return a ResultMessage body whose hash will pass
        Block.verify_result when patched."""
        return _result_msg(
            block_index=1, nonce=42, worker_id="w1",
            hash_val="0000b8d7deadbeefdeadbeefdeadbeef",
        ).to_json().encode()

    @patch("pool.pool.Block.verify_result")
    def test_acks_on_successful_forward(self, mock_verify: MagicMock) -> None:
        """H2: After valid PoW is forwarded to NCT, pool must ack the message."""
        mock_verify.return_value = (True, "0000b8d7deadbeefdeadbeefdeadbeef")

        method = _fake_method(42)
        self.pool._on_worker_result(self.channel, method, None,
                                     self._valid_result_body())

        # Must have forwarded to NCT
        self.channel.basic_publish.assert_called()
        # Must have acked the worker's result
        self.channel.basic_ack.assert_called_once_with(delivery_tag=42)
        self.channel.basic_nack.assert_not_called()

    @patch("pool.pool.Block.verify_result")
    def test_nacks_on_invalid_pow(self, mock_verify: MagicMock) -> None:
        """H2: Invalid PoW must be nacked (without requeue) — bad worker data."""
        mock_verify.return_value = (False, "deadbeefdeadbeefdeadbeefdeadbeef")

        method = _fake_method(99)
        self.pool._on_worker_result(self.channel, method, None,
                                     self._valid_result_body())

        self.channel.basic_publish.assert_not_called()
        self.channel.basic_nack.assert_called_once_with(
            delivery_tag=99, requeue=False,
        )
        self.channel.basic_ack.assert_not_called()

    def test_nacks_on_malformed_body(self) -> None:
        """H2: Malformed message body must be nacked, not crash."""
        method = _fake_method(7)
        self.pool._on_worker_result(self.channel, method, None, b"not valid json")

        self.channel.basic_nack.assert_called_once_with(
            delivery_tag=7, requeue=False,
        )
        self.channel.basic_ack.assert_not_called()

    def test_acks_stale_result(self) -> None:
        """H2: Stale result (wrong block_index) is handled and acked."""
        stale = _result_msg(block_index=999).to_json().encode()
        method = _fake_method(55)
        self.pool._on_worker_result(self.channel, method, None, stale)

        # Stale result is dropped, but we still ack since we processed it
        self.channel.basic_ack.assert_called_once_with(delivery_tag=55)
        self.channel.basic_nack.assert_not_called()
        self.channel.basic_publish.assert_not_called()

    @patch("pool.pool.Block.verify_result")
    def test_nacks_on_forward_failure(self, mock_verify: MagicMock) -> None:
        """H2: If forwarding to NCT fails, the worker's result must be nacked
        so another pool/worker can try."""
        mock_verify.return_value = (True, "0000b8d7deadbeefdeadbeefdeadbeef")
        self.channel.basic_publish.side_effect = ConnectionError("NCT unreachable")

        method = _fake_method(33)
        self.pool._on_worker_result(self.channel, method, None,
                                     self._valid_result_body())

        self.channel.basic_nack.assert_called_once_with(
            delivery_tag=33, requeue=True,
        )
        self.channel.basic_ack.assert_not_called()


class TestPoolNctHeartbeat(unittest.TestCase):
    """Audit H2 corrected: pool registers as a worker with the NCT.

    The ``_nct_heartbeat_loop`` creates its OWN RabbitMQ connection
    (thread-safety requirement — pika.BlockingConnection is not shared
    across threads).  We mock ``broker.broker.get_connection`` so the
    loop never touches a real broker.
    """

    def setUp(self):
        # Mock the connection that _nct_heartbeat_loop creates internally.
        # pool.py imports get_connection with `from broker.broker import`,
        # creating a local reference — we must patch the name in pool.pool.
        self._mock_conn_patcher = patch("pool.pool.get_connection")
        self._mock_conn = self._mock_conn_patcher.start()
        self._mock_props_patcher = patch("pool.pool.persistent_props")
        self._mock_props = self._mock_props_patcher.start()

        self._mock_channel = MagicMock()
        self._mock_conn.return_value.channel.return_value = self._mock_channel
        self._mock_conn.return_value.is_open = True
        self._mock_channel.is_open = True

        self.pool = PoolCoordinator(
            pool_id="pool-corrected",
            rmq_url="amqp://fake",
            worker_count=2,
        )
        # Shorter interval for testing
        self.pool._nct_heartbeat_interval = 0.01
        self.pool._shutdown.clear()

    def tearDown(self):
        self._mock_conn_patcher.stop()
        self._mock_props_patcher.stop()

    def test_heartbeat_publishes_to_worker_routing_key(self):
        """Pool heartbeat must use routing key worker.{pool_id}, not worker.*.*"""
        # Run one heartbeat iteration (kill after first publish)
        self._mock_channel.basic_publish.side_effect = (
            lambda **kw: self.pool._shutdown.set() or None  # stop after first
        )
        self.pool._nct_heartbeat_loop()

        self._mock_channel.basic_publish.assert_called_once()
        call_kw = self._mock_channel.basic_publish.call_args[1]
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

        self._mock_channel.basic_publish.side_effect = _fail_once
        self.pool._nct_heartbeat_loop()  # must not raise

        # Second call succeeded
        self.assertGreaterEqual(call_count, 2)


# ---------------------------------------------------------------------------
# Dead-worker monitor republish (audit M1)
# ---------------------------------------------------------------------------


class TestMonitorRepublishWithoutOverlap(unittest.TestCase):
    """M1: When workers die, the monitor must abort survivors before
    republishing the full range — otherwise surviving workers would keep
    working on their old sub-ranges while receiving new overlapping tasks."""

    def setUp(self) -> None:
        # _monitor_loop creates its OWN pika connection — mock it.
        # pool.py imports get_connection with `from broker.broker import`.
        self._mock_conn_patcher = patch("pool.pool.get_connection")
        self._mock_conn = self._mock_conn_patcher.start()
        self._mock_props_patcher = patch("pool.pool.persistent_props")
        self._mock_props = self._mock_props_patcher.start()

        self._mock_channel = MagicMock()
        self._mock_conn.return_value.channel.return_value = self._mock_channel
        self._mock_conn.return_value.is_open = True
        self._mock_channel.is_open = True

        self.pool = PoolCoordinator(
            pool_id="test-pool", rmq_url="amqp://fake", worker_count=3,
        )
        self.pool._channel = MagicMock()
        self.pool._monitor_interval = 0.01  # fast polling for test
        self.pool._result_timeout = 999  # don't trigger timeout

    def tearDown(self) -> None:
        self._mock_conn_patcher.stop()
        self._mock_props_patcher.stop()

    def test_abort_called_before_republish_on_worker_death(self):
        """M1: When worker count drops, _broadcast_abort must be called
        BEFORE publish_tasks so old sub-ranges are discarded cleanly."""
        # Set up mining context
        self.pool._current_block_index = 1
        self.pool._current_fingerprint = "abc123"
        self.pool._current_difficulty = 4
        self.pool._current_task_id = "t1"
        self.pool._current_nonce_space = 1_000_000
        self.pool._original_worker_count = 3

        # Mock _get_active_worker_count to simulate worker death
        call_seq = [3, 1, 0]  # first call: 3 alive → no republish
                              # second call: 1 alive → trigger republish
                              # third call: shutdown
        get_active_original = self.pool._get_active_worker_count

        def _fake_active():
            if not call_seq:
                self.pool._monitor_active.clear()
                return 0
            return call_seq.pop(0)

        # Patch the monitor to run a controlled iteration
        with patch.object(self.pool, "_get_active_worker_count",
                          side_effect=_fake_active):
            with patch.object(self.pool, "_broadcast_abort") as mock_abort:
                # Start monitoring, but stop after first republish
                self.pool._monitor_active.set()
                gen = self.pool._monitor_generation  # current generation

                # Run the monitor in a thread; kill after short delay
                t = threading.Thread(
                    target=self.pool._monitor_loop, args=(gen,), daemon=True,
                )
                t.start()
                t.join(timeout=1.0)

                # After worker count drops, abort must have been called
                # before publish_tasks republishes
                self.assertTrue(mock_abort.called,
                                "_broadcast_abort was NOT called when workers died")
                self.assertEqual(mock_abort.call_args[0], ("t1",))


# ---------------------------------------------------------------------------
# Monitor generation replacement (audit H1)
# ---------------------------------------------------------------------------


class TestMonitorGenerationReplacement(unittest.TestCase):
    """H1: The dead-worker monitor must be replaced (not reused) when a new
    mining task arrives, to prevent the TOCTOU race where the old monitor
    is still alive for a few milliseconds after the previous block finished."""

    def setUp(self) -> None:
        # _monitor_loop creates its OWN pika connection — mock it.
        # pool.py imports get_connection with `from broker.broker import`.
        self._mock_conn_patcher = patch("pool.pool.get_connection")
        self._mock_conn = self._mock_conn_patcher.start()
        self._mock_props_patcher = patch("pool.pool.persistent_props")
        self._mock_props = self._mock_props_patcher.start()

        self._mock_channel = MagicMock()
        self._mock_conn.return_value.channel.return_value = self._mock_channel
        self._mock_conn.return_value.is_open = True
        self._mock_channel.is_open = True

        self.pool = PoolCoordinator(
            pool_id="test-pool", rmq_url="amqp://fake", worker_count=2,
        )
        self.pool._channel = MagicMock()
        self.pool._monitor_interval = 0.01  # fast polling for test

    def tearDown(self) -> None:
        self._mock_conn_patcher.stop()
        self._mock_props_patcher.stop()

    def test_new_task_starts_fresh_monitor(self):
        """H1: _on_mining_task must always create a new monitor thread,
        even when the old one is still alive."""
        # Start a fake first monitor — set mining context so it stays alive
        self.pool._current_block_index = 1
        self.pool._current_fingerprint = "abc"
        self.pool._current_difficulty = 4
        self.pool._current_nonce_space = 1_000_000
        self.pool._original_worker_count = 2  # prevent republish trigger
        self.pool._monitor_active.set()
        gen_before = self.pool._monitor_generation
        old_thread = threading.Thread(
            target=self.pool._monitor_loop, args=(gen_before,), daemon=True,
        )
        old_thread.start()
        # Wait for the old monitor to enter its loop
        time.sleep(0.05)
        self.assertTrue(old_thread.is_alive(), "old monitor should be running")

        # Simulate a new mining task arriving — this is the H1 trigger
        self.pool._current_block_index = 2
        self.pool._monitor_generation += 1
        gen_after = self.pool._monitor_generation
        self.pool._monitor_active.set()
        new_thread = threading.Thread(
            target=self.pool._monitor_loop, args=(gen_after,), daemon=True,
        )
        new_thread.start()

        # Both threads are now running (brief overlap)
        time.sleep(0.1)
        # Old monitor must have exited (generation check)
        old_thread.join(timeout=0.5)
        self.assertFalse(old_thread.is_alive(),
                         "Old monitor should have exited due to generation bump")
        # New monitor should still be running
        self.assertTrue(new_thread.is_alive(),
                        "New monitor must be running with new generation")

        # Clean up
        self.pool._monitor_active.clear()
        new_thread.join(timeout=0.5)

    def test_monitor_ignores_stale_generation(self):
        """H1: A monitor started with an old generation must exit immediately
        when _monitor_generation no longer matches."""
        # Bump generation so the monitor we're about to start is already stale
        self.pool._monitor_generation = 42
        stale_gen = 41  # old generation
        self.pool._monitor_active.set()
        self.pool._current_block_index = 1

        # Start a monitor with a stale generation
        t = threading.Thread(
            target=self.pool._monitor_loop, args=(stale_gen,), daemon=True,
        )
        t.start()
        t.join(timeout=0.5)

        # The monitor must have exited quickly (generation mismatch)
        self.assertFalse(t.is_alive(),
                         "Monitor with stale generation must exit immediately")

    def test_monitor_exits_when_block_cleared(self):
        """H1: The monitor must exit when _monitor_active is cleared,
        even with a matching generation."""
        gen = self.pool._monitor_generation
        self.pool._monitor_active.set()
        self.pool._current_block_index = 1

        t = threading.Thread(
            target=self.pool._monitor_loop, args=(gen,), daemon=True,
        )
        t.start()
        time.sleep(0.05)
        self.assertTrue(t.is_alive(), "Monitor should be running")

        # Clear the active flag (simulating block mined)
        self.pool._monitor_active.clear()
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive(),
                         "Monitor must exit when _monitor_active is cleared")


# ---------------------------------------------------------------------------
# Worker registration (audit M3)
# ---------------------------------------------------------------------------


class TestWorkerRegistration(unittest.TestCase):
    """M3: Worker registration closes the "0 workers until first heartbeat" gap."""

    def setUp(self) -> None:
        self.pool = PoolCoordinator(
            pool_id="test-pool", rmq_url="amqp://fake", worker_count=2,
        )

    def test_registration_adds_worker(self):
        """M3: _on_worker_registration must add worker to _registered_workers."""
        self.pool._on_worker_registration(
            None, None, None,
            json.dumps({"worker_id": "w1", "pool_id": "test-pool", "timestamp": time.time()}).encode(),
        )
        self.assertIn("w1", self.pool._registered_workers)

    def test_registered_worker_is_counted_when_heartbeating(self):
        """M3: A registered worker with a heartbeat must be counted as active."""
        self.pool._on_worker_registration(
            None, None, None,
            json.dumps({"worker_id": "w1", "pool_id": "test-pool", "timestamp": time.time()}).encode(),
        )
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        self.assertEqual(self.pool._get_active_worker_count(), 1)

    def test_unregistered_worker_not_counted_when_registrations_exist(self):
        """M3: A heartbeating but unregistered worker must NOT be counted
        when other workers HAVE registered."""
        # Register w1 but not w2
        self.pool._on_worker_registration(
            None, None, None,
            json.dumps({"worker_id": "w1", "pool_id": "test-pool", "timestamp": time.time()}).encode(),
        )
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w2"))
        # Only w1 should count (registered + heartbeating)
        self.assertEqual(self.pool._get_active_worker_count(), 1)

    def test_fallback_works_when_no_registrations(self):
        """M3: When no registrations have arrived, fall back to counting
        all ready workers (backward compatibility with solo workers)."""
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w1"))
        self.pool._on_worker_heartbeat(None, None, None, _heartbeat_json("w2"))
        # No registrations → count all ready workers
        self.assertEqual(self.pool._get_active_worker_count(), 2)


# ---------------------------------------------------------------------------
# Routing key hygiene (audit M1 + L3)
# ---------------------------------------------------------------------------


class TestRoutingKeyHygiene(unittest.TestCase):
    """M1 + L3: Pool worker heartbeats use pool-worker.*, pool status uses pool.*.status."""

    def setUp(self) -> None:
        self.pool = PoolCoordinator(
            pool_id="test-pool", rmq_url="amqp://fake", worker_count=2,
        )
        self.pool._channel = MagicMock()

    def test_pool_worker_registry_binds_to_pool_worker_routing_key(self):
        """M1: Pool's registry queue must bind to pool-worker.{pool_id}.*,
        NOT worker.{pool_id}.*."""
        self.pool.run = lambda: None  # prevent actual run
        # Simulate what run() would do — check the topology setup function
        from pool.pool import _setup_pool_topology
        _setup_pool_topology(self.pool._channel, self.pool.pool_id)
        bind_calls = [
            c.kwargs.get("routing_key", "")
            for c in self.pool._channel.queue_bind.call_args_list
        ]
        registry_bindings = [rk for rk in bind_calls if "registry" in str(rk) or "pool-worker" in rk]
        # Must have a pool-worker binding, NOT worker.{pool_id}.*
        self.assertTrue(
            any("pool-worker" in rk for rk in registry_bindings),
            f"Registry must bind to pool-worker.*, got: {registry_bindings}",
        )
        self.assertFalse(
            any(rk.startswith("worker.") for rk in registry_bindings),
            f"Registry must NOT bind to worker.*, got: {registry_bindings}",
        )

    def test_pool_status_publishes_to_pool_routing_key(self):
        """L3: pool_no_workers must publish to pool.{pool_id}.status,
        NOT worker.{pool_id}.status."""
        # Trigger pool_no_workers by setting count to 0 with fallback=0
        self.pool._worker_count_fallback = 0
        task = TaskMessage(
            task_id="t1", block_index=1, fingerprint="abc",
            difficulty=4, range_min=0, range_max=999,
        )
        mock_ch = MagicMock()
        self.pool._on_mining_task(mock_ch, MagicMock(), None, task.to_json().encode())
        publish_calls = [
            kwargs.get("routing_key", "")
            for call in self.pool._channel.basic_publish.call_args_list
            for _, kwargs in [call]
        ]
        status_calls = [rk for rk in publish_calls if "status" in rk]
        self.assertTrue(
            any(rk.startswith("pool.") and rk.endswith(".status") for rk in status_calls),
            f"pool_no_workers must use pool.*.status, got: {status_calls}",
        )


if __name__ == "__main__":
    unittest.main()
