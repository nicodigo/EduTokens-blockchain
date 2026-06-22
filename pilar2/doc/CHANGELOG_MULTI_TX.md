# Changelog — Soporte multi-transacción por sender por bloque

**Fecha**: 2026-06-22  
**Alcance**: Pilar 2 — NCT (orquestador de la blockchain)  
**Tests**: 271 passed, 0 failed (incluye 29 nuevos + 3 correcciones de mocks preexistentes)

---

## Resumen del cambio

Antes de este cambio, un mismo sender solo podía tener **una transacción pendiente por bloque**. Si quería enviar una segunda, debía esperar a que la primera fuera minada y confirmada en cadena. Esto era una limitación importante para la experiencia de usuario.

Ahora el sistema soporta que un sender envíe **múltiples transacciones con nonces consecutivos** que se incluyen en el mismo bloque, manteniendo todas las protecciones de seguridad (anti-replay, anti-doble-gasto).

---

## Qué cambió — flujo simplificado

```
ANTES:
  POST /tx(nonce=2) → 400 "expected 1, got 2"  ← solo aceptaba el nonce exacto
  Pool: FIFO sin ordenar
  Gaps de nonce → descartados (no volvían al pool)
  GET /account → solo mostraba nonce confirmado

AHORA:
  POST /tx(nonce=2) → 201 ✓  ← acepta nonce ≥ confirmado (con cota)
  Pool: snapshot + sort por sender/nonce al ensamblar bloque
  Gaps de nonce → permanecen en pool esperando que se llene el hueco
  GET /account → nonce confirmado + pending_nonce (el próximo a usar)
```

---

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `nct/state.py` | + `get_sender_nonces(pubkey)` — consulta nonces pendientes en pool |
| | + `remove_transactions(tx_ids)` — elimina txs procesadas del pool |
| | + campo `max_nonce_window` en `NCTConfig` |
| `nct/nct.py` | `drain_pool_validated()` reescrito: snapshot + agrupación + sort por nonce |
| | `accumulate_transactions()` con reintento cuando el pool solo tiene gaps |
| | `POST /transaction` — nonce aceptado si `≥ confirmado` y `≤ confirmado + MAX_NONCE_WINDOW` |
| | `GET /account/{pubkey}` — nuevo campo `pending_nonce` |
| | `load_config()` — nueva variable de entorno `MAX_NONCE_WINDOW` |
| `shared/schemas.py` | `AccountResponse` + campo `pending_nonce` |
| `tools/send_test_tx.py` | Soporte para `--nonce N` y muestra `confirmed_nonce` + `pending_nonce` |

---

## Nuevo endpoint: `GET /account/{pubkey}`

Respuesta ampliada:

```json
{
  "address": "a1b2c3...",
  "balance": 5000,
  "nonce": 3,
  "pending_nonce": 5,
  "discarded_transactions": ["txid1", "txid2"]
}
```

| Campo | Significado |
|---|---|
| `nonce` | Próximo nonce **confirmado** en cadena. Una transacción con este nonce puede ser minada inmediatamente. |
| `pending_nonce` | Próximo nonce **disponible para enviar**. Avanza sobre las transacciones contiguas ya en el pool. Si hay un hueco (gap), apunta al nonce faltante. |
| `discarded_transactions` | tx_ids de transacciones del sender que fueron descartadas en el último ensamblaje de bloque (replay o saldo insuficiente). |

### Ejemplos de `pending_nonce`

```
nonce confirmado = 3

pool vacío                          → pending_nonce = 3  (sin pendientes)
pool: [nonce=3]                     → pending_nonce = 4
pool: [nonce=3, nonce=4]            → pending_nonce = 5  (ambas contiguas)
pool: [nonce=3, nonce=5]            → pending_nonce = 4  (gap en 4 — ¡hay que llenarlo!)
pool: [nonce=5]                     → pending_nonce = 3  (gap desde el inicio)
pool: [nonce=1, nonce=5]            → pending_nonce = 3  (nonce=1 ignorado, ya consumido)
```

---

## Nueva variable de entorno

| Variable | Default | Descripción |
|---|---|---|
| `MAX_NONCE_WINDOW` | `100` | Máximo salto de nonce aceptado en el POST. Previene inundación del pool con nonces futuros extremos. |

---

## Limitaciones (decisiones conscientes para el PoC)

### 1. Huecos de nonce no se resuelven automáticamente

Si un cliente envía nonce 1 y nonce 3 sin enviar nonce 2, tx3 queda en el pool **indefinidamente**. La cadena no puede minarla porque el modelo de nonces es secuencial estricto.

**Justificación**: es el comportamiento estándar de cualquier blockchain con nonces (Ethereum incluido). La responsabilidad de no generar gaps es del cliente. `GET /account/{pubkey}` expone `pending_nonce` justamente para que el cliente detecte y corrija el hueco.

**Workaround para el cliente**: si se genera un gap, enviar una transacción con el nonce faltante (por ejemplo, un SPEND de 1 token a sí mismo, o un EARN si es el authority) para "destrabar" la cola.

### 2. Sin timeouts de evicción para gap-txs

Las transacciones en gap permanecen en el pool para siempre. No hay un mecanismo automático que las descarte por antigüedad.

**Justificación**: descartar una gap-tx no resuelve el problema — el nonce on-chain no avanza, así que transacciones futuras con nonces más altos seguirían trabadas. Un timeout solo liberaría espacio en el pool, pero no destrabaría la cuenta. Para un PoC universitario con pocos usuarios, el pool nunca crecerá lo suficiente como para ser un problema.

**Posible Fase 2**: si se necesita, agregar un `pool_cleaner` periódico que descarte txs con más de N minutos de antigüedad (solo para liberar memoria, sin avanzar nonces).

### 3. Sin límite de tamaño de pool en memoria

El pool (`NCTState._tx_pool`) es una lista Python sin cota máxima de elementos.

**Justificación**: `MAX_NONCE_WINDOW` (default 100) previene inundación por nonces futuros extremos. Con pocos usuarios concurrentes, el pool se mantiene pequeño. En producción se agregaría `MAX_POOL_SIZE`.

### 4. Sin "salto" de nonces (unstick por checkpoint)

No existe un mecanismo para que una cuenta "salte" un nonce perdido (por ejemplo, una transacción especial que declare "renuncio al nonce 2, próximo nonce es 3").

**Justificación**: requiere cambios en el modelo de datos, validación de bloques, y firma. Es una feature compleja que excede el alcance de un PoC universitario.

### 5. Nonces no reordenables dentro del pool

Las transacciones se ordenan por nonce al momento de ensamblar el bloque (`drain_pool_validated`), pero no se reordenan dentro del pool en tiempo real. Si un cliente envía nonce 3 y luego nonce 2 (con latencia de red), el pool las almacena en orden de llegada [3, 2], pero al ensamblar el bloque se ordenan correctamente → ambas se incluyen.

**Justificación**: ordenar en `drain_pool_validated` es suficiente para el caso de uso. Mantener el pool ordenado en tiempo real agregaría complejidad sin beneficio práctico para un PoC.

---

## Seguridad — lo que NO cambió

| Protección | Estado |
|---|---|
| Anti-replay (nonce secuencial firmado) | ✅ Mantenida — nonce < confirmado → rechazado |
| Anti-doble-gasto (overlay de saldo intra-bloque) | ✅ Mantenida — balance + overlay por sender |
| Verificación de firma Ed25519 | ✅ Sin cambios |
| Authority check para EARN | ✅ Sin cambios |
| Rate limiting | ✅ Sin cambios |
