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
    _source_account: str = ""
    _orders_complete: bool = False
    _explicitly_removed_order_ids: set[str] = field(default_factory=set)
    _dispatchable_order_ids: set[str] = field(default_factory=set)

    @property
    def has_synchronized_snapshot(self) -> bool:
        return self._session_id is not None and self._synchronized

    def apply(self, event: SourceEvent) -> SequenceStatus:
        if event.source_symbol != self.source_symbol:
            raise ValueError(f"event belongs to {event.source_symbol}, expected {self.source_symbol}")

        event_account = event.source_account.strip()
        # ATAS currently exposes the account as a display string such as
        # ``2268517 Bal:120245/114530.75/...``. Margin and available funds
        # change whenever an order is placed or cancelled; comparing the
        # whole display value would turn an ordinary cancellation into a false
        # account switch and discard that cancellation event.
        account_changed = bool(
            event_account
            and self._source_account
            and _account_identity(event_account) != _account_identity(self._source_account)
        )
        if account_changed:
            self._reset_for_session(event.session_id)
            self._source_account = event_account
            if event.event_type != SourceEventType.SNAPSHOT:
                self._last_event_id = event.event_id
                return SequenceStatus.ACCOUNT_RESET
        elif self._session_id is None or event.session_id != self._session_id:
            self._reset_for_session(event.session_id)
            # A new session without an account identity must not inherit a
            # stale portfolio label from the previous connector session.
            self._source_account = event_account
            if event.event_type != SourceEventType.SNAPSHOT:
                self._last_event_id = event.event_id
                return SequenceStatus.SESSION_RESET
        elif event_account:
            # Preserve the raw label for diagnostics while comparing only its
            # stable account identity above.
            self._source_account = event_account

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
            source_account=self._source_account,
            orders_complete=self._orders_complete,
            explicitly_removed_order_ids=frozenset(self._explicitly_removed_order_ids),
            dispatchable_order_ids=frozenset(self._dispatchable_order_ids),
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
        self._source_account = state.source_account
        self._orders_complete = state.orders_complete
        self._explicitly_removed_order_ids = set(state.explicitly_removed_order_ids)
        # A persisted active order may have been cancelled while the bridge
        # was down. Keep it only as non-destructive reconciliation context;
        # a fresh callback or a complete ATAS inventory is required before a
        # new MT5 pending order can be created.
        self._dispatchable_order_ids.clear()

    def _reset_for_session(self, session_id: str) -> None:
        self._session_id = session_id
        self._last_event_id = 0
        self._position = None
        self._active_orders.clear()
        self._synchronized = False
        self._quote = None
        self._position_complete = False
        self._orders_complete = False
        self._explicitly_removed_order_ids.clear()
        self._dispatchable_order_ids.clear()

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
                self._orders_complete = True
                self._explicitly_removed_order_ids.clear()
                self._dispatchable_order_ids = set(self._active_orders)
        elif event.event_type == SourceEventType.POSITION:
            self._position = event.position
            self._position_complete = True
        elif event.event_type == SourceEventType.AUXILIARY and event.quote is not None:
            self._quote = event.quote
        elif event.order is not None:
            if event.order.is_active:
                self._active_orders[event.order.source_order_id] = event.order
                self._explicitly_removed_order_ids.discard(event.order.source_order_id)
                self._dispatchable_order_ids.add(event.order.source_order_id)
            else:
                self._active_orders.pop(event.order.source_order_id, None)
                self._explicitly_removed_order_ids.add(event.order.source_order_id)
                self._dispatchable_order_ids.discard(event.order.source_order_id)


def _account_identity(value: str) -> str:
    """Extract an ATAS account's stable identifier from a display label."""

    normalized = " ".join(value.strip().split())
    if not normalized:
        return ""
    marker = normalized.casefold().find(" bal:")
    if marker >= 0:
        return normalized[:marker].strip()
    # Other connectors may not use the Rithmic-style ``Bal:`` suffix. Their
    # full label remains the conservative identity until adapted explicitly.
    return normalized
