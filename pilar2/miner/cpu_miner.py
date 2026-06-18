"""CPU fallback miner — development substitute for the CUDA md5_range binary.

Same interface as md5_range:
    cpu_miner.py <base_string> <target_prefix> <range_min> <range_max> [--timeout SECONDS]

Output on success:
    nonce = 12345
    MD5(<base_string>+12345) = 0000abcd...

Output on failure:
    No solution found

Exit codes: 0 (found or not found), non-zero only on argument errors.

The optional ``--timeout`` flag stops the search early (default 295 s,
just under the 300 s wrapper timeout) so the process exits cleanly
instead of being killed.
"""

import hashlib
import sys
import time

HARD_TIMEOUT_SECONDS = 295


def main() -> None:
    if len(sys.argv) < 5:
        print(
            f"Usage: {sys.argv[0]} <base_string> <target_prefix>"
            f" <range_min> <range_max> [--timeout SECONDS]",
            file=sys.stderr,
        )
        sys.exit(2)

    base_string = sys.argv[1]
    target_prefix = sys.argv[2]
    range_min = int(sys.argv[3])
    range_max = int(sys.argv[4])

    # Optional timeout (audit M3)
    timeout_seconds: float = HARD_TIMEOUT_SECONDS
    if len(sys.argv) >= 7 and sys.argv[5] == "--timeout":
        try:
            timeout_seconds = float(sys.argv[6])
        except ValueError:
            print("Invalid --timeout value", file=sys.stderr)
            sys.exit(2)

    start = time.monotonic()
    for nonce in range(range_min, range_max + 1):
        # M3: built-in timeout so the process exits cleanly instead of
        # being killed by the wrapper.
        if (time.monotonic() - start) >= timeout_seconds:
            print("No solution found")
            sys.stdout.flush()
            return

        digest = hashlib.md5((base_string + str(nonce)).encode()).hexdigest()
        if digest.startswith(target_prefix):
            print(f"nonce = {nonce}")
            print(f"MD5({base_string}+{nonce}) = {digest}")
            sys.stdout.flush()  # L3: avoid partial output on kill
            return

    print("No solution found")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
