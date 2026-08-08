from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from atas_mt5_bridge.bridge.service import ExecutionResult
from atas_mt5_bridge.core.models import MarketOrderIntent, Mt5NetPosition, OrderSide
from atas_mt5_bridge.core.volume import SymbolVolumeRules


@dataclass
class FakeMt5Adapter:
    positions: dict[str, Decimal]
    rules: Mapping[str, SymbolVolumeRules]
    orders: list[MarketOrderIntent] = field(default_factory=list)
    next_order_id: int = 1

    def get_volume_rules(self, symbol: str) -> SymbolVolumeRules:
        return self.rules[symbol]

    def get_net_position(self, symbol: str, magic: int) -> Mt5NetPosition:
        net = self.positions.get(symbol, Decimal("0"))
        return Mt5NetPosition(
            symbol=symbol,
            net_lot=net,
            long_lot=max(net, Decimal("0")),
            short_lot=abs(min(net, Decimal("0"))),
        )

    def execute_market_order(self, order: MarketOrderIntent) -> ExecutionResult:
        self.orders.append(order)
        signed = order.volume if order.side == OrderSide.BUY else -order.volume
        self.positions[order.symbol] = self.positions.get(order.symbol, Decimal("0")) + signed
        external_id = f"fake-{self.next_order_id}"
        self.next_order_id += 1
        return ExecutionResult(
            order=order,
            accepted=True,
            message="fake execution accepted",
            external_id=external_id,
        )
