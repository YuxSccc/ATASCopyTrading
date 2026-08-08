from __future__ import annotations

from dataclasses import dataclass, field, replace
import time
from typing import Mapping, Protocol

from atas_mt5_bridge.core.execution import ExecutionPlanner
from atas_mt5_bridge.core.mapping import SymbolMapping
from atas_mt5_bridge.core.models import (
    AtasPositionEvent,
    ExecutionPlan,
    MarketOrderIntent,
    Mt5NetPosition,
)
from atas_mt5_bridge.core.risk import RiskConfig
from atas_mt5_bridge.core.risk import RiskViolation
from atas_mt5_bridge.core.state import StateStore
from atas_mt5_bridge.core.volume import SymbolVolumeRules


@dataclass(frozen=True)
class ExecutionResult:
    order: MarketOrderIntent
    accepted: bool
    message: str
    external_id: str | None = None


class Mt5Adapter(Protocol):
    def get_volume_rules(self, symbol: str) -> SymbolVolumeRules:
        raise NotImplementedError

    def get_net_position(self, symbol: str, magic: int) -> Mt5NetPosition:
        raise NotImplementedError

    def execute_market_order(self, order: MarketOrderIntent) -> ExecutionResult:
        raise NotImplementedError


@dataclass(frozen=True)
class BridgeResult:
    plan: ExecutionPlan
    executions: tuple[ExecutionResult, ...]


@dataclass
class BridgeService:
    mappings: Mapping[str, SymbolMapping]
    mt5: Mt5Adapter
    state: StateStore
    risk: RiskConfig
    retry_cooldown_seconds: float = 5.0
    _last_order_attempt_by_target: dict[tuple[str, str], float] = field(default_factory=dict)

    def handle_position_event(self, event: AtasPositionEvent) -> BridgeResult:
        mapping = self.mappings.get(event.source_symbol)
        if mapping is None:
            raise RiskViolation(f"unknown source symbol: {event.source_symbol}")

        target_symbol = mapping.target_symbol if mapping else event.source_symbol
        rules = self.mt5.get_volume_rules(target_symbol)
        actual = self.mt5.get_net_position(target_symbol, self.risk.magic)

        planner = ExecutionPlanner(
            mappings=self.mappings,
            volume_rules={target_symbol: rules},
            state=self.state,
            risk=self.risk,
        )
        plan = planner.plan(event, actual)
        if plan.dry_run or plan.ignored_reason:
            return BridgeResult(plan=plan, executions=())

        if plan.orders and self._is_retry_in_cooldown(plan):
            return BridgeResult(
                plan=replace(plan, orders=(), ignored_reason="retry_cooldown"),
                executions=(),
            )

        self._record_order_attempt(plan)
        executions = tuple(self.mt5.execute_market_order(order) for order in plan.orders)
        return BridgeResult(plan=plan, executions=executions)

    def _is_retry_in_cooldown(self, plan: ExecutionPlan) -> bool:
        if self.retry_cooldown_seconds <= 0:
            return False

        key = (plan.target.symbol, str(plan.target.net_lot))
        last_attempt = self._last_order_attempt_by_target.get(key)
        return last_attempt is not None and time.monotonic() - last_attempt < self.retry_cooldown_seconds

    def _record_order_attempt(self, plan: ExecutionPlan) -> None:
        key = (plan.target.symbol, str(plan.target.net_lot))
        self._last_order_attempt_by_target[key] = time.monotonic()
