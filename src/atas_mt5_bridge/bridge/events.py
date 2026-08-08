from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from atas_mt5_bridge.core.models import AtasPositionEvent


def parse_atas_position_event(data: Mapping[str, Any]) -> AtasPositionEvent:
    avg_price = data.get("avg_price")
    return AtasPositionEvent(
        seq=int(data["seq"]),
        source_symbol=str(data["source_symbol"]),
        target_net_contracts=Decimal(str(data["target_net_contracts"])),
        avg_price=Decimal(str(avg_price)) if avg_price not in (None, "") else None,
        timestamp=str(data["timestamp"]),
    )
