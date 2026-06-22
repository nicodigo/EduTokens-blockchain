# Guía para el cliente — Envío de múltiples transacciones sin bloquear

**Dirigido a**: desarrollador del frontend / wallet  
**Versión del NCT**: post Phase 1 (multi-tx)  
**Última actualización**: 2026-06-22

---

## 1. Regla de oro

```
Siempre usá pending_nonce, nunca nonce, al construir una transacción nueva.
```

`GET /account/{pubkey}` ahora devuelve **dos** nonces distintos. Usar el equivocado **bloqueará tu cuenta**.

---

## 2. El endpoint `GET /account/{pubkey}`

### Request

```
GET /account/{pubkey}
```

Donde `{pubkey}` es la clave pública Ed25519 de 64 caracteres hex.

### Response (nuevo campo en **negrita**)

```json
{
  "address": "1a2b3c4d5e6f...",
  "balance": 5000,
  "nonce": 3,
  "pending_nonce": 5,
  "discarded_transactions": []
}
```

| Campo | Qué significa | Cuándo usarlo |
|---|---|---|
| `nonce` | Nonce **confirmado** en cadena. La próxima tx que se mine debe tener este nonce (o mayor). | Solo para depuración / mostrar en UI como "nonce on-chain". |
| **`pending_nonce`** | **Nonce que DEBÉS usar en tu próxima tx**. Considera las txs que ya enviaste y están en el pool. | **Este es el valor correcto para `nonce` en `POST /transaction`.** |
| `balance` | Saldo confirmado en cadena (no descuenta txs pendientes). | Mostrar en UI, validar antes de enviar. |
| `discarded_transactions` | tx_ids de tus transacciones que el NCT descartó (saldo insuficiente o replay). | Mostrar al usuario para que sepa qué falló. |

---

## 3. Flujo correcto para enviar N transacciones seguidas

### Escenario: un estudiante quiere hacer 3 gastos (TP1, COMEDOR, FOTOCOPIAS)

```
Paso 1 — Consultar estado de la cuenta
─────────────────────────────────────────
GET /account/{pubkey}
→ nonce: 5, pending_nonce: 5, balance: 1000

Paso 2 — Preparar y enviar tx1 (nonce = pending_nonce = 5)
────────────────────────────────────────────────────────────
POST /transaction
{
  "sender_pubkey": "...",
  "receiver_pubkey": "...",
  "amount": 100,
  "tx_type": "SPEND",
  "concept": "TP1",
  "nonce": 5,            ← pending_nonce del paso 1
  "signature": "..."
}
→ 201 { "tx_id": "abc111", "status": "pending" }

Paso 3 — Preparar y enviar tx2 (nonce = nonce_anterior + 1 = 6)
─────────────────────────────────────────────────────────────────
NO necesitás consultar /account de nuevo. El nonce es secuencial:
si enviaste nonce=5, la siguiente es nonce=6.

POST /transaction
{
  ...
  "concept": "COMEDOR",
  "nonce": 6,            ← 5 + 1
  ...
}
→ 201 { "tx_id": "abc222", "status": "pending" }

Paso 4 — Preparar y enviar tx3 (nonce = 7)
─────────────────────────────────────────────
POST /transaction
{
  ...
  "concept": "FOTOCOPIAS",
  "nonce": 7,            ← 6 + 1
  ...
}
→ 201 { "tx_id": "abc333", "status": "pending" }

Paso 5 — Esperar confirmación
───────────────────────────────
Las 3 transacciones están en el pool. Cuando el NCT mine el próximo bloque,
las 3 se incluirán juntas (nonces 5, 6, 7 son contiguos → todas pasan).

Podés consultar GET /account/{pubkey} para ver si nonce avanzó de 5 a 8.
Si nonce == 8, las 3 transacciones fueron confirmadas.
```

### Regla nemotécnica

```
pending_nonce inicial → usalo en tx1
tx(n+1).nonce = tx(n).nonce + 1   ← para las siguientes en la misma ráfaga
```

---

## 4. Qué NO hacer

### ❌ Usar `nonce` en vez de `pending_nonce`

```python
# MAL
account = GET /account/{pubkey}
nonce = account["nonce"]        # ← usa 5, pero ya enviaste tx con nonce=5
POST /transaction { "nonce": nonce }  # ← 400 "nonce already consumed"
```

Si ya enviaste una transacción con nonce=5 y todavía no se minó, `nonce` sigue siendo 5 (no avanza hasta confirmación). Usar `nonce` te dará error de replay. **Siempre usá `pending_nonce`**.

### ❌ Dejar huecos en los nonces

```python
# MAL — te salteaste nonce=6
POST /tx { "nonce": 5 }  # ✓
POST /tx { "nonce": 7 }  # ✓ entra al pool, pero queda en gap
POST /tx { "nonce": 8 }  # ✓ entra al pool, pero queda en gap también
```

La tx con nonce=7 y nonce=8 **nunca se minarán** hasta que envíes la tx con nonce=6. Tu cuenta queda bloqueada. `pending_nonce` te mostrará `6` para que sepas qué falta.

### ❌ Usar el mismo nonce dos veces

```python
# MAL
POST /tx { "nonce": 5, "concept": "TP1" }  # ✓
POST /tx { "nonce": 5, "concept": "COMEDOR" }  # → descartada como duplicado
```

Solo una transacción por nonce por sender. La segunda será descartada.

---

## 5. Cómo recuperarse de un hueco (gap)

Si accidentalmente generaste un gap (ej: enviaste nonce 5 y nonce 7, falta nonce 6):

```
Paso 1 — Detectarlo
GET /account/{pubkey}
→ nonce: 5, pending_nonce: 6  ← ¡pending_nonce apunta al hueco!

Paso 2 — Llenar el hueco
Enviá UNA transacción con el nonce faltante:
POST /transaction { "nonce": 6, ... }
→ 201

Paso 3 — Verificar que se destrabó
GET /account/{pubkey}
→ nonce: 5, pending_nonce: 8  ← ¡las tres (5,6,7) ahora son contiguas!
```

La transacción que llena el hueco puede ser un SPEND de 1 token a vos mismo (no-op económico) o cualquier operación real que necesites hacer con ese nonce.

---

## 6. Manejo de errores

### `400 "nonce X already consumed (current: Y)"`

Tu transacción tiene un nonce que ya fue minado en un bloque anterior. **No reenvíes con el mismo nonce.** Consultá `/account` y usá `pending_nonce`.

### `400 "nonce X too far ahead of current Y (max gap: 100)"`

El nonce que enviaste está más de 100 posiciones adelante del confirmado. Reducilo. Si realmente necesitás ese nonce, enviá transacciones con los nonces intermedios primero.

### `409 "transaction already in pool"`

Esa misma transacción (mismo tx_id) ya está pendiente. No necesitás hacer nada — esperá a que se mine.

### Transacción en `discarded_transactions`

Significa que tu transacción fue descartada durante el ensamblaje del bloque. Dos causas posibles:

- **Saldo insuficiente**: el total de tus SPENDs en ese bloque superaba tu balance. Reducí los montos o esperá a que llegue un EARN.
- **Nonce duplicado**: enviaste dos txs con el mismo nonce. La segunda fue descartada.

En ambos casos, **volvé a enviar la transacción** con los datos corregidos.

---

## 7. Algoritmo resumido (pseudocódigo)

```python
def enviar_transacciones(privkey, operaciones):
    """
    operaciones: lista de (receiver, amount, tx_type, concept)
    Envía todas las transacciones con nonces consecutivos.
    """
    pubkey = derivar_pubkey(privkey)
    
    # 1. Obtener el punto de partida
    account = GET(f"/account/{pubkey}")
    next_nonce = account["pending_nonce"]
    
    tx_ids = []
    for (receiver, amount, tx_type, concept) in operaciones:
        # 2. Construir y firmar
        tx = construir_transaccion(
            sender=pubkey,
            receiver=receiver,
            amount=amount,
            tx_type=tx_type,
            concept=concept,
            nonce=next_nonce,
        )
        tx["signature"] = firmar(privkey, tx["tx_id"])
        
        # 3. Enviar
        resp = POST("/transaction", tx)
        if resp.status == 201:
            tx_ids.append(resp["tx_id"])
            next_nonce += 1
        elif resp.status == 400 and "already consumed" in resp["error"]:
            # Nonce ya usado — refrescar y reintentar
            account = GET(f"/account/{pubkey}")
            next_nonce = account["pending_nonce"]
            # reintentar esta operación
        elif resp.status == 400 and "too far ahead" in resp["error"]:
            # Error de programa — el gap es demasiado grande
            raise Exception("Nonce demasiado adelantado — revisar lógica")
        else:
            raise Exception(f"Error inesperado: {resp}")
    
    # 4. Esperar confirmación (opcional, podés hacer polling)
    while True:
        account = GET(f"/account/{pubkey}")
        if account["nonce"] > next_nonce:
            break  # todas confirmadas
        sleep(2)
    
    return tx_ids
```

---

## 8. Cambios respecto a la versión anterior

| Aspecto | Antes | Ahora |
|---|---|---|
| Nonce para nueva tx | `account["nonce"]` | **`account["pending_nonce"]`** |
| Enviar 2+ txs seguidas | ❌ Bloqueado (400 "expected X, got Y") | ✅ Funciona si los nonces son consecutivos |
| Consultar `/account` entre txs | Obligatorio (nonce solo avanza al confirmar) | Opcional — podés incrementar secuencialmente |
| Detectar hueco | No había forma | `pending_nonce` apunta al nonce faltante |
