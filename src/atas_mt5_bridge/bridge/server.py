from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import importlib
import json
import traceback
from typing import Any, Mapping

from atas_mt5_bridge.bridge.config import BridgeConfig, load_bridge_config
from atas_mt5_bridge.bridge.events import parse_atas_position_event
from atas_mt5_bridge.bridge.result_json import bridge_result_to_json
from atas_mt5_bridge.bridge.service import BridgeService
from atas_mt5_bridge.core.models import BridgeMode
from atas_mt5_bridge.core.state import FileStateStore, InMemoryStateStore, StateStore
from atas_mt5_bridge.mt5.fake import FakeMt5Adapter
from atas_mt5_bridge.mt5.metatrader5_adapter import MetaTrader5Adapter


def build_dry_run_service(
    config: BridgeConfig,
    state: StateStore,
    actual_positions: Mapping[str, Decimal] | None = None,
) -> BridgeService:
    risk = config.risk
    if risk.mode == BridgeMode.LIVE:
        risk = replace(risk, mode=BridgeMode.DRY_RUN)

    mt5 = FakeMt5Adapter(
        positions=dict(actual_positions or {}),
        rules=config.volume_rules,
    )
    return BridgeService(
        mappings=config.mappings,
        mt5=mt5,
        state=state,
        risk=risk,
    )


def build_live_service(
    config: BridgeConfig,
    state: StateStore,
    mt5_module: Any | None = None,
) -> BridgeService:
    if config.risk.mode != BridgeMode.LIVE:
        raise ValueError("build_live_service requires bridge.mode=live")

    mt5 = mt5_module if mt5_module is not None else importlib.import_module("MetaTrader5")
    if not mt5.initialize():
        raise RuntimeError(f"failed to initialize MetaTrader5: {mt5.last_error()}")

    return BridgeService(
        mappings=config.mappings,
        mt5=MetaTrader5Adapter(mt5),
        state=state,
        risk=config.risk,
    )


def build_service(
    config: BridgeConfig,
    state: StateStore,
    actual_positions: Mapping[str, Decimal] | None = None,
    mt5_module: Any | None = None,
) -> BridgeService:
    if config.risk.mode == BridgeMode.LIVE:
        return build_live_service(config, state, mt5_module)
    return build_dry_run_service(config, state, actual_positions)


def handle_ws_message(message: str, service: BridgeService) -> dict[str, Any]:
    payload = json.loads(message)
    message_type = payload.get("type")
    if message_type == "strategy_status":
        return {
            "status": "ok",
            "type": "strategy_status_ack",
            "message": "strategy status received",
            "strategy": payload.get("strategy", {}),
        }

    if message_type != "atas_position":
        raise ValueError(f"unsupported message type: {message_type}")

    event_payload = payload.get("event", payload)
    event = parse_atas_position_event(event_payload)
    result = service.handle_position_event(event)
    return {
        "status": "ok",
        "type": "bridge_plan",
        "plan": bridge_result_to_json(result),
    }


def format_plan_log(response: Mapping[str, Any]) -> str:
    if response.get("type") == "strategy_status_ack":
        strategy = response.get("strategy", {})
        return (
            "strategy_status "
            f"source={strategy.get('source_symbol', '')} "
            f"chart={strategy.get('chart_symbol', '')} "
            f"server_url={strategy.get('server_url', '')}"
        )

    if response.get("status") != "ok":
        return f"error: {response.get('message', '')}"

    plan = response["plan"]
    target = plan["target"]
    actual = plan["actual"]
    orders = plan["orders"]
    order_text = "none"
    if orders:
        order_text = ", ".join(
            f"{order['side']} {order['volume']} {order['symbol']}"
            for order in orders
        )
    executions = plan.get("executions", [])
    execution_text = "none"
    if executions:
        execution_text = ", ".join(
            f"{'accepted' if execution.get('accepted') else 'rejected'} "
            f"{execution.get('message', '')}"
            for execution in executions
        )
    ignored = plan.get("ignored_reason")
    ignored_text = f", ignored={ignored}" if ignored else ""
    return (
        f"source={target['source_symbol']} seq={target['source_seq']} "
        f"target={target['net_lot']} {target['symbol']} "
        f"actual={actual['net_lot']} dry_run={plan.get('dry_run')} "
        f"orders=[{order_text}] executions=[{execution_text}]"
        f"{ignored_text}"
    )


def log_server(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


async def serve(config_path: str, host: str, port: int) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets package is required to run the bridge server") from exc

    config = load_bridge_config(config_path)
    state = FileStateStore(config.state_path) if config.state_path else InMemoryStateStore()
    service = build_service(config, state)

    async def handler(websocket: Any) -> None:
        remote = getattr(websocket, "remote_address", None)
        log_server(f"client connected: {remote}")
        try:
            async for message in websocket:
                try:
                    response = handle_ws_message(message, service)
                except Exception as exc:  # Keep the ATAS strategy connected and report the error.
                    log_server("handler error:\n" + traceback.format_exc())
                    response = {
                        "status": "error",
                        "type": "bridge_error",
                        "message": str(exc),
                    }
                log_server(format_plan_log(response))
                await websocket.send(json.dumps(response, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed as exc:
            log_server(f"client disconnected: {remote} code={exc.code} reason={exc.reason!r}")

    log_server(
        f"ATAS-MT5 bridge server listening on ws://{host}:{port} mode={config.risk.mode.value} "
        "ping_interval=disabled"
    )
    async with websockets.serve(handler, host, port, ping_interval=None):
        await asyncio.Future()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ATAS->MT5 local bridge server.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    asyncio.run(serve(args.config, args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
