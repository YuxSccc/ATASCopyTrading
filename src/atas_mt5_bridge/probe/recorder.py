from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Mapping


class ProbeProtocolError(ValueError):
    """Raised when a client message does not conform to probe protocol v1."""


@dataclass(frozen=True)
class ProbeEvent:
    session_id: str
    event_id: int
    event_type: str
    source_symbol: str
    delivery_mode: str
    payload: Mapping[str, Any]


def parse_probe_event(payload: Mapping[str, Any]) -> ProbeEvent:
    if payload.get("type") != "probe_event":
        raise ProbeProtocolError("unsupported message type")
    if int(payload.get("protocol_version", 0)) != 1:
        raise ProbeProtocolError("unsupported probe protocol version")

    session_id = str(payload.get("session_id", "")).strip()
    event_type = str(payload.get("event_type", "")).strip()
    source_symbol = str(payload.get("source_symbol", "")).strip()
    if not session_id or not event_type or not source_symbol:
        raise ProbeProtocolError("session_id, event_type, and source_symbol are required")

    event_id = int(payload.get("event_id", 0))
    if event_id <= 0:
        raise ProbeProtocolError("event_id must be positive")

    delivery_mode = str(payload.get("delivery_mode", "async")).lower()
    if delivery_mode not in {"async", "sync"}:
        raise ProbeProtocolError("delivery_mode must be async or sync")

    return ProbeEvent(
        session_id=session_id,
        event_id=event_id,
        event_type=event_type,
        source_symbol=source_symbol,
        delivery_mode=delivery_mode,
        payload=payload,
    )


@dataclass
class ProbeRecorder:
    output_directory: Path
    _recorded_keys: set[tuple[str, int]] = field(default_factory=set, init=False)

    def record(self, event: ProbeEvent) -> dict[str, Any]:
        received_utc = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        received_monotonic_ns = time.perf_counter_ns()
        event_key = (event.session_id, event.event_id)
        if event_key in self._recorded_keys:
            return {
                "type": "probe_ack",
                "protocol_version": 1,
                "session_id": event.session_id,
                "event_id": event.event_id,
                "server_received_utc": received_utc,
                "server_received_monotonic_ns": received_monotonic_ns,
            }
        record = {
            "received_at_utc": received_utc,
            "received_monotonic_ns": received_monotonic_ns,
            "event": event.payload,
        }

        self.output_directory.mkdir(parents=True, exist_ok=True)
        output_path = self.output_directory / f"atas_probe_{event.session_id}.jsonl"
        with output_path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")

        self._recorded_keys.add(event_key)

        return {
            "type": "probe_ack",
            "protocol_version": 1,
            "session_id": event.session_id,
            "event_id": event.event_id,
            "server_received_utc": received_utc,
            "server_received_monotonic_ns": received_monotonic_ns,
        }
