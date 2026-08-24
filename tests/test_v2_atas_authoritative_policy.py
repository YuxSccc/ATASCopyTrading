from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from dataclasses import replace

from atas_mt5_bridge.v2.coordinator import SymbolCoordinator
from atas_mt5_bridge.v2.intent_ledger import IntentLedger, TargetLegSpec
from atas_mt5_bridge.v2.models import (
    CommandType,
    ExecutionReceipt,
    OperationStatus,
    SourceEvent,
    SourceEventType,
    SourceOrder,
    SourceOrderState,
    SourcePosition,
    TargetAccountModel,
    TargetAccountState,
    TargetLegRole,
    TargetLegState,
    TargetOrderState,
    TargetPositionState,
    TargetQuote,
    TargetSymbolSpec,
)
from atas_mt5_bridge.v2.policy import AtasAuthoritativePolicy
from atas_mt5_bridge.v2.pricing import PriceDecision


class MutableExecutionPort:
    def __init__(self, state: TargetAccountState) -> None:
        self.state = state
        self.commands = []
        self.receipt_status = OperationStatus.ACCEPTED
        self.raise_on_read = False
        self.raise_on_execute = False

    def read_state(self, target_symbol: str) -> TargetAccountState:
        if self.raise_on_read:
            raise ConnectionError("MT5 terminal is disconnected")
        return self.state

    def execute(self, command) -> ExecutionReceipt:
        self.commands.append(command)
        if self.raise_on_execute:
            raise TimeoutError("request outcome unavailable")
        return ExecutionReceipt(command.command_id, self.receipt_status, target_reference="target-ref")


class MovingBasisMapper:
    """Returns a different mapped price per call, like a changing basis."""

    def __init__(self) -> None:
        self.calls = 0

    def record_pair(self, source_quote, target_quote) -> None:
        return None

    def to_data(self) -> dict:
        return {}

    def map_pending(self, order, source_quote, target, target_quantity=None) -> PriceDecision:
        self.calls += 1
        return PriceDecision(True, price=Decimal("4000") + Decimal(self.calls), trigger_price=None)


def target_state(
    *,
    long: str = "0",
    short: str = "0",
    pending_orders=(),
    positions=(),
    complete: bool = True,
    quote: TargetQuote | None = None,
    symbol_spec: TargetSymbolSpec | None = None,
) -> TargetAccountState:
    return TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal(long),
        short_quantity=Decimal(short),
        pending_orders=tuple(pending_orders),
        positions=tuple(positions),
        snapshot_complete=complete,
        quote=quote,
        symbol_spec=symbol_spec,
    )


def source_order(order_id: str = "100", *, comment: str = "") -> SourceOrder:
    return SourceOrder(
        source_order_id=order_id,
        source_external_id=None,
        source_symbol="GC",
        side="sell",
        order_type="stop",
        state=SourceOrderState.ACTIVE,
        quantity=Decimal("2"),
        unfilled=Decimal("2"),
        price=Decimal("4000"),
        trigger_price=Decimal("4000"),
        cancelled=False,
        comment=comment,
    )


def event(
    event_id: int,
    event_type: SourceEventType,
    *,
    order: SourceOrder | None = None,
    position: SourcePosition | None = None,
    snapshot_orders=(),
) -> SourceEvent:
    return SourceEvent(
        session_id="session-1",
        event_id=event_id,
        event_type=event_type,
        source_symbol="GC",
        occurred_at_utc="2026-08-08T00:00:00Z",
        order=order,
        position=position,
        snapshot_orders=tuple(snapshot_orders),
    )


def snapshot(*, position: str = "0", orders=()) -> SourceEvent:
    return event(
        1,
        SourceEventType.SNAPSHOT,
        position=SourcePosition("GC", Decimal(position), None),
        snapshot_orders=orders,
    )


def test_active_source_order_creates_one_tracked_target_leg_without_duplicate_submission():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))

    coordinator.handle(snapshot())
    created = coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))

    assert [command.command_type for command in created.commands] == [CommandType.CREATE_PENDING]
    leg = next(iter(coordinator.ledger.legs.values()))
    assert leg.role == TargetLegRole.ENTRY_PENDING
    assert leg.state == TargetLegState.CREATE_PENDING

    # The source may emit another state change before the MT5 order observer
    # catches up. The bridge must not send the same risk-increasing order twice.
    again = coordinator.handle(
        event(3, SourceEventType.POSITION, position=SourcePosition("GC", Decimal("0"), None))
    )
    assert again.commands == ()


def test_basis_refresh_does_not_cancel_or_reprice_an_existing_source_order_instance():
    mapper = MovingBasisMapper()
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, price_mapper=mapper),
    )
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="unchanged-source-order",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4001"),
                trigger_price=None,
                leg_id=leg.leg_id,
            ),
        )
    )
    coordinator.handle_target_observation()

    observed = coordinator.handle(event(3, SourceEventType.POSITION, position=SourcePosition("GC", Decimal("0"), None)))

    assert observed.commands == ()
    assert mapper.calls == 1
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.ACTIVE


def test_atas_authoritative_policy_is_inert_until_execution_is_explicitly_enabled():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy())

    coordinator.handle(snapshot())
    result = coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))

    assert result.commands == ()
    assert port.commands == []


def test_source_cancel_cancels_the_exact_target_leg_after_target_confirms_it_exists():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="mt5-1",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=Decimal("4000"),
                leg_id=leg.leg_id,
            ),
        )
    )
    cancelled_source_order = SourceOrder(**{**source_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True})

    cancelled = coordinator.handle(event(3, SourceEventType.ORDER, order=cancelled_source_order))

    assert [command.command_type for command in cancelled.commands] == [CommandType.CANCEL_PENDING]
    assert cancelled.commands[0].leg_id == leg.leg_id


def test_pending_target_snapshot_does_not_duplicate_an_in_flight_cancel():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="mt5-1",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=Decimal("4000"),
                leg_id=leg.leg_id,
            ),
        )
    )
    coordinator.handle_target_observation()
    cancelled_source_order = SourceOrder(
        **{**source_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True}
    )

    first = coordinator.handle(event(3, SourceEventType.ORDER, order=cancelled_source_order))
    repeated_snapshot = coordinator.handle_target_observation()

    assert [command.command_type for command in first.commands] == [CommandType.CANCEL_PENDING]
    assert repeated_snapshot.commands == ()
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.CANCEL_PENDING


def test_intent_ledger_requires_leg_id_when_one_source_order_has_many_target_legs():
    ledger = IntentLedger()
    state = snapshot(orders=(source_order(),))
    # Use the production SourceStateBook path rather than manufacturing an
    # incompatible snapshot object.
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port)
    coordinator.handle(state)
    ledger.synchronize_source(coordinator.source_book.snapshot())
    intent = ledger.active_intents()[0]
    legs = ledger.ensure_legs(
        intent.intent_id,
        (
            TargetLegSpec(TargetLegRole.REDUCE_PENDING, "sell", "stop", Decimal("1"), Decimal("4000"), Decimal("4000")),
            TargetLegSpec(TargetLegRole.ENTRY_PENDING, "sell", "stop", Decimal("1"), Decimal("4000"), Decimal("4000")),
        ),
    )

    ledger.synchronize_target(
        target_state(
            pending_orders=(
                TargetOrderState(
                    ticket="ambiguous",
                    side="sell",
                    order_type="stop",
                    quantity=Decimal("1"),
                    price=Decimal("4000"),
                    trigger_price=Decimal("4000"),
                    source_order_id="100",
                ),
            )
        )
    )

    assert all(ledger.get_leg(leg.leg_id).state == TargetLegState.DESIRED for leg in legs)


def test_net_equivalent_policy_does_not_force_cleanup_of_correct_hedged_target_net():
    port = MutableExecutionPort(
        target_state(
            long="1",
            short="2",
            positions=(
                TargetPositionState("long-ticket", "long", Decimal("1")),
                TargetPositionState("short-ticket", "short", Decimal("2")),
            ),
        )
    )
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))

    result = coordinator.handle(snapshot(position="-1"))

    assert result.commands == ()


def test_position_repair_uses_ticket_specific_close_before_opening_new_risk():
    port = MutableExecutionPort(
        target_state(
            short="1",
            positions=(TargetPositionState("short-ticket", "short", Decimal("1")),),
        )
    )
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))

    result = coordinator.handle(snapshot(position="2"))

    assert [command.command_type for command in result.commands] == [CommandType.CLOSE_POSITION]
    assert result.commands[0].payload["position_ticket"] == "short-ticket"
    assert result.commands[0].payload["quantity"] == Decimal("1")


def test_target_network_failure_blocks_new_risk_and_recovers_after_complete_snapshot():
    port = MutableExecutionPort(target_state())
    port.raise_on_read = True
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))

    unavailable = coordinator.handle(snapshot(orders=(source_order(),)))
    assert unavailable.reconciliation_required is True
    assert unavailable.commands == ()

    port.raise_on_read = False
    assert coordinator.reconcile_target() is True


def test_unknown_execute_outcome_is_not_retried_even_when_source_has_another_event():
    port = MutableExecutionPort(target_state())
    port.raise_on_execute = True
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())

    first = coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    second = coordinator.handle(
        event(3, SourceEventType.POSITION, position=SourcePosition("GC", Decimal("0"), None))
    )

    assert first.receipts[0].status == OperationStatus.UNKNOWN
    assert second.commands == ()
    assert len(port.commands) == 1


def test_target_observation_finishes_a_cancel_that_arrived_before_create_confirmation():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    cancelled_source_order = SourceOrder(**{**source_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True})

    # MT5 has accepted the create request but has not yet exposed its pending
    # order in the snapshot, so sending a blind cancel is unsafe.
    early_cancel = coordinator.handle(event(3, SourceEventType.ORDER, order=cancelled_source_order))
    assert early_cancel.commands == ()

    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="late-order",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=Decimal("4000"),
                leg_id=leg.leg_id,
            ),
        )
    )
    observed = coordinator.handle_target_observation()

    assert [command.command_type for command in observed.commands] == [CommandType.CANCEL_PENDING]


def test_source_cancel_uses_ticket_from_accepted_create_before_next_snapshot():
    """A just-created pending order must not wait for recovery polling to cancel."""

    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    created = coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    created_command = created.commands[0]
    leg = next(iter(coordinator.ledger.legs.values()))

    # This represents the synchronous EA receipt.  MT5's cached full
    # snapshot is deliberately still old and empty here.
    coordinator.handle_target_receipt(
        ExecutionReceipt(created_command.command_id, OperationStatus.ACCEPTED, target_reference="123456")
    )
    assert coordinator.ledger.get_leg(leg.leg_id).target_order_ticket == "123456"
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.CREATE_PENDING

    cancelled_source_order = SourceOrder(**{**source_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True})
    cancelled = coordinator.handle(event(3, SourceEventType.ORDER, order=cancelled_source_order))

    assert [command.command_type for command in cancelled.commands] == [CommandType.CANCEL_PENDING]
    assert cancelled.commands[0].payload["target_order_ticket"] == "123456"


def test_target_partial_pending_fill_is_aggressively_cancelled_before_net_repair():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="partial-order",
                side="sell",
                order_type="stop",
                quantity=Decimal("1"),
                price=Decimal("4000"),
                trigger_price=Decimal("4000"),
                leg_id=leg.leg_id,
            ),
        )
    )

    result = coordinator.handle_target_observation()

    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.CANCEL_PENDING
    assert [command.command_type for command in result.commands] == [CommandType.CANCEL_PENDING]


def test_unknown_cancel_is_confirmed_when_a_complete_snapshot_has_no_matching_order():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="existing-order",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=Decimal("4000"),
                leg_id=leg.leg_id,
            ),
        )
    )
    coordinator.handle_target_observation()
    port.raise_on_execute = True
    cancelled_source_order = SourceOrder(**{**source_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True})
    cancelled = coordinator.handle(event(3, SourceEventType.ORDER, order=cancelled_source_order))
    assert cancelled.receipts[0].status == OperationStatus.UNKNOWN
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.UNKNOWN

    port.raise_on_execute = False
    port.state = target_state()
    observed = coordinator.handle_target_observation()

    assert observed.reconciliation_required is False
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.CANCELLED


def test_active_target_pending_order_missing_from_complete_snapshot_quarantines_the_symbol():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="existing-order",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=None,
                leg_id=leg.leg_id,
            ),
        )
    )
    coordinator.handle_target_observation()
    port.state = target_state()

    observed = coordinator.handle_target_observation()

    assert observed.reconciliation_required is True
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.MISSING_ON_TARGET


def _activate_target_pending_leg(coordinator: SymbolCoordinator, port: MutableExecutionPort):
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order()))
    leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="pending-fill-race",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=Decimal("4000"),
                leg_id=leg.leg_id,
            ),
        )
    )
    coordinator.handle_target_observation()
    assert coordinator.ledger.get_leg(leg.leg_id).state == TargetLegState.ACTIVE
    return leg


def _target_first_fill_state():
    return target_state(
        short="2",
        positions=(TargetPositionState("target-short", "short", Decimal("2")),),
    )


def test_target_first_pending_fill_gets_bounded_grace_without_delaying_other_repairs():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    leg = _activate_target_pending_leg(coordinator, port)
    port.state = _target_first_fill_state()

    observed = coordinator.handle_target_observation()

    assert observed is not None
    assert observed.commands == ()
    saved = coordinator.ledger.get_leg(leg.leg_id)
    assert saved is not None
    assert saved.state == TargetLegState.TARGET_FILL_GRACE
    assert saved.target_fill_grace_started_at_utc is not None


def test_target_first_fill_grace_expires_to_an_ordinary_atas_net_repair():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    leg = _activate_target_pending_leg(coordinator, port)
    port.state = _target_first_fill_state()
    coordinator.handle_target_observation()
    grace_leg = coordinator.ledger.get_leg(leg.leg_id)
    assert grace_leg is not None
    coordinator.ledger.update_leg(
        replace(
            grace_leg,
            target_fill_grace_started_at_utc=(datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat(),
        )
    )

    expired = coordinator.handle_target_observation()

    assert expired is not None
    assert [command.command_type for command in expired.commands] == [CommandType.CLOSE_POSITION]
    assert expired.commands[0].payload["position_ticket"] == "target-short"
    assert expired.commands[0].payload["quantity"] == Decimal("2")


def test_explicit_atas_cancellation_ends_target_first_fill_grace_immediately():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    leg = _activate_target_pending_leg(coordinator, port)
    port.state = _target_first_fill_state()
    coordinator.handle_target_observation()
    cancelled = SourceOrder(**{**source_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True})

    result = coordinator.handle(event(3, SourceEventType.ORDER, order=cancelled))

    assert [command.command_type for command in result.commands] == [CommandType.CLOSE_POSITION]
    saved = coordinator.ledger.get_leg(leg.leg_id)
    assert saved is not None
    assert saved.state == TargetLegState.CANCELLED


def test_atas_tp_sl_comments_map_to_native_mt5_position_protection_not_opposite_pending_orders():
    quote = TargetQuote("XAUUSD", Decimal("4000"), Decimal("4000.2"), "2026-08-08T00:00:00Z")
    symbol_spec = TargetSymbolSpec(
        tick_size=Decimal("0.1"),
        volume_min=Decimal("1"),
        volume_max=Decimal("100"),
        volume_step=Decimal("1"),
        stops_level=Decimal("0.1"),
        freeze_level=Decimal("0.1"),
    )
    protection_target = target_state(
        long="2",
        positions=(TargetPositionState("long-ticket", "long", Decimal("2")),),
        quote=quote,
        symbol_spec=symbol_spec,
    )
    port = MutableExecutionPort(protection_target)
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    stop_loss = SourceOrder(**{**source_order("sl").__dict__, "comment": "SL", "trigger_price": Decimal("3991"), "price": Decimal("0")})
    take_profit = SourceOrder(
        **{
            **source_order("tp").__dict__,
            "comment": "TP",
            "order_type": "limit",
            "price": Decimal("4007"),
            "trigger_price": None,
        }
    )

    result = coordinator.handle(snapshot(position="2", orders=(stop_loss, take_profit)))

    assert [command.command_type for command in result.commands] == [CommandType.MODIFY_POSITION_PROTECTION]
    assert result.commands[0].payload == {
        "position_ticket": "long-ticket",
        "stop_loss": Decimal("3991"),
        "take_profit": Decimal("4007"),
    }


def test_incomplete_reconnect_snapshot_never_clears_existing_mt5_protection():
    quote = TargetQuote("XAUUSD", Decimal("4000"), Decimal("4000.2"), "2026-08-08T00:00:00Z")
    symbol_spec = TargetSymbolSpec(
        tick_size=Decimal("0.1"),
        volume_min=Decimal("1"),
        volume_max=Decimal("100"),
        volume_step=Decimal("1"),
        stops_level=Decimal("0.1"),
        freeze_level=Decimal("0.1"),
    )
    port = MutableExecutionPort(
        target_state(
            long="2",
            positions=(TargetPositionState("long-ticket", "long", Decimal("2"), Decimal("3991"), Decimal("4007")),),
            quote=quote,
            symbol_spec=symbol_spec,
        )
    )
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    incomplete = SourceEvent(
        session_id="reconnected-session",
        event_id=1,
        event_type=SourceEventType.SNAPSHOT,
        source_symbol="GC",
        occurred_at_utc="2026-08-08T00:00:00Z",
        position=SourcePosition("GC", Decimal("2"), None),
        snapshot_orders=(),
        snapshot_orders_complete=False,
        position_complete=True,
    )

    result = coordinator.handle(incomplete)

    assert result.commands == ()


def test_incomplete_inventory_can_move_observed_sl_without_clearing_unseen_tp():
    quote = TargetQuote("XAUUSD", Decimal("4000"), Decimal("4000.2"), "2026-08-08T00:00:00Z")
    symbol_spec = TargetSymbolSpec(
        tick_size=Decimal("0.1"), volume_min=Decimal("1"), volume_max=Decimal("100"),
        volume_step=Decimal("1"), stops_level=Decimal("0.1"), freeze_level=Decimal("0.1"),
    )
    port = MutableExecutionPort(
        target_state(
            long="2",
            positions=(TargetPositionState("long-ticket", "long", Decimal("2"), Decimal("3991"), Decimal("4007")),),
            quote=quote,
            symbol_spec=symbol_spec,
        )
    )
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(
        SourceEvent(
            session_id="reconnected-session", event_id=1, event_type=SourceEventType.SNAPSHOT,
            source_symbol="GC", occurred_at_utc="2026-08-08T00:00:00Z",
            position=SourcePosition("GC", Decimal("2"), None), snapshot_orders=(),
            snapshot_orders_complete=False, position_complete=True,
        )
    )
    moved_stop = SourceOrder(**{**source_order("sl").__dict__, "comment": "SL", "trigger_price": Decimal("3992")})

    result = coordinator.handle(
        SourceEvent(
            session_id="reconnected-session", event_id=2, event_type=SourceEventType.ORDER,
            source_symbol="GC", occurred_at_utc="2026-08-08T00:00:01Z", order=moved_stop,
        )
    )

    assert [command.command_type for command in result.commands] == [CommandType.MODIFY_POSITION_PROTECTION]
    assert result.commands[0].payload["stop_loss"] == Decimal("3992")
    assert result.commands[0].payload["take_profit"] == Decimal("4007")


def test_same_atas_order_id_price_change_cancels_confirmed_target_leg_then_versions_replacement():
    port = MutableExecutionPort(target_state())
    coordinator = SymbolCoordinator("GC", "XAUUSD", port, policy=AtasAuthoritativePolicy(execution_enabled=True))
    coordinator.handle(snapshot())
    coordinator.handle(event(2, SourceEventType.ORDER, order=source_order("same-id")))
    original_leg = next(iter(coordinator.ledger.legs.values()))
    port.state = target_state(
        pending_orders=(
            TargetOrderState(
                ticket="old-ticket",
                side="sell",
                order_type="stop",
                quantity=Decimal("2"),
                price=Decimal("4000"),
                trigger_price=None,
                leg_id=original_leg.leg_id,
            ),
        )
    )
    coordinator.handle_target_observation()
    moved = SourceOrder(**{**source_order("same-id").__dict__, "trigger_price": Decimal("3999")})

    cancel = coordinator.handle(event(3, SourceEventType.ORDER, order=moved))

    assert [command.command_type for command in cancel.commands] == [CommandType.CANCEL_PENDING]
    port.state = target_state()
    replacement = coordinator.handle_target_observation()
    assert [command.command_type for command in replacement.commands] == [CommandType.CREATE_PENDING]
    assert replacement.commands[0].leg_id != original_leg.leg_id
    assert replacement.commands[0].payload["price"] == Decimal("3999")
