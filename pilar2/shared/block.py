"""Transaction and Block schemas for the distributed blockchain."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    """A transfer of value from one user to another.

    Fields:
        sender_pubkey:    Ed25519 public key (64 hex chars) of the sender.
        receiver_pubkey:  Ed25519 public key (64 hex chars) of the receiver.
        amount:           Amount being transferred, in the smallest unit
                          (e.g. millitokens, like Ethereum's wei). Must be
                          a positive integer.
        tx_type:          ``"EARN"`` (university → student) or ``"SPEND"``
                          (student → vendor).
        concept:          Free-text description (e.g. ``"TP1"``, ``"COMEDOR"``).
        signature:        Ed25519 signature (128 hex chars) over ``tx_id``.
        timestamp:        Unix timestamp (UTC) of creation.
    """

    sender_pubkey: str
    receiver_pubkey: str
    amount: int
    tx_type: str = ""
    concept: str = ""
    signature: str = ""
    timestamp: float = field(default_factory=time.time)
    nonce: int = 0

    # ------------------------------------------------------------------
    # Hashing (signature excluded — breaks circular dependency)
    # ------------------------------------------------------------------

    def _signing_dict(self) -> dict[str, Any]:
        """Fields that are signed.  ``signature`` is excluded so that
        ``tx_id = SHA-256(signing_dict)`` is computable *before* signing.

        ``timestamp`` is deliberately excluded — it is set server-side on
        arrival and the client cannot predict ``time.time()`` on the NCT.
        ``nonce`` provides replay protection.
        """
        return {
            "sender_pubkey": self.sender_pubkey,
            "receiver_pubkey": self.receiver_pubkey,
            "amount": self.amount,
            "tx_type": self.tx_type,
            "concept": self.concept,
            "nonce": self.nonce,
        }

    @property
    def tx_id(self) -> str:
        """SHA-256 content identifier, deterministic across instances.

        The signature is deliberately excluded so that the client can
        compute ``tx_id``, sign it, and attach the signature without
        changing the identifier.
        """
        raw = json.dumps(self._signing_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Full serialisation, **including** the signature (for storage)."""
        d = self._signing_dict()  # nonce, no timestamp
        d["timestamp"] = self.timestamp
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transaction:
        return cls(
            sender_pubkey=data["sender_pubkey"],
            receiver_pubkey=data["receiver_pubkey"],
            amount=data["amount"],
            tx_type=data.get("tx_type", ""),
            concept=data.get("concept", ""),
            signature=data.get("signature", ""),
            timestamp=data["timestamp"],
            nonce=data.get("nonce", 0),
        )

    # ------------------------------------------------------------------
    # Validation (structural only — signature verified at the API layer)
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Return structural validation errors (empty if valid).

        Structural rules only — stateless.  Signature verification and
        authority checks happen at ``POST /transaction`` in the NCT.
        Balance validation happens at block-assembly time in
        ``drain_pool_validated``.
        """
        from shared.crypto import ED25519_PUBKEY_HEX_LEN, ED25519_SIG_HEX_LEN

        errors: list[str] = []

        # -- Pubkey fields -------------------------------------------------
        if not self.sender_pubkey:
            errors.append("sender_pubkey must not be empty")
        elif len(self.sender_pubkey) != ED25519_PUBKEY_HEX_LEN:
            errors.append(
                f"sender_pubkey must be {ED25519_PUBKEY_HEX_LEN} hex chars, "
                f"got {len(self.sender_pubkey)}"
            )

        if not self.receiver_pubkey:
            errors.append("receiver_pubkey must not be empty")
        elif len(self.receiver_pubkey) != ED25519_PUBKEY_HEX_LEN:
            errors.append(
                f"receiver_pubkey must be {ED25519_PUBKEY_HEX_LEN} hex chars, "
                f"got {len(self.receiver_pubkey)}"
            )

        if self.sender_pubkey and self.receiver_pubkey and self.sender_pubkey == self.receiver_pubkey:
            errors.append("sender and receiver must be different")

        # -- Amount --------------------------------------------------------
        if self.amount <= 0:
            errors.append("amount must be positive")
        elif self.amount > 1_000_000_000:
            errors.append("amount must not exceed 1,000,000,000")

        # -- Type ----------------------------------------------------------
        if self.tx_type not in ("EARN", "SPEND"):
            errors.append("tx_type must be EARN or SPEND")

        # -- Concept -------------------------------------------------------
        if not self.concept:
            errors.append("concept must not be empty")

        # -- Signature -----------------------------------------------------
        if not self.signature:
            errors.append("signature must not be empty")
        elif len(self.signature) != ED25519_SIG_HEX_LEN:
            errors.append(
                f"signature must be {ED25519_SIG_HEX_LEN} hex chars, "
                f"got {len(self.signature)}"
            )

        # -- Nonce -----------------------------------------------------------
        if self.nonce < 0:
            errors.append("nonce must be >= 0")

        # -- Timestamp -------------------------------------------------------
        if self.timestamp <= 0:
            errors.append("timestamp must be positive")

        return errors


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """A block in the blockchain.

    Each block contains an ordered list of transactions, a reference to the
    previous block (via its SHA-256 hash), and a nonce that satisfies the
    Proof-of-Work difficulty target.

    Fields:
        index:          Position in the chain. 0 = genesis block.
        timestamp:      Unix timestamp (UTC) when this block was created.
        transactions:   List of transactions included in this block.
        previous_hash:  SHA-256 of the previous block (64 hex chars).
                        Genesis blocks use ``"0" * 64``.
        difficulty:     Number of leading zero nibbles required by PoW.
        nonce:          Integer found by miners that satisfies PoW.
        hash:           SHA-256 of *this* block's complete contents (computed
                        after mining and stored for chain linking).
    """

    index: int
    timestamp: float
    transactions: list[Transaction]
    previous_hash: str
    difficulty: int
    nonce: int = 0
    hash: str = ""

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def create_genesis(cls) -> Block:
        """Build the genesis block.  PoW is not enforced for block 0."""
        genesis = cls(
            index=0,
            timestamp=time.time(),
            transactions=[],
            previous_hash="0" * 64,
            difficulty=0,
        )
        genesis.hash = genesis.compute_hash()
        return genesis

    # ------------------------------------------------------------------
    # Hashing helpers
    # ------------------------------------------------------------------

    def _core_dict(self, include_nonce: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "index": self.index,
            "timestamp": self.timestamp,
            "transactions": [t.to_dict() for t in self.transactions],
            "previous_hash": self.previous_hash,
            "difficulty": self.difficulty,
        }
        if include_nonce:
            d["nonce"] = self.nonce
        return d

    @property
    def fingerprint(self) -> str:
        """SHA-256 block identifier **without** the nonce.

        This is the value that workers receive and use as the *base string*
        for Proof-of-Work mining::

            PoW_hash = MD5(fingerprint + str(nonce))
        """
        raw = json.dumps(self._core_dict(include_nonce=False),
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def compute_hash(self) -> str:
        """SHA-256 over **all** block data including the nonce.

        This is the final block identifier; it is stored in ``self.hash``
        after mining and used by the next block as ``previous_hash``.
        """
        raw = json.dumps(self._core_dict(include_nonce=True),
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "transactions": [t.to_dict() for t in self.transactions],
            "previous_hash": self.previous_hash,
            "difficulty": self.difficulty,
            "nonce": self.nonce,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Block:
        return cls(
            index=data["index"],
            timestamp=data["timestamp"],
            transactions=[Transaction.from_dict(tx) for tx in data["transactions"]],
            previous_hash=data["previous_hash"],
            difficulty=data["difficulty"],
            nonce=data.get("nonce", 0),
            hash=data.get("hash", ""),
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, previous_block: Optional[Block] = None) -> list[str]:
        """Validate structural and Proof-of-Work integrity.

        Checks index, chaining, transactions, difficulty range, hash
        consistency, and MD5 Proof-of-Work (for non-genesis blocks).
        """
        errors: list[str] = []

        if self.index < 0:
            errors.append("index must be non-negative")

        # --- previous_hash format ---
        if len(self.previous_hash) != 64:
            errors.append(
                f"previous_hash must be 64 hex chars, got {len(self.previous_hash)}"
            )
        else:
            try:
                bytes.fromhex(self.previous_hash)
            except ValueError:
                errors.append("previous_hash must be hex-encoded")

        # --- Difficulty range ---
        if self.difficulty < 0 or self.difficulty > 32:
            errors.append(
                f"difficulty must be 0-32, got {self.difficulty}"
            )

        # --- Chaining consistency (when a previous block is provided) ---
        if previous_block is not None:
            if self.index != previous_block.index + 1:
                errors.append(
                    f"expected index {previous_block.index + 1}, got {self.index}"
                )
            if self.previous_hash != previous_block.hash:
                errors.append("previous_hash does not match previous block's hash")

        # --- Genesis block ---
        if self.index == 0:
            if self.previous_hash != "0" * 64:
                errors.append("genesis block must have previous_hash = '0' * 64")
        else:
            if not self.transactions:
                errors.append("non-genesis block must contain at least one transaction")

            # --- Proof-of-Work (non-genesis only) ---
            if not Block.verify_pow(self):
                errors.append("Proof-of-Work verification failed")

        # --- Transaction validation ---
        for i, tx in enumerate(self.transactions):
            for e in tx.validate():
                errors.append(f"transaction[{i}]: {e}")

        # --- Hash integrity ---
        if self.hash and self.hash != self.compute_hash():
            errors.append(
                f"hash mismatch: computed {self.compute_hash()}, "
                f"stored {self.hash}"
            )

        return errors

    @staticmethod
    def verify_result(
        fingerprint: str,
        difficulty: int,
        nonce: int,
        claimed_hash: str,
    ) -> tuple[bool, str]:
        """Check that *claimed_hash* is ``MD5(fingerprint + nonce)`` and meets
        the difficulty target.

        Returns ``(is_valid, actual_md5_hash)``.

        Canonical verification used by the NCT and pool coordinators when
        a worker submits a mining result.
        """
        pow_hash = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
        valid = (pow_hash == claimed_hash) and pow_hash.startswith(
            "0" * difficulty
        )
        return valid, pow_hash

    @staticmethod
    def verify_pow(block: Block) -> bool:
        """Check that MD5(fingerprint + nonce) satisfies the difficulty target.

        Delegates to :meth:`verify_result` for the canonical computation.
        """
        if block.index == 0:
            return True  # genesis block is not mined

        raw = (block.fingerprint + str(block.nonce)).encode()
        digest = hashlib.md5(raw).hexdigest()
        return digest.startswith("0" * block.difficulty)
