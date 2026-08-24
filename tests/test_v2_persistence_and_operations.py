from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from atas_mt5_bridge.v2.coordinator import SymbolCoordinator
from atas_mt5_bridge.v2.intent_ledger import IntentLedger
from atas_mt5_bridge.v2.models import (
    BridgeCommand,
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
    TargetOrderState,
)
from atas_mt5_bridge.v2.operation_book import OperationBook
from atas_mt5_bridge.v2.persistence import BridgeStateStore, NullBridgeStateStore, SqliteBridgeStateStore
from atas_mt5_bridge.v2.policy import AtasAuthoritativePolicy
from atas_mt5_bridge.v2.simulation import SimulatedTargetExecutionPort


class StaticTargetPort:
    def __init__(self, state: TargetAccountState, receipt_status: OperationStatus = OperationStatus.ACCEPTED) -> None:
        self.state = state
        self.receipt_status = receipt_status
        self.commands = []

    def read_state(self, target_symbol: str) -> TargetAccountState:
        return self.state

    def execute(self, command) -> ExecutionReceipt:
        self.commands.append(command)
        return ExecutionReceipt(command.command_id, self.receipt_status)


class RaisingExecutePort(StaticTargetPort):
    def execute(self, command) -> ExecutionReceipt:
        self.commands.append(command)
        raise TimeoutError("terminal connection reset after request dispatch")


class FailingOperationStore(NullBridgeStateStore):
    def save_ledger_and_operations(self, target_symbol, ledger, operations) -> None:
        if tuple(operations):
            raise OSError("disk full")


def target_state(*, long: str = "0", pending_orders=()) -> TargetAccountState:
    return TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal(long),
        short_quantity=Decimal("0"),
        pending_orders=tuple(pending_orders),
    )


def source_event(event_id: int, event_type: SourceEventType, *, position: str = "0", order: SourceOrder | None = None) -> SourceEvent:
    return SourceEvent(
        session_id="session-persist",
        event_id=event_id,
        event_type=event_type,
        source_symbol="GC",
        occurred_at_utc="2026-08-09T00:00:00Z",
        position=SourcePosition("GC", Decimal(position), None) if event_type in {SourceEventType.SNAPSHOT, SourceEventType.POSITION} else None,
        order=order,
    )


def source_order() -> SourceOrder:
    return SourceOrder(
        source_order_id="order-persist",
        source_external_id=None,
        source_symbol="GC",
        side="buy",
        order_type="limit",
        state=SourceOrderState.ACTIVE,
        quantity=Decimal("1"),
        unfilled=Decimal("1"),
        price=Decimal("4000"),
        trigger_price=None,
        cancelled=False,
    )


def test_failed_cancel_is_released_only_when_a_complete_snapshot_proves_order_absent():
    book = OperationBook()
    command = BridgeCommand(
        command_id="cancel-failed",
        command_type=CommandType.CANCEL_PENDING,
        target_symbol="XAUUSD",
        source_event_id=1,
        source_order_id="source-order",
        intent_id="intent",
        leg_id="missing-leg",
        payload={"target_order_ticket": "123"},
    )
    book.dispatch(command)
    book.apply_receipt(ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="OrderSend failed"))

    incomplete = TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal("0"),
        short_quantity=Decimal("0"),
        snapshot_complete=False,
    )
    assert book.resolve_from_snapshot(incomplete, IntentLedger()) == ()

    resolved = book.resolve_from_snapshot(target_state(), IntentLedger())
    assert len(resolved) == 1
    assert resolved[0].status == OperationStatus.CONFIRMED


def test_failed_ticket_close_is_released_only_when_complete_snapshot_proves_position_absent():
    book = OperationBook()
    command = BridgeCommand(
        command_id="close-failed",
        command_type=CommandType.CLOSE_POSITION,
        target_symbol="XAUUSD",
        source_event_id=1,
        payload={
            "position_ticket": "123",
            "quantity": Decimal("0.05"),
            "expected_position_remaining": Decimal("0"),
        },
    )
    book.dispatch(command)
    book.apply_receipt(ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="OrderSend failed"))

    incomplete = TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal("0"),
        short_quantity=Decimal("0"),
        snapshot_complete=False,
    )
    assert book.resolve_from_snapshot(incomplete, IntentLedger()) == ()

    resolved = book.resolve_from_snapshot(target_state(), IntentLedger())
    assert len(resolved) == 1
    assert resolved[0].status == OperationStatus.CONFIRMED


def test_confirmed_cancel_resolves_an_unobserved_create_for_the_same_leg():
    """A fast ATAS cancel can remove an EA-accepted create before a snapshot."""

    book = OperationBook()
    create = BridgeCommand(
        command_id="create-before-snapshot",
        command_type=CommandType.CREATE_PENDING,
        target_symbol="XAUUSD",
        source_event_id=1,
        intent_id="intent",
        leg_id="leg",
    )
    cancel = BridgeCommand(
        command_id="cancel-before-snapshot",
        command_type=CommandType.CANCEL_PENDING,
        target_symbol="XAUUSD",
        source_event_id=2,
        intent_id="intent",
        leg_id="leg",
        payload={"target_order_ticket": "123"},
    )
    book.dispatch(create)
    book.apply_receipt(ExecutionReceipt(create.command_id, OperationStatus.ACCEPTED, target_reference="123"))
    book.dispatch(cancel)
    book.apply_receipt(ExecutionReceipt(cancel.command_id, OperationStatus.ACCEPTED, target_reference="123"))

    cancel_resolution = book.resolve_from_snapshot(target_state(), IntentLedger())
    superseded = book.resolve_creates_superseded_by_cancellation(target_state(), IntentLedger(), cancel_resolution)

    assert [record.command.command_id for record in cancel_resolution] == [cancel.command_id]
    assert [record.command.command_id for record in superseded] == [create.command_id]
    assert book.has_in_flight() is False

    # On a bridge restart the cancellation is already persisted as resolved;
    # the same complete snapshot must still clear the formerly unknown create.
    restarted = OperationBook()
    restarted.restore(book.records.values())
    restarted.records[create.command_id] = replace(
        restarted.records[create.command_id], status=OperationStatus.UNKNOWN, resolved_at_utc=None
    )
    recovered = restarted.resolve_creates_superseded_by_cancellation(target_state(), IntentLedger(), ())
    assert [record.command.command_id for record in recovered] == [create.command_id]


def test_market_reconciliation_is_not_repeated_before_observed_target_confirmation():
    port = StaticTargetPort(target_state())
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, direct_pending_enabled=False),
    )

    first = coordinator.handle(source_event(1, SourceEventType.SNAPSHOT, position="1"))
    second = coordinator.handle(source_event(2, SourceEventType.POSITION, position="1"))

    assert len(first.commands) == 1
    assert second.commands == ()
    assert len(port.commands) == 1

    port.state = target_state(long="1")
    observed = coordinator.handle_target_observation()
    assert observed.reconciliation_required is False
    assert coordinator.operation_book.has_in_flight() is False


def test_sqlite_restart_restores_source_intent_leg_and_pending_operation(tmp_path: Path):
    database_path = tmp_path / "bridge.db"
    store = SqliteBridgeStateStore(database_path)
    simulator = SimulatedTargetExecutionPort("XAUUSD")
    first = SymbolCoordinator(
        "GC",
        "XAUUSD",
        simulator,
        policy=AtasAuthoritativePolicy(execution_enabled=True),
        state_store=store,
    )
    first.handle(source_event(1, SourceEventType.SNAPSHOT))
    created = first.handle(source_event(2, SourceEventType.ORDER, order=source_order()))
    leg_id = created.commands[0].leg_id
    store.close()

    restarted_store = SqliteBridgeStateStore(database_path)
    restarted = SymbolCoordinator(
        "GC",
        "XAUUSD",
        simulator,
        policy=AtasAuthoritativePolicy(execution_enabled=True),
        state_store=restarted_store,
    )

    assert restarted.source_book.snapshot().last_event_id == 2
    assert restarted.ledger.get_leg(leg_id) is not None
    assert restarted.operation_book.has_in_flight() is True

    resolved = restarted.handle_target_observation()
    assert resolved.reconciliation_required is False
    assert restarted.operation_book.has_in_flight() is False
    restarted_store.close()


def test_sqlite_persists_market_state_for_basis_history_restart(tmp_path: Path):
    database_path = tmp_path / "market.db"
    store = SqliteBridgeStateStore(database_path)
    payload = {"samples": [{"buy_basis": "10", "sell_basis": "10"}]}
    store.save_market_state("XAUUSD", payload)
    store.close()

    restarted = SqliteBridgeStateStore(database_path)
    assert restarted.load_market_state("XAUUSD") == payload
    restarted.close()


def test_target_command_is_not_sent_if_its_write_ahead_record_cannot_be_saved():
    port = StaticTargetPort(target_state())
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, direct_pending_enabled=False),
        state_store=FailingOperationStore(),
    )

    result = coordinator.handle(source_event(1, SourceEventType.SNAPSHOT, position="1"))

    assert result.receipts[0].status == OperationStatus.UNKNOWN
    assert port.commands == []
    assert result.reconciliation_required is True


def test_unknown_market_request_is_resolved_by_observed_net_state_without_resend():
    port = RaisingExecutePort(target_state())
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, direct_pending_enabled=False),
    )

    first = coordinator.handle(source_event(1, SourceEventType.SNAPSHOT, position="1"))
    assert first.receipts[0].status == OperationStatus.UNKNOWN
    assert coordinator.reconciliation_required is True

    # The request may actually have reached MT5 despite the broken local
    # connection. A later complete snapshot proves the desired target net and
    # resolves the persisted operation instead of sending a second buy.
    port.state = target_state(long="1")
    observed = coordinator.handle_target_observation()

    assert observed.reconciliation_required is False
    assert coordinator.operation_book.has_in_flight() is False
    assert len(port.commands) == 1


def test_foreign_target_symbol_objects_quarantine_the_bridge_before_any_command():
    state = TargetAccountState(
        target_symbol="XAUUSD",
        model=TargetAccountModel.HEDGING,
        long_quantity=Decimal("0"),
        short_quantity=Decimal("0"),
        foreign_symbol_objects_present=True,
    )
    port = StaticTargetPort(state)
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, direct_pending_enabled=False),
    )

    result = coordinator.handle(source_event(1, SourceEventType.SNAPSHOT, position="1"))

    assert result.reconciliation_required is True
    assert port.commands == []


def test_mt5_observation_before_first_atas_snapshot_is_cached_but_never_planned():
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        StaticTargetPort(target_state()),
        policy=AtasAuthoritativePolicy(execution_enabled=True),
    )

    result = coordinator.handle_target_observation()

    assert result is None


def test_unconfirmed_operation_becomes_unknown_without_a_dangerous_retry():
    port = StaticTargetPort(target_state())
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, direct_pending_enabled=False),
        operation_confirmation_timeout=timedelta(milliseconds=1),
    )
    first = coordinator.handle(source_event(1, SourceEventType.SNAPSHOT, position="1"))
    command_id = first.commands[0].command_id
    record = coordinator.operation_book.records[command_id]
    coordinator.operation_book.records[command_id] = replace(
        record,
        dispatched_at_utc=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    observed = coordinator.handle_target_observation()

    assert observed.reconciliation_required is True
    assert coordinator.operation_book.records[command_id].status == OperationStatus.UNKNOWN
    assert len(port.commands) == 1


def test_operator_can_explicitly_release_a_verified_no_effect_market_request():
    port = StaticTargetPort(target_state())
    coordinator = SymbolCoordinator(
        "GC",
        "XAUUSD",
        port,
        policy=AtasAuthoritativePolicy(execution_enabled=True, direct_pending_enabled=False),
    )
    first = coordinator.handle(source_event(1, SourceEventType.SNAPSHOT, position="1"))
    command_id = first.commands[0].command_id
    record = coordinator.operation_book.records[command_id]
    coordinator.operation_book.records[command_id] = replace(record, status=OperationStatus.UNKNOWN)
    coordinator.reconciliation_required = True

    released = coordinator.confirm_no_effect_after_operator_check(command_id)

    assert coordinator.operation_book.records[command_id].status == OperationStatus.OBSERVED_NO_EFFECT
    assert released is not None
    assert len(port.commands) == 2
    assert port.commands[1].command_id == f"{command_id}:attempt:2"
