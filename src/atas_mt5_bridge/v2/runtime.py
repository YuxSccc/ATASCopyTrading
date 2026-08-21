from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from .atas_adapter import parse_probe_message
from .coordinator import ObserveOnlyPolicy, SymbolCoordinator
from .journal import JsonlOperationJournal
from .models import CoordinationResult, TargetAccountModel
from .policy import AtasAuthoritativePolicy
from .simulation import SimulatedTargetExecutionPort


class BridgeRuntime:
    """Consumes ATAS probe envelopes and writes auditable safe runtime decisions."""

    def __init__(
        self,
        *,
        source_symbol: str,
        target_symbol: str,
        mode: str,
        output_directory: Path,
    ) -> None:
        if mode not in {"observe", "simulate"}:
            raise ValueError("runtime mode must be observe or simulate")
        self.mode = mode
        self.output_directory = output_directory
        execution_port = SimulatedTargetExecutionPort(
            target_symbol=target_symbol,
            model=TargetAccountModel.HEDGING,
        )
        policy = (
            ObserveOnlyPolicy()
            if mode == "observe"
            else AtasAuthoritativePolicy(execution_enabled=True)
        )
        self.coordinator = SymbolCoordinator(
            source_symbol=source_symbol,
            target_symbol=target_symbol,
            execution_port=execution_port,
            policy=policy,
            journal=JsonlOperationJournal(path=output_directory / "bridge-operation-journal.jsonl"),
        )

    def handle_probe_message(self, message: Mapping[str, Any]) -> CoordinationResult:
        event = parse_probe_message(message)
        result = self.coordinator.handle(event)
        self._record_decision(event.event_id, result)
        return result

    def _record_decision(self, event_id: int, result: CoordinationResult) -> None:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        record = {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "event_id": event_id,
            "mode": self.mode,
            "result": asdict(result),
        }
        with (self.output_directory / "bridge-runtime-decisions.jsonl").open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(json.dumps(record, default=_json_default, sort_keys=True))
            stream.write("\n")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, frozenset):
        return sorted(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value)!r}")
