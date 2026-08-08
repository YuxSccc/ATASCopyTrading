from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .models import ExecutionReceipt, OperationRecord, BridgeCommand


class OperationJournal(Protocol):
    def record(self, command: BridgeCommand, receipt: ExecutionReceipt) -> None:
        raise NotImplementedError


@dataclass
class InMemoryOperationJournal:
    records: dict[str, OperationRecord] = field(default_factory=dict)

    def record(self, command: BridgeCommand, receipt: ExecutionReceipt) -> None:
        self.records[command.command_id] = OperationRecord(
            command=command,
            status=receipt.status,
            target_reference=receipt.target_reference,
            message=receipt.message,
        )


@dataclass
class JsonlOperationJournal(InMemoryOperationJournal):
    """Append-only audit journal. It is intentionally not a retry queue."""

    records: dict[str, OperationRecord] = field(default_factory=dict, init=False)
    path: Path = field(default_factory=lambda: Path("bridge-operation-journal.jsonl"))

    def record(self, command: BridgeCommand, receipt: ExecutionReceipt) -> None:
        super().record(command, receipt)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "command": asdict(command),
            "receipt": asdict(receipt),
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, default=_json_default, sort_keys=True))
            stream.write("\n")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value)!r}")
