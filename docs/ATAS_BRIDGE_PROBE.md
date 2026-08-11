# ATAS Bridge Probe

`AtasMt5BridgeProbe` is a read-only ATAS Chart Strategy for validating the
actual event semantics and transport latency of the installed ATAS/MT5 setup.
It also actively requests and aggregates the ATAS closed M1 basis window used
by the live bridge. It never sends an MT5 trade request and never changes an
ATAS order.

It is read-only. It does not submit, modify, cancel, or copy trading orders.

## What it captures

| ATAS callback or state | Probe event | Why it is needed later |
| --- | --- | --- |
| `OnPositionChanged` | `position_changed` | Net-position source and duplicate callback rate. |
| `OnNewOrder` | `order_new` | New limit, stop, and protective orders. |
| `OnOrderChanged` | `order_changed` | Price moves, partial fills, state transitions, SL/TP/trailing updates, and cancellations. |
| `Orders` at startup | `state_snapshot` | Reconnect recovery and callback-scope validation. |
| `OnBestBidAskChanged` | `quote` | ATAS quote availability, bid/ask semantics, and price freshness. |
| ATAS cumulative-trade request/cache | `basis_history` | Closed M1 GC history for the GC/XAUUSD basis guard. |

Each order record includes its order/external IDs, direction, type, state,
price, trigger price, remaining quantity, OCO group, attachment flags,
auto-cancel flag, and ATAS-reported order latency.

The `IsAttached`, `IsInPosition`, OCO, direction, and trigger fields are
captured as raw observations. The probe intentionally does not label an order
as SL or TP: that classification must be based on the data observed for your
connector and your manual workflow.

Production needs only this strategy. The old standalone
`AtasMt5BasisCollector` source is retained for reference but is not included
in the deployed DLL; `AtasMt5TestDriver` remains an optional demo-only test
strategy.

## Protocol and latency points

The plugin assigns a session ID and increasing event ID. Every event stores:

```text
ATAS callback time (UTC)
ATAS monotonic timestamp
WebSocket server receive time (UTC and monotonic)
optional acknowledgement round-trip time in ATAS StrategyLog
```

`async` delivery sends immediately and does not wait for the acknowledgement.
`sync` delivery still returns from the ATAS callback immediately, but its
background sender waits for the matching acknowledgement and logs its round-trip
time. This lets us compare transport behavior without blocking the ATAS UI.

Quote collection defaults to one event per 50 ms. It is a diagnostic sampling
limit, not a trading throttle; set it to zero only for a short, controlled test.

## Build and run

Build the probe DLL under Windows with ATAS installed:

```powershell
cd atas_probe
$env:ATAS_HOME = "C:\Program Files (x86)\ATAS Platform"
dotnet build -c Release
```

Copy `atas_probe\bin\Release\netstandard2.1\AtasMt5ProbeStrategy.dll` to
`%APPDATA%\ATAS\Strategies\`, restart ATAS, then right-click the target chart
and choose **Chart Strategies**. In the strategy list, select
`AtasMt5BridgeProbe` and click **Add**. Do not install this DLL in the
`Indicators` directory or look for it in the indicators dialog.

```text
Chart context menu -> Chart Strategies -> AtasMt5BridgeProbe -> Add
```

Start the recorder before adding the strategy:

```powershell
$env:PYTHONPATH = "src"
py -m atas_mt5_bridge.probe.server --output-dir probe-data
```

Set the strategy's `Probe WebSocket URL` to `ws://127.0.0.1:8767`. For the
first run, retain the defaults: `async` delivery and a 50 ms quote interval.

The recorder writes one JSONL file per ATAS session, for example:

```text
probe-data/atas_probe_4c16....jsonl
```

## Test sequence

Use an ATAS simulation account with the live copier stopped. Record the exact
clock time for each manual action.

1. Start the probe and verify `lifecycle_started` and `state_snapshot` appear.
2. Place a buy limit, move it twice, then cancel it.
3. Place a buy stop and a sell stop; record any OCO relationship.
4. Open a small market position; add SL and TP using the normal ATAS UI.
5. Move the SL, move the TP, enable breakeven/trailing if used, and then close
   the position manually.
6. Repeat steps 2–5 with `Delivery mode = sync`.
7. Disconnect/reconnect the probe server, then restart the strategy while an
   order and a protection order are active. Preserve the resulting JSONL files.

Do not test an intentionally triggerable stop on a live account. The objective
is callback data, not execution.

## What to provide for the next analysis

Provide the JSONL files and the matching ATAS StrategyLog. We will measure:

- callback-to-server latency and acknowledgement round-trip;
- duplicate position and order callbacks;
- whether manual Chart Trader orders appear in `OnNewOrder` / `OnOrderChanged`;
- the raw field pattern that identifies SL, TP, trailing, OCO, cancellation, and
  partial fill; and
- whether ATAS quote timestamps and bid/ask updates are suitable for direct
  pending-order price mapping.

Only after those observations will the bridge add a copier state machine.
