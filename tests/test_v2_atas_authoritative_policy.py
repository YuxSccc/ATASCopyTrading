from __future__ import annotations

from decimal import Decimal

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
)
from atas_mt5_bridge.v2.policy import AtasAuthoritativePolicy


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


def target_state(
    *,
    long: str = "0",
    short: str = "0",
    pending_orders=(),
    positions=(),
    complete: bool = True,
) -> TargetAccountState:
    return TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal(long),
        short_quantity=Decimal(short),
        pending_orders=tuple(pending_orders),
        positions=tuple(positions),
        snapshot_complete=complete,
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
