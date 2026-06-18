"""Unit tests for broker topology and publish operations (mock pika)."""

import json
import unittest
from unittest.mock import MagicMock, call, patch

from broker.broker import (
    EXCHANGE,
    RESULTS_QUEUE,
    WORKER_REGISTRY_QUEUE,
    broadcast_abort,
    declare_consumer_queue,
    declare_topology,
    is_recoverable_rabbitmq_error,
    persistent_props,
    publish_mining_task,
    publish_tasks,
)
from broker.messages import ControlMessage, ResultMessage, TaskMessage


# ---------------------------------------------------------------------------
# Messages (pure dataclasses — no mocking needed)
# ---------------------------------------------------------------------------


class TestTaskMessage(unittest.TestCase):
    def test_create_and_serialise(self):
        task = TaskMessage.create(
            block_index=2,
            fingerprint="abc123",
            difficulty=4,
            range_min=0,
            range_max=100,
        )
        self.assertIsNotNone(task.task_id)
        self.assertEqual(task.block_index, 2)
        self.assertEqual(task.difficulty, 4)

        raw = task.to_json()
        restored = TaskMessage.from_json(raw)
        self.assertEqual(restored.task_id, task.task_id)
        self.assertEqual(restored.fingerprint, "abc123")
        self.assertEqual(restored.range_max, 100)


class TestResultMessage(unittest.TestCase):
    def test_roundtrip(self):
        result = ResultMessage(
            task_id="t1",
            block_index=2,
            worker_id="w1",
            nonce=42,
            hash="0000abcd1234",
        )
        raw = result.to_json()
        restored = ResultMessage.from_json(raw)
        self.assertEqual(restored.worker_id, "w1")
        self.assertEqual(restored.nonce, 42)
        self.assertEqual(restored.hash, "0000abcd1234")


class TestControlMessage(unittest.TestCase):
    def test_roundtrip(self):
        msg = ControlMessage(action="abort", task_id="t1")
        raw = msg.to_json()
        restored = ControlMessage.from_json(raw)
        self.assertEqual(restored.action, "abort")
        self.assertEqual(restored.task_id, "t1")


# ---------------------------------------------------------------------------
# Topology declaration
# ---------------------------------------------------------------------------


class TestDeclareTopology(unittest.TestCase):
    def test_creates_exchange_and_queues(self):
        channel = MagicMock()
        declare_topology(channel)

        channel.exchange_declare.assert_called_once_with(
            exchange=EXCHANGE, exchange_type="topic", durable=True
        )
        channel.queue_bind.assert_any_call(
            exchange=EXCHANGE, queue=RESULTS_QUEUE, routing_key="result.*"
        )
        channel.queue_bind.assert_any_call(
            exchange=EXCHANGE, queue=WORKER_REGISTRY_QUEUE, routing_key="worker.*"
        )
        # Audit L1: return callback registered
        channel.add_on_return_callback.assert_called_once()


class TestDeclareConsumerQueue(unittest.TestCase):
    """Tests for declare_consumer_queue — used by solo workers."""

    def test_declares_durable_queue_and_binds(self):
        channel = MagicMock()
        declare_consumer_queue(channel, "worker.abc.inbox", "task.mining")

        channel.queue_declare.assert_called_once_with(
            queue="worker.abc.inbox", durable=True,
        )
        channel.queue_bind.assert_called_once_with(
            exchange=EXCHANGE, queue="worker.abc.inbox", routing_key="task.mining",
        )


# ---------------------------------------------------------------------------
# Publish operations
# ---------------------------------------------------------------------------


class TestPublishMiningTask(unittest.TestCase):
    def test_publishes_with_correct_routing_key_and_properties(self):
        channel = MagicMock()
        task = publish_mining_task(
            channel, block_index=1, fingerprint="ff",
            difficulty=4, range_size=100,
        )

        self.assertEqual(task.block_index, 1)
        channel.basic_publish.assert_called_once()
        call_args = channel.basic_publish.call_args
        self.assertEqual(call_args[1]["exchange"], EXCHANGE)
        self.assertEqual(call_args[1]["routing_key"], "task.mining")
        # Audit H1: persistent
        props = call_args[1]["properties"]
        self.assertEqual(props.delivery_mode, 2)
        # Audit L1: mandatory
        self.assertTrue(call_args[1]["mandatory"])


class TestPublishTasks(unittest.TestCase):
    def test_partitions_nonce_space(self):
        channel = MagicMock()
        tasks = publish_tasks(
            channel, block_index=1, fingerprint="f", difficulty=2,
            num_workers=3, range_size=300,
            routing_key_prefix="task",
        )

        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].range_min, 0)
        self.assertEqual(tasks[0].range_max, 99)
        self.assertEqual(tasks[1].range_min, 100)
        self.assertEqual(tasks[1].range_max, 199)
        self.assertEqual(tasks[2].range_min, 200)
        self.assertEqual(tasks[2].range_max, 299)

        self.assertEqual(channel.basic_publish.call_count, 3)

    def test_last_range_absorbs_remainder(self):
        channel = MagicMock()
        tasks = publish_tasks(
            channel, block_index=1, fingerprint="f", difficulty=2,
            num_workers=3, range_size=100,
            routing_key_prefix="task",
        )
        self.assertEqual(tasks[-1].range_max, 99)

    def test_requires_routing_key_prefix(self):
        """M2: routing_key_prefix is required — TypeError without it."""
        channel = MagicMock()
        with self.assertRaises(TypeError):
            publish_tasks(
                channel, block_index=1, fingerprint="f", difficulty=2,
                num_workers=3, range_size=100,
                # missing routing_key_prefix
            )

    def test_publishes_with_persistent_and_mandatory(self):
        """H1 + L1: each task publish must have delivery_mode=2 and mandatory=True."""
        channel = MagicMock()
        publish_tasks(
            channel, block_index=1, fingerprint="f", difficulty=2,
            num_workers=1, range_size=100,
            routing_key_prefix="pool.abc.task",
        )
        call_args = channel.basic_publish.call_args
        props = call_args[1]["properties"]
        self.assertEqual(props.delivery_mode, 2)
        self.assertTrue(call_args[1]["mandatory"])


class TestBroadcastAbort(unittest.TestCase):
    def test_publishes_control_message(self):
        channel = MagicMock()
        broadcast_abort(channel, "task-abc")

        channel.basic_publish.assert_called_once()
        call_args = channel.basic_publish.call_args
        self.assertEqual(call_args[1]["exchange"], EXCHANGE)
        self.assertEqual(call_args[1]["routing_key"], "control")

        body = json.loads(call_args[1]["body"])
        self.assertEqual(body["action"], "abort")
        self.assertEqual(body["task_id"], "task-abc")

        # H1 + L1
        props = call_args[1]["properties"]
        self.assertEqual(props.delivery_mode, 2)
        self.assertTrue(call_args[1]["mandatory"])


# ---------------------------------------------------------------------------
# Persistent properties  (audit H1)
# ---------------------------------------------------------------------------


class TestPersistentProps(unittest.TestCase):
    """Verify that persistent_props() returns delivery_mode=2."""

    def test_returns_basic_properties_with_delivery_mode_2(self):
        props = persistent_props()
        self.assertEqual(props.delivery_mode, 2)

    def test_cache_reuses_same_instance(self):
        a = persistent_props()
        b = persistent_props()
        self.assertIs(a, b, "persistent_props() should cache the same instance")


# ---------------------------------------------------------------------------
# Reconnection helpers  (audit H2)
# ---------------------------------------------------------------------------


class TestIsRecoverableError(unittest.TestCase):
    def test_connection_closed_is_recoverable(self):
        # Duck-type: type name contains "ConnectionClosed"
        class ConnectionClosed(Exception):
            pass
        exc = ConnectionClosed("boom")
        self.assertTrue(is_recoverable_rabbitmq_error(exc))

    def test_stream_lost_is_recoverable(self):
        # Duck-type: type name contains "StreamLostError"
        class StreamLostError(Exception):
            pass
        exc = StreamLostError("boom")
        self.assertTrue(is_recoverable_rabbitmq_error(exc))

    def test_value_error_is_not_recoverable(self):
        exc = ValueError("bad value")
        self.assertFalse(is_recoverable_rabbitmq_error(exc))


if __name__ == "__main__":
    unittest.main()
