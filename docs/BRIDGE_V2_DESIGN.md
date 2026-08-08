# Bridge V2: Integration Boundaries

This design defines the integration boundaries for the implemented safe bridge
core. Live MT5 execution, price conversion, and broker-specific validation are
deliberately still outside the executable runtime.

## Boundaries

```text
ATAS callback adapter
  -> durable event/snapshot transport
  -> bridge coordinator (one serialized worker per target symbol)
  -> MT5 execution adapter
  -> MT5 transaction observer

ATAS and MT5 quote adapters
  -> quote cache with source timestamp and freshness state
```

### 1. ATAS callback adapter

Owns only ATAS API access. It emits versioned messages for positions, order
creation/change, full snapshots, and bid/ask updates. It assigns a session ID
and increasing event ID. It must not decide whether an ATAS order is an entry,
TP, SL, or trailing stop.

### 2. Transport and capture journal

The local transport accepts events quickly, responds with an acknowledgement,
and records raw JSONL. It must retain session and event IDs so reconnects can
be diagnosed. Production transport may replace the JSONL recorder, but must
keep the message schema and observable timestamps.

### 3. Canonical source state

Builds one authoritative ATAS state per source symbol from a startup snapshot
and later order/position updates. It is responsible for stale-event detection
and gap detection, after observed ATAS callback semantics are known.

### 4. Per-symbol bridge coordinator

Serializes actions for one MT5 symbol. It accepts newer source state while a
previous MT5 action is pending, but retains only the latest desired state for
the next decision. This removes fixed debounce latency without allowing
concurrent stale actions for the same instrument.

### 5. MT5 execution adapter

Initial implementation may use Python `MetaTrader5` for validation. Its public
contract must support market execution, pending create/modify/remove, and
position SL/TP modification. A later MQL5 EA adapter can use `OrderSendAsync`
when measurements show Python's synchronous request is the relevant bottleneck.
The execution adapter reports request acceptance separately from confirmed
account state.

### 6. MT5 transaction observer

For production, a minimal MQL5 EA should forward `OnTradeTransaction` events
to the local bridge. It confirms order/position state after manual actions,
pending activation, partial fills, and broker changes. Python's `positions_get`
and `orders_get` remain useful periodic snapshots, but are not a push feed.

### 7. Quote adapters

ATAS and MT5 quotes are collected independently with their source timestamps,
bid/ask values, and freshness. No price mapping is performed at this boundary.
That preserves the option to use raw-price, offset, or relative-distance mapping
only after the observed data supports it.

## Implemented safe framework

The framework is in `src/atas_mt5_bridge/v2/`. It is deliberately separate
from the legacy target-net bridge and is not connected to a live MT5 adapter.

- `atas_adapter.py` converts probe messages to a canonical source event.
- `source_state.py` maintains the current ATAS position and active order
  instances for one symbol.
- `intent_ledger.py` maintains ATAS source intents and their `1..N` target
  legs without inferring replacement orders by timing or price.
- `policy.py` contains the ATAS-authoritative net-equivalent policy. It is
  disabled by default and does not send commands unless explicitly enabled by
  a non-live simulator.
- `coordinator.py` defines the one-worker execution boundary, in-flight risk
  limit, unknown-outcome block, and MT5-observer reconciliation entry point.
- `simulation.py` and `runtime.py` run deterministic local tests through the
  existing probe protocol; neither module can connect to MT5.

An ATAS `Order.Id` is treated as an **order-instance ID**, not an intention
ID. A cancellation followed by a new order is therefore represented as two
independent source facts. A future policy may send the same cancellation and
new-order lifecycle to MT5 without any timing heuristic.

The transport event ID is global: it also counts quote and lifecycle messages.
The framework records those as auxiliary events to validate continuity, but
does not call a copy policy for them. A gap blocks planning until a fresh
source snapshot is received.

The execution port records `accepted`, `confirmed`, `failed`, and `unknown`
separately. A failed or unknown target result blocks further automatic plans
for that symbol and marks it as requiring reconciliation. A target observer
can call `handle_target_observation()` after an MT5 transaction to finish a
pending cancellation or repair without waiting for another ATAS event.

`TargetAccountState` explicitly retains long and short quantities and the
target account model (`netting` or `hedging`). Consequently, a later position
policy can decide how to reduce opposite MT5 exposure before opening a new
side; the framework itself does not make that decision.

## Latency acceptance criteria

The 100 ms target is an end-to-end user objective, but broker execution latency
cannot be guaranteed by the local bridge. The first measurable local criteria
are:

- ATAS callback to recorder receive: P95 below 20 ms on the same machine;
- no fixed sleep/debounce in the position/event delivery path;
- no ATAS callback waits for an MT5 or WebSocket acknowledgement;
- every unacknowledged synchronous probe event is visible in the log; and
- quote sampling never blocks position or order events.

The probe records the evidence for these criteria before any execution rule is
activated.
