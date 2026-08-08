from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping


class SourceEventType(str, Enum):
    ORDER = "order"
    POSITION = "position"
    SNAPSHOT = "snapshot"
    AUXILIARY = "auxiliary"


class SourceOrderState(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TargetAccountModel(str, Enum):
    NETTING = "netting"
    HEDGING = "hedging"


class CommandType(str, Enum):
    CREATE_PENDING = "create_pending"
    CANCEL_PENDING = "cancel_pending"
    MODIFY_PENDING = "modify_pending"
    EXECUTE_MARKET = "execute_market"
    MODIFY_POSITION_PROTECTION = "modify_position_protection"
    RECONCILE = "reconcile"


class OperationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SequenceStatus(str, Enum):
    APPLIED = "applied"
    DUPLICATE_OR_OLD = "duplicate_or_old"
    GAP_DETECTED = "gap_detected"
    SNAPSHOT_REQUIRED = "snapshot_required"
    SESSION_RESET = "session_reset"


@dataclass(frozen=True)
class SourceOrder:
    """A single ATAS order instance, not a user-level order intention.

    ATAS can represent a drag/move as a cancelled instance followed by a new
    instance. ``source_order_id`` therefore identifies only this instance.
    """

    source_order_id: str
    source_external_id: str | None
    source_symbol: str
    side: str
    order_type: str
    state: SourceOrderState
    quantity: Decimal
    unfilled: Decimal
    price: Decimal | None
    trigger_price: Decimal | None
    cancelled: bool
    comment: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.state == SourceOrderState.ACTIVE and not self.cancelled


@dataclass(frozen=True)
class SourcePosition:
    """ATAS' single net position for one source symbol."""

    source_symbol: str
    net_quantity: Decimal
    average_price: Decimal | None


@dataclass(frozen=True)
class SourceEvent:
    session_id: str
    event_id: int
    event_type: SourceEventType
    source_symbol: str
    occurred_at_utc: str
    order: SourceOrder | None = None
    position: SourcePosition | None = None
    snapshot_orders: tuple[SourceOrder, ...] = ()

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        if self.event_id <= 0:
            raise ValueError("event_id must be positive")
        if not self.source_symbol:
            raise ValueError("source_symbol is required")
        if self.event_type == SourceEventType.ORDER and self.order is None:
            raise ValueError("order event requires order")
        if self.event_type == SourceEventType.POSITION and self.position is None:
            raise ValueError("position event requires position")


@dataclass(frozen=True)
class SourceSymbolState:
    source_symbol: str
    session_id: str
    last_event_id: int
    position: SourcePosition | None
    active_orders: Mapping[str, SourceOrder]
    synchronized: bool


@dataclass(frozen=True)
class TargetOrderState:
    ticket: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trigger_price: Decimal | None
    source_order_id: str | None = None


@dataclass(frozen=True)
class TargetAccountState:
    target_symbol: str
    model: TargetAccountModel
    long_quantity: Decimal
    short_quantity: Decimal
    pending_orders: tuple[TargetOrderState, ...] = ()

    @property
    def net_quantity(self) -> Decimal:
        return self.long_quantity - self.short_quantity

    @property
    def has_opposing_exposure(self) -> bool:
        return self.long_quantity > 0 and self.short_quantity > 0


@dataclass(frozen=True)
class BridgeCommand:
    command_id: str
    command_type: CommandType
    target_symbol: str
    source_event_id: int
    source_order_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionReceipt:
    command_id: str
    status: OperationStatus
    target_reference: str | None = None
    message: str = ""


@dataclass(frozen=True)
class OperationRecord:
    command: BridgeCommand
    status: OperationStatus
    target_reference: str | None = None
    message: str = ""


@dataclass(frozen=True)
class CoordinationResult:
    sequence_status: SequenceStatus
    source_state: SourceSymbolState
    commands: tuple[BridgeCommand, ...] = ()
    receipts: tuple[ExecutionReceipt, ...] = ()
    reconciliation_required: bool = False
    reason: str | None = None
