# ATAS to MT5 Bridge

This repository contains a read-only ATAS integration probe and a safety-first
ATAS-to-MT5 bridge core. It contains no live MT5 execution adapter and cannot
send an order to MT5. A local simulator exercises the intended order/position
state machine without contacting a trading terminal.

The probe captures ATAS order, position, and quote callbacks over localhost
WebSocket. The v2 framework provides ordered source-state handling, per-symbol
coordination, explicit execution outcomes, an `intent -> 1..N target legs`
mapping model, and a safe local simulator for either MT5 netting or hedging
state modelling.

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
ATAS probe.
