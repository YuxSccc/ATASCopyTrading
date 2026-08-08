from decimal import Decimal

from atas_mt5_bridge.core.mapping import SymbolMapping, map_target_lots
from atas_mt5_bridge.core.models import AtasPositionEvent
from atas_mt5_bridge.core.volume import SymbolVolumeRules, normalize_volume


def test_contracts_map_to_configured_ratio_for_gc_xauusd():
    event = AtasPositionEvent(
        seq=1,
        source_symbol="COMEX Gold August 2026",
        target_net_contracts=Decimal("1"),
        avg_price=Decimal("2400"),
        timestamp="2026-06-03T10:00:00Z",
    )
    mapping = SymbolMapping(
        source_symbol="COMEX Gold August 2026",
        target_symbol="XAUUSD",
        lot_ratio=Decimal("0.05"),
        max_abs_target_lot=Decimal("1.00"),
        max_order_lot=Decimal("0.20"),
    )

    target = map_target_lots(event, mapping)

    assert target.symbol == "XAUUSD"
    assert target.net_lot == Decimal("0.05")


def test_negative_contracts_map_to_negative_target_lots():
    event = AtasPositionEvent(
        seq=2,
        source_symbol="NQ June 2026",
        target_net_contracts=Decimal("-2"),
        avg_price=None,
        timestamp="2026-06-03T10:01:00Z",
    )
    mapping = SymbolMapping(
        source_symbol="NQ June 2026",
        target_symbol="USTEC",
        lot_ratio=Decimal("0.05"),
        max_abs_target_lot=Decimal("1.00"),
        max_order_lot=Decimal("0.20"),
    )

    assert map_target_lots(event, mapping).net_lot == Decimal("-0.10")


def test_volume_normalization_rounds_down_to_step():
    rules = SymbolVolumeRules(
        symbol="XAUUSD",
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("100"),
    )

    assert normalize_volume(Decimal("0.056"), rules) == Decimal("0.05")


def test_volume_below_min_is_rejected_instead_of_rounded_up():
    rules = SymbolVolumeRules(
        symbol="XAUUSD",
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("100"),
    )

    assert normalize_volume(Decimal("0.009"), rules) is None
