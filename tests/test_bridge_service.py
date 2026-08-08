from decimal import Decimal

from atas_mt5_bridge.bridge.service import BridgeService
from atas_mt5_bridge.core.mapping import SymbolMapping
from atas_mt5_bridge.core.models import AtasPositionEvent, BridgeMode, OrderSide
from atas_mt5_bridge.core.risk import RiskConfig
from atas_mt5_bridge.core.state import InMemoryStateStore
from atas_mt5_bridge.core.volume import SymbolVolumeRules
from atas_mt5_bridge.mt5.fake import FakeMt5Adapter


def make_service(mode: BridgeMode = BridgeMode.LIVE) -> tuple[BridgeService, FakeMt5Adapter]:
    adapter = FakeMt5Adapter(
        positions={"XAUUSD": Decimal("0")},
        rules={
            "XAUUSD": SymbolVolumeRules(
                symbol="XAUUSD",
                volume_min=Decimal("0.01"),
                volume_step=Decimal("0.01"),
                volume_max=Decimal("100"),
            )
        },
    )
    service = BridgeService(
        mappings={
            "COMEX Gold August 2026": SymbolMapping(
                source_symbol="COMEX Gold August 2026",
                target_symbol="XAUUSD",
                lot_ratio=Decimal("0.05"),
                max_abs_target_lot=Decimal("1.00"),
                max_order_lot=Decimal("0.20"),
            )
        },
        mt5=adapter,
        state=InMemoryStateStore(),
        risk=RiskConfig(mode=mode, magic=880501),
    )
    return service, adapter


def make_event(seq: int, contracts: str) -> AtasPositionEvent:
    return AtasPositionEvent(
        seq=seq,
        source_symbol="COMEX Gold August 2026",
        target_net_contracts=Decimal(contracts),
        avg_price=Decimal("2400"),
        timestamp="2026-06-03T10:00:00Z",
    )


def test_bridge_service_executes_market_delta_with_fake_adapter():
    service, adapter = make_service()

    result = service.handle_position_event(make_event(1, "1"))

    assert result.plan.target.net_lot == Decimal("0.05")
    assert len(result.executions) == 1
    assert result.executions[0].accepted is True
    assert adapter.orders[0].side == OrderSide.BUY
    assert adapter.orders[0].volume == Decimal("0.05")
    assert adapter.get_net_position("XAUUSD", 880501).net_lot == Decimal("0.05")


def test_bridge_service_duplicate_event_does_not_execute_again():
    service, adapter = make_service()

    first = service.handle_position_event(make_event(1, "1"))
    duplicate = service.handle_position_event(make_event(1, "2"))

    assert len(first.executions) == 1
    assert duplicate.plan.ignored_reason == "duplicate_or_old_seq"
    assert len(adapter.orders) == 1


def test_bridge_service_dry_run_plans_without_execution():
    service, adapter = make_service(BridgeMode.DRY_RUN)

    result = service.handle_position_event(make_event(1, "1"))

    assert result.plan.dry_run is True
    assert len(result.plan.orders) == 1
    assert result.executions == ()
    assert adapter.orders == []


def test_bridge_service_live_retry_cooldown_prevents_same_target_order_spam():
    service, adapter = make_service()

    first = service.handle_position_event(make_event(1, "1"))
    adapter.positions["XAUUSD"] = Decimal("0")
    repeated = service.handle_position_event(make_event(2, "1"))

    assert len(first.executions) == 1
    assert repeated.plan.ignored_reason == "retry_cooldown"
    assert repeated.plan.orders == ()
    assert len(adapter.orders) == 1
