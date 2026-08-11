from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from atas_mt5_bridge.v2.live_service import start_test_http_server
from atas_mt5_bridge.v2.test_control import DemoTestController


def _order_event(run_id: str, order_id: str, state: str, *, cancelled: bool = False) -> dict:
    return {
        "event_type": "order_changed",
        "data": {
            "id": order_id,
            "state": state,
            "canceled": cancelled,
            "comment": f"AB2TEST:{run_id}",
        },
    }


def _receipt(run_id: str, command: str) -> dict:
    return {
        "event_type": "test_command_receipt",
        "data": {"run_id": run_id, "command": command, "accepted": True, "message": "ok"},
    }


def test_pending_lifecycle_requires_receipts_and_tracks_recreated_order(tmp_path: Path):
    control = DemoTestController(enabled=True, report_path=tmp_path / "runs.jsonl")
    created = control.create_run({"scenario": "pending_lifecycle"})
    run_id = str(created["run_id"])

    control.observe_probe_event({"event_type": "test_driver_ready", "session_id": "test-driver", "data": {}})
    command = control.command_for_ack("test-driver")
    assert command is not None
    assert command["command_id"] == f"{run_id}:0:open_pending"
    assert command["command"] == "open_pending"
    assert command["parameters"] == {"side": "buy", "order_type": "limit", "quantity": "1", "price_offset_ticks": -100}
    assert control.command_for_ack("ordinary-probe") is None
    control.observe_probe_event(_receipt(run_id, "open_pending"))
    control.observe_probe_event(_order_event(run_id, "source-1", "Active"))
    assert control.command_for_ack("test-driver")["command"] == "modify_pending"  # type: ignore[index]
    assert control.command_for_ack("test-driver")["source_order_id"] == "source-1"  # type: ignore[index]

    control.observe_probe_event(_receipt(run_id, "modify_pending"))
    # A move can produce a new ATAS order ID; the test state follows it.
    control.observe_probe_event(_order_event(run_id, "source-2", "Active"))
    assert control.command_for_ack("test-driver")["command"] == "cancel_pending"  # type: ignore[index]
    assert control.command_for_ack("test-driver")["source_order_id"] == "source-2"  # type: ignore[index]

    control.observe_probe_event(_receipt(run_id, "cancel_pending"))
    control.observe_probe_event(_order_event(run_id, "source-2", "Done", cancelled=True))
    result = control.get_run(run_id)
    assert result is not None
    assert result["status"] == "completed"
    assert control.command_for_ack("test-driver") is None
    assert (tmp_path / "runs.jsonl").read_text(encoding="utf-8")


def test_pending_lifecycle_accepts_callbacks_that_arrive_before_command_receipts(tmp_path: Path):
    control = DemoTestController(enabled=True, report_path=tmp_path / "runs.jsonl")
    control.observe_probe_event({"event_type": "test_driver_ready", "session_id": "test-driver", "data": {}})
    run_id = str(control.create_run({})["run_id"])

    control.observe_probe_event(_order_event(run_id, "source-1", "Active"))
    control.observe_probe_event(_receipt(run_id, "open_pending"))
    assert control.command_for_ack("test-driver")["command"] == "modify_pending"  # type: ignore[index]

    control.observe_probe_event(_order_event(run_id, "source-2", "Active"))
    control.observe_probe_event(_receipt(run_id, "modify_pending"))
    assert control.command_for_ack("test-driver")["command"] == "cancel_pending"  # type: ignore[index]


def test_test_control_rejects_arbitrary_order_options(tmp_path: Path):
    control = DemoTestController(enabled=True, report_path=tmp_path / "runs.jsonl")
    with pytest.raises(ValueError, match="unsupported"):
        control.create_run({"quantity": "100"})


def test_parameterized_script_is_hot_loaded_without_changing_the_driver(tmp_path: Path):
    control = DemoTestController(enabled=True, report_path=tmp_path / "runs.jsonl")
    control.observe_probe_event({"event_type": "test_driver_ready", "session_id": "test-driver", "data": {}})
    created = control.create_run(
        {
            "scenario": "pending_script",
            "steps": [
                {"id": "short_entry", "action": "open_pending", "parameters": {"side": "sell", "quantity": "0.5", "price_offset_ticks": 80}},
                {"id": "move", "action": "modify_pending", "parameters": {"order_ref": "short_entry", "price_offset_ticks": 120}},
                {"id": "cleanup", "action": "cancel_pending", "parameters": {"order_ref": "short_entry"}},
            ],
        }
    )
    command = control.command_for_ack("test-driver")
    assert command is not None
    assert command["parameters"]["side"] == "sell"  # type: ignore[index]
    assert command["parameters"]["quantity"] == "0.5"  # type: ignore[index]


def test_loopback_test_http_server_can_abort(tmp_path: Path):
    control = DemoTestController(enabled=True, report_path=tmp_path / "runs.jsonl")
    server = start_test_http_server(control, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/v1/test/runs",
            data=json.dumps({"scenario": "pending_lifecycle"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 201
            created = json.loads(response.read())
        run_id = created["run_id"]
        abort = Request(
            f"{base}/v1/test/runs/{run_id}/abort",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(abort, timeout=3) as response:
            assert json.loads(response.read())["status"] == "aborted"
        with urlopen(f"{base}/v1/test/status", timeout=3) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
