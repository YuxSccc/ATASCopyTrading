from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from atas_mt5_bridge.v2.models import (
    SourceOrder,
    SourceOrderState,
    SourceQuote,
    TargetAccountModel,
    TargetAccountState,
    TargetQuote,
    TargetSymbolSpec,
)
from atas_mt5_bridge.v2.pricing import PriceGuardConfig, PriceMapper, SideBasisEstimator


NOW = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)


def quote(source_bid: str = "3999", source_ask: str = "4000", target_bid: str = "4009", target_ask: str = "4010"):
    source = SourceQuote("GC", Decimal(source_bid), Decimal(source_ask), NOW.isoformat())
    target = TargetQuote("XAUUSD", Decimal(target_bid), Decimal(target_ask), NOW.isoformat())
    return source, target


def account(target_quote: TargetQuote) -> TargetAccountState:
    return TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal("0"),
        short_quantity=Decimal("0"),
        quote=target_quote,
        symbol_spec=TargetSymbolSpec(
            tick_size=Decimal("0.1"),
            volume_min=Decimal("1"),
            volume_max=Decimal("100"),
            volume_step=Decimal("1"),
            stops_level=Decimal("0.1"),
            freeze_level=Decimal("0.1"),
        ),
    )


def buy_limit() -> SourceOrder:
    return SourceOrder(
        source_order_id="price-order",
        source_external_id=None,
        source_symbol="GC",
        side="buy",
        order_type="limit",
        state=SourceOrderState.ACTIVE,
        quantity=Decimal("1"),
        unfilled=Decimal("1"),
        price=Decimal("3985"),
        trigger_price=None,
        cancelled=False,
    )


def test_side_basis_maps_pending_price_using_recent_weighted_history():
    estimator = SideBasisEstimator(minimum_sample_interval=timedelta(0))
    mapper = PriceMapper(
        estimator=estimator,
        config=PriceGuardConfig(minimum_basis_samples=3, max_current_basis_deviation=Decimal("0.5")),
    )
    source, target = quote()
    for _ in range(3):
        mapper.record_pair(source, target)

    decision = mapper.map_pending(buy_limit(), source, account(target), now=NOW)

    assert decision.allowed is True
    assert decision.basis == Decimal("10")
    assert decision.price == Decimal("3995.0")


def test_stop_order_uses_atas_trigger_price_as_the_mt5_pending_open_price():
    mapper = PriceMapper(
        estimator=SideBasisEstimator(minimum_sample_interval=timedelta(0)),
        config=PriceGuardConfig(minimum_basis_samples=1),
    )
    source, target = quote()
    mapper.record_pair(source, target)
    stop = SourceOrder(
        source_order_id="sl-order",
        source_external_id=None,
        source_symbol="GC",
        side="sell",
        order_type="stop",
        state=SourceOrderState.ACTIVE,
        quantity=Decimal("1"),
        unfilled=Decimal("1"),
        price=Decimal("0"),
        trigger_price=Decimal("3991.2"),
        cancelled=False,
        comment="SL",
    )

    decision = mapper.map_pending(stop, source, account(target), now=NOW)

    assert decision.allowed is True
    assert decision.price == Decimal("4001.2")
    assert decision.trigger_price is None


def test_basis_shock_blocks_new_target_order_but_does_not_silently_fallback_to_raw_price():
    mapper = PriceMapper(
        estimator=SideBasisEstimator(minimum_sample_interval=timedelta(0)),
        config=PriceGuardConfig(minimum_basis_samples=3, max_current_basis_deviation=Decimal("0.5")),
    )
    source, baseline_target = quote()
    for _ in range(3):
        mapper.record_pair(source, baseline_target)
    _, shocked_target = quote(target_bid="4029", target_ask="4030")

    decision = mapper.map_pending(buy_limit(), source, account(shocked_target), now=NOW)

    assert decision.allowed is False
    assert decision.reason == "price_guard_basis_shock"
    assert decision.price is None


def test_stale_quote_blocks_mapping():
    mapper = PriceMapper(config=PriceGuardConfig(minimum_basis_samples=1))
    stale = (NOW - timedelta(milliseconds=251)).isoformat()
    source = SourceQuote("GC", Decimal("3999"), Decimal("4000"), stale)
    target = TargetQuote("XAUUSD", Decimal("4009"), Decimal("4010"), stale)

    decision = mapper.map_pending(buy_limit(), source, account(target), now=NOW)

    assert decision.allowed is False
    assert decision.reason == "price_guard_stale_quote"


def test_basis_history_round_trips_for_database_restart_recovery():
    mapper = PriceMapper(config=PriceGuardConfig(minimum_basis_samples=1))
    source, target = quote()
    mapper.record_pair(source, target)

    restored = PriceMapper.from_data(mapper.to_data(), config=mapper.config)

    assert restored.estimator.estimate("buy", NOW) == Decimal("10")
    assert restored.estimator.estimate("sell", NOW) == Decimal("10")


def test_price_guard_validates_the_volume_after_the_configured_mapping_multiplier():
    mapper = PriceMapper(config=PriceGuardConfig(minimum_basis_samples=1))
    source, target = quote()
    mapper.record_pair(source, target)

    decision = mapper.map_pending(
        buy_limit(),
        source,
        account(target),
        target_quantity=Decimal("0.5"),
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.reason == "price_guard_invalid_target_volume"
