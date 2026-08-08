from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .mapping import SymbolMapping
from .models import BridgeMode, Mt5NetPosition, TargetPosition


class RiskViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskConfig:
    mode: BridgeMode = BridgeMode.DRY_RUN
    reject_hedged_same_magic: bool = True
    allow_external_positions: bool = False
    magic: int = 880501
    order_comment_prefix: str = "ATAS-MT5"


def validate_target(
    target: TargetPosition,
    actual: Mt5NetPosition,
    mapping: SymbolMapping,
    risk: RiskConfig,
) -> None:
    if risk.mode == BridgeMode.KILL_SWITCH:
        raise RiskViolation("kill switch is enabled")

    if target.symbol != mapping.target_symbol:
        raise RiskViolation(f"target symbol mismatch: {target.symbol}")

    if actual.symbol != target.symbol:
        raise RiskViolation(f"actual symbol mismatch: {actual.symbol} != {target.symbol}")

    if risk.reject_hedged_same_magic and actual.is_hedged:
        raise RiskViolation(f"hedged same-magic position is not allowed for {actual.symbol}")

    if abs(target.net_lot) > mapping.max_abs_target_lot:
        raise RiskViolation(
            f"target lot {target.net_lot} exceeds max {mapping.max_abs_target_lot}"
        )

    if risk.mode == BridgeMode.FLATTEN_ONLY and abs(target.net_lot) > abs(actual.net_lot):
        raise RiskViolation("flatten-only mode rejects increasing exposure")

    if risk.mode == BridgeMode.FLATTEN_ONLY and _sign(target.net_lot) != _sign(actual.net_lot):
        if target.net_lot != 0:
            raise RiskViolation("flatten-only mode rejects reversing exposure")


def _sign(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0
