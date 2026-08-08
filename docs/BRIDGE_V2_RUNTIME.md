# Bridge V2 safe runtime

The current runtime can consume the existing `AtasMt5BridgeProbe` WebSocket
stream in two safe modes. It does not contain an MT5 terminal adapter, uses no
account credentials, and cannot submit an MT5 order.

| Mode | Effect |
| --- | --- |
| `recorder` | Existing read-only JSONL capture only. |
| `observe` | Builds ordered ATAS state and writes decisions, but emits no target commands. |
| `simulate` | Runs the ATAS-authoritative policy against an in-process MT5-like simulator. |

## Run the simulator

Use the exact value emitted by the probe's `source_symbol`; set the strategy's
`Source symbol override` to `GC` first if you want a stable short symbol.

```powershell
$env:PYTHONPATH = "src"
C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe `
  -m atas_mt5_bridge.probe.server `
  --output-dir probe-data `
  --bridge-mode simulate `
  --source-symbol GC `
  --target-symbol XAUUSD
```

Then add the already-installed `AtasMt5BridgeProbe` to the GC chart. The
strategy stays read-only; the Python process creates and cancels only simulated
target orders in memory.

## Outputs

The normal raw probe file remains in `probe-data/atas_probe_<session>.jsonl`.
The runtime additionally writes:

- `probe-data/runtime/bridge-runtime-decisions.jsonl` — state-machine result
  for every processed source event.
- `probe-data/runtime/bridge-operation-journal.jsonl` — append-only simulated
  target commands and receipts.

The important simulation sequence is: place a remote pending order, move it,
cancel it; then open a position, add and move TP/SL, and close it. The logs
should show source order instances becoming separately tracked target legs,
old legs being cancelled, and no duplicate target create after repeated source
callbacks.

## Deliberate limitations before MT5 activation

- The simulator accepts source prices directly. A live adapter must first add
  the GC/XAUUSD 4-hour side-basis estimator, quote freshness, tick, stops, and
  freeze-level checks from `COPY_ALGORITHM_DRAFT.md`.
- The runtime does not persist/reload an intent ledger across a process restart
  yet; it records an audit journal and reconstructs current desired state only
  from a fresh ATAS snapshot. Live activation requires durable ledger recovery
  and an MT5 snapshot reconciliation procedure.
- No MQL5 EA transaction observer exists yet. A live adapter must tag all
  bridge-owned orders with magic number plus `leg_id`, publish
  `OnTradeTransaction`, and provide complete bridge-only positions including
  tickets for a hedging account.
- Automatic ATAS reverse repair and the near-price execution-quality policy
  are not enabled in this version.
