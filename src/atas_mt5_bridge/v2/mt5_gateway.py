from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from threading import RLock
from time import monotonic
from typing import Any, Mapping

from .coordinator import TargetExecutionPort
from .models import (
    BridgeCommand,
    ExecutionReceipt,
    OperationStatus,
    TargetAccountModel,
    TargetAccountState,
    TargetOrderState,
    TargetPositionState,
    TargetQuote,
    TargetSymbolSpec,
)


class Mt5GatewayError(RuntimeError):
    pass


@dataclass
class Mt5HttpGateway(TargetExecutionPort):
    """In-memory boundary to the local MT5 EA HTTP protocol.

    It deliberately returns ``PENDING`` on command enqueue. Only an EA receipt
    and later account snapshot can advance the coordinator operation state.
    The gateway never sends directly to a broker.
    """

    target_symbol: str
    bridge_magic: int
    snapshot_ttl_seconds: float = 1.0
    _state: TargetAccountState | None = field(default=None, init=False)
    _state_received_at: float = field(default=0.0, init=False)
    _queued_commands: dict[str, BridgeCommand] = field(default_factory=dict, init=False)
    _acknowledged_receipts: dict[str, ExecutionReceipt] = field(default_factory=dict, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)

    def read_state(self, target_symbol: str) -> TargetAccountState:
        with self._lock:
            if target_symbol != self.target_symbol:
                raise Mt5GatewayError(f"unexpected target symbol: {target_symbol}")
            if self._state is None:
                raise Mt5GatewayError("no complete MT5 snapshot received")
            if monotonic() - self._state_received_at > self.snapshot_ttl_seconds:
                raise Mt5GatewayError("latest MT5 snapshot is stale")
            return self._state

    def execute(self, command: BridgeCommand) -> ExecutionReceipt:
        with self._lock:
            if command.target_symbol != self.target_symbol:
                return ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="wrong target symbol")
            if self._state is None:
                return ExecutionReceipt(command.command_id, OperationStatus.UNKNOWN, message="MT5 snapshot unavailable")
            if monotonic() - self._state_received_at > self.snapshot_ttl_seconds:
                return ExecutionReceipt(command.command_id, OperationStatus.UNKNOWN, message="MT5 snapshot is stale")
            self._queued_commands.setdefault(command.command_id, command)
            return ExecutionReceipt(command.command_id, OperationStatus.PENDING, message="queued for MT5 EA")

    def ingest_snapshot(self, payload: Mapping[str, Any]) -> TargetAccountState:
        """Accept a complete bridge-filtered account snapshot from the EA."""

        target_symbol = _text(payload, "target_symbol")
        if target_symbol != self.target_symbol:
            raise Mt5GatewayError(f"snapshot symbol {target_symbol} does not match {self.target_symbol}")
        if int(payload.get("bridge_magic", -1)) != self.bridge_magic:
            raise Mt5GatewayError("snapshot bridge magic does not match")
        # The EA's clock can be a broker/server clock and its basic datetime
        # precision is seconds. Freshness must therefore be measured at the
        # local bridge boundary, where the complete snapshot is received.
        observed_at_utc = datetime.now(timezone.utc).isoformat()
        state = TargetAccountState(
            target_symbol=target_symbol,
            model=TargetAccountModel(_text(payload, "account_model")),
            long_quantity=_decimal(payload.get("long_quantity")),
            short_quantity=_decimal(payload.get("short_quantity")),
            pending_orders=tuple(_parse_order(item) for item in _object_list(payload, "pending_orders")),
            positions=tuple(_parse_position(item) for item in _object_list(payload, "positions")),
            snapshot_complete=bool(payload.get("snapshot_complete", False)),
            quote=_parse_quote(payload.get("quote"), target_symbol, observed_at_utc),
            symbol_spec=_parse_symbol_spec(payload.get("symbol_spec")),
            foreign_symbol_objects_present=bool(payload.get("foreign_symbol_objects_present", False)),
        )
        if not state.snapshot_complete:
            raise Mt5GatewayError("EA snapshot is marked incomplete")
        with self._lock:
            self._state = state
            self._state_received_at = monotonic()
        return state

    def next_commands(self, limit: int = 1) -> tuple[BridgeCommand, ...]:
        """Offer each queued command until the EA durably acknowledges it.

        The EA persists its last command ID before dispatching to MT5, so this
        at-least-once local transport is safe against a lost HTTP GET response
        and safe across an EA restart. It is distinct from broker execution,
        which still needs a complete target snapshot for confirmation.
        """

        with self._lock:
            if self._state is None or monotonic() - self._state_received_at > self.snapshot_ttl_seconds:
                return ()
            return tuple(list(self._queued_commands.values())[:limit])

    def snapshot_status(self) -> dict[str, bool | int | None]:
        """Expose only snapshot liveness for the local operator status page."""

        with self._lock:
            if self._state is None:
                return {"received": False, "fresh": False, "age_ms": None}
            age_seconds = max(0.0, monotonic() - self._state_received_at)
            return {
                "received": True,
                "fresh": age_seconds <= self.snapshot_ttl_seconds,
                "age_ms": round(age_seconds * 1000),
            }

    def restore_unresolved_command(self, command: BridgeCommand) -> None:
        """Make a pre-restart request acknowledgeable without re-delivering it.

        The durable operation book is authoritative after a bridge restart.  A
        recovered command may be acknowledged by an EA that was still running,
        but must never be blindly sent to the EA a second time.
        """

        with self._lock:
            self._queued_commands.setdefault(command.command_id, command)

    def restore_acknowledged_receipt(self, receipt: ExecutionReceipt) -> None:
        """Keep a completed command's EA receipt idempotent across restart."""

        with self._lock:
            self._acknowledged_receipts.setdefault(receipt.command_id, receipt)

    def acknowledge_command(self, payload: Mapping[str, Any]) -> ExecutionReceipt:
        command_id = _text(payload, "command_id")
        status = OperationStatus(_text(payload, "status"))
        receipt = ExecutionReceipt(
            command_id=command_id,
            status=status,
            target_reference=_optional_text(payload.get("target_reference")),
            message=str(payload.get("message", "")),
        )
        with self._lock:
            previous = self._acknowledged_receipts.get(command_id)
            if previous is not None:
                if previous.status != status:
                    raise Mt5GatewayError("EA receipt status conflicts with prior acknowledgement")
                return previous
            if command_id not in self._queued_commands:
                raise Mt5GatewayError(f"EA acknowledged unknown command: {command_id}")
            if status in {
                OperationStatus.ACCEPTED,
                OperationStatus.CONFIRMED,
                OperationStatus.FAILED,
                OperationStatus.UNKNOWN,
            }:
                self._queued_commands.pop(command_id, None)
            self._acknowledged_receipts[command_id] = receipt
            return receipt


def command_to_wire(command: BridgeCommand) -> dict[str, Any]:
    """JSON representation returned by the local HTTP server to the EA."""

    return {
        "command_id": command.command_id,
        "command_type": command.command_type.value,
        "target_symbol": command.target_symbol,
        "source_order_id": command.source_order_id,
        "leg_id": command.leg_id,
        "payload": {key: _wire_value(value) for key, value in command.payload.items()},
    }


def command_to_tsv(command: BridgeCommand) -> str:
    """Compact poll response parsed by MQL5 without a JSON dependency."""

    payload = command.payload
    fields = (
        command.command_id,
        command.command_type.value,
        command.target_symbol,
        _tsv_field(payload.get("side", "")),
        _tsv_field(payload.get("order_type", "")),
        _tsv_field(payload.get("quantity", "")),
        _tsv_field(payload.get("price", payload.get("stop_loss", ""))),
        _tsv_field(payload.get("trigger_price", payload.get("take_profit", ""))),
        _tsv_field(payload.get("position_ticket", "")),
        _tsv_field(payload.get("target_order_ticket", "")),
        _tsv_field(payload.get("bridge_tag", "")),
    )
    if any("\t" in value or "\n" in value or "\r" in value for value in fields):
        raise Mt5GatewayError("command contains an invalid wire field")
    return "\t".join(fields)


def _parse_order(data: Mapping[str, Any]) -> TargetOrderState:
    return TargetOrderState(
        ticket=_text(data, "ticket"),
        side=_text(data, "side"),
        order_type=_text(data, "order_type"),
        quantity=_decimal(data.get("quantity")),
        price=_optional_decimal(data.get("price")),
        trigger_price=_optional_decimal(data.get("trigger_price")),
        source_order_id=_optional_text(data.get("source_order_id")),
        leg_id=_optional_text(data.get("leg_id")),
        bridge_tag=_optional_text(data.get("bridge_tag")),
    )


def _parse_position(data: Mapping[str, Any]) -> TargetPositionState:
    return TargetPositionState(
        ticket=_text(data, "ticket"),
        side=_text(data, "side"),
        quantity=_decimal(data.get("quantity")),
        stop_loss=_optional_nonzero_decimal(data.get("stop_loss")),
        take_profit=_optional_nonzero_decimal(data.get("take_profit")),
    )


def _parse_quote(data: object, symbol: str, observed_at_utc: str) -> TargetQuote | None:
    if not isinstance(data, Mapping):
        return None
    return TargetQuote(
        target_symbol=symbol,
        bid=_optional_decimal(data.get("bid")),
        ask=_optional_decimal(data.get("ask")),
        occurred_at_utc=observed_at_utc,
    )


def _parse_symbol_spec(data: object) -> TargetSymbolSpec | None:
    if not isinstance(data, Mapping):
        return None
    return TargetSymbolSpec(
        tick_size=_decimal(data.get("tick_size")),
        volume_min=_decimal(data.get("volume_min")),
        volume_max=_decimal(data.get("volume_max")),
        volume_step=_decimal(data.get("volume_step")),
        stops_level=_decimal(data.get("stops_level")),
        freeze_level=_decimal(data.get("freeze_level")),
    )


def _wire_value(value: object) -> object:
    return str(value) if isinstance(value, Decimal) else value


def _tsv_field(value: object) -> str:
    return "" if value is None else str(value)


def _object_list(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise Mt5GatewayError(f"{key} must be a list of objects")
    return value


def _decimal(value: object) -> Decimal:
    if value is None:
        raise Mt5GatewayError("decimal field is required")
    return Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    return None if value in (None, "") else _decimal(value)


def _optional_nonzero_decimal(value: object) -> Decimal | None:
    parsed = _optional_decimal(value)
    return None if parsed == 0 else parsed


def _text(data: Mapping[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise Mt5GatewayError(f"{key} is required")
    return value


def _optional_text(value: object) -> str | None:
    value = str(value).strip() if value is not None else ""
    return value or None
