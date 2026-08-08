from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from atas_mt5_bridge.v2.models import BridgeCommand, CommandType, OperationStatus
from atas_mt5_bridge.v2.live_service import LiveBridgeController
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
        assert status["in_flight_operations"] == 0
    finally:
        controller.close()
