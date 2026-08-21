from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    # Full account state is now pushed on trade transactions and at a slow
    # recovery interval.  Heartbeats carry only the quote, so the full state
    # must remain valid for slightly longer than that recovery interval.
    snapshot_ttl_seconds: float = 6.0
    # A heartbeat proves that the EA/terminal loop is still reachable, but it
    # deliberately carries no positions or orders.  Once a heartbeat has
    # been seen, command delivery also requires a recent heartbeat; the full
    # snapshot remains the authority for execution confirmation.
    heartbeat_ttl_seconds: float = 0.5
    # Optional logical pair identifier. Empty values preserve the legacy
    # single-pair protocol; the multi-pair router validates it when supplied.
    pair_tag: str | None = None
    _state: TargetAccountState | None = field(default=None, init=False)
    _state_received_at: float = field(default=0.0, init=False)
    _last_heartbeat_at: float = field(default=0.0, init=False)
    _queued_commands: dict[str, BridgeCommand] = field(default_factory=dict, init=False)
    _quarantined_commands: dict[str, BridgeCommand] = field(default_factory=dict, init=False)
    _acknowledged_receipts: dict[str, ExecutionReceipt] = field(default_factory=dict, init=False)
    _m1_history: tuple[dict[str, object], ...] = field(default=(), init=False)
    _transport_times: dict[str, dict[str, float | str | int]] = field(default_factory=dict, init=False)
    _last_transport: dict[str, object] | None = field(default=None, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)

    def read_state(self, target_symbol: str) -> TargetAccountState:
        with self._lock:
            if target_symbol != self.target_symbol:
                raise Mt5GatewayError(f"unexpected target symbol: {target_symbol}")
            if self._state is None:
                raise Mt5GatewayError("no complete MT5 snapshot received")
            if not self._transport_is_fresh_locked():
                raise Mt5GatewayError("latest MT5 snapshot is stale")
            return self._state

    def execute(self, command: BridgeCommand) -> ExecutionReceipt:
        with self._lock:
            if command.target_symbol != self.target_symbol:
                return ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="wrong target symbol")
            if self._state is None:
                return ExecutionReceipt(command.command_id, OperationStatus.UNKNOWN, message="MT5 snapshot unavailable")
            if not self._transport_is_fresh_locked():
                return ExecutionReceipt(command.command_id, OperationStatus.UNKNOWN, message="MT5 snapshot is stale")
            self._queued_commands.setdefault(command.command_id, command)
            timing = self._transport_times.setdefault(command.command_id, {})
            timing.setdefault("queued_monotonic", monotonic())
            timing.setdefault("queued_at_utc", datetime.now(timezone.utc).isoformat())
            return ExecutionReceipt(command.command_id, OperationStatus.PENDING, message="queued for MT5 EA")

    def ingest_snapshot(self, payload: Mapping[str, Any]) -> TargetAccountState:
        """Accept a complete bridge-filtered account snapshot from the EA."""

        target_symbol = _text(payload, "target_symbol")
        if target_symbol != self.target_symbol:
            raise Mt5GatewayError(f"snapshot symbol {target_symbol} does not match {self.target_symbol}")
        if int(payload.get("bridge_magic", -1)) != self.bridge_magic:
            raise Mt5GatewayError("snapshot bridge magic does not match")
        self._validate_pair_tag(payload, "snapshot")
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
            history = _parse_m1_history(payload.get("m1_bars"))
            if history:
                self._m1_history = history
        return state

    def ingest_heartbeat(self, payload: Mapping[str, Any]) -> TargetAccountState | None:
        """Update only the fast quote path without replacing account state.

        Positions and pending orders are authoritative only in a complete
        snapshot.  Keeping them untouched here prevents a quote heartbeat
        from being mistaken for proof that an asynchronous MT5 operation has
        completed.
        """

        target_symbol = _text(payload, "target_symbol")
        if target_symbol != self.target_symbol:
            raise Mt5GatewayError(f"heartbeat symbol {target_symbol} does not match {self.target_symbol}")
        if int(payload.get("bridge_magic", -1)) != self.bridge_magic:
            raise Mt5GatewayError("heartbeat bridge magic does not match")
        self._validate_pair_tag(payload, "heartbeat")
        quote = _parse_quote(payload.get("quote"), target_symbol, datetime.now(timezone.utc).isoformat())
        if quote is None or not quote.is_complete:
            raise Mt5GatewayError("heartbeat quote is incomplete")
        with self._lock:
            if self._state is None:
                # A service restart can occur while the EA is already
                # running.  Accepting the liveness/quote message here avoids
                # a burst of 400 responses until the next recovery snapshot;
                # no command can be offered because _state is still absent.
                self._last_heartbeat_at = monotonic()
                return None
            self._state = replace(self._state, quote=quote)
            self._last_heartbeat_at = monotonic()
            return self._state

    def next_commands(self, limit: int = 1) -> tuple[BridgeCommand, ...]:
        """Offer each queued command until the EA durably acknowledges it.

        The EA persists its last command ID before dispatching to MT5, so this
        at-least-once local transport is safe against a lost HTTP GET response
        and safe across an EA restart. It is distinct from broker execution,
        which still needs a complete target snapshot for confirmation.
        """

        with self._lock:
            if not self._transport_is_fresh_locked():
                return ()
            commands = tuple(list(self._queued_commands.values())[:limit])
            offered_monotonic = monotonic()
            offered_at_utc = datetime.now(timezone.utc).isoformat()
            for command in commands:
                timing = self._transport_times.setdefault(command.command_id, {})
                timing.setdefault("offered_monotonic", offered_monotonic)
                timing.setdefault("offered_at_utc", offered_at_utc)
                timing["offer_count"] = int(timing.get("offer_count", 0)) + 1
            return commands

    def next_queued_command(self) -> BridgeCommand | None:
        """Return the next command without exposing it to the MT5 EA."""

        with self._lock:
            if not self._transport_is_fresh_locked():
                return None
            return next(iter(self._queued_commands.values()), None)

    def discard_undelivered_command(self, command_id: str) -> bool:
        """Remove a queued command only if no EA poll has received it.

        Once a command has been offered, a missing receipt is ambiguous and
        must continue through the normal durable reconciliation path.  This
        distinction is what lets a later ATAS cancellation invalidate a
        queued create without risking a duplicate broker request.
        """

        with self._lock:
            if command_id not in self._queued_commands:
                return False
            timing = self._transport_times.get(command_id, {})
            if "offered_monotonic" in timing:
                return False
            self._queued_commands.pop(command_id, None)
            self._transport_times.pop(command_id, None)
            return True

    def transport_status(self) -> dict[str, object]:
        """Return the latest local queue/EA receipt timing for diagnostics.

        These measurements stop at the EA's HTTP receipt. They intentionally
        do not claim that a broker-side order or position is already visible;
        that separate confirmation interval is represented by the operation
        record's dispatched/resolved timestamps.
        """

        with self._lock:
            return dict(self._last_transport) if self._last_transport is not None else {
                "available": False,
                "queued_commands": len(self._queued_commands),
            }

    def snapshot_status(self) -> dict[str, bool | int | None]:
        """Expose only snapshot liveness for the local operator status page."""

        with self._lock:
            if self._state is None:
                heartbeat_age_seconds = (
                    max(0.0, monotonic() - self._last_heartbeat_at) if self._last_heartbeat_at else None
                )
                return {
                    "received": False,
                    "fresh": False,
                    "age_ms": None,
                    "heartbeat_received": self._last_heartbeat_at > 0,
                    "heartbeat_age_ms": round(heartbeat_age_seconds * 1000) if heartbeat_age_seconds is not None else None,
                    "transport_alive": False,
                }
            age_seconds = max(0.0, monotonic() - self._state_received_at)
            heartbeat_age_seconds = (
                max(0.0, monotonic() - self._last_heartbeat_at) if self._last_heartbeat_at else None
            )
            return {
                "received": True,
                "fresh": age_seconds <= self.snapshot_ttl_seconds,
                "age_ms": round(age_seconds * 1000),
                "heartbeat_received": self._last_heartbeat_at > 0,
                "heartbeat_age_ms": round(heartbeat_age_seconds * 1000) if heartbeat_age_seconds is not None else None,
                "transport_alive": self._transport_is_fresh_locked(),
            }

    def _transport_is_fresh_locked(self) -> bool:
        """Return whether it is safe to read/offer through the EA boundary.

        Complete account state is intentionally allowed to live for the
        five-second recovery interval plus a small scheduling margin.  A
        heartbeat, when available, supplies the fast liveness check without
        pretending to be account-state confirmation.  Older EAs that never
        send heartbeats continue to work with complete snapshots alone.
        """

        if self._state is None:
            return False
        if monotonic() - self._state_received_at > self.snapshot_ttl_seconds:
            return False
        if self._last_heartbeat_at and monotonic() - self._last_heartbeat_at > self.heartbeat_ttl_seconds:
            return False
        return True

    def latest_state(self) -> TargetAccountState | None:
        """Return the most recent complete snapshot for diagnostics only.

        Planning and command delivery must still use :meth:`read_state`, which
        rejects stale data. The status endpoint needs to explain *why* a guard
        is blocking without weakening that execution rule.
        """
        with self._lock:
            return self._state

    def latest_m1_history(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return self._m1_history

    def restore_unresolved_command(self, command: BridgeCommand) -> None:
        """Restore only a command whose delivery is still unambiguous."""

        with self._lock:
            self._queued_commands.setdefault(command.command_id, command)

    def withdraw_command(self, command_id: str) -> None:
        """Stop offering an ambiguous command while retaining its receipt key."""

        with self._lock:
            command = self._queued_commands.pop(command_id, None)
            if command is not None:
                self._quarantined_commands.setdefault(command_id, command)

    def remember_quarantined_command(self, command: BridgeCommand) -> None:
        """Accept a late receipt after restart without re-delivering its action."""

        with self._lock:
            self._quarantined_commands.setdefault(command.command_id, command)

    def restore_acknowledged_receipt(self, receipt: ExecutionReceipt) -> None:
        """Keep a completed command's EA receipt idempotent across restart."""

        with self._lock:
            self._acknowledged_receipts.setdefault(receipt.command_id, receipt)

    def acknowledge_command(self, payload: Mapping[str, Any]) -> ExecutionReceipt:
        self._validate_pair_tag(payload, "receipt")
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
            if command_id not in self._queued_commands and command_id not in self._quarantined_commands:
                raise Mt5GatewayError(f"EA acknowledged unknown command: {command_id}")
            if status in {
                OperationStatus.ACCEPTED,
                OperationStatus.CONFIRMED,
                OperationStatus.FAILED,
                OperationStatus.UNKNOWN,
            }:
                self._queued_commands.pop(command_id, None)
                self._quarantined_commands.pop(command_id, None)
            self._acknowledged_receipts[command_id] = receipt
            timing = self._transport_times.get(command_id)
            if timing is not None:
                received_monotonic = monotonic()
                offered_monotonic = timing.get("offered_monotonic")
                queued_monotonic = timing.get("queued_monotonic")
                self._last_transport = {
                    "available": True,
                    "command_id": command_id,
                    "status": status.value,
                    "queued_at_utc": timing.get("queued_at_utc"),
                    "offered_at_utc": timing.get("offered_at_utc"),
                    "receipt_at_utc": datetime.now(timezone.utc).isoformat(),
                    "offer_count": int(timing.get("offer_count", 0)),
                    "queued_to_offer_ms": _elapsed_ms(queued_monotonic, offered_monotonic),
                    "offer_to_receipt_ms": _elapsed_ms(offered_monotonic, received_monotonic),
                    "queued_to_receipt_ms": _elapsed_ms(queued_monotonic, received_monotonic),
                }
                self._transport_times.pop(command_id, None)
            return receipt

    def _validate_pair_tag(self, payload: Mapping[str, Any], message_type: str) -> None:
        expected = (self.pair_tag or "").strip()
        supplied = str(payload.get("pair_tag", "")).strip()
        if expected and supplied and supplied != expected:
            raise Mt5GatewayError(f"{message_type} pair tag {supplied} does not match {expected}")


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


def _parse_m1_history(data: object) -> tuple[dict[str, object], ...]:
    """Keep at most one in-memory closed-bar calibration window from the EA."""
    if not isinstance(data, list):
        return ()
    bars: list[dict[str, object]] = []
    for item in data[-240:]:
        if not isinstance(item, Mapping):
            continue
        time_utc = str(item.get("time_utc", "")).strip()
        close = item.get("close")
        if not time_utc or close in (None, ""):
            continue
        bars.append({"time_utc": time_utc, "close": str(close)})
    return tuple(bars)


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


def _elapsed_ms(start: object, end: object) -> float | None:
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    return round(max(0.0, float(end) - float(start)) * 1000.0, 3)
