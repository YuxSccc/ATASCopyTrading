# Bridge V2: Integration Boundaries

This design defines the integration boundaries for the implemented bridge core.
The live boundary is local and deliberately gated: a Python service maintains
durable state, while a small MQL5 EA is the only component that can submit a
request to the MT5 terminal.

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

`mt5_ea/AtasMt5BridgeEA.mq5` uses a loopback HTTP protocol and
`OrderSendAsync`. It polls at a configurable interval (100 ms by default),
but execution is disabled by default in both the EA and Python service. The
execution adapter reports queue delivery and request acceptance separately
from confirmed account state.

### 6. MT5 transaction observer

The EA schedules an immediate full, bridge-filtered snapshot after
`OnTradeTransaction`, and also publishes a periodic snapshot. That snapshot
confirms order/position state after manual actions, pending activation, partial
fills, and broker changes. A command is never considered complete solely
because `OrderSendAsync` accepted its local request.

### 7. Quote adapters

ATAS and MT5 quotes are collected independently with their source timestamps,
bid/ask values, and freshness. No price mapping is performed at this boundary.
That preserves the option to use raw-price, offset, or relative-distance mapping
only after the observed data supports it. The initial live policy stores a
four-hour side-aware GC/XAUUSD basis history in SQLite, uses a recent-weighted
median, and blocks new pending orders when quotes are stale, the basis shocks,
the dispersion is high, the resulting volume is invalid, or an MT5 stops/freeze
rule would be violated.

## Implemented safe framework

The framework is in `src/atas_mt5_bridge/v2/`. It is deliberately separate
from the deleted legacy target-net bridge.

- `atas_adapter.py` converts probe messages to a canonical source event.
- `source_state.py` maintains the current ATAS position and active order
  instances for one symbol.
- `intent_ledger.py` maintains ATAS source intents and their `1..N` target
  legs without inferring replacement orders by timing or price.
- `policy.py` contains the ATAS-authoritative net-equivalent policy. It is
  disabled by default and does not send commands unless explicitly enabled by
  a non-live simulator.
- `coordinator.py` defines the one-worker execution boundary, persisted
  write-ahead operation state, in-flight risk limit, unknown-outcome block,
  and MT5-observer reconciliation entry point.
- `persistence.py`, `state_codec.py`, and `operation_book.py` persist source
  checkpoints, ledger legs, operation outcomes, and price history to SQLite.
- `pricing.py` applies the side-aware basis and market-validity guard before a
  risk-increasing pending order can be created.
- `mt5_gateway.py` and `live_service.py` implement the local authenticated
  MT5 EA protocol. The gateway never connects to a broker directly.
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

The execution port records `pending`, `accepted`, `confirmed`, `failed`, and
`unknown` separately. Every command is persisted before it is offered to the
EA. A failed or unknown target result blocks further automatic plans for that
symbol and marks it as requiring reconciliation. On process restart unresolved
commands may be offered again only through the EA's durable command-ID
deduplication protocol; an EA that restarted after persisting intent but before
recording the request outcome reports `unknown` instead of submitting a second
request. A target observer can call `handle_target_observation()` after an MT5
transaction to finish a pending cancellation or repair without waiting for
another ATAS event.

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
