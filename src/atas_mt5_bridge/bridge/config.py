from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Mapping

from atas_mt5_bridge.core.mapping import SymbolMapping
from atas_mt5_bridge.core.models import BridgeMode
from atas_mt5_bridge.core.risk import RiskConfig
from atas_mt5_bridge.core.volume import SymbolVolumeRules


@dataclass(frozen=True)
class BridgeConfig:
    risk: RiskConfig
    mappings: Mapping[str, SymbolMapping]
    volume_rules: Mapping[str, SymbolVolumeRules]
    state_path: Path | None = None


def load_bridge_config(path: str | Path) -> BridgeConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    bridge = raw.get("bridge", {})
    mode = BridgeMode(str(bridge.get("mode", "dry_run")))
    if bool(bridge.get("kill_switch", False)):
        mode = BridgeMode.KILL_SWITCH

    risk = RiskConfig(
        mode=mode,
        reject_hedged_same_magic=bool(bridge.get("reject_hedged_same_magic", True)),
        allow_external_positions=bool(bridge.get("allow_external_positions", False)),
        magic=int(bridge.get("magic", 880501)),
        order_comment_prefix=str(bridge.get("comment_prefix", "ATAS-MT5")),
    )

    mappings: dict[str, SymbolMapping] = {}
    volume_rules: dict[str, SymbolVolumeRules] = {}
    for source_symbol, item in raw.get("symbols", {}).items():
        target_symbol = str(item["target_symbol"])
        mappings[source_symbol] = SymbolMapping(
            source_symbol=source_symbol,
            target_symbol=target_symbol,
            lot_ratio=Decimal(str(item["lot_ratio"])),
            max_abs_target_lot=Decimal(str(item["max_abs_target_lot"])),
            max_order_lot=Decimal(str(item["max_order_lot"])),
        )
        volume_rules[target_symbol] = SymbolVolumeRules(
            symbol=target_symbol,
            volume_min=Decimal(str(item.get("volume_min", "0.01"))),
            volume_step=Decimal(str(item.get("volume_step", "0.01"))),
            volume_max=Decimal(str(item.get("volume_max", "1000000"))),
        )

    state_path = bridge.get("state_path")
    resolved_state_path = None
    if state_path:
        candidate = Path(str(state_path))
        resolved_state_path = candidate if candidate.is_absolute() else config_path.parent / candidate

    return BridgeConfig(
        risk=risk,
        mappings=mappings,
        volume_rules=volume_rules,
        state_path=resolved_state_path,
    )
