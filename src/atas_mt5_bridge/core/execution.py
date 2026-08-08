from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .mapping import SymbolMapping, map_target_lots
from .models import (
    AtasPositionEvent,
    BridgeMode,
    ExecutionPlan,
    MarketOrderIntent,
    Mt5NetPosition,
    OrderSide,
)
from .risk import RiskConfig, RiskViolation, validate_target
from .state import StateStore
from .volume import SymbolVolumeRules, normalize_volume


@dataclass
class ExecutionPlanner:
    mappings: Mapping[str, SymbolMapping]
    volume_rules: Mapping[str, SymbolVolumeRules]
    state: StateStore
    risk: RiskConfig

    def plan(self, event: AtasPositionEvent, actual: Mt5NetPosition) -> ExecutionPlan:
        mapping = self.mappings.get(event.source_symbol)
        if mapping is None:
            raise RiskViolation(f"unknown source symbol: {event.source_symbol}")

        target = map_target_lots(event, mapping)
        if not self.state.is_new_event(event.source_symbol, event.seq):
            return ExecutionPlan(target=target, actual=actual, orders=(), ignored_reason="duplicate_or_old_seq")

        previous_target = self.state.get_desired(target.symbol)
        if (
            previous_target is not None
            and previous_target.source_symbol == target.source_symbol
            and previous_target.net_lot == target.net_lot
            and actual.net_lot == target.net_lot
        ):
            self.state.record_target(target)
            return ExecutionPlan(target=target, actual=actual, orders=(), ignored_reason="unchanged_target")

        rules = self.volume_rules.get(target.symbol)
        if rules is None:
            raise RiskViolation(f"missing volume rules for symbol: {target.symbol}")

        validate_target(target, actual, mapping, self.risk)

        orders = tuple(self._build_orders(target.net_lot, actual, mapping, rules))
        self.state.record_target(target)
        return ExecutionPlan(
            target=target,
            actual=actual,
            orders=orders,
            dry_run=self.risk.mode == BridgeMode.DRY_RUN,
        )

    def _build_orders(
        self,
        target_net_lot: Decimal,
        actual: Mt5NetPosition,
        mapping: SymbolMapping,
        rules: SymbolVolumeRules,
    ) -> list[MarketOrderIntent]:
        orders: list[MarketOrderIntent] = []
        desired_long = max(target_net_lot, Decimal("0"))
        desired_short = abs(min(target_net_lot, Decimal("0")))

        if actual.long_lot > desired_long:
            self._append_split_orders(
                orders=orders,
                symbol=actual.symbol,
                side=OrderSide.SELL,
                volume=actual.long_lot - desired_long,
                mapping=mapping,
                rules=rules,
            )
        if actual.short_lot > desired_short:
            self._append_split_orders(
                orders=orders,
                symbol=actual.symbol,
                side=OrderSide.BUY,
                volume=actual.short_lot - desired_short,
                mapping=mapping,
                rules=rules,
            )
        if desired_long > actual.long_lot:
            self._append_split_orders(
                orders=orders,
                symbol=actual.symbol,
                side=OrderSide.BUY,
                volume=desired_long - actual.long_lot,
                mapping=mapping,
                rules=rules,
            )
        if desired_short > actual.short_lot:
            self._append_split_orders(
                orders=orders,
                symbol=actual.symbol,
                side=OrderSide.SELL,
                volume=desired_short - actual.short_lot,
                mapping=mapping,
                rules=rules,
            )

        return orders

    def _append_split_orders(
        self,
        orders: list[MarketOrderIntent],
        symbol: str,
        side: OrderSide,
        volume: Decimal,
        mapping: SymbolMapping,
        rules: SymbolVolumeRules,
    ) -> None:
        remaining = volume
        while remaining > 0:
            requested = min(remaining, mapping.max_order_lot)
            volume = normalize_volume(requested, rules)
            if volume is None:
                break
            orders.append(
                MarketOrderIntent(
                    symbol=symbol,
                    side=side,
                    volume=volume,
                    magic=self.risk.magic,
                    comment=f"{self.risk.order_comment_prefix}:target-net",
                )
            )
            remaining -= volume
