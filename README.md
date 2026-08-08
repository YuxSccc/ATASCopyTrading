# ATAS to MT5 Bridge

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
