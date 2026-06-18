"""Validated environment-variable helpers.

Drop-in replacements for ``int(os.getenv(...))`` that produce clear,
actionable error messages instead of opaque ``ValueError`` tracebacks.
"""

from __future__ import annotations

import os


def env_int(name: str, default: int, min_val: int = 0) -> int:
    """Read *name* from the environment, returning *default* when unset.

    Raises ``SystemExit`` with a descriptive message when the value cannot
    be parsed as an integer or falls below *min_val*.
    """
    raw = os.getenv(name, str(default))
    try:
        val = int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid {name}={raw!r} — must be an integer"
        ) from None
    if val < min_val:
        raise SystemExit(
            f"Invalid {name}={val} — must be >= {min_val}"
        )
    return val


def env_float(name: str, default: float, min_val: float = 0.0) -> float:
    """Read *name* from the environment, returning *default* when unset.

    Raises ``SystemExit`` with a descriptive message when the value cannot
    be parsed as a float or falls below *min_val*.
    """
    raw = os.getenv(name, str(default))
    try:
        val = float(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid {name}={raw!r} — must be a number"
        ) from None
    if val < min_val:
        raise SystemExit(
            f"Invalid {name}={val} — must be >= {min_val}"
        )
    return val
