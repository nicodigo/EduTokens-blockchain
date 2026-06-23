#!/usr/bin/env python3
"""Smoke test para EduTokens blockchain — GKE deployment.

Ejecuta una serie de verificaciones contra el NCT para validar que
todo el stack (NCT → Pool → Workers → Redis → RabbitMQ) funciona
correctamente.

Uso:
    # Contra nct.edutokens.xyz (público vía Ingress)
    python3 pilar2/tools/smoke_test_gke.py --url https://nct.edutokens.xyz

    # Contra cluster local (usa kubectl port-forward)
    python3 pilar2/tools/smoke_test_gke.py --port-forward

    # Con autoridad explícita (genera keypair si no se provee)
    python3 pilar2/tools/smoke_test_gke.py --url http://localhost:8080 \\
        --authority-priv <hex> --authority-pub <hex>

Dependencias:
    - cryptography (pip install cryptography)
    - httpx (pip install httpx)
    - kubectl (solo modo --port-forward)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Optional


# ---------------------------------------------------------------------------
# Crypto helpers (inline — no depende de shared/)
# ---------------------------------------------------------------------------

def _make_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return (
        private_key.private_bytes_raw().hex(),
        public_key.public_bytes_raw().hex(),
    )


def _sign(private_hex: str, message: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return private_key.sign(message).hex()


def _compute_tx_id(sender_pubkey: str, receiver_pubkey: str, amount: int,
                   tx_type: str, concept: str, nonce: int) -> str:
    import hashlib

    raw = json.dumps({
        "sender_pubkey": sender_pubkey,
        "receiver_pubkey": receiver_pubkey,
        "amount": amount,
        "tx_type": tx_type,
        "concept": concept,
        "nonce": nonce,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(session, path: str) -> dict:
    from httpx import Client

    resp = session.get(path)
    resp.raise_for_status()
    return resp.json()


def _post_json(session, path: str, body: dict, expect: int = 201) -> dict:
    resp = session.post(path, json=body)
    if resp.status_code != expect:
        print(f"  ❌ Expected {expect}, got {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

class SmokeTest:
    def __init__(self, base_url: str, authority_priv: str = "",
                 authority_pub: str = ""):
        from httpx import Client

        self.base_url = base_url.rstrip("/")
        self.session = Client(base_url=self.base_url, timeout=30.0,
                              verify=False)  # TLS self-signed in dev

        self.authority_priv = authority_priv
        self.authority_pub = authority_pub
        self.passed = 0
        self.failed = 0

    def _ok(self, msg: str) -> None:
        self.passed += 1
        print(f"  ✅ {msg}")

    def _fail(self, msg: str) -> None:
        self.failed += 1
        print(f"  ❌ {msg}")

    def test_health(self) -> None:
        """GET /health returns status ok."""
        print("\n── Health endpoint ──")
        data = _get_json(self.session, "/health")
        if data.get("status") == "ok":
            self._ok("/health → status=ok")
        else:
            self._fail(f"/health unexpected: {data}")

    def test_status(self) -> None:
        """GET /status returns chain metadata."""
        print("\n── Status endpoint ──")
        data = _get_json(self.session, "/status")
        chain_height = data.get("chain_height", -1)
        pending = data.get("pending_transactions", -1)
        pools = data.get("active_pools", -1)

        if chain_height >= 0:
            self._ok(f"chain_height={chain_height}")
        else:
            self._fail(f"chain_height missing: {data}")

        print(f"     pending_transactions={pending}, active_pools={pools}")

    def test_metrics(self) -> None:
        """GET /metrics returns Prometheus format."""
        print("\n── Metrics endpoint ──")
        resp = self.session.get("/metrics")
        if resp.status_code != 200:
            self._fail(f"/metrics returned {resp.status_code}")
            return

        text = resp.text
        if "nct_uptime_seconds" in text and "nct_blocks_mined_total" in text:
            self._ok("Prometheus metrics present")
        else:
            self._fail("metrics missing expected fields")
            return

        # Extract a few values for display
        for line in text.split("\n"):
            if line.startswith("nct_") and not line.startswith("#"):
                print(f"     {line}")

    def test_submit_transaction(self) -> None:
        """POST /transaction with a valid EARN tx."""
        print("\n── Transaction submission ──")

        if not self.authority_priv:
            self._fail("No authority keypair — skipping transaction test")
            return

        # Get current nonce
        try:
            account = _get_json(self.session,
                                f"/account/{self.authority_pub}")
        except Exception:
            # If /account fails, try with nonce=0
            account = {"nonce": 0}

        nonce = account.get("pending_nonce", account.get("nonce", 0))
        student_priv, student_pub = _make_keypair()

        tx_id = _compute_tx_id(
            sender_pubkey=self.authority_pub,
            receiver_pubkey=student_pub,
            amount=100,
            tx_type="EARN",
            concept="SMOKE_TEST",
            nonce=nonce,
        )
        signature = _sign(self.authority_priv, tx_id.encode())

        body = {
            "sender_pubkey": self.authority_pub,
            "receiver_pubkey": student_pub,
            "amount": 100,
            "tx_type": "EARN",
            "concept": "SMOKE_TEST",
            "signature": signature,
            "nonce": nonce,
        }

        resp_data = _post_json(self.session, "/transaction", body, expect=201)
        self._ok(f"Transaction accepted: tx_id={resp_data['tx_id'][:16]}...")

        # Store for chain verification
        self._student_pub = student_pub

    def test_chain_growth(self) -> None:
        """Wait for block mining and verify chain grew."""
        print("\n── Chain growth ──")

        initial = _get_json(self.session, "/status")["chain_height"]
        print(f"     Initial chain_height={initial}")

        # Wait up to 120s for a new block (BLOCK_TIMEOUT=30 + mining + margin)
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(5)
            current = _get_json(self.session, "/status")["chain_height"]
            sys.stdout.write(f"\r     chain_height={current} (waiting...)")
            sys.stdout.flush()
            if current > initial:
                print()
                self._ok(f"Chain grew: {initial} → {current}")
                return

        print()
        self._fail(f"Chain did not grow after 120s (still {initial})")

    def test_chain_integrity(self) -> None:
        """GET /chain returns valid blocks."""
        print("\n── Chain integrity ──")
        data = _get_json(self.session, "/chain?start=0&count=3")
        if isinstance(data, list) and len(data) > 0:
            last = data[-1]
            self._ok(
                f"Chain has {len(data)} blocks, "
                f"latest block index={last.get('index', '?')}"
            )
        else:
            self._fail(f"/chain returned unexpected: {data}")

    def run_all(self) -> bool:
        print(f"🔍 Smoke test — NCT @ {self.base_url}")
        print("=" * 50)

        self.test_health()
        self.test_status()
        self.test_metrics()
        self.test_submit_transaction()
        self.test_chain_growth()
        self.test_chain_integrity()

        print("\n" + "=" * 50)
        total = self.passed + self.failed
        print(f"Results: {self.passed}/{total} passed, "
              f"{self.failed} failed")

        return self.failed == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _start_port_forward(port: int = 8080) -> subprocess.Popen:
    """Start kubectl port-forward to the NCT service."""
    proc = subprocess.Popen(
        ["kubectl", "-n", "blockchain", "port-forward",
         "svc/nct", f"{port}:8080"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)  # Wait for tunnel
    if proc.poll() is not None:
        print("❌ kubectl port-forward failed — is the cluster accessible?")
        sys.exit(1)
    return proc


def main() -> None:
    parser = argparse.ArgumentParser(description="EduTokens smoke test")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="NCT base URL (e.g. https://nct.edutokens.xyz)")
    group.add_argument("--port-forward", action="store_true",
                       help="Use kubectl port-forward to localhost:8080")
    parser.add_argument("--authority-priv", help="Authority private key (hex)")
    parser.add_argument("--authority-pub", help="Authority public key (hex)")
    args = parser.parse_args()

    # Resolve base URL
    pf_proc: Optional[subprocess.Popen] = None
    if args.port_forward:
        print("🚇 Starting kubectl port-forward svc/nct → localhost:8080 ...")
        pf_proc = _start_port_forward()
        base_url = "http://localhost:8080"
    else:
        base_url = args.url

    # Resolve authority keypair
    if args.authority_priv and args.authority_pub:
        auth_priv, auth_pub = args.authority_priv, args.authority_pub
    else:
        print("🔑 No authority keypair provided — generating ephemeral one")
        auth_priv, auth_pub = _make_keypair()
        print(f"   Public key:  {auth_pub}")
        print(f"   ⚠️  This key is NOT the NCT's AUTHORITY_PUBKEY.")
        print(f"   EARN transactions will be REJECTED (expected).")
        print(f"   Use --authority-priv/--authority-pub for real testing.")

    try:
        test = SmokeTest(base_url, auth_priv, auth_pub)
        ok = test.run_all()
    finally:
        if pf_proc:
            pf_proc.terminate()
            pf_proc.wait()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
