from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
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
    control = DemoTestController(enabled=True, token="test-token", report_path=tmp_path / "runs.jsonl")
    created = control.create_run({"scenario": "pending_lifecycle"})
    run_id = str(created["run_id"])

    control.observe_probe_event({"event_type": "test_driver_ready", "session_id": "test-driver", "data": {}})
    assert control.command_for_ack("test-driver") == {
        "command_id": f"{run_id}:open_pending",
        "run_id": run_id,
        "command": "open_pending",
        "token": "test-token",
    }
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


def test_test_control_rejects_arbitrary_order_options(tmp_path: Path):
    control = DemoTestController(enabled=True, token="test-token", report_path=tmp_path / "runs.jsonl")
    with pytest.raises(ValueError, match="unsupported"):
        control.create_run({"quantity": "100"})


def test_test_http_server_uses_a_separate_token_and_can_abort(tmp_path: Path):
    control = DemoTestController(enabled=True, token="test-token", report_path=tmp_path / "runs.jsonl")
    server = start_test_http_server(control, "127.0.0.1", 0, "control-token")
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/v1/test/runs",
            data=json.dumps({"scenario": "pending_lifecycle"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Test-Control-Token": "control-token"},
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 201
            created = json.loads(response.read())
        run_id = created["run_id"]
        abort = Request(
            f"{base}/v1/test/runs/{run_id}/abort",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Test-Control-Token": "control-token"},
        )
        with urlopen(abort, timeout=3) as response:
            assert json.loads(response.read())["status"] == "aborted"
        unauthorized = Request(f"{base}/v1/test/status", headers={"X-Test-Control-Token": "wrong"})
        with pytest.raises(HTTPError) as error:
            urlopen(unauthorized, timeout=3)
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
