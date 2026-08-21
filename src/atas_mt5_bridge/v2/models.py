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
    CLOSE_POSITION = "close_position"
    MODIFY_POSITION_PROTECTION = "modify_position_protection"
    RECONCILE = "reconcile"


class OperationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    OBSERVED_NO_EFFECT = "observed_no_effect"


class SequenceStatus(str, Enum):
    APPLIED = "applied"
    DUPLICATE_OR_OLD = "duplicate_or_old"
    GAP_DETECTED = "gap_detected"
    SNAPSHOT_REQUIRED = "snapshot_required"
    SESSION_RESET = "session_reset"
    ACCOUNT_RESET = "account_reset"


class IntentState(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"


class TargetLegRole(str, Enum):
    ENTRY_PENDING = "entry_pending"
    REDUCE_PENDING = "reduce_pending"
    PROTECTIVE_TP = "protective_tp"
    PROTECTIVE_SL = "protective_sl"
    MARKET_RECONCILE = "market_reconcile"
    CLEANUP = "cleanup"


class TargetLegState(str, Enum):
    DESIRED = "desired"
    CREATE_PENDING = "create_pending"
    ACTIVE = "active"
    PARTIAL_FILL = "partial_fill"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    FILLED = "filled"
    MISSING_ON_TARGET = "missing_on_target"
    # A complete target snapshot proves a bridge pending order disappeared,
    # and its directional net change matches that order while ATAS is still
    # reporting it active.  This bounded state avoids a close/re-open cycle.
    TARGET_FILL_GRACE = "target_fill_grace"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceOrder:
    """A single ATAS order instance, not a user-level trading intention.

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
class SourceQuote:
    source_symbol: str
    bid: Decimal | None
    ask: Decimal | None
    occurred_at_utc: str

    @property
    def is_complete(self) -> bool:
        return self.bid is not None and self.ask is not None


@dataclass(frozen=True)
class SourceEvent:
    session_id: str
    event_id: int
    event_type: SourceEventType
    source_symbol: str
    occurred_at_utc: str
    source_account: str = ""
    order: SourceOrder | None = None
    position: SourcePosition | None = None
    quote: SourceQuote | None = None
    snapshot_orders: tuple[SourceOrder, ...] = ()
    # A probe may know the net position at startup while ATAS has not exposed
    # a complete enumeration of manually created pending orders. Such a
    # snapshot establishes event sequencing but must not delete orders already
    # observed through callbacks in the same session.
    snapshot_orders_complete: bool = True
    position_complete: bool | None = None

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
    quote: SourceQuote | None = None
    position_complete: bool = False
    source_account: str = ""
    # ``False`` means ATAS has only provided callback-derived order events.
    # In particular, a ChartStrategy restart cannot enumerate manually placed
    # chart orders.  Absence from ``active_orders`` must therefore not be
    # interpreted as a cancellation while this flag is false.
    orders_complete: bool = False
    # Order ids removed by an explicit ATAS callback.  This is deliberately
    # separate from an absent order in an incomplete recovery snapshot.
    explicitly_removed_order_ids: frozenset[str] = frozenset()
    # Orders which this running bridge process has observed as active through
    # a direct callback (or a genuinely complete ATAS inventory).  Persisted
    # callback state is useful to avoid blind target cancellation after a
    # restart, but is never sufficient authority to create a fresh target
    # pending order again.
    dispatchable_order_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SourceOrderKey:
    """Stable source-instance key. ExtId is intentionally not part of it."""

    session_id: str
    source_symbol: str
    source_order_id: str

    @property
    def value(self) -> str:
        return f"{self.session_id}:{self.source_symbol}:{self.source_order_id}"


@dataclass(frozen=True)
class BridgeIntent:
    """One source order's desired target outcome, possibly implemented by many legs."""

    intent_id: str
    source_key: SourceOrderKey
    order: SourceOrder
    state: IntentState


@dataclass(frozen=True)
class TargetLeg:
    """One target-side operation belonging to a source order intent."""

    leg_id: str
    intent_id: str
    source_key: SourceOrderKey
    role: TargetLegRole
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trigger_price: Decimal | None
    state: TargetLegState = TargetLegState.DESIRED
    target_order_ticket: str | None = None
    target_position_ticket: str | None = None
    bridge_tag: str = ""
    # Persisted wall time: restarting the bridge must not create a fresh
    # target-fill grace interval.
    target_fill_grace_started_at_utc: str | None = None

    @property
    def requires_target_order(self) -> bool:
        return self.role in {
            TargetLegRole.ENTRY_PENDING,
            TargetLegRole.REDUCE_PENDING,
            TargetLegRole.PROTECTIVE_TP,
            TargetLegRole.PROTECTIVE_SL,
        }


@dataclass(frozen=True)
class TargetOrderState:
    ticket: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trigger_price: Decimal | None
    source_order_id: str | None = None
    leg_id: str | None = None
    bridge_tag: str | None = None


@dataclass(frozen=True)
class TargetPositionState:
    """A bridge-owned target position. HEDGING snapshots must include tickets."""

    ticket: str
    side: str
    quantity: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


@dataclass(frozen=True)
class TargetQuote:
    target_symbol: str
    bid: Decimal | None
    ask: Decimal | None
    occurred_at_utc: str

    @property
    def is_complete(self) -> bool:
        return self.bid is not None and self.ask is not None


@dataclass(frozen=True)
class TargetSymbolSpec:
    tick_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level: Decimal
    freeze_level: Decimal


@dataclass(frozen=True)
class TargetAccountState:
    """Observed bridge-owned state only; unrelated manual MT5 objects stay outside it."""

    target_symbol: str
    model: TargetAccountModel
    long_quantity: Decimal
    short_quantity: Decimal
    pending_orders: tuple[TargetOrderState, ...] = ()
    positions: tuple[TargetPositionState, ...] = ()
    snapshot_complete: bool = True
    quote: TargetQuote | None = None
    symbol_spec: TargetSymbolSpec | None = None
    foreign_symbol_objects_present: bool = False

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
    intent_id: str | None = None
    leg_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def increases_risk(self) -> bool:
        return self.command_type in {CommandType.CREATE_PENDING, CommandType.EXECUTE_MARKET}


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
    dispatched_at_utc: str | None = None
    resolved_at_utc: str | None = None


@dataclass(frozen=True)
class CoordinationResult:
    sequence_status: SequenceStatus
    source_state: SourceSymbolState
    commands: tuple[BridgeCommand, ...] = ()
    receipts: tuple[ExecutionReceipt, ...] = ()
    reconciliation_required: bool = False
    reason: str | None = None
