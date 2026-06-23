#!/usr/bin/env python3
"""Continuous traffic simulator for EduTokens — demo companion.

Generates student keypairs, seeds them with EARN transactions, then
runs a continuous loop of SPEND (student→student) and EARN
(authority→student) at a configurable rate to simulate a university
day with ~1000 students worth of transaction volume.

Usage:
    # Seed 20 students with 500 tokens each (one-time)
    python tools/simulate_traffic.py seed --students 20 --amount 500 \\
        --authority-priv <hex> --authority-pub <hex>

    # Run continuous traffic at 30 tx/min until Ctrl+C
    python tools/simulate_traffic.py run --rate 30 \\
        --authority-priv <hex> --authority-pub <hex>

    # Combined: seed then run
    python tools/simulate_traffic.py full --students 20 --amount 500 --rate 30 \\
        --authority-priv <hex> --authority-pub <hex>

    # Against GKE
    python tools/simulate_traffic.py run --url https://nct.edutokens.xyz --rate 30 \\
        --authority-priv <hex> --authority-pub <hex>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Crypto (mirrors send_test_tx.py for standalone use)
# ---------------------------------------------------------------------------


def generate_keypair() -> dict[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    return {
        "privkey_hex": sk.private_bytes_raw().hex(),
        "pubkey_hex": pk.public_bytes_raw().hex(),
    }


def _signing_dict(
    sender_pubkey: str, receiver_pubkey: str, amount: int,
    tx_type: str, concept: str, nonce: int,
) -> dict[str, Any]:
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
    privkey_hex: str, sender_pubkey: str, receiver_pubkey: str,
    amount: int, tx_type: str, concept: str, nonce: int,
) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privkey_hex))
    signing = _signing_dict(sender_pubkey, receiver_pubkey, amount,
                            tx_type, concept, nonce)
    tx_id = compute_tx_id(signing)
    signature = sk.sign(tx_id.encode()).hex()
    body = dict(signing)
    body["signature"] = signature
    return body


# ---------------------------------------------------------------------------
# Student ledger (in-memory)
# ---------------------------------------------------------------------------


@dataclass
class Student:
    pubkey: str
    privkey: str
    nonce: int = 0       # next expected nonce (tracked client-side)


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------


CONCEPTS_EARN = [
    "BECA_UNLU", "SUBSIDIO_COMEDOR", "PREMIO_ACADEMICO",
    "AYUDA_TRANSPORTE", "BECA_DEPORTIVA", "ESTIPENDIO_INVESTIGACION",
    "BECA_EXTENSION", "CREDITO_FOTOCOPIAS",
]

CONCEPTS_SPEND = [
    "COMEDOR", "FOTOCOPIAS", "LIBRERIA", "BAR", "TRANSPORTE",
    "IMPRESIONES", "LABORATORIO", "INSCRIPCION_EVENTO",
    "CUOTA_CENTRO", "CURSO_EXTRA",
]


@dataclass
class SimStats:
    accepted: int = 0
    rejected: int = 0
    started_at: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP helpers (httpx)
# ---------------------------------------------------------------------------


class HttpClient:
    def __init__(self, base_url: str) -> None:
        import httpx
        import warnings
        # Disable SSL warnings for self-signed certs
        warnings.filterwarnings("ignore", message=".*unverified HTTPS.*")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=15.0,
            verify=False,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str) -> dict:
        r = self._client.get(path)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, data: dict) -> dict:
        r = self._client.post(path, json=data)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Seed phase: EARN tokens to each student
# ---------------------------------------------------------------------------


def seed_students(
    http: HttpClient,
    students: list[Student],
    authority_priv: str,
    authority_pub: str,
    amount: int,
    parallel: int = 5,
) -> int:
    """EARN ``amount`` tokens to each student from the authority."""
    import concurrent.futures

    # Get authority nonce once
    try:
        account = http.get_json(f"/account/{authority_pub}")
        nonce_start = account.get("pending_nonce", account.get("nonce", 0))
    except Exception:
        nonce_start = 0

    print(f"\n🌱 Seed phase — EARN {amount} tokens × {len(students)} students")
    print(f"   Authority nonce start: {nonce_start}")
    print(f"   Parallel workers:      {parallel}")
    print("-" * 50)

    bodies: list[dict] = []
    for i, s in enumerate(students):
        body = sign_transaction(
            authority_priv, authority_pub, s.pubkey,
            amount, "EARN", f"INICIAL_{i + 1:03d}",
            nonce=nonce_start + i,
        )
        bodies.append(body)

    accepted = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(http.post_json, "/transaction", b): i
            for i, b in enumerate(bodies)
        }
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                result = fut.result()
                accepted += 1
                sys.stdout.write(
                    f"\r   {accepted}/{len(students)} accepted"
                )
            except Exception as e:
                print(f"\n   ✗ Student {i} failed: {e}")
            sys.stdout.flush()

    print(f"\n   ✅ {accepted}/{len(students)} EARN transactions submitted")
    return accepted


# ---------------------------------------------------------------------------
# Continuous traffic loop
# ---------------------------------------------------------------------------


def _run_once(
    http: HttpClient,
    students: list[Student],
    authority_priv: str,
    authority_pub: str,
    authority_nonce: list[int],  # mutable counter
    stats: SimStats,
) -> None:
    """Submit one transaction (EARN or SPEND) and update state."""
    # Decide: 70% SPEND, 30% EARN
    if random.random() < 0.70:
        # SPEND: student → student
        sender = random.choice(students)
        # Prefer students with some balance (best-effort — we don't track
        # exact balances client-side to avoid complexity)
        receiver = random.choice([s for s in students if s.pubkey != sender.pubkey])
        concept = random.choice(CONCEPTS_SPEND)
        amount = random.randint(1, 20)

        t0 = time.monotonic()
        try:
            body = sign_transaction(
                sender.privkey, sender.pubkey, receiver.pubkey,
                amount, "SPEND", concept, nonce=sender.nonce,
            )
            result = http.post_json("/transaction", body)
            elapsed_ms = (time.monotonic() - t0) * 1000
            stats.accepted += 1
            stats.latencies_ms.append(elapsed_ms)
            sender.nonce += 1
            sys.stdout.write(
                f"\r   SPEND {amount:3d} {sender.pubkey[:8]}…→"
                f"{receiver.pubkey[:8]}…  [{concept}] ✓"
            )
        except Exception as e:
            stats.rejected += 1
            err = str(e)[:80]
            sys.stdout.write(f"\r   SPEND ✗ {err}")
    else:
        # EARN: authority → student
        student = random.choice(students)
        concept = random.choice(CONCEPTS_EARN)
        amount = random.randint(10, 100)

        t0 = time.monotonic()
        try:
            nonce = authority_nonce[0]
            body = sign_transaction(
                authority_priv, authority_pub, student.pubkey,
                amount, "EARN", concept, nonce=nonce,
            )
            result = http.post_json("/transaction", body)
            elapsed_ms = (time.monotonic() - t0) * 1000
            stats.accepted += 1
            stats.latencies_ms.append(elapsed_ms)
            authority_nonce[0] += 1
            sys.stdout.write(
                f"\r   EARN  {amount:3d} → {student.pubkey[:8]}…"
                f"  [{concept}] ✓"
            )
        except Exception as e:
            stats.rejected += 1
            err = str(e)[:80]
            sys.stdout.write(f"\r   EARN  ✗ {err}")

    sys.stdout.flush()


def run_traffic(
    http: HttpClient,
    students: list[Student],
    authority_priv: str,
    authority_pub: str,
    rate_per_minute: int,
    duration_sec: int = 0,
) -> SimStats:
    """Run continuous traffic at ``rate_per_minute`` tx/min.

    Stops on Ctrl+C or after ``duration_sec`` (0 = forever).
    """
    stats = SimStats(started_at=time.time())
    interval = 60.0 / rate_per_minute if rate_per_minute > 0 else 1.0

    # Get authority nonce once at start
    try:
        account = http.get_json(f"/account/{authority_pub}")
        nonce = account.get("pending_nonce", account.get("nonce", 0))
    except Exception:
        nonce = 0
    authority_nonce = [nonce]

    print(f"\n🚦 Traffic loop — {rate_per_minute} tx/min")
    print(f"   Students: {len(students)}")
    print(f"   Interval: {interval:.1f}s")
    print(f"   SPEND/EARN ratio: 70/30")
    print("-" * 50)
    print("   Press Ctrl+C to stop\n")

    deadline = time.time() + duration_sec if duration_sec > 0 else float("inf")
    running = True

    def _on_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    tick = 0
    while running and time.time() < deadline:
        _run_once(http, students, authority_priv, authority_pub,
                  authority_nonce, stats)
        tick += 1

        # Print summary every 30 tx
        if tick % 30 == 0:
            elapsed = time.time() - stats.started_at
            rate = stats.accepted / (elapsed / 60) if elapsed > 0 else 0
            sys.stdout.write(
                f"\n   [{tick:4d}] accepted={stats.accepted} "
                f"rejected={stats.rejected} "
                f"rate={rate:.0f} tx/min\n"
            )
            sys.stdout.flush()

        # Sleep until next tick
        time.sleep(max(0, interval - 0.05))

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_final_stats(stats: SimStats) -> None:
    elapsed = time.time() - stats.started_at
    rate = stats.accepted / (elapsed / 60) if elapsed > 0 else 0
    print("\n" + "=" * 50)
    print("📊 Final stats")
    print("-" * 50)
    print(f"   Runtime:       {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"   Accepted:      {stats.accepted}")
    print(f"   Rejected:      {stats.rejected}")
    print(f"   Success rate:  {stats.accepted / max(1, stats.accepted + stats.rejected) * 100:.1f}%")
    print(f"   Avg rate:      {rate:.1f} tx/min")
    if stats.latencies_ms:
        lats = sorted(stats.latencies_ms)
        print(f"   Latency p50:   {lats[len(lats) // 2]:.0f} ms")
        print(f"   Latency p99:   {lats[int(len(lats) * 0.99)]:.0f} ms")
    print("=" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EduTokens traffic simulator for demos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url", default=os.getenv("NCT_URL", "http://localhost:8080"),
        help="NCT base URL (default: $NCT_URL or http://localhost:8080)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # seed
    p_seed = sub.add_parser("seed", help="EARN tokens to students")
    p_seed.add_argument("--students", type=int, default=20)
    p_seed.add_argument("--amount", type=int, default=500)
    p_seed.add_argument("--parallel", type=int, default=5)
    p_seed.add_argument("--authority-priv", required=True)
    p_seed.add_argument("--authority-pub", required=True)

    # run
    p_run = sub.add_parser("run", help="Continuous traffic loop")
    p_run.add_argument("--rate", type=int, default=30,
                       help="Target tx per minute (default: 30)")
    p_run.add_argument("--duration", type=int, default=0,
                       help="Max duration in seconds (0=forever)")
    p_run.add_argument("--authority-priv", required=True)
    p_run.add_argument("--authority-pub", required=True)
    p_run.add_argument("--students-file", default="/tmp/edutokens_students.json",
                       help="JSON file with student keypairs")

    # full (seed + run)
    p_full = sub.add_parser("full", help="Seed then run")
    p_full.add_argument("--students", type=int, default=20)
    p_full.add_argument("--amount", type=int, default=500)
    p_full.add_argument("--rate", type=int, default=30)
    p_full.add_argument("--duration", type=int, default=0)
    p_full.add_argument("--parallel", type=int, default=5)
    p_full.add_argument("--authority-priv", required=True)
    p_full.add_argument("--authority-pub", required=True)

    args = parser.parse_args()
    http = HttpClient(args.url)

    try:
        if args.command == "seed":
            # Generate students
            print(f"Generating {args.students} student keypairs...")
            students = []
            for i in range(args.students):
                kp = generate_keypair()
                students.append(Student(
                    pubkey=kp["pubkey_hex"],
                    privkey=kp["privkey_hex"],
                ))
            # Save for later
            with open("/tmp/edutokens_students.json", "w") as f:
                json.dump(
                    [{"pubkey": s.pubkey, "privkey": s.privkey}
                     for s in students],
                    f, indent=2,
                )
            print(f"   Saved to /tmp/edutokens_students.json")
            seed_students(http, students, args.authority_priv,
                          args.authority_pub, args.amount, args.parallel)
            print("\n✅ Seed complete. Run with:")
            print(f"   python tools/simulate_traffic.py run --rate {args.rate} "
                  f"--authority-priv ... --authority-pub ...")

        elif args.command == "run":
            # Load students
            if not os.path.exists(args.students_file):
                print(f"Student file not found: {args.students_file}")
                print("Run 'seed' first or create the file manually.")
                sys.exit(1)
            with open(args.students_file) as f:
                data = json.load(f)
            students = [Student(
                pubkey=d["pubkey"], privkey=d["privkey"],
            ) for d in data]
            print(f"Loaded {len(students)} students from {args.students_file}")

            stats = run_traffic(
                http, students,
                args.authority_priv, args.authority_pub,
                args.rate, args.duration,
            )
            _print_final_stats(stats)

        elif args.command == "full":
            # Generate + seed + save
            print(f"Generating {args.students} student keypairs...")
            students = []
            for i in range(args.students):
                kp = generate_keypair()
                students.append(Student(
                    pubkey=kp["pubkey_hex"],
                    privkey=kp["privkey_hex"],
                ))
            with open("/tmp/edutokens_students.json", "w") as f:
                json.dump(
                    [{"pubkey": s.pubkey, "privkey": s.privkey}
                     for s in students],
                    f, indent=2,
                )
            print(f"   Saved to /tmp/edutokens_students.json")

            seed_students(http, students, args.authority_priv,
                          args.authority_pub, args.amount, args.parallel)

            # Wait for mining
            print("\n⏳ Waiting for seed transactions to be mined...")
            time.sleep(15)
            try:
                status = http.get_json("/status")
                print(f"   chain_height={status['chain_height']}, "
                      f"pending={status.get('pending_transactions', '?')}")
            except Exception:
                pass

            stats = run_traffic(
                http, students,
                args.authority_priv, args.authority_pub,
                args.rate, args.duration,
            )
            _print_final_stats(stats)

    finally:
        http.close()


if __name__ == "__main__":
    main()
