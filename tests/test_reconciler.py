from decimal import Decimal

import pytest

from atas_mt5_bridge.core.execution import ExecutionPlanner
from atas_mt5_bridge.core.mapping import SymbolMapping
from atas_mt5_bridge.core.models import (
    AtasPositionEvent,
    BridgeMode,
    Mt5NetPosition,
    OrderSide,
)
from atas_mt5_bridge.core.risk import RiskConfig, RiskViolation
from atas_mt5_bridge.core.state import InMemoryStateStore
from atas_mt5_bridge.core.volume import SymbolVolumeRules


def make_mapping() -> SymbolMapping:
    return SymbolMapping(
        source_symbol="COMEX Gold August 2026",
        target_symbol="XAUUSD",
        lot_ratio=Decimal("0.05"),
        max_abs_target_lot=Decimal("0.50"),
        max_order_lot=Decimal("0.10"),
    )


def make_rules() -> SymbolVolumeRules:
    return SymbolVolumeRules(
        symbol="XAUUSD",
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("100"),
    )


def make_event(seq: int, contracts: str) -> AtasPositionEvent:
    return AtasPositionEvent(
        seq=seq,
        source_symbol="COMEX Gold August 2026",
        target_net_contracts=Decimal(contracts),
        avg_price=Decimal("2400"),
        timestamp="2026-06-03T10:00:00Z",
    )


def test_market_delta_plan_from_flat_to_long_target():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))

    plan = planner.plan(make_event(1, "1"), actual)

    assert plan.target.symbol == "XAUUSD"
    assert plan.target.net_lot == Decimal("0.05")
    assert len(plan.orders) == 1
    assert plan.orders[0].side == OrderSide.BUY
    assert plan.orders[0].volume == Decimal("0.05")


def test_market_delta_plan_splits_by_max_order_size():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))

    plan = planner.plan(make_event(2, "3"), actual)

    assert [order.volume for order in plan.orders] == [Decimal("0.10"), Decimal("0.05")]
    assert all(order.side == OrderSide.BUY for order in plan.orders)


def test_duplicate_or_old_event_is_idempotent():
    state = InMemoryStateStore()
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=state,
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))

    first = planner.plan(make_event(10, "1"), actual)
    duplicate = planner.plan(make_event(10, "2"), actual)
    old = planner.plan(make_event(9, "-1"), actual)

    assert len(first.orders) == 1
    assert duplicate.ignored_reason == "duplicate_or_old_seq"
    assert old.ignored_reason == "duplicate_or_old_seq"


def test_new_seq_with_same_target_is_ignored_when_actual_matches_target():
    state = InMemoryStateStore()
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=state,
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    flat = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))
    target_reached = Mt5NetPosition(
        symbol="XAUUSD",
        net_lot=Decimal("0.05"),
        long_lot=Decimal("0.05"),
        short_lot=Decimal("0"),
    )

    first = planner.plan(make_event(10, "1"), flat)
    same_target = planner.plan(make_event(11, "1"), target_reached)

    assert len(first.orders) == 1
    assert same_target.orders == ()
    assert same_target.ignored_reason == "unchanged_target"
    assert state.is_new_event("COMEX Gold August 2026", 11) is False


def test_new_seq_with_same_target_replans_when_actual_does_not_match_target():
    state = InMemoryStateStore()
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=state,
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))

    first = planner.plan(make_event(10, "1"), actual)
    same_target = planner.plan(make_event(11, "1"), actual)

    assert len(first.orders) == 1
    assert same_target.ignored_reason is None
    assert [order.volume for order in same_target.orders] == [Decimal("0.05")]


def test_kill_switch_rejects_new_plan():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.KILL_SWITCH),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))

    with pytest.raises(RiskViolation, match="kill switch"):
        planner.plan(make_event(1, "1"), actual)


def test_flatten_only_allows_risk_reducing_order():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.FLATTEN_ONLY),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0.05"), long_lot=Decimal("0.05"), short_lot=Decimal("0"))

    plan = planner.plan(make_event(1, "0"), actual)

    assert len(plan.orders) == 1
    assert plan.orders[0].side == OrderSide.SELL
    assert plan.orders[0].volume == Decimal("0.05")


def test_flatten_only_rejects_increasing_exposure():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.FLATTEN_ONLY),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))

    with pytest.raises(RiskViolation, match="flatten-only"):
        planner.plan(make_event(1, "1"), actual)


def test_hedged_same_magic_position_is_rejected_by_default():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0.05"), short_lot=Decimal("0.05"))

    with pytest.raises(RiskViolation, match="hedged"):
        planner.plan(make_event(1, "0"), actual)


def test_hedged_same_magic_position_can_be_repaired_when_allowed():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.LIVE, reject_hedged_same_magic=False),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0.05"), long_lot=Decimal("0.10"), short_lot=Decimal("0.05"))

    plan = planner.plan(make_event(1, "0"), actual)

    assert [(order.side, order.volume) for order in plan.orders] == [
        (OrderSide.SELL, Decimal("0.10")),
        (OrderSide.BUY, Decimal("0.05")),
    ]


def test_hedged_same_magic_position_repairs_gross_exposure_even_when_net_matches():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.LIVE, reject_hedged_same_magic=False),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0.05"), long_lot=Decimal("0.10"), short_lot=Decimal("0.05"))

    plan = planner.plan(make_event(1, "1"), actual)

    assert [(order.side, order.volume) for order in plan.orders] == [
        (OrderSide.SELL, Decimal("0.05")),
        (OrderSide.BUY, Decimal("0.05")),
    ]


def test_unknown_source_symbol_is_rejected():
    planner = ExecutionPlanner(
        mappings={"COMEX Gold August 2026": make_mapping()},
        volume_rules={"XAUUSD": make_rules()},
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=BridgeMode.LIVE),
    )
    actual = Mt5NetPosition(symbol="XAUUSD", net_lot=Decimal("0"), long_lot=Decimal("0"), short_lot=Decimal("0"))
    unknown = AtasPositionEvent(
        seq=1,
        source_symbol="UNKNOWN",
        target_net_contracts=Decimal("1"),
        avg_price=None,
        timestamp="2026-06-03T10:00:00Z",
    )

    with pytest.raises(RiskViolation, match="unknown source symbol"):
        planner.plan(unknown, actual)
