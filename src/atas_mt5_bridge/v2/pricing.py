from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from .models import SourceOrder, SourceQuote, TargetAccountState, TargetQuote, TargetSymbolSpec


@dataclass(frozen=True)
class BasisSample:
    occurred_at_utc: datetime
    buy_basis: Decimal
    sell_basis: Decimal


@dataclass(frozen=True)
class PriceDecision:
    allowed: bool
    reason: str | None = None
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    basis: Decimal | None = None


@dataclass
class SideBasisEstimator:
    """Recent-weighted GC/XAUUSD side-basis estimator sampled once per minute."""

    window: timedelta = timedelta(hours=4)
    half_life: timedelta = timedelta(minutes=15)
    minimum_sample_interval: timedelta = timedelta(minutes=1)
    samples: list[BasisSample] = field(default_factory=list)

    def record(self, source: SourceQuote, target: TargetQuote) -> None:
        if not source.is_complete or not target.is_complete:
            return
        occurred_at = min(_parse_time(source.occurred_at_utc), _parse_time(target.occurred_at_utc))
        if self.samples and occurred_at - self.samples[-1].occurred_at_utc < self.minimum_sample_interval:
            return
        self.samples.append(
            BasisSample(
                occurred_at_utc=occurred_at,
                buy_basis=target.ask - source.ask,  # type: ignore[operator]
                sell_basis=target.bid - source.bid,  # type: ignore[operator]
            )
        )
        self.prune(occurred_at)

    def prune(self, now: datetime) -> None:
        lower_bound = now - self.window
        self.samples = [sample for sample in self.samples if sample.occurred_at_utc >= lower_bound]

    def estimate(self, side: str, now: datetime) -> Decimal | None:
        values = self._weighted_values(side, now)
        return _weighted_median(values) if values else None

    def dispersion(self, side: str, now: datetime) -> Decimal | None:
        values = self._weighted_values(side, now)
        median = _weighted_median(values)
        if median is None:
            return None
        deviations = [(abs(value - median), weight) for value, weight in values]
        return _weighted_median(deviations)

    def current_basis(self, side: str, source: SourceQuote, target: TargetQuote) -> Decimal | None:
        if not source.is_complete or not target.is_complete:
            return None
        return target.ask - source.ask if side.lower() == "buy" else target.bid - source.bid  # type: ignore[operator]

    def _weighted_values(self, side: str, now: datetime) -> list[tuple[Decimal, Decimal]]:
        self.prune(now)
        half_life_seconds = Decimal(str(self.half_life.total_seconds()))
        if half_life_seconds <= 0:
            raise ValueError("half_life must be positive")
        values: list[tuple[Decimal, Decimal]] = []
        for sample in self.samples:
            age_seconds = max(Decimal("0"), Decimal(str((now - sample.occurred_at_utc).total_seconds())))
            # 2 ** (-age / half-life) without float conversion in the stored price.
            weight = Decimal(str(2 ** float(-(age_seconds / half_life_seconds))))
            value = sample.buy_basis if side.lower() == "buy" else sample.sell_basis
            values.append((value, weight))
        return values


@dataclass(frozen=True)
class PriceGuardConfig:
    quote_max_age: timedelta = timedelta(milliseconds=250)
    # GC has a regular one-hour maintenance halt and may occasionally have a
    # zero-trade minute in thin periods. 170 matched M1 closes in a four-hour
    # window preserves nearly three hours of observed calibration without
    # treating normal session gaps as a pricing fault.
    minimum_basis_samples: int = 170
    max_current_basis_deviation: Decimal = Decimal("2.00")
    max_basis_dispersion: Decimal = Decimal("1.00")
    basis_window: timedelta = timedelta(hours=4)
    basis_half_life: timedelta = timedelta(minutes=15)
    minimum_sample_interval: timedelta = timedelta(minutes=1)


@dataclass
class PriceMapper:
    """Maps a source pending order only when both market-data sides are safe."""

    estimator: SideBasisEstimator = field(default_factory=SideBasisEstimator)
    config: PriceGuardConfig = field(default_factory=PriceGuardConfig)

    def record_pair(self, source: SourceQuote | None, target: TargetQuote | None) -> None:
        if source is not None and target is not None:
            self.estimator.record(source, target)

    def calibrate_from_minute_history(
        self,
        source_bars: Iterable[Mapping[str, Any]],
        target_bars: Iterable[Mapping[str, Any]],
    ) -> int:
        """Replace the rolling calibration from matching closed M1 bars.

        The bridge receives raw bars from each terminal only in memory; it
        retains the resulting compact basis samples, not OHLC history. Exact
        UTC minute keys prevent a chart-display timezone from changing a
        price mapping.
        """
        source_by_minute = _minute_closes(source_bars)
        target_by_minute = _minute_closes(target_bars)
        matched_minutes = sorted(set(source_by_minute).intersection(target_by_minute))
        samples = [
            BasisSample(
                occurred_at_utc=minute,
                buy_basis=target_by_minute[minute] - source_by_minute[minute],
                sell_basis=target_by_minute[minute] - source_by_minute[minute],
            )
            for minute in matched_minutes
        ]
        self.estimator.samples = samples[-int(self.estimator.window.total_seconds() // 60) :]
        return len(self.estimator.samples)

    def to_data(self) -> dict:
        """Export no historical calibration data.

        The basis is deliberately rebuilt from the latest closed M1 windows
        after every service start.  Persisting old GC/XAUUSD samples would
        make a stale session look ready before the current feeds were
        observed.  The empty shape is retained only for compatibility with an
        existing database schema.
        """
        return {
            "window_seconds": self.estimator.window.total_seconds(),
            "half_life_seconds": self.estimator.half_life.total_seconds(),
            "minimum_sample_interval_seconds": self.estimator.minimum_sample_interval.total_seconds(),
            "samples": [],
        }

    def diagnostics(
        self,
        source_quote: SourceQuote | None,
        target_quote: TargetQuote | None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return read-only calibration state for the local status endpoint."""
        now = now or datetime.now(timezone.utc)
        buy_basis = self.estimator.current_basis("buy", source_quote, target_quote) if source_quote and target_quote else None
        sell_basis = self.estimator.current_basis("sell", source_quote, target_quote) if source_quote and target_quote else None
        return {
            "sample_count": len(self.estimator.samples),
            "minimum_samples": self.config.minimum_basis_samples,
            "window_seconds": int(self.estimator.window.total_seconds()),
            "half_life_seconds": int(self.estimator.half_life.total_seconds()),
            "quote_max_age_ms": int(self.config.quote_max_age.total_seconds() * 1000),
            "max_current_basis_deviation": str(self.config.max_current_basis_deviation),
            "max_basis_dispersion": str(self.config.max_basis_dispersion),
            "last_sample_at_utc": self.estimator.samples[-1].occurred_at_utc.isoformat() if self.estimator.samples else None,
            "current_buy_basis": str(buy_basis) if buy_basis is not None else None,
            "current_sell_basis": str(sell_basis) if sell_basis is not None else None,
            "estimated_buy_basis": _decimal_text(self.estimator.estimate("buy", now)),
            "estimated_sell_basis": _decimal_text(self.estimator.estimate("sell", now)),
            "buy_basis_dispersion": _decimal_text(self.estimator.dispersion("buy", now)),
            "sell_basis_dispersion": _decimal_text(self.estimator.dispersion("sell", now)),
        }

    @classmethod
    def from_data(cls, data: dict | None, config: PriceGuardConfig | None = None) -> "PriceMapper":
        config = config or PriceGuardConfig()
        if not data:
            return cls(
                estimator=SideBasisEstimator(
                    window=config.basis_window,
                    half_life=config.basis_half_life,
                    minimum_sample_interval=config.minimum_sample_interval,
                ),
                config=config,
            )
        estimator = SideBasisEstimator(
            window=config.basis_window,
            half_life=config.basis_half_life,
            minimum_sample_interval=config.minimum_sample_interval,
            # Do not restore old persisted calibration.  Fresh M1 windows
            # must establish the basis for this bridge process.
            samples=[],
        )
        estimator.prune(datetime.now(timezone.utc))
        return cls(estimator=estimator, config=config)

    def map_pending(
        self,
        order: SourceOrder,
        source_quote: SourceQuote | None,
        target: TargetAccountState,
        target_quantity: Decimal | None = None,
        now: datetime | None = None,
    ) -> PriceDecision:
        now = now or datetime.now(timezone.utc)
        target_quote = target.quote
        spec = target.symbol_spec
        if source_quote is None or target_quote is None or spec is None:
            return PriceDecision(False, "price_guard_missing_quote_or_symbol_spec")
        if not source_quote.is_complete or not target_quote.is_complete:
            return PriceDecision(False, "price_guard_incomplete_quote")
        if not _is_fresh(source_quote.occurred_at_utc, now, self.config.quote_max_age) or not _is_fresh(
            target_quote.occurred_at_utc, now, self.config.quote_max_age
        ):
            return PriceDecision(False, "price_guard_stale_quote")
        self.record_pair(source_quote, target_quote)
        if len(self.estimator.samples) < self.config.minimum_basis_samples:
            return PriceDecision(False, "price_guard_insufficient_basis_samples")

        side = order.side.lower()
        current_basis = self.estimator.current_basis(side, source_quote, target_quote)
        estimated_basis = self.estimator.estimate(side, now)
        dispersion = self.estimator.dispersion(side, now)
        if current_basis is None or estimated_basis is None or dispersion is None:
            return PriceDecision(False, "price_guard_basis_unavailable")
        if abs(current_basis - estimated_basis) > self.config.max_current_basis_deviation:
            return PriceDecision(False, "price_guard_basis_shock")
        if dispersion > self.config.max_basis_dispersion:
            return PriceDecision(False, "price_guard_basis_dispersion")
        volume = target_quantity if target_quantity is not None else order.unfilled
        if not _valid_volume(volume, spec):
            return PriceDecision(False, "price_guard_invalid_target_volume")

        mapped_price = _map_price(_pending_open_price(order), estimated_basis, spec.tick_size)
        if not _has_pending_semantics(order, mapped_price, None, target_quote, spec):
            return PriceDecision(False, "price_guard_invalid_pending_semantics")
        return PriceDecision(True, price=mapped_price, trigger_price=None, basis=estimated_basis)

    def map_protection(
        self,
        order: SourceOrder,
        source_quote: SourceQuote | None,
        target: TargetAccountState,
        now: datetime | None = None,
    ) -> PriceDecision:
        """Map a native MT5 SL or TP price without pending-order semantics."""

        now = now or datetime.now(timezone.utc)
        target_quote = target.quote
        spec = target.symbol_spec
        if source_quote is None or target_quote is None or spec is None:
            return PriceDecision(False, "price_guard_missing_quote_or_symbol_spec")
        if not source_quote.is_complete or not target_quote.is_complete:
            return PriceDecision(False, "price_guard_incomplete_quote")
        if not _is_fresh(source_quote.occurred_at_utc, now, self.config.quote_max_age) or not _is_fresh(
            target_quote.occurred_at_utc, now, self.config.quote_max_age
        ):
            return PriceDecision(False, "price_guard_stale_quote")
        self.record_pair(source_quote, target_quote)
        if len(self.estimator.samples) < self.config.minimum_basis_samples:
            return PriceDecision(False, "price_guard_insufficient_basis_samples")
        side = order.side.lower()
        current_basis = self.estimator.current_basis(side, source_quote, target_quote)
        estimated_basis = self.estimator.estimate(side, now)
        dispersion = self.estimator.dispersion(side, now)
        if current_basis is None or estimated_basis is None or dispersion is None:
            return PriceDecision(False, "price_guard_basis_unavailable")
        if abs(current_basis - estimated_basis) > self.config.max_current_basis_deviation:
            return PriceDecision(False, "price_guard_basis_shock")
        if dispersion > self.config.max_basis_dispersion:
            return PriceDecision(False, "price_guard_basis_dispersion")
        source_price = order.trigger_price if "stop" in order.order_type.lower() else order.price
        mapped_price = _map_price(source_price, estimated_basis, spec.tick_size)
        if mapped_price is None:
            return PriceDecision(False, "price_guard_missing_protection_price")
        return PriceDecision(True, price=mapped_price, basis=estimated_basis)


def _weighted_median(values: Iterable[tuple[Decimal, Decimal]]) -> Decimal | None:
    ordered = sorted(values, key=lambda item: item[0])
    if not ordered:
        return None
    total = sum((weight for _, weight in ordered), start=Decimal("0"))
    threshold = total / Decimal("2")
    running = Decimal("0")
    for value, weight in ordered:
        running += weight
        if running >= threshold:
            return value
    return ordered[-1][0]


def _map_price(value: Decimal | None, basis: Decimal, tick_size: Decimal) -> Decimal | None:
    if value is None:
        return None
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    return ((value + basis) / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick_size


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _minute_closes(bars: Iterable[Mapping[str, Any]]) -> dict[datetime, Decimal]:
    result: dict[datetime, Decimal] = {}
    for bar in bars:
        raw_time = bar.get("time_utc")
        raw_close = bar.get("close")
        if raw_time in (None, "") or raw_close in (None, ""):
            continue
        try:
            occurred_at = _parse_time(str(raw_time)).replace(second=0, microsecond=0)
            result[occurred_at] = Decimal(str(raw_close))
        except (InvalidOperation, ValueError):
            continue
    return result


def _has_pending_semantics(
    order: SourceOrder,
    price: Decimal | None,
    trigger_price: Decimal | None,
    quote: TargetQuote,
    spec: TargetSymbolSpec,
) -> bool:
    effective_price = trigger_price if trigger_price is not None else price
    if effective_price is None or quote.bid is None or quote.ask is None:
        return False
    minimum_distance = max(spec.stops_level, spec.freeze_level)
    side = order.side.lower()
    order_type = order.order_type.lower()
    if side == "buy" and "limit" in order_type:
        return effective_price <= quote.ask - minimum_distance
    if side == "buy" and "stop" in order_type:
        return effective_price >= quote.ask + minimum_distance
    if side == "sell" and "limit" in order_type:
        return effective_price >= quote.bid + minimum_distance
    if side == "sell" and "stop" in order_type:
        return effective_price <= quote.bid - minimum_distance
    return False


def _pending_open_price(order: SourceOrder) -> Decimal | None:
    """Normalize ATAS Limit/Stop into MT5's single pending open price field."""

    return order.trigger_price if "stop" in order.order_type.lower() else order.price


def _valid_volume(volume: Decimal, spec: TargetSymbolSpec) -> bool:
    if volume < spec.volume_min or volume > spec.volume_max or spec.volume_step <= 0:
        return False
    steps = (volume - spec.volume_min) / spec.volume_step
    return steps == steps.to_integral_value()


def _is_fresh(timestamp: str, now: datetime, maximum_age: timedelta) -> bool:
    age = now - _parse_time(timestamp)
    return timedelta(0) <= age <= maximum_age


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
