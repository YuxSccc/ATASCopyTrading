from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class BridgeMode(str, Enum):
    LIVE = "live"
    DRY_RUN = "dry_run"
    KILL_SWITCH = "kill_switch"
    FLATTEN_ONLY = "flatten_only"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class AtasPositionEvent:
    seq: int
    source_symbol: str
    target_net_contracts: Decimal
    avg_price: Optional[Decimal]
    timestamp: str


@dataclass(frozen=True)
class TargetPosition:
    source_symbol: str
    symbol: str
    net_lot: Decimal
    source_seq: int


@dataclass(frozen=True)
class Mt5NetPosition:
    symbol: str
    net_lot: Decimal
    long_lot: Decimal
    short_lot: Decimal

    @property
    def gross_lot(self) -> Decimal:
        return abs(self.long_lot) + abs(self.short_lot)

    @property
    def is_hedged(self) -> bool:
        return self.long_lot > 0 and self.short_lot > 0


@dataclass(frozen=True)
class MarketOrderIntent:
    symbol: str
    side: OrderSide
    volume: Decimal
    magic: int
    comment: str


@dataclass(frozen=True)
class ExecutionPlan:
    target: TargetPosition
    actual: Mt5NetPosition
    orders: tuple[MarketOrderIntent, ...]
    dry_run: bool = False
    ignored_reason: Optional[str] = None
