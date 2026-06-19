# Audit P1 — Data Models & Cryptography

**Scope:** `pilar2/shared/block.py` (325 lines), `pilar2/shared/crypto.py` (106 lines), `pilar2/shared/schemas.py` (89 lines)

**Audit date:** 2026-06-18

**Status:** FIXED

---

## Findings

### CRITICAL

#### C1 — Unhandled `ValueError` in Ed25519 public-key parsing → HTTP 500 / crash

- **File:** `pilar2/shared/crypto.py:43-56`
- **Fragment:**
  ```python
  pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))  # line 43
  sig = bytes.fromhex(signature_hex)                                           # line 45
  try:                                                                         # line 47
      pubkey.verify(sig, message)
      return True
  except InvalidSignature:                                                     # line 54
      return False
  ```
- **Risk:** `from_public_bytes()` raises `ValueError` when the 32-byte string is not a valid Ed25519 curve point (e.g. all-zeros, low-order point, or random bytes that happen to be valid hex).  This exception is **not** inside the `try/except InvalidSignature` block — it propagates unhandled.  At `POST /transaction` in the NCT (`nct/nct.py:420`), this becomes an uncaught 500 Internal Server Error, crashing the request and potentially leaving the connection in a bad state.

  `_validate_hex()` (called at lines 39–40) only checks length + hex charset — it does **not** validate curve membership.  An attacker can craft a 64-hex-char string that passes `_validate_hex` but triggers this crash.

- **Recommendation:** Wrap `from_public_bytes()` in the same `try` block (or a separate one) and return `False` on `ValueError`, treating an invalid public key the same as an invalid signature.  Example fix:
  ```python
  try:
      pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
  except ValueError:
      return False
  ```

---

### HIGH

#### H1 — `Block.validate()` does not verify Proof-of-Work; caller can forget

- **File:** `pilar2/shared/block.py:271-312` (validate), `pilar2/shared/block.py:314-325` (verify_pow)
- **Fragment:**
  ```python
  def validate(self, previous_block: Optional[Block] = None) -> list[str]:
      """Validate structural integrity.
      *Note*: Proof-of-Work is **not** checked here — use
      :meth:`verify_pow` separately. …"""
      # … checks index, chaining, transactions, hash integrity …
      # PoW check is absent
  ```
  ```python
  @staticmethod
  def verify_pow(block: Block) -> bool:
      if block.index == 0:
          return True
      raw = (block.fingerprint + str(block.nonce)).encode()
      digest = hashlib.md5(raw).hexdigest()
      return digest.startswith("0" * block.difficulty)
  ```
- **Risk:** `validate()` is the canonical "is this block well-formed?" method — it checks structural integrity, chaining, and `hash == compute_hash()`.  But PoW is a separate static method.  A caller that only calls `validate()` (e.g. an auditor, a chain-repair tool, a future API endpoint) would accept a block with a forged nonce.  Currently the NCT calls both, but the API design invites misuse.  The `hash` integrity check at line 306 (`self.hash == self.compute_hash()`) only verifies SHA-256 consistency, not MD5 PoW.

- **Recommendation:** Either (a) call `verify_pow()` inside `validate()` when `block.index > 0` and `block.nonce != 0` (the block already has a nonce = it was "mined"), or (b) rename `validate()` to `validate_structural()` to make the separation unambiguous at the call site.

#### H2 — Pydantic hex-pattern rejects uppercase; `bytes.fromhex()` accepts it

- **File:** `pilar2/shared/schemas.py:41-42` vs `pilar2/shared/crypto.py:39-40`
- **Fragment (schemas.py):**
  ```python
  _PUBKEY_HEX_RE = rf"^[0-9a-f]{{{ED25519_PUBKEY_HEX_LEN}}}$"
  _SIG_HEX_RE   = rf"^[0-9a-f]{{{ED25519_SIG_HEX_LEN}}}$"
  ```
  **Fragment (crypto.py):**
  ```python
  _validate_hex(public_key_hex, ED25519_PUBKEY_HEX_LEN, "public_key")
  # internally calls bytes.fromhex(value) — which accepts [0-9a-fA-F]
  ```
- **Risk:** The Pydantic schemas reject uppercase hex (`A-F`) at the HTTP boundary, returning 422 Validation Error.  But `crypto.py` (and Python's `bytes.fromhex()`) accept uppercase.  A client that generates keys with uppercase hex (e.g. `openssl rand -hex 32` which produces lowercase, but some JS libraries may produce uppercase) would be blocked at the API layer even though the key is cryptographically valid.  Inconsistency between the two validation layers.

- **Recommendation:** Change both regexes to `[0-9a-fA-F]`.  Normalise to lowercase in `crypto.py` before storage if needed, or accept both cases consistently.

---

### MEDIUM

#### M1 — Float `amount` field loses precision and breaks cross-version hash determinism

- **File:** `pilar2/shared/block.py:33` (Transaction.amount), `pilar2/shared/block.py:66` (tx_id), `pilar2/shared/block.py:227,237` (fingerprint/compute_hash)
- **Fragment:**
  ```python
  amount: float   # line 33
  …
  raw = json.dumps(self._signing_dict(), sort_keys=True, ensure_ascii=False)
  return hashlib.sha256(raw.encode()).hexdigest()  # line 65-66
  ```
- **Risk:** Python's `json.dumps` serialises floats via `repr()`, which produces the shortest round-trippable decimal.  Representations can differ across Python versions (e.g. `10.0` → `10.0` in 3.11 vs `1e1` in 3.13).  Since `tx_id` and `fingerprint` are SHA-256 over the JSON serialisation, a transaction's ID could change when deserialised and re-serialised on a different Python version.  In a blockchain, content-addressable identifiers (tx_id) must be **bit-identical forever**.  Float precision also means `0.1 + 0.2 != 0.3` in IEEE 754 — amounts like `0.33` cannot be represented exactly.

- **Recommendation:** Replace `amount: float` with `amount: int` representing the smallest unit (e.g. "millitokens", like Ethereum's wei).  For display, convert at the UI layer.  If float must stay for the PoC, at minimum document the risk and use a fixed-precision serialisation (`f"{amount:.6f}"`) in `_signing_dict()`.

#### M2 — `Block.difficulty` has no range validation

- **File:** `pilar2/shared/block.py:180`
- **Fragment:**
  ```python
  difficulty: int
  ```
- **Risk:** Nothing prevents `difficulty < 0` or `difficulty > 32` (MD5 produces 32 hex chars, so difficulty > 32 is impossible PoW).  A negative difficulty would make `"0" * difficulty` produce an empty string, causing **every** nonce to trivially satisfy PoW.  A difficulty of 33+ would make PoW unsolvable (32 zeros impossible with MD5's 128-bit output, though practically 16+ zeros already takes millions of GPU-years).

- **Recommendation:** Add validation in `Block.validate()`: `0 <= difficulty <= 32`.  Apply the same constraint in `NCTConfig` environment-variable parsing (`nct/state.py` or `nct/nct.py:load_config`).

#### M3 — `Block.previous_hash` length not validated

- **File:** `pilar2/shared/block.py:179` (declaration), `pilar2/shared/block.py:271-312` (validate)
- **Fragment:**
  ```python
  previous_hash: str   # line 179 — no length constraint
  …
  # validate() checks genesis previous_hash == "0"*64, but no length check for non-genesis
  ```
- **Risk:** A malformed block could carry a `previous_hash` of wrong length (not 64 hex chars), breaking chain linking.  `validate()` only checks that `previous_hash` matches the previous block's `.hash` when a `previous_block` is provided, but never checks the string length or charset.  A corrupt Redis entry could propagate.

- **Recommendation:** Add length (64) and hex-charset validation on `previous_hash` in `Block.validate()`, similar to `Transaction.validate()` on pubkey fields.

#### M4 — `Transaction.validate()` omits `nonce` validation

- **File:** `pilar2/shared/block.py:96-149`
- **Fragment:** The `validate()` method checks sender_pubkey, receiver_pubkey, amount, tx_type, concept, and signature — but **not** `nonce`.
- **Risk:** The `nonce` field is part of the cryptographically-signed payload (`_signing_dict()` includes it at line 54), yet structural validation never checks `nonce >= 0`.  The nonce check currently happens only at the API layer (`nct/nct.py:429-436`).  If the model is reused in another context (e.g. a block validator, a migration script), the nonce invariant could be silently violated.

- **Recommendation:** Add `if self.nonce < 0: errors.append("nonce must be >= 0")` to `Transaction.validate()`.  The "expected nonce" check (vs Redis) belongs at the API layer, but the structural floor belongs in the model.

#### M5 — `pubkey_to_address()` uses 96-bit truncated hash

- **File:** `pilar2/shared/crypto.py:63-73`
- **Fragment:**
  ```python
  def pubkey_to_address(public_key_hex: str) -> str:
      _validate_hex(public_key_hex, ED25519_PUBKEY_HEX_LEN, "public_key")
      raw = bytes.fromhex(public_key_hex)
      return hashlib.sha256(raw).hexdigest()[:24]  # 12 bytes → 24 hex chars
  ```
- **Risk:** The address is 12 bytes of SHA-256 output — 96 bits of address space.  Birthday-bound collision probability ≈ 2^48 (281 trillion keys needed for 50% collision chance).  For a PoC with < 10^6 students this is negligible.  However, the property is not documented: if someone later reuses this for a production system, collision attacks become feasible.  Also, Ethereum-style addresses use the *last* 20 bytes; using the *first* 12 is different and could confuse developers.

- **Recommendation:** Document the 96-bit truncation trade-off explicitly in the docstring.  Consider using the last 20 bytes (like Ethereum) for familiarity, or the full 32-byte SHA-256 for zero-collision safety in a PoC.

---

### LOW

#### L1 — Output Pydantic models lack field validation

- **File:** `pilar2/shared/schemas.py:13-32`
- **Fragment:**
  ```python
  class BalanceResponse(BaseModel):
      address: str = Field(..., description="Public-key derived address (24 hex chars)")
      balance: float
  ```
- **Risk:** `address` is described as "24 hex chars" but has no `min_length`/`max_length`/`pattern` constraint.  If a bug elsewhere produces a malformed address, it would be serialised into the HTTP response without validation.  Similarly `AccountResponse.address` (line 28) has no pattern.  This is a contract-weakness: API consumers cannot rely on the documented format.

- **Recommendation:** Add `pattern=r"^[0-9a-f]{24}$"` to `BalanceResponse.address` and `AccountResponse.address`.  Use a shared `ADDRESS_RE` constant.

#### L2 — `Transaction.timestamp` has no validation

- **File:** `pilar2/shared/block.py:37`
- **Fragment:**
  ```python
  timestamp: float = field(default_factory=time.time)
  ```
- **Risk:** No checks prevent a timestamp in the far past, far future, or negative.  The field can be set arbitrarily (it's part of the signing dict, so the signer controls it).  While the blockchain doesn't rely on timestamps for ordering (block index does that), extreme values could affect log analysis, metrics, or time-based UI features.

- **Recommendation:** Add `timestamp > 0` and optionally `timestamp <= time.time() + ALLOWED_FUTURE_DRIFT` in `Transaction.validate()`.  Even a soft check improves debuggability.

#### L3 — `Transaction.to_dict()` has redundant `nonce` assignment

- **File:** `pilar2/shared/block.py:72-77`
- **Fragment:**
  ```python
  def to_dict(self) -> dict[str, Any]:
      d = self._signing_dict()       # ← already includes "nonce": self.nonce  (line 54)
      d["signature"] = self.signature
      d["nonce"] = self.nonce        # ← overwrites with same value — redundant
      return d
  ```
- **Risk:** None functionally — the value is the same.  But the redundancy is a maintenance hazard: if `_signing_dict()` is ever refactored to *exclude* nonce (e.g. to match a spec change), `to_dict()` would still add it, potentially masking the removal.  Code smell only.

- **Recommendation:** Remove the redundant line and add a comment if the nonce inclusion in `to_dict()` is intentional (it should be, since `to_dict()` is for storage and must include nonce).

---

### INFO

#### I1 — Two distinct hash algorithms: design clarity

- **Files:** `pilar2/shared/block.py:218-228` (fingerprint — SHA-256), `pilar2/shared/block.py:314-325` (verify_pow — MD5)
- **Details:** The system deliberately uses SHA-256 for chain linking (collision-resistant) and MD5 for Proof-of-Work (fast on GPU).  This is a legitimate PoC trade-off, well-documented in `project_overview.md` and `pilar2/README.md`.  No action needed, but future productionisation should replace MD5 with SHA-256 PoW (or a memory-hard function).

#### I2 — Genesis block edge cases handled correctly

- **File:** `pilar2/shared/block.py:188-199` (create_genesis), `pilar2/shared/block.py:271-312` (validate)
- **Details:** Genesis block is created with `difficulty=0`, `nonce=0`, `previous_hash="0"*64`, empty transactions.  `validate()` special-cases genesis: allows empty transactions, enforces `previous_hash="0"*64`.  `verify_pow()` returns `True` for block 0.  These edge cases are correctly handled.

#### I3 — `ensure_ascii=False` is safe for current field set

- **File:** `pilar2/shared/block.py:65,227,237`
- **Details:** All three hash computations pass `ensure_ascii=False` to `json.dumps`.  Since pubkeys, hashes, and signatures are hex strings (ASCII-only), and amounts are floats (JSON-encoded as ASCII digits), this is currently safe.  However, the `concept` field is free-text; if a non-ASCII concept (e.g. `"cafetería"`) is submitted, the JSON will contain raw UTF-8 bytes, which is deterministic within the same Python but could vary if `concept` undergoes NFC/NFD normalisation at different layers.  Worth noting for future i18n support.

---

## Summary

| Severity | Count | IDs |
|----------|-------|-----|
| CRITICAL | 1 | C1 — Unhandled `ValueError` in Ed25519 pubkey parsing |
| HIGH | 2 | H1 — PoW check not integrated in `validate()`, H2 — hex-case inconsistency |
| MEDIUM | 5 | M1 — Float `amount`, M2 — `difficulty` range, M3 — `previous_hash` length, M4 — `nonce` in validate, M5 — 96-bit address |
| LOW | 3 | L1 — Output model validation, L2 — `timestamp`, L3 — redundant code |
| INFO | 3 | I1 — Dual-hash design, I2 — Genesis handling, I3 — ensure_ascii safety |

**Most impactful fix:** C1 (crypto crash on invalid pubkey) — blocks legitimate error handling and is a one-line fix.
