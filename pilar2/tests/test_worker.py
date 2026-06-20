"""Unit tests for worker service and NCT dynamic worker tracking."""

import time
import unittest
from unittest.mock import MagicMock, patch

from miner.miner import MinerError, MinerResult
from nct.state import NCTConfig, NCTState


# ---------------------------------------------------------------------------
# NCTState worker registry
# ---------------------------------------------------------------------------


class TestNCTStateWorkerRegistry(unittest.TestCase):
    def test_single_worker_active(self):
        state = NCTState(worker_timeout=60)
        state.update_worker("worker-1")
        self.assertEqual(state.get_active_worker_count(), 1)

    def test_multiple_workers(self):
        state = NCTState(worker_timeout=60)
        state.update_worker("worker-1")
        state.update_worker("worker-2")
        state.update_worker("worker-3")
        self.assertEqual(state.get_active_worker_count(), 3)

    def test_worker_expiry(self):
        state = NCTState(worker_timeout=0.1)  # 100ms timeout
        state.update_worker("worker-1")
        self.assertEqual(state.get_active_worker_count(), 1)

        time.sleep(0.15)
        self.assertEqual(state.get_active_worker_count(), 0)

    def test_active_workers_snapshot(self):
        state = NCTState(worker_timeout=60)
        state.update_worker("worker-b")
        state.update_worker("worker-a")
        state.update_worker("worker-c")
        self.assertEqual(state.active_workers_snapshot(), ["worker-a", "worker-b", "worker-c"])

    def test_update_resets_expiry(self):
        state = NCTState(worker_timeout=0.3)
        state.update_worker("worker-1")
        time.sleep(0.15)
        state.update_worker("worker-1")  # reset timer
        time.sleep(0.15)  # only 0.15s since reset, not 0.3
        self.assertEqual(state.get_active_worker_count(), 1)


class TestNCTConfigDefaults(unittest.TestCase):
    def test_defaults(self):
        config = NCTConfig()
        self.assertEqual(config.worker_count, 2)
        self.assertEqual(config.heartbeat_timeout, 15.0)
        self.assertEqual(config.heartbeat_interval, 5.0)


# ---------------------------------------------------------------------------
# WorkerService _on_task error handling (audit H1+M4)
# ---------------------------------------------------------------------------


class TestWorkerOnTaskErrorHandling(unittest.TestCase):
    """Tests for _on_task resilience — MinerError / PermissionError should
    not crash the pika consumer.  Instead the message must be requeued via
    basic_nack."""

    def setUp(self) -> None:
        from worker.worker import WorkerService
        self.worker = WorkerService(
            worker_id="test-worker",
            rmq_url="amqp://fake",
            miner_binary="python3 /fake/miner.py",
        )
        # Replace the real MinerService with a mock
        self.mock_miner = MagicMock()
        self.worker.miner = self.mock_miner

    def test_on_task_miner_error_dead_letters(self):
        """MinerError (binary crash) must dead-letter the message (requeue=False),
        not requeue infinitely.  _do_task catches MinerError internally."""
        self.mock_miner.mine_cancellable.side_effect = MinerError(
            "simulated miner crash",
        )

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 42

        task_body = (
            b'{"task_id":"t1","block_index":1,"fingerprint":"abc",'
            b'"difficulty":4,"range_min":0,"range_max":999}'
        )

        # This must NOT raise — MinerError caught inside _do_task
        self.worker._on_task(mock_ch, mock_method, None, task_body)

        # Must dead-letter (requeue=False) — binary crash is not transient
        mock_ch.basic_nack.assert_called_once_with(
            delivery_tag=42, requeue=False
        )
        mock_ch.basic_ack.assert_not_called()

    def test_on_task_unexpected_error_nacks_and_requeues(self):
        """H1+M4: Any unexpected Exception in _on_task must also nack+requeue,
        not crash the consumer."""
        self.mock_miner.mine_cancellable.side_effect = RuntimeError(
            "unexpected failure",
        )

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 99

        task_body = (
            b'{"task_id":"t2","block_index":2,"fingerprint":"def",'
            b'"difficulty":4,"range_min":0,"range_max":999}'
        )

        # This must NOT raise
        self.worker._on_task(mock_ch, mock_method, None, task_body)

        mock_ch.basic_nack.assert_called_once_with(
            delivery_tag=99, requeue=True
        )
        mock_ch.basic_ack.assert_not_called()


# ---------------------------------------------------------------------------
# WorkerService cancellable mining (audit C1)
# ---------------------------------------------------------------------------


class TestWorkerCancellableMining(unittest.TestCase):
    """C1: _do_task must use mine_cancellable with Popen + polling so that
    abort signals (control messages) can interrupt a running mining task."""

    def setUp(self) -> None:
        from worker.worker import WorkerService
        self.worker = WorkerService(
            worker_id="test-worker",
            rmq_url="amqp://fake",
            miner_binary="python3 /fake/miner.py",
        )
        self.mock_miner = MagicMock()
        self.worker.miner = self.mock_miner
        self.worker._connection = MagicMock()
        self.worker._channel = MagicMock()

    def _task_body(self) -> bytes:
        return (
            b'{"task_id":"t1","block_index":1,"fingerprint":"abc",'
            b'"difficulty":4,"range_min":0,"range_max":999}'
        )

    def test_normal_mining_publishes_result_and_acks(self):
        """C1: When miner finds a solution, result is published and message acked."""
        self.mock_miner.mine_cancellable.return_value = MinerResult(
            nonce=42, hash="0000b8d7deadbeefdeadbeefdeadbeef",
        )

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 10

        self.worker._on_task(mock_ch, mock_method, None, self._task_body())

        # Must have acked
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=10)
        mock_ch.basic_nack.assert_not_called()
        # Must have published result
        self.worker._channel.basic_publish.assert_called()
        self.assertEqual(self.worker.tasks_processed, 1)

    def test_no_solution_acks_without_publishing(self):
        """C1: When miner finds no solution, ack but don't publish."""
        self.mock_miner.mine_cancellable.return_value = None
        # Simulate: aborted is NOT set (no solution, not abort)
        self.worker._aborted.clear()

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 20

        self.worker._on_task(mock_ch, mock_method, None, self._task_body())

        mock_ch.basic_ack.assert_called_once_with(delivery_tag=20)
        mock_ch.basic_nack.assert_not_called()
        # No result to publish
        self.worker._channel.basic_publish.assert_not_called()
        self.assertEqual(self.worker.tasks_processed, 1)

    def test_abort_during_mining_acks_without_publishing(self):
        """C1: When abort is received during mining, ack and discard."""
        # Simulate abort arriving DURING mining: mine_cancellable sets
        # the event (as the polling loop would) and returns None.
        def _abort_during_mining(*args, **kwargs):
            self.worker._aborted.set()
            return None
        self.mock_miner.mine_cancellable.side_effect = _abort_during_mining

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 30

        self.worker._on_task(mock_ch, mock_method, None, self._task_body())

        # Aborted tasks are acked (not requeued — another worker already won)
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=30)
        mock_ch.basic_nack.assert_not_called()
        self.worker._channel.basic_publish.assert_not_called()
        # tasks_processed should NOT increment for aborted tasks
        self.assertEqual(self.worker.tasks_processed, 0)

    def test_mine_cancellable_receives_abort_event(self):
        """C1: _do_task must call mine_cancellable with abort_event=self._aborted."""
        self.mock_miner.mine_cancellable.return_value = MinerResult(
            nonce=7, hash="0000aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 5

        self.worker._on_task(mock_ch, mock_method, None, self._task_body())

        # Verify mine_cancellable was called with abort_event
        self.mock_miner.mine_cancellable.assert_called_once()
        call_kwargs = self.mock_miner.mine_cancellable.call_args[1]
        self.assertIn("abort_event", call_kwargs)
        self.assertIs(call_kwargs["abort_event"], self.worker._aborted)

    def test_miner_error_dead_letters_in_do_task(self):
        """When mine_cancellable raises MinerError, _do_task must nack
        with requeue=False (dead-letter) and NOT propagate the exception."""
        self.mock_miner.mine_cancellable.side_effect = MinerError(
            "CUDA error: no CUDA-capable device is detected",
        )

        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 77

        # Must NOT raise — MinerError caught inside _do_task
        self.worker._on_task(mock_ch, mock_method, None, self._task_body())

        # Must dead-letter (requeue=False) to avoid infinite loop
        mock_ch.basic_nack.assert_called_once_with(
            delivery_tag=77, requeue=False,
        )
        mock_ch.basic_ack.assert_not_called()
        # Must NOT have published any result
        self.worker._channel.basic_publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
