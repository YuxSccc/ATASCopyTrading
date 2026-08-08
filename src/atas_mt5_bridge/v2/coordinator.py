from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .intent_ledger import IntentLedger
from .journal import InMemoryOperationJournal, OperationJournal
from .models import (
    BridgeCommand,
    CommandType,
    CoordinationResult,
    ExecutionReceipt,
    OperationStatus,
    SequenceStatus,
    SourceEvent,
    SourceEventType,
    SourceSymbolState,
    TargetAccountState,
)
from .source_state import SourceStateBook


class TargetExecutionPort(Protocol):
    """MT5 boundary. Accepted commands are not treated as confirmed state."""

    def read_state(self, target_symbol: str) -> TargetAccountState:
        raise NotImplementedError

    def execute(self, command: BridgeCommand) -> ExecutionReceipt:
        raise NotImplementedError


class CopyPolicy(Protocol):
    """Compiles canonical ATAS state into target commands without executing them."""

    def plan(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        ledger: IntentLedger,
    ) -> tuple[BridgeCommand, ...]:
        raise NotImplementedError


@dataclass
class ObserveOnlyPolicy:
    """Safe default used until a separately reviewed policy is installed."""

    def plan(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        ledger: IntentLedger,
    ) -> tuple[BridgeCommand, ...]:
        return ()


@dataclass
class SymbolCoordinator:
    """Serialized coordinator for exactly one ATAS/MT5 symbol pair.

    Host code must call this object from one worker. Target connectivity errors
    and unknown command outcomes never escape as an unhandled exception and
    never trigger blind retries. A complete MT5 snapshot is the only way to
    clear an unknown outcome.
    """

    source_symbol: str
    target_symbol: str
    execution_port: TargetExecutionPort
    policy: CopyPolicy = field(default_factory=ObserveOnlyPolicy)
    source_book: SourceStateBook = field(init=False)
    ledger: IntentLedger = field(default_factory=IntentLedger)
    journal: OperationJournal = field(default_factory=InMemoryOperationJournal)
    reconciliation_required: bool = False
    _target_unavailable_reason: str | None = field(default=None, init=False)
    _unresolved_nonleg_command_ids: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.source_book = SourceStateBook(source_symbol=self.source_symbol)

    def handle(self, event: SourceEvent) -> CoordinationResult:
        sequence_status = self.source_book.apply(event)
        state = self.source_book.snapshot()
        if sequence_status != SequenceStatus.APPLIED:
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=self.reconciliation_required or not state.synchronized,
                reason="source event was not safe to plan",
            )

        # Keep the source intent graph current before touching the target. That
        # means a target-network outage cannot erase a source cancellation.
        self.ledger.synchronize_source(state)

        if event.event_type == SourceEventType.AUXILIARY:
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=self.reconciliation_required,
                reason="auxiliary event recorded for sequence continuity",
            )

        target = self._read_target_state()
        if target is None:
            self.reconciliation_required = True
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason=self._target_unavailable_reason,
            )

        self.ledger.synchronize_target(target)
        self.reconciliation_required = self._ledger_requires_operator()
        if self._has_blocking_uncertainty():
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="target command outcome is unresolved; complete snapshot required",
            )

        commands = self._limit_in_flight_risk(
            self.policy.plan(state, target, self.ledger),
            risk_command_in_flight=self.ledger.has_in_flight_leg(),
        )
        receipts: list[ExecutionReceipt] = []
        for command in commands:
            self._mark_command_dispatched(command)
            receipt = self._execute_safely(command)
            self.journal.record(command, receipt)
            self._apply_receipt(command, receipt)
            receipts.append(receipt)
            if receipt.status in {OperationStatus.FAILED, OperationStatus.UNKNOWN}:
                self.reconciliation_required = True
                # A fresh snapshot is attempted immediately, but a missing or
                # ambiguous result is never retried from this call.
                self._read_target_state_and_reconcile_ledger()
                break

        return CoordinationResult(
            sequence_status=sequence_status,
            source_state=state,
            commands=commands[: len(receipts)],
            receipts=tuple(receipts),
            reconciliation_required=self.reconciliation_required,
            reason="target execution uncertainty" if self.reconciliation_required else None,
        )

    def reconcile_target(self) -> bool:
        """Refresh target state after an MT5 transaction or timeout.

        Returns true only when a complete target snapshot proves there is no
        unknown/failed leg. It does not manufacture a retry.
        """

        target = self._read_target_state()
        if target is None:
            self.reconciliation_required = True
            return False
        self.ledger.synchronize_target(target)
        self.reconciliation_required = self._ledger_requires_operator()
        return not self.reconciliation_required

    def handle_target_observation(self) -> CoordinationResult:
        """Plan from the latest ATAS state after an MT5 transaction/snapshot.

        This is the entry point a future MQL5 ``OnTradeTransaction`` observer
        calls. It is essential when a source cancellation arrived while a prior
        target create was still awaiting confirmation: once the target order is
        observable, the cancellation can be sent without waiting for another
        ATAS event.
        """

        state = self.source_book.snapshot()
        target = self._read_target_state()
        if target is None:
            self.reconciliation_required = True
            return CoordinationResult(
                sequence_status=SequenceStatus.APPLIED,
                source_state=state,
                reconciliation_required=True,
                reason=self._target_unavailable_reason,
            )
        self.ledger.synchronize_target(target)
        self.reconciliation_required = self._ledger_requires_operator()
        if self._has_blocking_uncertainty():
            return CoordinationResult(
                sequence_status=SequenceStatus.APPLIED,
                source_state=state,
                reconciliation_required=True,
                reason="target command outcome is unresolved; complete snapshot required",
            )

        commands = self._limit_in_flight_risk(
            self.policy.plan(state, target, self.ledger),
            risk_command_in_flight=self.ledger.has_in_flight_leg(),
        )
        receipts: list[ExecutionReceipt] = []
        for command in commands:
            self._mark_command_dispatched(command)
            receipt = self._execute_safely(command)
            self.journal.record(command, receipt)
            self._apply_receipt(command, receipt)
            receipts.append(receipt)
            if receipt.status in {OperationStatus.FAILED, OperationStatus.UNKNOWN}:
                self.reconciliation_required = True
                self._read_target_state_and_reconcile_ledger()
                break
        return CoordinationResult(
            sequence_status=SequenceStatus.APPLIED,
            source_state=state,
            commands=commands[: len(receipts)],
            receipts=tuple(receipts),
            reconciliation_required=self.reconciliation_required,
            reason="target execution uncertainty" if self.reconciliation_required else "target observation reconciled",
        )

    def _read_target_state_and_reconcile_ledger(self) -> None:
        target = self._read_target_state()
        if target is not None:
            self.ledger.synchronize_target(target)
            self.reconciliation_required = self._ledger_requires_operator()

    def _read_target_state(self) -> TargetAccountState | None:
        try:
            target = self.execution_port.read_state(self.target_symbol)
        except Exception as error:  # boundary: MT5 terminal/network may disappear
            self._target_unavailable_reason = f"target snapshot unavailable: {type(error).__name__}: {error}"
            return None
        if target.target_symbol != self.target_symbol:
            self._target_unavailable_reason = "target adapter returned a snapshot for the wrong symbol"
            return None
        if not target.snapshot_complete:
            self._target_unavailable_reason = "target adapter returned an incomplete snapshot"
            return None
        self._target_unavailable_reason = None
        return target

    def _execute_safely(self, command: BridgeCommand) -> ExecutionReceipt:
        try:
            receipt = self.execution_port.execute(command)
        except Exception as error:  # an interrupted request has an unknown effect
            return ExecutionReceipt(
                command_id=command.command_id,
                status=OperationStatus.UNKNOWN,
                message=f"target execution exception: {type(error).__name__}: {error}",
            )
        if receipt.command_id != command.command_id:
            return ExecutionReceipt(
                command_id=command.command_id,
                status=OperationStatus.UNKNOWN,
                message="target receipt command id does not match dispatched command",
            )
        return receipt

    def _mark_command_dispatched(self, command: BridgeCommand) -> None:
        if command.leg_id is None:
            return
        if command.command_type == CommandType.CREATE_PENDING:
            self.ledger.mark_leg_create_pending(command.leg_id)
        elif command.command_type == CommandType.CANCEL_PENDING:
            self.ledger.mark_leg_cancel_pending(command.leg_id)

    def _apply_receipt(self, command: BridgeCommand, receipt: ExecutionReceipt) -> None:
        if command.leg_id is None:
            if receipt.status in {OperationStatus.FAILED, OperationStatus.UNKNOWN}:
                self._unresolved_nonleg_command_ids.add(command.command_id)
            return
        if receipt.status == OperationStatus.FAILED:
            self.ledger.mark_leg_failed(command.leg_id)
        elif receipt.status == OperationStatus.UNKNOWN:
            self.ledger.mark_leg_unknown(command.leg_id)

    def _has_blocking_uncertainty(self) -> bool:
        return self.reconciliation_required or self._ledger_requires_operator()

    def _ledger_requires_operator(self) -> bool:
        return bool(self._unresolved_nonleg_command_ids) or self.ledger.has_unknown_leg() or any(
            leg.state.value == "failed" for leg in self.ledger.legs.values()
        )

    @staticmethod
    def _limit_in_flight_risk(
        commands: tuple[BridgeCommand, ...],
        *,
        risk_command_in_flight: bool,
    ) -> tuple[BridgeCommand, ...]:
        """Keep cancellation/reduction priority while allowing one new-risk command."""

        selected: list[BridgeCommand] = []
        risk_increasing_in_flight = risk_command_in_flight
        for command in commands:
            if command.increases_risk:
                if risk_increasing_in_flight:
                    continue
                risk_increasing_in_flight = True
            selected.append(command)
        return tuple(selected)
