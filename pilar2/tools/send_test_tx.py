#!/usr/bin/env python3
"""Test client for EduTokens blockchain.

Generates Ed25519 keypairs, signs transactions, and sends them to the NCT.

Usage:
    # Generate a keypair
    python tools/send_test_tx.py gen

    # Send a SPEND transaction (student -> vendor)
    python tools/send_test_tx.py spend <sender_privkey_hex> <receiver_pubkey_hex> <amount> <concept>

    # Send an EARN transaction  (requires AUTHORITY_PUBKEY set on NCT)
    python tools/send_test_tx.py earn <authority_privkey_hex> <student_pubkey_hex> <amount> <concept>

    # Check chain status
    python tools/send_test_tx.py status

    # Check balance of an address (pubkey)
    python tools/send_test_tx.py balance <pubkey_hex>

Examples:
    python tools/send_test_tx.py gen
    # → Saves keypair to /tmp/edutokens_test_keypair.json

    python tools/send_test_tx.py spend <priv> <pub> 100 COMEDOR
    python tools/send_test_tx.py status
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any

NCT_URL = os.getenv("NCT_URL", "http://localhost:8080")

# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def generate_keypair() -> dict[str, str]:
    """Return {privkey_hex, pubkey_hex} for a fresh Ed25519 keypair."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()

    priv_raw = sk.private_bytes_raw()
    pub_raw = pk.public_bytes_raw()

    return {
        "privkey_hex": priv_raw.hex(),
        "pubkey_hex": pub_raw.hex(),
    }


# ---------------------------------------------------------------------------
# Transaction signing
# ---------------------------------------------------------------------------


def _signing_dict(
    sender_pubkey: str,
    receiver_pubkey: str,
    amount: int,
    tx_type: str,
    concept: str,
    nonce: int = 0,
) -> dict[str, Any]:
    # timestamp deliberately excluded — the NCT sets it server-side and
    # the client cannot predict time.time() on the server.
    return {
        "sender_pubkey": sender_pubkey,
        "receiver_pubkey": receiver_pubkey,
        "amount": amount,
        "tx_type": tx_type,
        "concept": concept,
        "nonce": nonce,
    }


def compute_tx_id(signing: dict[str, Any]) -> str:
    raw = json.dumps(signing, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def sign_transaction(
    privkey_hex: str,
    sender_pubkey: str,
    receiver_pubkey: str,
    amount: int,
    tx_type: str,
    concept: str,
    nonce: int = 0,
) -> dict[str, Any]:
    """Build the full JSON body for POST /transaction, signed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privkey_hex))

    signing = _signing_dict(sender_pubkey, receiver_pubkey, amount, tx_type,
                            concept, nonce)
    tx_id = compute_tx_id(signing)
    signature = sk.sign(tx_id.encode()).hex()

    body = dict(signing)
    body["signature"] = signature
    return body


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post(path: str, data: dict | None = None) -> dict:
    url = f"{NCT_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  HTTP {e.code}: {err}")
        sys.exit(1)


def _get(path: str) -> dict:
    return _post(path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_gen() -> None:
    kp = generate_keypair()
    out = "/tmp/edutokens_test_keypair.json"
    with open(out, "w") as f:
        json.dump(kp, f, indent=2)
    print(f"Keypair saved to {out}")
    print(f"  pubkey:  {kp['pubkey_hex']}")
    print(f"  privkey: {kp['privkey_hex']}")
    print()
    print("Keep the private key secret — it's stored in /tmp for testing only.")
    print()
    print("To use:")
    print(f"  python tools/send_test_tx.py spend {kp['privkey_hex']} <receiver_pubkey> 100 COMEDOR")


def cmd_spend(
    privkey_hex: str, receiver_pubkey: str, amount: int, concept: str,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privkey_hex))
    sender_pubkey = sk.public_key().public_bytes_raw().hex()

    # Get current nonce for this sender
    try:
        account = _get(f"/account/{sender_pubkey}")
    except SystemExit:
        print("Could not reach NCT. Is docker compose up?")
        sys.exit(1)
    nonce = account.get("nonce", 0)
    print(f"Current nonce for {sender_pubkey[:16]}...: {nonce}")

    body = sign_transaction(
        privkey_hex, sender_pubkey, receiver_pubkey, amount,
        "SPEND", concept, nonce=nonce,
    )
    print(f"Sending SPEND {amount} → {receiver_pubkey[:16]}... ({concept})")
    result = _post("/transaction", body)
    print(f"  tx_id: {result['tx_id']}")
    print("  Transaction accepted into mempool.")


def cmd_earn(
    privkey_hex: str, student_pubkey: str, amount: int, concept: str,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privkey_hex))
    authority_pubkey = sk.public_key().public_bytes_raw().hex()
    print(f"Authority pubkey: {authority_pubkey}")

    # Get current nonce for the authority
    try:
        account = _get(f"/account/{authority_pubkey}")
    except SystemExit:
        print("Could not reach NCT. Is docker compose up?")
        sys.exit(1)
    nonce = account.get("nonce", 0)
    print(f"Current nonce for authority: {nonce}")

    body = sign_transaction(
        privkey_hex, authority_pubkey, student_pubkey, amount,
        "EARN", concept, nonce=nonce,
    )
    print(f"Sending EARN {amount} → {student_pubkey[:16]}... ({concept})")
    result = _post("/transaction", body)
    print(f"  tx_id: {result['tx_id']}")
    print("  Transaction accepted into mempool.")


def cmd_status() -> None:
    result = _get("/status")
    print(f"Chain height:       {result['chain_height']}")
    print(f"Pending txs:        {result['pending_transactions']}")
    print(f"Current block:      {result['current_block']}")
    print(f"Active pools:       {result['active_pools']}")


def cmd_balance(pubkey: str) -> None:
    result = _get(f"/balance/{pubkey}")
    print(f"Address:  {result['address']}")
    print(f"Balance:  {result['balance']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """Usage:
  python tools/send_test_tx.py gen
  python tools/send_test_tx.py spend <privkey> <receiver_pubkey> <amount> <concept>
  python tools/send_test_tx.py earn <privkey> <student_pubkey> <amount> <concept>
  python tools/send_test_tx.py status
  python tools/send_test_tx.py balance <pubkey>"""


def main() -> None:
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "gen":
        cmd_gen()
    elif cmd == "spend":
        if len(sys.argv) != 6:
            print(USAGE)
            sys.exit(1)
        cmd_spend(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5])
    elif cmd == "earn":
        if len(sys.argv) != 6:
            print(USAGE)
            sys.exit(1)
        cmd_earn(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5])
    elif cmd == "status":
        cmd_status()
    elif cmd == "balance":
        if len(sys.argv) != 3:
            print(USAGE)
            sys.exit(1)
        cmd_balance(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
