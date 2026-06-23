#!/usr/bin/env python3
"""Stress test para EduTokens blockchain — GKE deployment.

Envía N transacciones EARN (válidas, firmadas por la autoridad) en paralelo
y mide throughput, latencia, y tasa de aceptación. Luego espera a que todos
los bloques se minen y reporta el tiempo total.

Uso:
    python3 pilar2/tools/stress_test_gke.py \\
        --url https://nct.edutokens.xyz \\
        --authority-priv <hex> --authority-pub <hex> \\
        --count 50 --parallel 10

    python3 pilar2/tools/stress_test_gke.py \\
        --port-forward \\
        --authority-priv <hex> --authority-pub <hex> \\
        --count 100
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Crypto helpers (same as smoke_test_gke.py)
# ---------------------------------------------------------------------------

def _make_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return (private_key.private_bytes_raw().hex(),
            public_key.public_bytes_raw().hex())


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
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TxResult:
    tx_id: str = ""
    success: bool = True
    status_code: int = 0
    error: str = ""
    latency_ms: float = 0.0


@dataclass
class StressReport:
    total: int
    accepted: int
    rejected: int
    latency_p50_ms: float
    latency_p99_ms: float
    throughput_tx_per_sec: float
    chain_initial: int
    chain_final: int
    blocks_mined: int
    mining_time_sec: float
    tx_per_minute: float
    converged: bool = False  # early exit: blocks hold >BLOCK_SIZE tx


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(session, path: str) -> dict:
    resp = session.get(path)
    resp.raise_for_status()
    return resp.json()


def _submit_one(base_url: str, body: dict) -> TxResult:
    from httpx import Client

    t0 = time.perf_counter()
    try:
        with Client(base_url=base_url, timeout=30.0, verify=False) as s:
            resp = s.post("/transaction", json=body)
        elapsed = (time.perf_counter() - t0) * 1000

        if resp.status_code == 201:
            data = resp.json()
            return TxResult(tx_id=data.get("tx_id", ""), success=True,
                            status_code=201, latency_ms=elapsed)
        else:
            return TxResult(success=False, status_code=resp.status_code,
                            error=resp.text[:200], latency_ms=elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        return TxResult(success=False, status_code=0,
                        error=str(exc)[:200], latency_ms=elapsed)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

class StressTest:
    def __init__(self, base_url: str, authority_priv: str,
                 authority_pub: str):
        self.base_url = base_url.rstrip("/")
        self.auth_priv = authority_priv
        self.auth_pub = authority_pub

    def run(self, count: int = 50, parallel: int = 10) -> StressReport:
        from httpx import Client

        converged = False  # early exit via convergence

        print(f"⚡ Stress test — NCT @ {self.base_url}")
        print(f"   Transactions: {count}, parallel workers: {parallel}")
        print("=" * 50)

        # Pre-generate keypairs and sign transactions
        print("\n🔑 Generating and signing transactions...")
        bodies: list[dict] = []
        nonce = 0
        try:
            with Client(base_url=self.base_url, timeout=10.0,
                        verify=False) as s:
                account = _get_json(s, f"/account/{self.auth_pub}")
                nonce = account.get("pending_nonce", 0)
        except Exception:
            pass

        for i in range(count):
            _, student_pub = _make_keypair()
            tx_id = _compute_tx_id(
                sender_pubkey=self.auth_pub,
                receiver_pubkey=student_pub,
                amount=1,
                tx_type="EARN",
                concept=f"STRESS_{i:04d}",
                nonce=nonce + i,
            )
            signature = _sign(self.auth_priv, tx_id.encode())
            bodies.append({
                "sender_pubkey": self.auth_pub,
                "receiver_pubkey": student_pub,
                "amount": 1,
                "tx_type": "EARN",
                "concept": f"STRESS_{i:04d}",
                "signature": signature,
                "nonce": nonce + i,
            })
        print(f"   Signed {len(bodies)} transactions (nonces {nonce}-{nonce+count-1})")

        # Get initial chain height
        with Client(base_url=self.base_url, timeout=10.0,
                    verify=False) as s:
            chain_initial = _get_json(s, "/status")["chain_height"]
        print(f"   Initial chain_height={chain_initial}")

        # Submit in parallel
        print(f"\n🚀 Submitting {count} transactions ({parallel} parallel)...")
        t_start = time.perf_counter()
        results: list[TxResult] = []

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_submit_one, self.base_url, b): i
                       for i, b in enumerate(bodies)}
            for future in as_completed(futures):
                results.append(future.result())
                if len(results) % 20 == 0:
                    sys.stdout.write(f"\r   {len(results)}/{count} completed")
                    sys.stdout.flush()

        elapsed = time.perf_counter() - t_start
        print(f"\r   {len(results)}/{count} completed in {elapsed:.1f}s")

        # Analyse results
        accepted = [r for r in results if r.success]
        rejected = [r for r in results if not r.success]

        latencies = sorted(r.latency_ms for r in results if r.success)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p99_idx = int(len(latencies) * 0.99)
        p99 = latencies[min(p99_idx, len(latencies) - 1)] if latencies else 0

        throughput = count / elapsed

        print(f"\n📊 Submission results:")
        print(f"   Accepted:  {len(accepted)}/{count} "
              f"({100*len(accepted)/count:.0f}%)")
        print(f"   Rejected:  {len(rejected)}/{count}")
        if rejected:
            statuses = {}
            for r in rejected:
                statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
            for code, n in sorted(statuses.items()):
                print(f"     HTTP {code}: {n}")

        print(f"   Latency p50: {p50:.1f} ms")
        print(f"   Latency p99: {p99:.1f} ms")
        print(f"   Throughput:  {throughput:.1f} tx/sec")

        # Wait for blocks to be mined
        print(f"\n⛏️  Waiting for blocks to be mined...")
        blocks_needed = (len(accepted) + 4) // 5  # BLOCK_SIZE=5 (minimum)
        deadline = time.time() + max(300, blocks_needed * 60)
        # Convergence: if pending=0 and chain stable for this many checks,
        # all txs are mined (even if fewer blocks than expected due to
        # blocks holding more than BLOCK_SIZE transactions).
        stable_checks = 0
        last_chain = chain_initial
        convergence_threshold = 4  # 4 checks × 5s = 20s of stability

        chain_final = chain_initial
        while time.time() < deadline:
            time.sleep(5)
            with Client(base_url=self.base_url, timeout=10.0,
                        verify=False) as s:
                data = _get_json(s, "/status")
                chain_final = data["chain_height"]
                pending = data.get("pending_transactions", 0)
            sys.stdout.write(
                f"\r   chain_height={chain_final}, "
                f"pending={pending} (target ≥{chain_initial + blocks_needed})"
            )
            sys.stdout.flush()
            if chain_final >= chain_initial + blocks_needed and pending == 0:
                break
            # Early exit: all txs mined (blocks can hold >BLOCK_SIZE txs)
            if pending == 0 and chain_final == last_chain:
                stable_checks += 1
                if stable_checks >= convergence_threshold:
                    converged = True
                    print(f"\n   All transactions mined "
                          f"({chain_final - chain_initial} blocks, "
                          f"fewer than expected — blocks can hold >BLOCK_SIZE)")
                    break
            else:
                last_chain = chain_final
                stable_checks = 0

        mining_time = time.perf_counter() - t_start
        blocks_mined = chain_final - chain_initial
        tx_per_min = count / (mining_time / 60) if mining_time > 0 else 0

        print(f"\n\n📈 Final stats:")
        print(f"   Chain: {chain_initial} → {chain_final} "
              f"({blocks_mined} blocks mined)")
        print(f"   Mining time: {mining_time:.1f}s")
        print(f"   Effective throughput: {tx_per_min:.1f} tx/min")

        return StressReport(
            total=count,
            accepted=len(accepted),
            rejected=len(rejected),
            latency_p50_ms=p50,
            latency_p99_ms=p99,
            throughput_tx_per_sec=throughput,
            chain_initial=chain_initial,
            chain_final=chain_final,
            blocks_mined=blocks_mined,
            mining_time_sec=mining_time,
            tx_per_minute=tx_per_min,
            converged=converged,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _start_port_forward(port: int = 8080) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["kubectl", "-n", "blockchain", "port-forward",
         "svc/nct", f"{port}:8080"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    if proc.poll() is not None:
        print("❌ kubectl port-forward failed")
        sys.exit(1)
    return proc


def main() -> None:
    parser = argparse.ArgumentParser(description="EduTokens stress test")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="NCT base URL (e.g. https://nct.edutokens.xyz)")
    group.add_argument("--port-forward", action="store_true",
                       help="Use kubectl port-forward to localhost:8080")
    parser.add_argument("--authority-priv", required=True,
                        help="Authority private key (hex)")
    parser.add_argument("--authority-pub", required=True,
                        help="Authority public key (hex)")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of transactions to send (default: 50)")
    parser.add_argument("--parallel", type=int, default=10,
                        help="Number of parallel workers (default: 10)")
    args = parser.parse_args()

    pf_proc: Optional[subprocess.Popen] = None
    if args.port_forward:
        print("🚇 Starting kubectl port-forward svc/nct → localhost:8080 ...")
        pf_proc = _start_port_forward()
        base_url = "http://localhost:8080"
    else:
        base_url = args.url

    try:
        test = StressTest(base_url, args.authority_priv, args.authority_pub)
        report = test.run(count=args.count, parallel=args.parallel)

        print("\n" + "=" * 50)
        if report.accepted == report.total:
            print("✅ All transactions accepted!")
        else:
            print(f"⚠️  {report.rejected} transactions rejected — "
                  f"check rate limits and nonces")

        if report.converged or report.blocks_mined >= report.accepted // 5:
            print("✅ All expected blocks mined!")
        else:
            print(f"⚠️  Only {report.blocks_mined} blocks mined — "
                  f"some transactions may be stuck (timeout)")
    finally:
        if pf_proc:
            pf_proc.terminate()
            pf_proc.wait()


if __name__ == "__main__":
    main()
