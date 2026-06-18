"""Pydantic schemas for HTTP request/response contracts.

Used by the NCT and Worker FastAPI health/status endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from shared.crypto import ED25519_PUBKEY_HEX_LEN, ED25519_SIG_HEX_LEN


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"


class ErrorResponse(BaseModel):
    error: str


_ADDRESS_RE = r"^[0-9a-fA-F]{24}$"

class BalanceResponse(BaseModel):
    address: str = Field(
        ..., min_length=24, max_length=24, pattern=_ADDRESS_RE,
        description="Public-key derived address (24 hex chars)",
    )
    balance: int


class AccountResponse(BaseModel):
    address: str = Field(
        ..., min_length=24, max_length=24, pattern=_ADDRESS_RE,
        description="Public-key derived address (24 hex chars)",
    )
    balance: int
    nonce: int = Field(..., ge=0, description="Next expected nonce for this account")
    discarded_transactions: list[str] = Field(
        default_factory=list,
        description="tx_ids of transactions from this account "
        "that were discarded during block assembly",
    )


# ---------------------------------------------------------------------------
# NCT
# ---------------------------------------------------------------------------

_PUBKEY_HEX_RE = rf"^[0-9a-fA-F]{{{ED25519_PUBKEY_HEX_LEN}}}$"
_SIG_HEX_RE = rf"^[0-9a-fA-F]{{{ED25519_SIG_HEX_LEN}}}$"


class TransactionRequest(BaseModel):
    sender_pubkey: str = Field(
        ...,
        min_length=ED25519_PUBKEY_HEX_LEN,
        max_length=ED25519_PUBKEY_HEX_LEN,
        pattern=_PUBKEY_HEX_RE,
        description="Ed25519 public key of the sender (64 hex chars)",
    )
    receiver_pubkey: str = Field(
        ...,
        min_length=ED25519_PUBKEY_HEX_LEN,
        max_length=ED25519_PUBKEY_HEX_LEN,
        pattern=_PUBKEY_HEX_RE,
        description="Ed25519 public key of the receiver (64 hex chars)",
    )
    amount: int = Field(
        ..., gt=0, le=1_000_000_000,
        description="Amount to transfer, in smallest unit (max 1 000 000 000)",
    )
    tx_type: str = Field(
        ..., pattern=r"^(EARN|SPEND)$", description="Transaction type: EARN or SPEND"
    )
    concept: str = Field(
        ..., min_length=1, max_length=128, description="Free-text concept (e.g. TP1, COMEDOR)"
    )
    signature: str = Field(
        ...,
        min_length=ED25519_SIG_HEX_LEN,
        max_length=ED25519_SIG_HEX_LEN,
        pattern=_SIG_HEX_RE,
        description="Ed25519 signature over tx_id (128 hex chars)",
    )
    nonce: int = Field(..., ge=0, description="Sequential nonce for replay protection")


class TransactionResponse(BaseModel):
    tx_id: str = Field(..., description="SHA-256 identifier of the transaction")


class NCTStatusResponse(BaseModel):
    chain_height: int
    pending_transactions: int
    current_block: int | None = Field(
        None, description="Index of the block currently being mined, if any"
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class WorkerHealthResponse(BaseModel):
    status: str = "ok"
    worker_id: str
    uptime_seconds: float


class WorkerStatusResponse(BaseModel):
    worker_id: str
    current_task: str | None = Field(
        None, description="task_id currently being mined, or None if idle"
    )
    tasks_processed: int
    uptime_seconds: float
