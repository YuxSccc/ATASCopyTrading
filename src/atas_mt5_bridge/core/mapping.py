from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import AtasPositionEvent, TargetPosition


@dataclass(frozen=True)
class SymbolMapping:
    source_symbol: str
    target_symbol: str
    lot_ratio: Decimal
    max_abs_target_lot: Decimal
    max_order_lot: Decimal

    def __post_init__(self) -> None:
        if self.lot_ratio <= 0:
            raise ValueError("lot_ratio must be positive")
        if self.max_abs_target_lot <= 0:
            raise ValueError("max_abs_target_lot must be positive")
        if self.max_order_lot <= 0:
            raise ValueError("max_order_lot must be positive")


def map_target_lots(event: AtasPositionEvent, mapping: SymbolMapping) -> TargetPosition:
    return TargetPosition(
        source_symbol=event.source_symbol,
        symbol=mapping.target_symbol,
        net_lot=event.target_net_contracts * mapping.lot_ratio,
        source_seq=event.seq,
    )
