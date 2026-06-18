# Frontend Integration Guide — EduTokens PKI

> **Audience:** Frontend team implementing the client-side key generation and
> transaction signing for the EduTokens blockchain PoC.
>
> **Backend:** NCT at `POST /transaction` (port 8080).
> **Algorithm:** Ed25519 (RFC 8032) for keypairs and signatures.
> **Hash for tx_id:** SHA-256.

---

## 1. Key generation

### Algorithm: Ed25519

Generate a fresh Ed25519 keypair **client-side**.  The private key MUST
NEVER leave the browser.  Store it in `localStorage` or similar
persistent, isolated storage.

### Recommended JS library

`@noble/curves` — pure JS, no native dependencies, works in the browser.

```bash
npm install @noble/curves
```

### Generate a keypair

```js
import { ed25519 } from '@noble/curves/ed25519';

const privateKey = ed25519.utils.randomPrivateKey();  // Uint8Array(32)
const publicKey  = ed25519.getPublicKey(privateKey);   // Uint8Array(32)

const privateKeyHex = bytesToHex(privateKey);           // 64 hex chars
const publicKeyHex  = bytesToHex(publicKey);            // 64 hex chars
const address       = sha256Hex(publicKey).slice(0, 24); // 24 hex chars
```

Helper:

```js
function bytesToHex(uint8) {
  return Array.from(uint8).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function sha256Hex(data) {
  // data is Uint8Array
  const hash = await crypto.subtle.digest('SHA-256', data);
  return bytesToHex(new Uint8Array(hash));
}
```

### Address derivation

The **address** is a human-readable shorthand used by the NCT in balance
lookups and logs.  It is computed as:

```
address = SHA-256(pubkey_raw_bytes)[:12]   → 24 hex chars
```

This is purely cosmetic — the system identifies accounts by their full
64-hex-char public key on the wire.

---

## 2. Transaction construction

### 2.1 — The signing payload (`tx_id`)

Every transaction has a unique identifier called `tx_id`.  It is a
**SHA-256 hash** over a canonical JSON object that includes everything
**except** the signature itself.  This breaks the circular dependency
because you need the hash *before* you can sign.

The fields you MUST include in the signing payload (in this exact order
in the JSON — `sort_keys` will handle alphabetical ordering, but the keys
and types MUST be exactly these):

| Key | Type | Example | Notes |
|---|---|---|---|
| `amount` | number (float) | `10.0` | Must be > 0 |
| `concept` | string | `"TP1"` | 1–128 chars, non-empty |
| `nonce` | number (int) | `0` | Sequential counter for replay protection (see §4.3) |
| `receiver_pubkey` | string | `"a1b2c3..."` | 64 hex chars |
| `sender_pubkey` | string | `"d4e5f6..."` | 64 hex chars |
| `timestamp` | number (float) | `1718697600.123` | Unix seconds (UTC) |
| `tx_type` | string | `"EARN"` or `"SPEND"` | — |

**CRITICAL:** JSON keys are **snake_case** exactly as shown.  Serialise
with `sort_keys` so the JSON is deterministic across platforms.

### 2.2 — Computing `tx_id`

```js
function computeTxId(txBody) {
  // txBody is { sender_pubkey, receiver_pubkey, amount, tx_type, concept, timestamp, nonce }
  // signature is NOT in txBody
  const json = JSON.stringify(txBody, Object.keys(txBody).sort());
  const hash = // SHA-256 of new TextEncoder().encode(json)
  return hash;  // 64 hex chars
}
```

Full implementation using the Web Crypto API:

```js
async function sha256Hex(text) {
  const enc = new TextEncoder();
  const hash = await crypto.subtle.digest('SHA-256', enc.encode(text));
  return bytesToHex(new Uint8Array(hash));
}

async function computeTxId(txBody) {
  const sortedKeys = Object.keys(txBody).sort();
  const json = JSON.stringify(txBody, sortedKeys);
  return await sha256Hex(json);  // 64 hex chars
}
```

### 2.3 — Signing

Sign the **raw bytes of `tx_id`** (the 64-char hex string encoded as
UTF-8), NOT the hex string itself:

```js
import { ed25519 } from '@noble/curves/ed25519';

function signTxId(txIdHex, privateKeyUint8) {
  // txIdHex is "a1b2...64chars..."
  const txIdBytes = new TextEncoder().encode(txIdHex);
  const signature = ed25519.sign(txIdBytes, privateKeyUint8);
  return bytesToHex(signature);  // 64 bytes → 128 hex chars
}
```

**WARNING:** Sign `tx_id` as a UTF-8 string, NOT as raw SHA-256 bytes.
The backend Python does `tx_id.encode()` which produces UTF-8 bytes.
Both sides MUST match.

### 2.4 — Full transaction example (JS)

```js
async function createSignedTransaction(senderPrivKey, senderPubKey,
                                        receiverPubKey, amount,
                                        txType, concept, nonce) {
  // 1. Build signing body (NO signature)
  const body = {
    amount: amount,
    concept: concept,
    nonce: nonce,                           // sequential counter (see §4.3)
    receiver_pubkey: receiverPubKey,        // 64 hex
    sender_pubkey: senderPubKey,            // 64 hex
    timestamp: Date.now() / 1000,           // seconds, float
    tx_type: txType,                        // "EARN" or "SPEND"
  };

  // 2. Compute tx_id
  const txId = await computeTxId(body);

  // 3. Sign
  const privBytes = hexToBytes(senderPrivKey);
  const txIdBytes = new TextEncoder().encode(txId);
  const sig = ed25519.sign(txIdBytes, privBytes);
  const signature = bytesToHex(sig);       // 128 hex

  // 4. Full wire payload (includes signature)
  return { ...body, signature: signature };
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.substring(i, i + 2), 16);
  }
  return bytes;
}
```

---

## 3. Wire format — `POST /transaction`

### Endpoint

```
POST http://localhost:8080/transaction
Content-Type: application/json
```

### Request body

```json
{
  "sender_pubkey":   "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "receiver_pubkey": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
  "amount": 10.0,
  "tx_type": "EARN",
  "concept": "TP1",
  "nonce": 3,
  "timestamp": 1718697600.123,
  "signature": "a1b2c3d4... (128 hex chars total)"
}
```

### Field constraints (enforced by the server)

| Field | Constraint |
|---|---|
| `sender_pubkey` | Exactly 64 lowercase hex chars `[0-9a-f]` |
| `receiver_pubkey` | Exactly 64 lowercase hex chars `[0-9a-f]` |
| `amount` | `> 0`, float |
| `tx_type` | `"EARN"` or `"SPEND"` |
| `concept` | 1–128 chars, non-empty |
| `nonce` | Integer `>= 0`, must match the next expected nonce for this sender |
| `timestamp` | Unix float (seconds) |
| `signature` | Exactly 128 lowercase hex chars `[0-9a-f]` |

**IMPORTANT:** Use **lowercase** hex for all fields.  The server regex is
`^[0-9a-f]+$`.  Uppercase `[A-F]` will be rejected with a 400 error.

### Successful response — 201

```json
{
  "tx_id": "b3a7f19c8d..."
}
```

### Error response — 400

```json
{
  "error": "invalid signature — does not match sender_pubkey"
}
```

Possible error messages emitted by the server:

- `"sender_pubkey must be 64 hex chars, got N"`
- `"signature must be 128 hex chars, got N"`
- `"invalid signature — does not match sender_pubkey"`
- `"invalid nonce: expected N, got M"` ← NEW (replay protection, see §4.3)
- `"EARN sender_pubkey does not match AUTHORITY_PUBKEY"`
- `"EARN transactions require AUTHORITY_PUBKEY to be configured"`
- `"concept must not be empty"`
- `"amount must be positive"`
- `"tx_type must be EARN or SPEND"`

---

## 4. Domain rules

### EARN transactions

- `sender_pubkey` MUST be the university's authority public key
  (configured in the NCT as `AUTHORITY_PUBKEY`).
- `tx_type` MUST be `"EARN"`.
- `concept` describes the academic activity (`"TP1"`, `"EJERCICIO_10"`,
  etc.).

**Only the holder of the university's private key can sign EARN
transactions.**

### SPEND transactions

- `sender_pubkey` is the student's public key.
- `receiver_pubkey` is the vendor's public key.
- `tx_type` MUST be `"SPEND"`.
- The student must sign the transaction with their private key.
- The student must have sufficient balance (enforced when the block is
  assembled, not at POST time).

**Vendors do not sign.**  A vendor receives tokens passively — their
address is a "burn address" from which nobody can spend (the private key
is discarded or never generated).  This is valid blockchain behaviour.

### 4.3 — Nonce (replay protection) ← NEW

Every account has a **sequential nonce counter** that prevents replay
attacks.  The nonce works as follows:

1. **Query before signing.**  Call `GET /account/{pubkey}` to get the
   current `nonce` for the sender.  The response includes both `balance`
   and `nonce`.

2. **Include in the signing body.**  The `nonce` field is part of
   `txBody` and therefore part of `tx_id` and covered by the Ed25519
   signature.  You cannot change the nonce without invalidating the
   signature.

3. **Send with the transaction.**  Include `nonce` in the JSON body of
   `POST /transaction`.

4. **Server rejects replays.**  If a transaction is re-submitted after
   being mined, the server will see `tx.nonce < expected_nonce` and
   return `400` with `"invalid nonce: expected N, got M"`.

5. **Sequential within an account.**  Nonces are per-account (per
   `sender_pubkey`).  The university and each student have independent
   nonce counters starting at 0.

```
Example flow:
  GET /account/{student_pubkey}
  → { "balance": 50.0, "nonce": 3 }

  Build txBody with nonce=3, sign, POST /transaction
  → 201 { "tx_id": "abc..." }

  Next tx from this student must use nonce=4.
```

**What if a transaction is rejected for insufficient balance?**  The
nonce is **not** consumed.  You can retry with the same nonce.  The
nonce only advances when a transaction is successfully mined in a block.

**What if the server rejects with "invalid nonce"?**  Your nonce is
stale.  Re-query `GET /account/{pubkey}`, get the new `nonce`, and
re-sign with the correct value.

---

## 5. Check balance and account state

```
GET http://localhost:8080/balance/{address_or_pubkey}
```

The path parameter can be either:
- The full 64-hex-char public key.
- The 24-hex-char derived address.

Response:

```json
{
  "address": "b3a7f19c8d...",
  "balance": 42.5
}
```

### Get account state (balance + nonce) ← NEW

```
GET http://localhost:8080/account/{pubkey}
```

Returns both balance and the next expected nonce for this account.
Call this **before signing** any transaction.

Response:

```json
{
  "address": "b3a7f19c8d... (full pubkey, 64 hex chars)",
  "balance": 42.5,
  "nonce": 3
}
```

Fields:
- `address`: the public key (64 hex chars).
- `balance`: confirmed balance (float).
- `nonce`: next expected nonce for this sender (integer ≥ 0).

---

## 6. Full audit trail

```
GET http://localhost:8080/chain
```

Returns the complete blockchain as JSON (array of blocks with their
transactions).

---

## 7. Reference: Python backend verification

For verification purposes, this is what the backend NCT does when it
receives your `POST /transaction`:

```python
# 1. Deserialize into Transaction dataclass
tx = Transaction(
    sender_pubkey=body["sender_pubkey"],
    receiver_pubkey=body["receiver_pubkey"],
    amount=body["amount"],
    tx_type=body["tx_type"],
    concept=body["concept"],
    signature=body["signature"],
    nonce=body["nonce"],
)

# 2. Compute tx_id from _signing_dict (EXCLUDES signature, INCLUDES nonce)
tx_id_hex = tx.tx_id  # SHA-256(json.dumps(_signing_dict(), sort_keys=True))

# 3. Verify Ed25519 signature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(tx.sender_pubkey))
sig = bytes.fromhex(tx.signature)
pubkey.verify(sig, tx_id_hex.encode())  # encode() → UTF-8 bytes

# 4. Verify nonce (replay protection)
current_nonce = redis.get(f"nonce:{tx.sender_pubkey}")  # None → 0
if tx.nonce != int(current_nonce or 0):
    raise ValueError(f"invalid nonce: expected {current_nonce}, got {tx.nonce}")

# 5. For EARN: check that sender_pubkey == AUTHORITY_PUBKEY
```

---

## 8. Quick-start checklist

- [ ] Install `@noble/curves` (`npm install @noble/curves`).
- [ ] Generate student keypair, store private key securely in browser.
- [ ] Obtain the university's **public key** from the admin (needed to
  build `sender_pubkey` for EARN transactions).
- [ ] Obtain each vendor's **public key** from the address registry
  (needed for `receiver_pubkey` in SPEND transactions).
- [ ] Implement `computeTxId()` with snake_case sorted keys (include `nonce`).
- [ ] Implement `signTxId()` signing `tx_id.encode()` → 128 hex chars.
- [ ] **Before signing:** call `GET /account/{pubkey}` to get the current `nonce`.
- [ ] Include `nonce` in the transaction body and wire payload.
- [ ] POST to `/transaction` with lowercase hex only.
- [ ] Handle 201 (success) and 400 (error with message, including nonce errors).
