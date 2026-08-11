from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    SequenceStatus,
    SourceEvent,
    SourceEventType,
    SourceOrder,
    SourcePosition,
    SourceQuote,
    SourceSymbolState,
)


@dataclass
class SourceStateBook:
    """Canonical ordered state for one ATAS source symbol.

    A gap deliberately blocks command planning until a full snapshot arrives.
    This prevents a lost cancellation or fill from being treated as a valid
    desired state.
    """

    source_symbol: str
    _session_id: str | None = None
    _last_event_id: int = 0
    _position: SourcePosition | None = None
    _active_orders: dict[str, SourceOrder] = field(default_factory=dict)
    _synchronized: bool = False
    _quote: SourceQuote | None = None
    _position_complete: bool = False

    @property
    def has_synchronized_snapshot(self) -> bool:
        return self._session_id is not None and self._synchronized

    def apply(self, event: SourceEvent) -> SequenceStatus:
        if event.source_symbol != self.source_symbol:
            raise ValueError(f"event belongs to {event.source_symbol}, expected {self.source_symbol}")

        if self._session_id is None or event.session_id != self._session_id:
            self._reset_for_session(event.session_id)
            if event.event_type != SourceEventType.SNAPSHOT:
                self._last_event_id = event.event_id
                return SequenceStatus.SESSION_RESET

        if event.event_id <= self._last_event_id:
            return SequenceStatus.DUPLICATE_OR_OLD

        if event.event_type != SourceEventType.SNAPSHOT and not self._synchronized:
            self._last_event_id = event.event_id
            return SequenceStatus.SNAPSHOT_REQUIRED

        if event.event_type != SourceEventType.SNAPSHOT and event.event_id != self._last_event_id + 1:
            self._last_event_id = event.event_id
            self._synchronized = False
            return SequenceStatus.GAP_DETECTED

        self._apply_payload(event)
        self._last_event_id = event.event_id
        if event.event_type == SourceEventType.SNAPSHOT:
            self._synchronized = True
        return SequenceStatus.APPLIED

    def snapshot(self) -> SourceSymbolState:
        if self._session_id is None:
            raise RuntimeError("source session has not started")
        return SourceSymbolState(
            source_symbol=self.source_symbol,
            session_id=self._session_id,
            last_event_id=self._last_event_id,
            position=self._position,
            active_orders=dict(self._active_orders),
            synchronized=self._synchronized,
            quote=self._quote,
            position_complete=self._position_complete,
        )

    def restore(self, state: SourceSymbolState) -> None:
        """Restore only a durable canonical checkpoint for this source symbol."""

        if state.source_symbol != self.source_symbol:
            raise ValueError(f"checkpoint belongs to {state.source_symbol}, expected {self.source_symbol}")
        self._session_id = state.session_id
        self._last_event_id = state.last_event_id
        self._position = state.position
        self._active_orders = dict(state.active_orders)
        self._synchronized = state.synchronized
        self._quote = state.quote
        self._position_complete = state.position_complete

    def _reset_for_session(self, session_id: str) -> None:
        self._session_id = session_id
        self._last_event_id = 0
        self._position = None
        self._active_orders.clear()
        self._synchronized = False
        self._quote = None
        self._position_complete = False

    def _apply_payload(self, event: SourceEvent) -> None:
        if event.event_type == SourceEventType.SNAPSHOT:
            self._position = event.position
            self._position_complete = event.position_complete if event.position_complete is not None else event.position is not None
            if event.snapshot_orders_complete:
                self._active_orders = {
                    order.source_order_id: order
                    for order in event.snapshot_orders
                    if order.is_active
                }
        elif event.event_type == SourceEventType.POSITION:
            self._position = event.position
            self._position_complete = True
        elif event.event_type == SourceEventType.AUXILIARY and event.quote is not None:
            self._quote = event.quote
        elif event.order is not None:
            if event.order.is_active:
                self._active_orders[event.order.source_order_id] = event.order
            else:
                self._active_orders.pop(event.order.source_order_id, None)
