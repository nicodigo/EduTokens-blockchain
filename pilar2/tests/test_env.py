"""Unit tests for shared/env.py — validated environment-variable helpers."""

import os
import unittest
from unittest.mock import patch

from shared.env import env_float, env_int


# ---------------------------------------------------------------------------
# env_int
# ---------------------------------------------------------------------------


class TestEnvInt(unittest.TestCase):
    def test_returns_default_when_var_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env_int("PORT", 8080), 8080)

    def test_returns_value_from_env(self):
        with patch.dict(os.environ, {"PORT": "3000"}):
            self.assertEqual(env_int("PORT", 8080), 3000)

    def test_negative_values_allowed_with_low_min(self):
        with patch.dict(os.environ, {"OFFSET": "-5"}):
            self.assertEqual(env_int("OFFSET", 0, min_val=-10), -5)

    def test_min_val_rejects_below_threshold(self):
        with patch.dict(os.environ, {"SCORE": "3"}):
            with self.assertRaises(SystemExit) as ctx:
                env_int("SCORE", 10, min_val=5)
            self.assertIn("must be >= 5", str(ctx.exception))

    def test_min_val_zero_default_is_noop(self):
        with patch.dict(os.environ, {"WORKERS": "0"}):
            self.assertEqual(env_int("WORKERS", 2), 0)

    def test_min_val_negative_allows_negative(self):
        with patch.dict(os.environ, {"DELTA": "-100"}):
            self.assertEqual(env_int("DELTA", 0, min_val=-1000), -100)

    def test_invalid_string_raises_systemexit(self):
        with patch.dict(os.environ, {"DIFFICULTY": "abc"}):
            with self.assertRaises(SystemExit) as ctx:
                env_int("DIFFICULTY", 4)
            self.assertIn("must be an integer", str(ctx.exception))
            self.assertIn("DIFFICULTY", str(ctx.exception))

    def test_float_string_raises_systemexit_for_env_int(self):
        with patch.dict(os.environ, {"COUNT": "3.14"}):
            with self.assertRaises(SystemExit) as ctx:
                env_int("COUNT", 1)
            self.assertIn("must be an integer", str(ctx.exception))

    def test_empty_string_raises_systemexit(self):
        with patch.dict(os.environ, {"SIZE": ""}):
            with self.assertRaises(SystemExit):
                env_int("SIZE", 10)


# ---------------------------------------------------------------------------
# env_float
# ---------------------------------------------------------------------------


class TestEnvFloat(unittest.TestCase):
    def test_returns_default_when_var_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env_float("TIMEOUT", 30.0), 30.0)

    def test_returns_value_from_env(self):
        with patch.dict(os.environ, {"TIMEOUT": "45.5"}):
            self.assertEqual(env_float("TIMEOUT", 30.0), 45.5)

    def test_integer_string_accepted(self):
        with patch.dict(os.environ, {"INTERVAL": "10"}):
            self.assertEqual(env_float("INTERVAL", 5.0), 10.0)

    def test_negative_accepted_when_min_val_allows(self):
        with patch.dict(os.environ, {"RATE": "-0.5"}):
            self.assertEqual(env_float("RATE", 1.0, min_val=-10.0), -0.5)

    def test_min_val_rejects_below_threshold(self):
        with patch.dict(os.environ, {"SPEED": "0.1"}):
            with self.assertRaises(SystemExit) as ctx:
                env_float("SPEED", 1.0, min_val=1.0)
            self.assertIn("must be >= 1.0", str(ctx.exception))

    def test_invalid_string_raises_systemexit(self):
        with patch.dict(os.environ, {"DELAY": "fast"}):
            with self.assertRaises(SystemExit) as ctx:
                env_float("DELAY", 1.0)
            self.assertIn("must be a number", str(ctx.exception))
            self.assertIn("DELAY", str(ctx.exception))

    def test_empty_string_raises_systemexit(self):
        with patch.dict(os.environ, {"WEIGHT": ""}):
            with self.assertRaises(SystemExit):
                env_float("WEIGHT", 0.0)
