from __future__ import annotations

import json

import pytest

from atas_mt5_bridge.probe.recorder import ProbeProtocolError, ProbeRecorder, parse_probe_event


def make_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "probe_event",
        "protocol_version": 1,
        "session_id": "session-123",
        "event_id": 7,
        "event_type": "order_changed",
        "source_symbol": "COMEX Gold August 2026",
        "delivery_mode": "sync",
        "data": {"price": 2400.5},
    }
    payload.update(overrides)
    return payload


def test_parse_probe_event_accepts_version_one():
    event = parse_probe_event(make_payload())

    assert event.session_id == "session-123"
    assert event.event_id == 7
    assert event.delivery_mode == "sync"


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "atas_position"},
        {"protocol_version": 2},
        {"event_id": 0},
        {"source_symbol": ""},
        {"delivery_mode": "buffered"},
    ],
)
def test_parse_probe_event_rejects_invalid_payload(overrides: dict[str, object]):
    with pytest.raises(ProbeProtocolError):
        parse_probe_event(make_payload(**overrides))


def test_recorder_writes_jsonl_and_returns_matching_acknowledgement(tmp_path):
    event = parse_probe_event(make_payload())
    acknowledgement = ProbeRecorder(tmp_path).record(event)

    assert acknowledgement["type"] == "probe_ack"
    assert acknowledgement["event_id"] == 7
    output_path = tmp_path / "atas_probe_session-123.jsonl"
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    assert stored["event"]["event_type"] == "order_changed"
    assert stored["event"]["data"]["price"] == 2400.5
