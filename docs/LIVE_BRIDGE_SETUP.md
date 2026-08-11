# Live bridge: gated setup and recovery

This is the first executable ATAS GC → MT5 XAUUSD boundary. It is designed
for a local machine and starts in **observe-only** mode. Do not enable either
execution switch until the commands and snapshots have been reviewed on a
demo account.

## Components

```text
ATAS strategy (localhost WebSocket, :8767)
    -> Python bridge service + SQLite
    -> localhost HTTP (:8768, token protected)
    -> MT5 EA on the XAUUSD chart
    -> MT5 terminal / broker
```

Only the EA uses `OrderSendAsync`; Python does not connect to a broker. The
EA reports complete snapshots of bridge-owned objects identified by its Magic
number. If there is any other XAUUSD order or position with a different Magic
number, the bridge quarantines the symbol and submits nothing.

## Build and first run

1. Compile `mt5_ea/AtasMt5BridgeEA.mq5` in MetaEditor, then attach it to the
   exact target symbol chart (normally `XAUUSD`; broker suffixes require the
   same suffix in `--target-symbol`). In the EA settings, add
   `http://127.0.0.1:8768` to MT5's allowed WebRequest URLs.
2. Use the same non-empty token and Magic number on both sides. Keep
   `EnableExecution=false` in the EA for the first run.
3. Start the service without `--enable-execution`; it will record and validate
   ATAS and MT5 state but cannot send a command:

   ```powershell
   $env:PYTHONPATH = "src"
   python -m atas_mt5_bridge.v2.live_service `
     --source-symbol GC `
     --target-symbol XAUUSD `
     --bridge-magic 26081001 `
     --volume-multiplier 1 `
     --token 'replace-with-a-long-local-token'
   ```

4. Configure the ATAS probe WebSocket as `ws://127.0.0.1:8767`. Add the
   separate `AtasMt5BasisCollector` strategy to a dedicated **GC M1** chart;
   keep its source symbol as `GC`, set its chart UTC offset to `480`, and keep
   the main trading chart on any timeframe you prefer. The collector exports
   the most recent 240 *closed* M1 bars once per minute and has no trading
   capability. Confirm that
   the bridge receives a full ATAS snapshot and the EA posts a complete MT5
   snapshot. The `/v1/status` endpoint (with the token header) must show both
   `source_synchronized=true` and `source_position_complete=true` before
   relying on position repair. If ATAS has not yet pushed the existing
   position after strategy start, the bridge deliberately treats it as unknown
   rather than as flat. Before execution, `/v1/status` must also show at least
   180 source and target M1 bars and at least 180 basis samples. Raw bars and
   a previous process's basis are not kept in SQLite: the calibration is
   rebuilt from the recent intraday windows after every bridge start.
5. For a demo test only, start the service with `--enable-execution` **and**
   set EA `EnableExecution=true`. Both gates are necessary. Keep volume at the
   broker's minimum until the observed lifecycle is understood.

`bridge-state/bridge.db` stores source checkpoints, intent/leg mapping, and
operation records. Copy this database before changing bridge versions; do not
remove it to clear an incident. Basis calibration is intentionally not stored.

## State machines

### Command operation

```text
persisted PENDING -> delivered to EA -> ACCEPTED -> snapshot CONFIRMED
                         |                    |
                         v                    v
                       FAILED              UNKNOWN (quarantine)
```

The EA persists its last command ID locally before `OrderSendAsync`; the
service therefore offers a command repeatedly until it receives a durable EA
receipt, rather than losing it with one dropped HTTP response. The EA will
return `UNKNOWN`, not submit twice, if it restarts after recording intent but
before it has recorded the local request result. A complete MT5 snapshot is
still required to prove that a pending order exists, was cancelled, a ticket
was reduced, or target net exposure reached the expected value. One symbol
has at most one unresolved command. This intentionally serializes commands
while avoiding any fixed 250 ms debounce on ATAS events.

After five seconds without snapshot proof, an accepted/pending command is
marked `UNKNOWN` and the symbol is quarantined. It is not retried
automatically. A later complete snapshot can still resolve it if the effect
appears.

### Order intent and target legs

```text
ATAS order instance -> intent -> 1..N target legs
DESIRED -> CREATE_PENDING -> ACTIVE -> CANCEL_PENDING -> CANCELLED
                                      \-> PARTIAL_FILL -> cancel remainder
```

ATAS `Order.Id` is treated as an order-instance ID. A drag is a cancellation
followed by a new order, so it becomes two independent intents; no timing or
price heuristic claims that they are the same order. Every MT5 pending order
uses a short `AB2:<tag>` comment so the durable ledger can identify it after
an EA/service restart.

For a target partial fill, this first policy cancels the residual target order
before doing the ATAS-authoritative net-position repair. It deliberately does
not attempt clever partial-fill replication. A still-active source order is
not silently re-created after this asymmetric incident; treat it as an
operator-visible divergence until the source order changes or is replaced.

### Exposure policy

ATAS is authoritative. The bridge compares confirmed ATAS net position with
the bridge-owned MT5 net position. On a hedging account it first closes an
opposite ticket, then opens only the residual market quantity. It intentionally
allows offsetting long/short MT5 tickets when their net is correct, avoiding a
complex “must always be one-directional” MT5 state machine.

## Incident procedure

1. Stop enabling new execution. Keep the process, EA log, `operations.jsonl`,
   and SQLite database.
2. Compare the current ATAS full snapshot with the EA's complete MT5 snapshot.
   Confirm that no foreign/manual XAUUSD object is present.
3. If an operation is `UNKNOWN`, first wait for one or more fresh MT5
   snapshots. If its expected effect appears, the service resolves it without
   a duplicate request.
4. Only after a human verifies **no target-side effect**, a token-protected
   local operator may release that one command for a new plan:

   ```powershell
   Invoke-RestMethod -Method Post `
     -Uri 'http://127.0.0.1:8768/v1/operator/confirm-no-effect' `
     -Headers @{ 'X-Bridge-Token' = 'replace-with-a-long-local-token' } `
     -ContentType 'application/json' `
     -Body '{"command_id":"copy-the-operation-id-from-the-journal"}'
   ```

   This endpoint is deliberately not used by the EA and never runs
   automatically. It records `OBSERVED_NO_EFFECT` before allowing a new plan.
5. A definite `FAILED` request remains blocked. Correct its cause (symbol,
   volume, stops/freeze distance, terminal permissions, or broker response),
   then use the same explicit endpoint only after verifying that it had no
   target-side effect. Never alter SQLite manually.

## Current safeguards and limits

- 100 ms EA poll/snapshot interval is configurable; no bridge-side debounce.
- ATAS events use serialized transport with acknowledgement/retry. During a
  disconnect only quote events are capped (20 by default); order/position
  events and periodic authoritative snapshots remain queued. MT5 and ATAS
  callbacks are serialized by symbol inside the service.
- Quotes must be fresh (250 ms), basis history must have 180 matching closed
  M1 samples, and the
  current side basis cannot deviate more than 2.00 or have dispersion above
  1.00. ATAS freshness uses its callback timestamp; MT5 freshness uses the
  local bridge's receipt time for each complete EA snapshot, avoiding the
  EA/server clock's one-second timestamp precision. These are deliberately
  conservative starting values to tune from logged GC/XAUUSD data, not
  trading recommendations.
- The estimator uses the latest four-hour M1 window. It is reconstructed from
  the ATAS GC M1 collector and MT5 `CopyRates(..., PERIOD_M1, 1, 240, ...)`
  on startup and each minute. Raw bars and basis samples are not persisted.
- MT5 volume, tick size, stops level and freeze level are checked before
  pending order creation. The mapped price is rounded only to the target tick.
- The initial executable mapping covers direct pending-order mirroring,
  ATAS-authoritative net-position repair, and the observed ATAS `TP`/`SL`
  comment pair. A single ATAS TP/SL pair is mapped to native protection on
  every bridge-owned MT5 position only when all target positions have the
  same direction as ATAS net exposure and the protection quantity equals that
  net exposure. Ambiguous multi-TP/SL layouts, direction mismatch, or hedged
  target exposure are intentionally quarantined from protection changes.
