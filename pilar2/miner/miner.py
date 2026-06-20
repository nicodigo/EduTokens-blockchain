"""CUDA miner worker — wraps the Pilar 1 binary as a callable service."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class MinerResult:
    """Successful Proof-of-Work solution returned by the CUDA miner."""

    nonce: int
    hash: str  # MD5 hex digest (32 chars), e.g. "0000004a7d..."

    def __repr__(self) -> str:
        return f"MinerResult(nonce={self.nonce}, hash={self.hash})"


class MinerError(Exception):
    """Raised when the CUDA miner subprocess fails (crashes, timeouts, etc.)."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_OUTPUT_RE = re.compile(
    r"^\s*nonce\s*=\s*(?P<nonce>\d+).*?"
    r"^.*?MD5\(.*?\)\s*=\s*(?P<hash>[0-9a-f]{32})",
    re.MULTILINE | re.DOTALL,
)

_NOT_FOUND_RE = re.compile(r"No solution found", re.IGNORECASE)


def _parse_miner_stdout(stdout: str, stderr: str = "") -> MinerResult | None:
    """Extract nonce + hash from the CUDA miner's stdout.

    Returns ``None`` if the miner explicitly reports *no solution found*,
    raises :class:`MinerError` if the output is unparseable.

    *stderr* is included in the error message when stdout is empty,
    so CUDA init failures (which only write to stderr) are diagnosable.
    """
    if _NOT_FOUND_RE.search(stdout):
        return None

    m = _OUTPUT_RE.search(stdout)
    if not m:
        detail = stderr.strip() if stderr.strip() and not stdout.strip() else stdout
        raise MinerError(f"Could not parse miner output:\n{detail}")
    return MinerResult(nonce=int(m.group("nonce")), hash=m.group("hash"))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class MinerService:
    """Wraps the Pilar 1 CUDA miner binary (``md5_range``) as a Python service.

    Usage::

        svc = MinerService(binary_path="./md5_range")
        result = svc.mine(block_fingerprint, "0000", 0, 10_000_000)
    """

    def __init__(
        self,
        binary_path: str = "./md5_range",
        timeout_seconds: int = 300,
    ) -> None:
        self.binary_path = binary_path
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mine(
        self,
        base_string: str,
        target_prefix: str,
        range_min: int = 0,
        range_max: int = 1_000_000_000,
    ) -> MinerResult | None:
        """Run a single mining task and return the first valid nonce found.

        Parameters
        ----------
        base_string:
            The block fingerprint (SHA-256 hex) that workers hash against.
        target_prefix:
            Hex prefix the MD5 must start with, e.g. ``"0000"``.
        range_min:
            Inclusive start of nonce search space.
        range_max:
            Inclusive end of nonce search space.

        Returns
        -------
        MinerResult | None
            The winning (nonce, hash) pair, or ``None`` if no valid nonce
            exists in the given range.

        Raises
        ------
        MinerError
            If the subprocess crashes, times out, or produces unparseable output.
        """
        cmd = shlex.split(self.binary_path) + [
            base_string,
            target_prefix,
            str(range_min),
            str(range_max),
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise MinerError(
                f"Miner timed out after {self.timeout_seconds}s "
                f"(range [{range_min}, {range_max}])"
            ) from exc
        except OSError as exc:
            raise MinerError(
                f"Miner binary error for '{self.binary_path}': {exc}"
            ) from exc

        # Check for runtime errors (e.g. CUDA init failure)
        if proc.returncode not in (0, 1):
            raise MinerError(
                f"Miner exited with code {proc.returncode}:\n{proc.stderr}"
            )

        # Surface CUDA errors that produced stderr but exited with code 0/1
        if proc.stderr and proc.returncode != 0:
            logger.error("Miner stderr (retcode=%d):\n%s", proc.returncode, proc.stderr)

        return _parse_miner_stdout(proc.stdout, proc.stderr)

    # ------------------------------------------------------------------
    # Cancellable mining (audit C1 + L2)
    # ------------------------------------------------------------------

    def mine_cancellable(
        self,
        base_string: str,
        target_prefix: str,
        range_min: int = 0,
        range_max: int = 1_000_000_000,
        *,
        abort_event: threading.Event,
        process_events: Callable[[], None] | None = None,
        poll_interval: float = 0.1,
    ) -> MinerResult | None:
        """Run a mining task that can be cancelled mid-flight via *abort_event*.

        Uses :class:`subprocess.Popen` with a polling loop.  Between polls,
        *process_events* is called (if provided) so the caller can drain
        I/O — for example, letting pika dispatch incoming control messages
        that set *abort_event*.

        Parameters
        ----------
        base_string:
            The block fingerprint (SHA-256 hex).
        target_prefix:
            Hex prefix the MD5 must start with, e.g. ``"0000"``.
        range_min / range_max:
            Inclusive nonce search bounds.
        abort_event:
            When set, the subprocess is killed and ``None`` is returned.
        process_events:
            Optional callback invoked between polls.  Use this to let the
            event loop / message broker process incoming messages without
            blocking the I/O thread (e.g. ``connection.process_data_events``).
        poll_interval:
            Seconds between poll checks (default 0.1 s).

        Returns
        -------
        MinerResult | None
            The winning (nonce, hash) pair, or ``None`` if the subprocess
            was aborted or no valid nonce exists in the range.

        Raises
        ------
        MinerError
            If the subprocess cannot start, crashes, or times out.
        """
        cmd = shlex.split(self.binary_path) + [
            base_string,
            target_prefix,
            str(range_min),
            str(range_max),
        ]

        try:
            # L2: start_new_session=True creates a new process group so
            # that if the worker process is killed, the miner subprocess
            # tree can be cleaned up (avoids orphans).
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise MinerError(
                f"Miner binary error for '{self.binary_path}': {exc}"
            ) from exc

        start = time.monotonic()
        try:
            while True:
                retcode = proc.poll()
                if retcode is not None:
                    # Process finished — collect remaining output
                    stdout, stderr = proc.communicate(timeout=5)
                    break

                if abort_event.is_set():
                    logger.info(
                        "Aborting miner subprocess (PID %d)", proc.pid,
                    )
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "Miner PID %d did not die — forcing SIGKILL",
                            proc.pid,
                        )
                        proc.kill()
                        proc.wait()
                    return None  # aborted

                # Check total timeout
                elapsed = time.monotonic() - start
                if elapsed > self.timeout_seconds:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise MinerError(
                        f"Miner timed out after {self.timeout_seconds}s "
                        f"(range [{range_min}, {range_max}])"
                    )

                # Yield to caller's I/O loop so that abort signals can
                # arrive (e.g. pika control messages).  If no callback is
                # provided, just sleep.
                if process_events is not None:
                    process_events()
                else:
                    time.sleep(poll_interval)

        except subprocess.TimeoutExpired as exc:
            # This catches communicate() timeout and wait() timeout
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
            raise MinerError(
                f"Miner timed out after {self.timeout_seconds}s "
                f"(range [{range_min}, {range_max}])"
            ) from exc

        # Surface CUDA errors that wrote to stderr
        if stderr and retcode != 0:
            logger.error("Miner stderr (PID %d, retcode=%d):\n%s",
                         proc.pid, retcode, stderr)
        elif stderr:
            logger.debug("Miner stderr (PID %d):\n%s", proc.pid, stderr)

        if retcode not in (0, 1):
            raise MinerError(
                f"Miner exited with code {retcode}:\n{stderr}"
            )

        return _parse_miner_stdout(stdout, stderr)
