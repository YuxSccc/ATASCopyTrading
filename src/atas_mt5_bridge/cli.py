from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from atas_mt5_bridge.bridge.config import load_bridge_config
from atas_mt5_bridge.bridge.events import parse_atas_position_event
from atas_mt5_bridge.bridge.result_json import bridge_result_to_json
from atas_mt5_bridge.bridge.service import BridgeService
from atas_mt5_bridge.core.models import BridgeMode
from atas_mt5_bridge.core.state import FileStateStore, InMemoryStateStore
from atas_mt5_bridge.core.volume import SymbolVolumeRules
from atas_mt5_bridge.mt5.fake import FakeMt5Adapter


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan one ATAS->MT5 target-position event.")
    parser.add_argument("--config", required=True, help="Path to bridge config JSON.")
    parser.add_argument("--event", required=True, help="Path to ATAS position event JSON.")
    parser.add_argument("--actual-net", default="0", help="Fake current MT5 net lot for dry-run planning.")
    parser.add_argument("--volume-min", default="0.01")
    parser.add_argument("--volume-step", default="0.01")
    parser.add_argument("--volume-max", default="100")
    args = parser.parse_args()

    config = load_bridge_config(args.config)
    with Path(args.event).open("r", encoding="utf-8") as f:
        event = parse_atas_position_event(json.load(f))

    mapping = config.mappings.get(event.source_symbol)
    if mapping is None:
        raise SystemExit(f"unknown source symbol: {event.source_symbol}")

    rules = SymbolVolumeRules(
        symbol=mapping.target_symbol,
        volume_min=Decimal(args.volume_min),
        volume_step=Decimal(args.volume_step),
        volume_max=Decimal(args.volume_max),
    )
    mt5 = FakeMt5Adapter(
        positions={mapping.target_symbol: Decimal(args.actual_net)},
        rules={mapping.target_symbol: rules},
    )
    state = FileStateStore(config.state_path) if config.state_path else InMemoryStateStore()

    # This command is a planning tool. If config says live, keep it dry-run to avoid
    # creating a misleading local fake execution path.
    risk = config.risk
    if risk.mode == BridgeMode.LIVE:
        risk = type(risk)(
            mode=BridgeMode.DRY_RUN,
            reject_hedged_same_magic=risk.reject_hedged_same_magic,
            allow_external_positions=risk.allow_external_positions,
            magic=risk.magic,
            order_comment_prefix=risk.order_comment_prefix,
        )

    service = BridgeService(
        mappings=config.mappings,
        mt5=mt5,
        state=state,
        risk=risk,
    )
    result = service.handle_position_event(event)
    print(json.dumps(_result_to_json(result), indent=2, ensure_ascii=False))
    return 0


def _result_to_json(result: Any) -> dict[str, Any]:
    return bridge_result_to_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
