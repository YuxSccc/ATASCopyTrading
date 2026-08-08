from __future__ import annotations

import json
from pathlib import Path

from atas_mt5_bridge.v2.runtime import BridgeRuntime


def probe_message(event_id: int, event_type: str, data: dict) -> dict:
    return {
        "session_id": "runtime-session",
        "event_id": event_id,
        "event_type": event_type,
        "source_symbol": "GC",
        "atas_timestamp_utc": "2026-08-08T00:00:00Z",
        "data": data,
    }


def source_order(state: str = "active", cancelled: bool = False) -> dict:
    return {
        "id": "order-1",
        "security": "GC",
        "direction": "Sell",
        "order_type": "Stop",
        "state": state,
        "quantity_to_fill": "1",
        "unfilled": "1",
        "price": "4000",
        "trigger_price": "4000",
        "canceled": cancelled,
    }


def test_simulation_runtime_creates_then_cancels_a_target_leg_without_mt5(tmp_path: Path):
    runtime = BridgeRuntime(
        source_symbol="GC",
        target_symbol="XAUUSD",
        mode="simulate",
        output_directory=tmp_path,
    )

    runtime.handle_probe_message(
        probe_message(
            1,
            "state_snapshot",
            {"last_observed_position": {"volume": "0", "average_price": None}, "orders": []},
        )
    )
    created = runtime.handle_probe_message(probe_message(2, "order_new", source_order()))
    cancelled = runtime.handle_probe_message(probe_message(3, "order_changed", source_order("done", True)))

    assert len(created.commands) == 1
    assert len(cancelled.commands) == 1
    assert cancelled.commands[0].command_type.value == "cancel_pending"
    records = [json.loads(line) for line in (tmp_path / "bridge-runtime-decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["event_id"] for record in records] == [1, 2, 3]
