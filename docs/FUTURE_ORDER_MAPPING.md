# Future Pending Order and SL/TP Mapping

## Principle

Future versions should not copy ATAS orders one by one. They should calculate a target MT5 state from ATAS intent, current MT5 state, and sampled basis, then choose the lowest-risk and lowest-cost transition.

```text
ATAS current state + ATAS working orders
  -> normalized desired state
  -> mapped MT5 desired state
  -> compare with MT5 actual positions/orders
  -> minimal transition plan
```

## Why limit and TP share logic

When a position already exists, an opposite-side limit order can be equivalent to a take-profit target.

Example:

```text
ATAS is long +1 GC
ATAS has sell limit above entry
```

MT5 target can be represented as either:

- a SELL LIMIT pending order, or
- a TP attached to the current long position.

The better representation depends on MT5 account mode, broker rules, current position tickets, existing pending orders, and whether partial reductions are needed.

## Why stop and SL share logic

When a position already exists, an opposite-side stop order can be equivalent to a stop-loss target.

Example:

```text
ATAS is long +1 GC
ATAS has sell stop below entry
```

MT5 target can be represented as either:

- a SELL STOP pending order, or
- an SL attached to the current long position.

Again, the final representation depends on current MT5 state.

## Basis sampling

GC and XAUUSD, or NQ and USTEC, are related but not identical. Absolute prices should not be copied blindly.

For a future mapped limit/stop:

```text
basis = target_mid - source_mid
basis_ema = EMA(basis)
mapped_price = source_order_price + basis_ema
```

For SL/TP around an actual MT5 fill, relative distance is usually safer:

```text
delta_stop = source_stop_price - source_entry_price
mapped_stop = actual_mt5_entry + delta_stop

delta_take_profit = source_tp_price - source_entry_price
mapped_take_profit = actual_mt5_entry + delta_take_profit
```

For intraday trading, the basis error may be acceptable if it is sampled frequently and the system records when sampling is stale.

## Desired state model

Future desired state should include:

```text
desired_position:
  symbol
  net_lot
  side
  entry_reference

desired_exits:
  reduce_only
  trigger_type: limit | stop
  source_price
  mapped_price
  target_reduce_lot

desired_entries:
  trigger_type: limit | stop | market
  source_price
  mapped_price
  target_lot
```

## Transition optimizer

The optimizer should minimize unnecessary commission, spread crossing, and cancel/replace churn.

Inputs:

- desired MT5 target state
- actual MT5 positions for bridge Magic Number
- actual MT5 pending orders for bridge Magic Number
- broker volume and price constraints
- current bid/ask
- account mode: hedging or netting

Candidate actions:

- no-op
- modify existing SL/TP
- modify pending order price/volume
- cancel stale pending order
- place new pending order
- market flatten residual
- market delta order

Cost model:

```text
cost = commission_estimate
     + spread_crossing_penalty
     + cancel_replace_penalty
     + residual_risk_penalty
     + broker_rejection_risk_penalty
```

The selected transition is the lowest-cost valid action sequence that reaches the target state.

## Safety rules

- Do not map unknown symbols.
- Do not use stale basis samples.
- Do not raise a too-small volume to broker minimum.
- Do not silently merge with non-bridge Magic Number positions.
- Do not attach SL/TP to positions not created or adopted by the bridge.
- Do not represent reduce-only exits as new exposure unless explicitly allowed.
- If hedged residual state exists under the bridge Magic Number, prefer flatten/rebuild or alert-only mode.

## Recommended implementation order

1. Market net-position bridge.
2. MT5 actual position and pending order snapshot model.
3. Basis sampler with freshness checks.
4. Desired state model for reduce-only exits.
5. SL/TP attachment planner for simple one-position cases.
6. Pending order planner for entry orders.
7. Optimizer for modify/cancel/place/market actions.
