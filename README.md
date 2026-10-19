# ATAS to MT5 Bridge

## GC/MGC drawing sync

The standalone `GC-MGC Drawing Sync V1` indicator displays native MGC rectangles
and trend lines on GC, including moves and deletions. Add it to both charts;
Auto mode publishes on MGC and receives on GC. See
[setup and diagnostics](docs/GC_MGC_DRAWING_SYNC.md).

## Automated demo test (ATAS order API)

The test driver is a separate ATAS strategy, `AtasMt5TestDriver`.  It is not
part of the live copy strategy and ships disabled.  When both explicit switches
below are enabled, a local HTTP call runs one fixed scenario: **distant buy
limit → modify farther away → cancel**.  It has no endpoint for arbitrary
price, side, or quantity, and the driver operates only on orders whose comment
is `AB2TEST:<run-id>`.

This first scenario validates ATAS' asynchronous order callbacks, order-ID
replacement after a modify, the WebSocket path, and the locally persisted test
report.  It deliberately does **not** enable MT5 execution.

1. Reload ATAS after installing the current DLL and add `AtasMt5TestDriver` to
   the GC demo chart. Set `Enable demo test trading=true`, choose the intended
   demo portfolio, keep `Maximum test quantity=1`, `Test price step=0.1`.
2. Start the bridge, but leave out `--enable-execution`:

   ```powershell
   cd 'D:\VSCODE PROJECT\ATASTOMT5'
   $env:PYTHONPATH = "$PWD\src"
   python -m atas_mt5_bridge.v2.live_service `
     --source-symbol GC --target-symbol XAUUSD --bridge-magic 26081001 `
     --state-db bridge-state-demo\bridge.db `
     --journal bridge-state-demo\operations.jsonl --output-dir probe-data-demo `
     --enable-test-control
   ```

3. After the driver appears in the chart strategies list and is activated,
   trigger one run from PowerShell:

   ```powershell
   $run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8769/v1/test/runs `
     -ContentType 'application/json' -Body '{"scenario":"pending_lifecycle"}'
   $run
   Invoke-RestMethod -Uri "http://127.0.0.1:8769/v1/test/runs/$($run.run_id)"
   ```

   The test plan is hot-loaded by the local service; no ATAS reload is needed
   for a new bounded script. For example, this sends a distant sell limit,
   moves it, then cancels it:

   ```powershell
   $body = @'
   {"scenario":"pending_script","steps":[
     {"id":"entry","action":"open_pending","parameters":{"side":"sell","quantity":"1","price_offset_ticks":100}},
     {"id":"move","action":"modify_pending","parameters":{"order_ref":"entry","price_offset_ticks":120}},
     {"id":"cancel","action":"cancel_pending","parameters":{"order_ref":"entry"}}
   ]}
   '@
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8769/v1/test/runs `
     -ContentType 'application/json' -Body $body
   ```

The run record is appended to `probe-data-demo/test_runs.jsonl`. To stop an
in-progress test, post `{}` to `/v1/test/runs/<run-id>/abort`; an observed
active test order is then cancelled. The test API binds
only to `127.0.0.1` and is absent unless `--enable-test-control` is supplied.

This repository contains a read-only ATAS integration probe and a safety-first
ATAS-to-MT5 bridge core. It also includes a **guarded local MT5 EA boundary**:
both the bridge service and the EA start with execution disabled, so merely
installing either component cannot send an order to a broker.

The probe captures ATAS order, position, and quote callbacks over localhost
WebSocket. The v2 framework provides ordered source-state handling, SQLite
recovery, per-symbol coordination, explicit execution outcomes, an
`intent -> 1..N target legs` mapping model, price-basis guards, and a safe
local simulator for either MT5 netting or hedging state modelling.

The legacy target-net copy plugin has been removed.

## Test the ATAS probe

Build and install the probe as described in `docs/ATAS_BRIDGE_PROBE.md`. Start
the local recorder before enabling the `AtasMt5BridgeProbe` chart strategy:

```powershell
$env:PYTHONPATH = "src"
python -m atas_mt5_bridge.probe.server --output-dir probe-data
```

Run the test suite:

```powershell
python -m pytest -q
```

See `docs/BRIDGE_V2_DESIGN.md` for the framework boundaries and safety model.
See `docs/BRIDGE_V2_RUNTIME.md` to run the safe simulator with the existing
ATAS probe. See `docs/LIVE_BRIDGE_SETUP.md` for the deliberately gated MT5 EA
installation, operation state machine, recovery protocol, and first-live-test
checklist.

## One-click live bridge startup

The project root contains `start_bridge.cmd` (or `start_bridge.ps1`). It starts
the current two-pair configuration without retyping the long command line:

```powershell
.\start_bridge.cmd
```

The default shortcut enables Python-side execution for the demo configuration
and keeps the local test-control endpoint enabled. The EA `EnableExecution`
switch is still an independent gate. For observation-only startup, use:

```powershell
.\start_bridge.ps1 -ObserveOnly
```

The script refuses to start a second service while ports 8767 or 8768 are
already listening. Stopping the bridge process does not close existing MT5
positions or pending orders.
