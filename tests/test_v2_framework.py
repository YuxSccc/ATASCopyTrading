from __future__ import annotations

from decimal import Decimal

from atas_mt5_bridge.v2.coordinator import SymbolCoordinator
from atas_mt5_bridge.v2.atas_adapter import parse_probe_message
from atas_mt5_bridge.v2.models import (
    BridgeCommand,
    CommandType,
    ExecutionReceipt,
    OperationStatus,
    SequenceStatus,
    SourceEvent,
    SourceEventType,
    SourceOrder,
    SourceOrderState,
    SourcePosition,
    TargetAccountModel,
    TargetAccountState,
)


class FakeExecutionPort:
    def __init__(self, status: OperationStatus = OperationStatus.ACCEPTED) -> None:
        self.status = status
        self.commands: list[BridgeCommand] = []

    def read_state(self, target_symbol: str) -> TargetAccountState:
        return TargetAccountState(
            target_symbol=target_symbol,
            model=TargetAccountModel.HEDGING,
            long_quantity=Decimal("0"),
            short_quantity=Decimal("0"),
        )

    def execute(self, command: BridgeCommand) -> ExecutionReceipt:
        self.commands.append(command)
        return ExecutionReceipt(command_id=command.command_id, status=self.status, target_reference="fake-ticket")


class OneCommandPolicy:
    def plan(self, source, target, ledger) -> tuple[BridgeCommand, ...]:
        return (
            BridgeCommand(
                command_id=f"command-{source.last_event_id}",
                command_type=CommandType.RECONCILE,
                target_symbol=target.target_symbol,
                source_event_id=source.last_event_id,
            ),
        )


def order_event(event_id: int, order: SourceOrder) -> SourceEvent:
    return SourceEvent(
        session_id="session-a",
        event_id=event_id,
        event_type=SourceEventType.ORDER,
        source_symbol="COMEX Gold",
        occurred_at_utc="2026-08-08T04:00:00Z",
        order=order,
    )


def snapshot_event(event_id: int = 1) -> SourceEvent:
    return SourceEvent(
        session_id="session-a",
        event_id=event_id,
        event_type=SourceEventType.SNAPSHOT,
        source_symbol="COMEX Gold",
        occurred_at_utc="2026-08-08T04:00:00Z",
        position=SourcePosition("COMEX Gold", Decimal("0"), None),
    )


def active_order(order_id: str = "83", price: str = "3987.6") -> SourceOrder:
    return SourceOrder(
        source_order_id=order_id,
        source_external_id="43087340",
        source_symbol="COMEX Gold",
        side="buy",
        order_type="limit",
        state=SourceOrderState.ACTIVE,
        quantity=Decimal("1"),
        unfilled=Decimal("1"),
        price=Decimal(price),
        trigger_price=None,
        cancelled=False,
    )


def test_cancelled_source_order_is_removed_without_replacement_assumption():
    port = FakeExecutionPort()
    coordinator = SymbolCoordinator("COMEX Gold", "XAUUSD", port)

    assert coordinator.handle(snapshot_event()).sequence_status == SequenceStatus.APPLIED
    coordinator.handle(order_event(2, active_order()))
    cancelled = SourceOrder(
        **{**active_order().__dict__, "state": SourceOrderState.DONE, "cancelled": True}
    )

    result = coordinator.handle(order_event(3, cancelled))

    assert result.sequence_status == SequenceStatus.APPLIED
    assert result.source_state.active_orders == {}
    assert result.commands == ()


def test_new_order_after_cancellation_is_an_independent_instance():
    port = FakeExecutionPort()
    coordinator = SymbolCoordinator("COMEX Gold", "XAUUSD", port)
    coordinator.handle(snapshot_event())
    coordinator.handle(order_event(2, active_order("83", "3987.6")))
    cancelled = SourceOrder(
        **{**active_order("83", "3987.6").__dict__, "state": SourceOrderState.DONE, "cancelled": True}
    )
    coordinator.handle(order_event(3, cancelled))

    result = coordinator.handle(order_event(4, active_order("84", "3986.2")))

    assert set(result.source_state.active_orders) == {"84"}
    assert result.source_state.active_orders["84"].price == Decimal("3986.2")


def test_gap_blocks_planning_until_a_snapshot_arrives():
    port = FakeExecutionPort()
    coordinator = SymbolCoordinator("COMEX Gold", "XAUUSD", port, policy=OneCommandPolicy())
    coordinator.handle(snapshot_event())

    gap = coordinator.handle(order_event(3, active_order()))
    blocked = coordinator.handle(order_event(4, active_order("84")))
    recovered = coordinator.handle(snapshot_event(5))

    assert gap.sequence_status == SequenceStatus.GAP_DETECTED
    assert blocked.sequence_status == SequenceStatus.SNAPSHOT_REQUIRED
    assert recovered.sequence_status == SequenceStatus.APPLIED
    assert len(port.commands) == 2


def test_failed_target_command_requires_manual_reconciliation():
    port = FakeExecutionPort(status=OperationStatus.FAILED)
    coordinator = SymbolCoordinator("COMEX Gold", "XAUUSD", port, policy=OneCommandPolicy())

    first = coordinator.handle(snapshot_event())
    second = coordinator.handle(order_event(2, active_order()))

    assert first.reconciliation_required is True
    assert first.receipts[0].status == OperationStatus.FAILED
    assert second.commands == ()
    assert second.reconciliation_required is True
    assert len(port.commands) == 1


def test_target_state_preserves_hedged_exposure_for_future_policy():
    state = TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal("0.20"),
        short_quantity=Decimal("0.05"),
    )

    assert state.net_quantity == Decimal("0.15")
    assert state.has_opposing_exposure is True


def test_auxiliary_probe_event_preserves_sequence_without_planning():
    port = FakeExecutionPort()
    coordinator = SymbolCoordinator("COMEX Gold", "XAUUSD", port, policy=OneCommandPolicy())
    coordinator.handle(snapshot_event())
    quote = parse_probe_message(
        {
            "session_id": "session-a",
            "event_id": 2,
            "event_type": "quote",
            "source_symbol": "COMEX Gold",
            "atas_timestamp_utc": "2026-08-08T04:00:01Z",
            "data": {},
        }
    )

    auxiliary = coordinator.handle(quote)
    order = coordinator.handle(order_event(3, active_order()))

    assert auxiliary.sequence_status == SequenceStatus.APPLIED
    assert auxiliary.commands == ()
    assert order.sequence_status == SequenceStatus.APPLIED
    assert len(port.commands) == 2
