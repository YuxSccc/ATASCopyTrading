from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from atas_mt5_bridge.v2.models import BridgeCommand, CommandType, OperationStatus
from atas_mt5_bridge.v2.live_service import LiveBridgeController, _merge_minute_bars, start_mt5_http_server
from atas_mt5_bridge.v2.test_control import DemoTestController
from atas_mt5_bridge.v2.mt5_gateway import Mt5GatewayError, Mt5HttpGateway, command_to_tsv


def snapshot() -> dict:
    return {
        "target_symbol": "XAUUSD",
        "bridge_magic": 26081001,
        "account_model": "hedging",
        "snapshot_complete": True,
        "long_quantity": "1",
        "short_quantity": "0",
        "quote": {"bid": "4000.0", "ask": "4000.2", "occurred_at_utc": "2026-08-10T09:00:00+00:00"},
        "symbol_spec": {
            "tick_size": "0.1",
            "volume_min": "0.01",
            "volume_max": "100",
            "volume_step": "0.01",
            "stops_level": "0.2",
            "freeze_level": "0.1",
        },
        "pending_orders": [
            {
                "ticket": "17",
                "side": "buy",
                "order_type": "limit",
                "quantity": "1",
                "price": "3990",
                "trigger_price": "0",
                "bridge_tag": "a1b2c3d4",
            }
        ],
        "positions": [{"ticket": "9", "side": "long", "quantity": "1", "stop_loss": "0", "take_profit": "4010"}],
    }


def command() -> BridgeCommand:
    return BridgeCommand(
        command_id="command-1",
        command_type=CommandType.CREATE_PENDING,
        target_symbol="XAUUSD",
        source_event_id=1,
        source_order_id="source-1",
        leg_id="leg-1",
        payload={
            "side": "buy",
            "order_type": "limit",
            "quantity": Decimal("1"),
            "price": Decimal("3990"),
            "trigger_price": None,
            "bridge_tag": "a1b2c3d4",
        },
    )


def test_gateway_requires_a_complete_ea_snapshot_before_queueing_commands():
    gateway = Mt5HttpGateway("XAUUSD", 26081001)

    with pytest.raises(Mt5GatewayError):
        gateway.read_state("XAUUSD")
    receipt = gateway.execute(command())
    assert receipt.status == OperationStatus.UNKNOWN


def test_gateway_parses_bridge_filtered_snapshot_queues_once_and_accepts_ea_receipt():
    gateway = Mt5HttpGateway("XAUUSD", 26081001)
    state = gateway.ingest_snapshot(snapshot())

    assert state.pending_orders[0].bridge_tag == "a1b2c3d4"
    assert state.quote is not None
    assert state.symbol_spec is not None
    assert state.positions[0].stop_loss is None
    assert state.positions[0].take_profit == Decimal("4010")
    assert gateway.execute(command()).status == OperationStatus.PENDING
    queued = gateway.next_commands()
    assert queued == (command(),)
    assert gateway.next_commands() == (command(),)
    assert len(command_to_tsv(queued[0]).split("\t")) == 11

    receipt = gateway.acknowledge_command({"command_id": "command-1", "status": "accepted", "target_reference": "17"})
    assert receipt.status == OperationStatus.ACCEPTED

    timing = gateway.transport_status()
    assert timing["available"] is True
    assert timing["command_id"] == "command-1"
    assert timing["status"] == "accepted"
    assert isinstance(timing["queued_to_offer_ms"], float)
    assert isinstance(timing["offer_to_receipt_ms"], float)
    assert isinstance(timing["queued_to_receipt_ms"], float)


def test_gateway_retains_only_the_latest_in_memory_m1_window():
    gateway = Mt5HttpGateway("XAUUSD", 26081001)
    payload = snapshot()
    payload["m1_bars"] = [
        {"time_utc": "2026-08-10T09:00:00+00:00", "close": "4001.2"},
        {"time_utc": "2026-08-10T09:01:00+00:00", "close": "4001.3"},
        {"time_utc": "", "close": "bad"},
    ]

    gateway.ingest_snapshot(payload)

    assert gateway.latest_m1_history() == (
        {"time_utc": "2026-08-10T09:00:00+00:00", "close": "4001.2"},
        {"time_utc": "2026-08-10T09:01:00+00:00", "close": "4001.3"},
    )


def test_gateway_rejects_a_snapshot_from_another_magic_number():
    gateway = Mt5HttpGateway("XAUUSD", 26081001)
    invalid = {**snapshot(), "bridge_magic": 1}

    with pytest.raises(Mt5GatewayError, match="magic"):
        gateway.ingest_snapshot(invalid)


def test_gateway_rejects_a_stale_complete_snapshot_before_command_queueing():
    gateway = Mt5HttpGateway("XAUUSD", 26081001, snapshot_ttl_seconds=0.001)
    gateway.ingest_snapshot(snapshot())
    gateway.execute(command())
    gateway._state_received_at -= 1  # Simulate an EA heartbeat that stopped.

    with pytest.raises(Mt5GatewayError, match="stale"):
        gateway.read_state("XAUUSD")
    assert gateway.next_commands() == ()


def test_gateway_receipt_is_idempotent_and_a_recovered_command_is_not_redelivered():
    gateway = Mt5HttpGateway("XAUUSD", 26081001)
    gateway.ingest_snapshot(snapshot())
    gateway.execute(command())
    assert gateway.next_commands() == (command(),)

    receipt_payload = {"command_id": "command-1", "status": "accepted", "target_reference": "17"}
    assert gateway.acknowledge_command(receipt_payload).status == OperationStatus.ACCEPTED
    assert gateway.acknowledge_command(receipt_payload).status == OperationStatus.ACCEPTED

    restarted = Mt5HttpGateway("XAUUSD", 26081001)
    restarted.ingest_snapshot(snapshot())
    restarted.restore_unresolved_command(command())
    assert restarted.next_commands() == (command(),)
    assert restarted.acknowledge_command(receipt_payload).status == OperationStatus.ACCEPTED


def test_gateway_quarantines_expired_command_without_rejecting_a_late_receipt():
    gateway = Mt5HttpGateway("XAUUSD", 26081001)
    gateway.ingest_snapshot(snapshot())
    gateway.execute(command())

    gateway.withdraw_command(command().command_id)

    assert gateway.next_commands() == ()
    assert gateway.acknowledge_command(
        {"command_id": command().command_id, "status": "unknown", "target_reference": ""}
    ).status == OperationStatus.UNKNOWN


def test_live_controller_caches_mt5_state_before_atas_snapshot_without_planning(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    try:
        controller.handle_mt5_snapshot(snapshot())
        status = controller.status()
        assert status["source_synchronized"] is False
        assert status["target_snapshot_received"] is True
        assert status["target_snapshot_fresh"] is True
        assert isinstance(status["target_snapshot_age_ms"], int)
        assert status["in_flight_operations"] == 0
    finally:
        controller.close()


def test_observation_mode_collects_and_exposes_basis_samples(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    now = datetime.now(timezone.utc).isoformat()
    try:
        controller.handle_mt5_snapshot(snapshot())
        controller.handle_atas_message(
            {
                "session_id": "atas-session",
                "event_id": 1,
                "event_type": "state_snapshot",
                "source_symbol": "GC",
                "atas_timestamp_utc": now,
                "data": {"position_known": True, "last_observed_position": {"security": "GC", "volume": "0", "average_price": "0"}, "orders": []},
            }
        )
        controller.handle_atas_message(
            {
                "session_id": "atas-session",
                "event_id": 2,
                "event_type": "quote",
                "source_symbol": "GC",
                "atas_timestamp_utc": now,
                "data": {"best_bid": "3990", "best_ask": "3990.2"},
            }
        )
        status = controller.status()
        assert status["execution_enabled"] is False
        assert status["basis"]["sample_count"] == 1  # type: ignore[index]
        assert status["basis"]["current_buy_basis"] == "10.0"  # type: ignore[index]
    finally:
        controller.close()


def test_controller_rebuilds_basis_only_from_matching_m1_windows(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    try:
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        first_minute = (minute - timedelta(minutes=2)).isoformat()
        second_minute = (minute - timedelta(minutes=1)).isoformat()
        target = snapshot()
        target["m1_bars"] = [
            {"time_utc": first_minute, "close": "4010"},
            {"time_utc": second_minute, "close": "4012"},
        ]
        controller.handle_mt5_snapshot(target)
        controller.handle_atas_message(
            {
                "session_id": "basis-session",
                "event_id": 1,
                "event_type": "basis_history",
                "source_symbol": "GC",
                "data": {
                    "bars": [
                        {"time_utc": first_minute, "close": "4000"},
                        {"time_utc": second_minute, "close": "4001"},
                    ]
                },
            }
        )

        status = controller.status()
        assert status["basis_history"] == {"source_m1_bars": 2, "target_m1_bars": 2}
        assert status["basis"]["sample_count"] == 2  # type: ignore[index]
        assert status["basis"]["estimated_buy_basis"] == "11"  # type: ignore[index]
    finally:
        controller.close()


def test_test_driver_events_do_not_reset_the_live_source_session(tmp_path: Path):
    test_control = DemoTestController(enabled=True, report_path=tmp_path / "test-runs.jsonl")
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
        test_controller=test_control,
    )
    try:
        controller.handle_atas_message(
            {
                "session_id": "test-driver-session",
                "event_type": "test_driver_ready",
                "data": {},
            }
        )
        assert controller.status()["source_synchronized"] is False
        created = test_control.create_run({})
        assert test_control.command_for_ack("test-driver-session") is not None
        assert created["scenario"] == "pending_lifecycle"
        controller.handle_atas_message(
            {
                "session_id": "test-driver-session",
                "event_type": "test_driver_heartbeat",
                "data": {},
            }
        )
        assert controller.status()["source_synchronized"] is False
    finally:
        controller.close()


def test_mt5_http_server_accepts_the_terminating_nul_sent_by_mql_webrequest(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    server = start_mt5_http_server(controller, "127.0.0.1", 0)
    try:
        payload = json.dumps(snapshot()).encode("utf-8") + b"\x00"
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/mt5/snapshot",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
        assert controller.status()["target_snapshot_received"] is True
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def test_mt5_http_server_offers_commands_over_post_for_mql_compatibility(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    server = start_mt5_http_server(controller, "127.0.0.1", 0)
    try:
        controller.gateway.ingest_snapshot(snapshot())
        controller.gateway.execute(command())
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/mt5/command",
            data=b"{}\x00",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert response.read().decode("utf-8") == command_to_tsv(command())
            assert response.headers["Connection"] == "close"
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def test_mt5_snapshot_poll_returns_command_in_the_same_http_response(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    server = start_mt5_http_server(controller, "127.0.0.1", 0)
    try:
        controller.gateway.ingest_snapshot(snapshot())
        controller.gateway.execute(command())
        payload = json.dumps(snapshot()).encode("utf-8") + b"\x00"
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/mt5/snapshot?poll=1",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/plain")
            assert response.read().decode("utf-8") == command_to_tsv(command())
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def test_source_history_merge_keeps_prior_closed_minutes_when_collector_restarts():
    existing = (
        {"time_utc": "2026-08-11T22:00:00+00:00", "close": "4420.0"},
        {"time_utc": "2026-08-11T22:01:00+00:00", "close": "4420.2"},
    )
    merged = _merge_minute_bars(
        existing,
        [
            {"time_utc": "2026-08-11T19:00:00+00:00", "close": "4410.0"},
            {"time_utc": "2026-08-11T22:01:00+00:00", "close": "4420.4"},
            {"time_utc": "2026-08-11T23:00:00+00:00", "close": "4421.0"},
        ],
    )

    assert [bar["time_utc"] for bar in merged] == [
        "2026-08-11T19:00:00+00:00",
        "2026-08-11T22:00:00+00:00",
        "2026-08-11T22:01:00+00:00",
        "2026-08-11T23:00:00+00:00",
    ]
    assert merged[2]["close"] == "4420.4"


def test_embedded_basis_event_keeps_probe_sequence_contiguous(tmp_path: Path):
    controller = LiveBridgeController(
        source_symbol="GC",
        target_symbol="XAUUSD",
        bridge_magic=26081001,
        database_path=tmp_path / "bridge.db",
        journal_path=tmp_path / "operations.jsonl",
    )
    try:
        common = {
            "type": "probe_event",
            "protocol_version": 1,
            "session_id": "probe-session",
            "source_symbol": "GC",
            "delivery_mode": "async",
            "atas_timestamp_utc": "2026-08-12T00:00:00Z",
        }
        controller.handle_atas_message(
            {
                **common,
                "event_id": 1,
                "event_type": "basis_history",
                "data": {
                    "embedded_in_probe": True,
                    "bars": [{"time_utc": "2026-08-11T23:59:00Z", "close": "4420.0"}],
                },
            }
        )
        controller.handle_atas_message(
            {
                **common,
                "event_id": 2,
                "event_type": "state_snapshot",
                "data": {
                    "last_observed_position": {"volume": 0, "average_price": 0},
                    "position_known": True,
                    "orders": [],
                    "orders_complete": False,
                },
            }
        )
        controller.handle_atas_message(
            {
                **common,
                "event_id": 3,
                "event_type": "quote",
                "data": {"best_bid": "4420", "best_ask": "4420.2"},
            }
        )
        assert controller.status()["source_synchronized"] is True
        assert controller.status()["basis_history"]["source_m1_bars"] == 1  # type: ignore[index]
    finally:
        controller.close()
