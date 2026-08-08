# Architecture

## Goal

Use ATAS as the chart, order-flow, and manual trading surface. The bridge maps ATAS simulated/chart-trading intent into MT5 execution.

v1 deliberately synchronizes target net position, not individual fills.

```text
ATAS target net position
  -> Local Bridge desired target
  -> read MT5 actual bridge position
  -> delta market order plan
  -> MT5 execution
```

## Why target net position first

ATAS simulated fills and MT5 broker fills are not the same execution model. Copying each order or fill is fragile. For v1 the safer invariant is:

```text
desired MT5 net lot = mapped ATAS net contracts
delta = desired MT5 net lot - actual MT5 bridge net lot
```

If `delta > 0`, buy `delta`.
If `delta < 0`, sell `abs(delta)`.

## ATAS model

ATAS futures positions are treated as signed net positions:

```text
+1 = long one source contract
 0 = flat
-1 = short one source contract
```

The bridge should not model ATAS as simultaneous long and short positions.

## MT5 model

MT5 may be netting or hedging. v1 reads long, short, net, and gross exposure for the bridge magic number.

Default safety rule:

```text
same magic long > 0 and same magic short > 0 => reject as hedged residual state
```

The bridge should also reject or alert on same-symbol external positions unless explicitly configured otherwise.

## Symbol and lot mapping

Configured example:

```json
{
  "COMEX Gold August 2026": {
    "target_symbol": "XAUUSD",
    "lot_ratio": "0.05"
  },
  "E-mini NASDAQ-100 June 2026": {
    "target_symbol": "USTEC",
    "lot_ratio": "0.05"
  }
}
```

This means:

```text
ATAS +1 GC contract -> MT5 +0.05 XAUUSD lot
ATAS -1 GC contract -> MT5 -0.05 XAUUSD lot
ATAS +1 NQ contract -> MT5 +0.05 USTEC lot
```

The current code uses direct `lot_ratio` because that matches your configuration preference. A later config can derive it from:

```text
atas_contract_oz / mt5_contract_size_oz * risk_scale
```

## Risk controls

Required controls:

- `dry_run`
- `kill_switch`
- `flatten_only`
- unknown symbol rejection
- illegal volume rejection
- max absolute target lot
- max single order lot
- same-magic hedged residual rejection
- Magic Number and comment tagging

v1 implements the pure planning logic for these controls. Real MT5 order sending remains an adapter task.

## Current code

The tested core is under `src/atas_mt5_bridge/core`.

- `models.py`: event, position, order, and plan data types.
- `mapping.py`: ATAS contracts to MT5 target lot.
- `volume.py`: broker volume normalization.
- `risk.py`: bridge-mode and safety validation.
- `state.py`: seq and desired-target state.
- `execution.py`: market delta order planner.

The bridge layer is under `src/atas_mt5_bridge/bridge`.

- `config.py`: loads JSON config into mappings and risk settings.
- `events.py`: parses ATAS position event payloads.
- `service.py`: coordinates event handling, MT5 actual state, planning, and execution.

The MT5 adapter layer is under `src/atas_mt5_bridge/mt5`.

- `fake.py`: test/offline adapter.
- `metatrader5_adapter.py`: injectable adapter around the Python `MetaTrader5` API.

Offline planning command:

```bash
PYTHONPATH=src python -m atas_mt5_bridge.cli \
  --config config.example.json \
  --event examples/atas_position_event.example.json \
  --actual-net 0
```

## Integration plan

ATAS side:

- C# `ChartStrategy` skeleton exists in `atas_strategy/`.
- It subscribes to `OnPositionChanged(Position position)`.
- It emits `AtasPositionEvent` with monotonic `seq`.
- It does not place MT5 orders from inside ATAS.
- Build/install instructions are in `docs/ATAS_STRATEGY.md`.

Bridge side:

- Dry-run WebSocket server exists in `atas_mt5_bridge.bridge.server`.
- Validate event schema and symbol mapping.
- Persist last accepted `seq` and desired target.
- Read MT5 actual bridge net position.
- Build and execute market delta plans.
- Current server uses a fake MT5 adapter for event capture and dry-run planning.

MT5 side:

- Start with Python `MetaTrader5` adapter.
- Query `symbol_info()` for volume constraints and trade availability.
- Query positions by symbol and magic.
- Send market orders with configured Magic Number and comment.

Current adapter behavior:

- `get_volume_rules(symbol)` reads `volume_min`, `volume_step`, and `volume_max`.
- `get_net_position(symbol, magic)` filters positions by Magic Number and returns long, short, and net lots.
- `execute_market_order(order)` sends `TRADE_ACTION_DEAL` with Magic Number and comment.

Live MT5 execution is intentionally not wired into the WebSocket server yet. This avoids accidentally sending real orders while ATAS event capture is still being validated.

## Integration tests that require you

These need the real ATAS and MT5 environment:

- Confirm ATAS strategy appears under chart strategies.
- Confirm ATAS simulated account manual market orders trigger `OnPositionChanged`.
- Record actual `Security.ToString()` values for GC and NQ charts.
- Confirm `position.Volume` is signed net contracts.
- Confirm `position.AveragePrice` behavior after add/reduce/reverse.
- Confirm MT5 symbol names such as `XAUUSD`, `XAUUSD.cn`, `USTEC`, or broker-specific alternatives.
- Confirm MT5 `volume_min`, `volume_step`, `volume_max`, and trade mode.
- Confirm broker account type: hedging or netting.
- Confirm Python `MetaTrader5.order_send()` is stable enough for your terminal.
