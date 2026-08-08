import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from atas_mt5_bridge.bridge.config import load_bridge_config
from atas_mt5_bridge.bridge.server import (
    build_dry_run_service,
    build_service,
    format_plan_log,
    handle_ws_message,
)
from atas_mt5_bridge.core.models import BridgeMode
from atas_mt5_bridge.core.state import InMemoryStateStore


class FakeMetaTrader5Module:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.initialize_called = False
        self.sent_requests = []
        self.positions = []
        self.symbol = SimpleNamespace(
            name="XAUUSD",
            visible=True,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            filling_mode=2,
        )
        self.tick = SimpleNamespace(ask=2401.2, bid=2401.0)

    def initialize(self):
        self.initialize_called = True
        return True

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(trade_allowed=True, trade_expert=True)

    def symbol_info(self, symbol):
        return self.symbol if symbol == "XAUUSD" else None

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return self.tick

    def positions_get(self, symbol=None):
        if symbol is None:
            return self.positions
        return [position for position in self.positions if position.symbol == symbol]

    def order_send(self, request):
        self.sent_requests.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done", order=12345)


def test_handle_ws_position_event_returns_plan(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "bridge": {"mode": "dry_run", "magic": 880501},
                "symbols": {
                    "COMEX Gold August 2026": {
                        "target_symbol": "XAUUSD",
                        "lot_ratio": "0.05",
                        "max_abs_target_lot": "1.00",
                        "max_order_lot": "0.20",
                        "volume_min": "0.01",
                        "volume_step": "0.01",
                        "volume_max": "100",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(path)
    service = build_dry_run_service(config, InMemoryStateStore(), {"XAUUSD": Decimal("0")})

    response = handle_ws_message(
        json.dumps(
            {
                "type": "atas_position",
                "event": {
                    "seq": 1,
                    "source_symbol": "COMEX Gold August 2026",
                    "target_net_contracts": "1",
                    "avg_price": "2400.5",
                    "timestamp": "2026-06-03T10:00:00Z",
                },
            }
        ),
        service,
    )

    assert response["status"] == "ok"
    assert response["plan"]["target"]["symbol"] == "XAUUSD"
    assert response["plan"]["target"]["net_lot"] == "0.05"
    assert response["plan"]["orders"][0]["side"] == "buy"
    assert response["plan"]["orders"][0]["volume"] == "0.05"
    assert "source=COMEX Gold August 2026" in format_plan_log(response)
    assert "orders=[buy 0.05 XAUUSD]" in format_plan_log(response)
    assert "dry_run=True" in format_plan_log(response)
    assert "executions=[none]" in format_plan_log(response)


def test_handle_ws_strategy_status_returns_ack(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"bridge": {"mode": "dry_run"}, "symbols": {}}),
        encoding="utf-8",
    )
    config = load_bridge_config(path)
    service = build_dry_run_service(config, InMemoryStateStore(), {})

    response = handle_ws_message(
        json.dumps(
            {
                "type": "strategy_status",
                "strategy": {
                    "source_symbol": "COMEX Gold August 2026",
                    "chart_symbol": "COMEX Gold August 2026",
                    "server_url": "ws://127.0.0.1:8766",
                },
            }
        ),
        service,
    )

    assert response["status"] == "ok"
    assert response["type"] == "strategy_status_ack"
    assert "strategy_status source=COMEX Gold August 2026" in format_plan_log(response)


def test_handle_ws_rejects_unknown_message_type(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"bridge": {"mode": "dry_run"}, "symbols": {}}),
        encoding="utf-8",
    )
    config = load_bridge_config(path)
    service = build_dry_run_service(config, InMemoryStateStore(), {})

    with pytest.raises(ValueError, match="unsupported message type"):
        handle_ws_message(json.dumps({"type": "unknown"}), service)


def test_dry_run_service_forces_live_config_to_dry_run_when_called_directly(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "bridge": {"mode": "live", "magic": 880501},
                "symbols": {
                    "COMEX Gold August 2026": {
                        "target_symbol": "XAUUSD",
                        "lot_ratio": "0.05",
                        "max_abs_target_lot": "1.00",
                        "max_order_lot": "0.20",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(path)

    service = build_dry_run_service(config, InMemoryStateStore(), {"XAUUSD": Decimal("0")})

    assert config.risk.mode == BridgeMode.LIVE
    assert service.risk.mode == BridgeMode.DRY_RUN


def test_build_service_uses_metatrader5_adapter_for_live_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "bridge": {"mode": "live", "magic": 880501},
                "symbols": {
                    "COMEX Gold August 2026": {
                        "target_symbol": "XAUUSD",
                        "lot_ratio": "0.05",
                        "max_abs_target_lot": "1.00",
                        "max_order_lot": "0.20",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_bridge_config(path)
    mt5 = FakeMetaTrader5Module()

    service = build_service(config, InMemoryStateStore(), mt5_module=mt5)

    assert mt5.initialize_called is True
    assert service.risk.mode == BridgeMode.LIVE

    response = handle_ws_message(
        json.dumps(
            {
                "type": "atas_position",
                "event": {
                    "seq": 1,
                    "source_symbol": "COMEX Gold August 2026",
                    "target_net_contracts": "1",
                    "avg_price": "2400.5",
                    "timestamp": "2026-06-03T10:00:00Z",
                },
            }
        ),
        service,
    )

    assert response["plan"]["dry_run"] is False
    assert response["plan"]["executions"][0]["accepted"] is True
    assert "executions=[accepted retcode=10009" in format_plan_log(response)
    assert mt5.sent_requests[0]["symbol"] == "XAUUSD"
