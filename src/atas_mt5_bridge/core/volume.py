from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Optional


@dataclass(frozen=True)
class SymbolVolumeRules:
    symbol: str
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal

    def __post_init__(self) -> None:
        if self.volume_min <= 0:
            raise ValueError("volume_min must be positive")
        if self.volume_step <= 0:
            raise ValueError("volume_step must be positive")
        if self.volume_max < self.volume_min:
            raise ValueError("volume_max must be >= volume_min")


def normalize_volume(volume: Decimal, rules: SymbolVolumeRules) -> Optional[Decimal]:
    """Round volume down to broker step. Return None instead of raising small orders."""
    abs_volume = abs(volume)
    if abs_volume < rules.volume_min:
        return None
    if abs_volume > rules.volume_max:
        return rules.volume_max

    steps = (abs_volume / rules.volume_step).to_integral_value(rounding=ROUND_FLOOR)
    normalized = steps * rules.volume_step
    if normalized < rules.volume_min:
        return None
    return normalized
