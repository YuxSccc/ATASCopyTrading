from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Protocol

from .intent_ledger import IntentLedger
from .journal import InMemoryOperationJournal, OperationJournal
from .models import (
    BridgeCommand,
    CommandType,
    CoordinationResult,
    ExecutionReceipt,
    IntentState,
    OperationRecord,
    OperationStatus,
    SequenceStatus,
    SourceEvent,
    SourceEventType,
    SourceSymbolState,
    TargetAccountState,
    TargetLegState,
)
from .operation_book import OperationBook
from .persistence import BridgeStateStore, NullBridgeStateStore
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
    """Durable serialized coordinator for exactly one ATAS/MT5 symbol pair.

    All target commands, including market reconciliation and ticket-specific
    close commands, enter a persisted operation state before dispatch. A new
    command cannot be emitted until a complete target snapshot or transaction
    proves the preceding command's result.
    """

    source_symbol: str
    target_symbol: str
    execution_port: TargetExecutionPort
    policy: CopyPolicy = field(default_factory=ObserveOnlyPolicy)
    source_book: SourceStateBook = field(init=False)
    ledger: IntentLedger = field(default_factory=IntentLedger)
    operation_book: OperationBook = field(default_factory=OperationBook)
    journal: OperationJournal = field(default_factory=InMemoryOperationJournal)
    state_store: BridgeStateStore = field(default_factory=NullBridgeStateStore)
    operation_confirmation_timeout: timedelta = timedelta(seconds=5)
    reconciliation_required: bool = False
    _target_unavailable_reason: str | None = field(default=None, init=False)
    _last_quote_checkpoint_at: float = field(default=0.0, init=False)
    _last_market_state_persist_at: float = field(default=0.0, init=False)
    quote_checkpoint_interval_seconds: float = 1.0
    market_state_persist_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        self.source_book = SourceStateBook(source_symbol=self.source_symbol)
        checkpoint = self.state_store.load_source_state(self.source_symbol)
        if checkpoint is not None:
            self.source_book.restore(checkpoint)
        persisted_ledger = self.state_store.load_ledger(self.target_symbol)
        if persisted_ledger is not None:
            self.ledger = persisted_ledger
        recovery_write_failed = False
        if checkpoint is not None:
            # A process can fail after the source checkpoint commit but before
            # its ledger commit. Rebuilding the source projection at startup
            # keeps that window conservative and self-healing.
            self.ledger.synchronize_source(checkpoint)
            try:
                self.state_store.save_ledger(self.target_symbol, self.ledger)
            except Exception:
                recovery_write_failed = True
        self.operation_book.restore(self.state_store.load_operations(self.target_symbol))
        self.reconciliation_required = recovery_write_failed or self._requires_operator_action() or self.operation_book.has_in_flight()

    def handle(self, event: SourceEvent) -> CoordinationResult:
        sequence_status = self.source_book.apply(event)
        state = self.source_book.snapshot()
        if not self._persist_source(event, state):
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="durable state store failed; target execution is blocked",
            )
        if sequence_status != SequenceStatus.APPLIED:
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=self.reconciliation_required or not state.synchronized,
                reason="source event was not safe to plan",
            )

        # Keep the source intent graph current before touching the target. A
        # target-network outage therefore cannot erase an ATAS cancellation.
        self.ledger.synchronize_source(state)
        if not self._persist_ledger():
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="intent ledger persistence failed; target execution is blocked",
            )

        # Quotes and lifecycle notices take part in sequence validation but do
        # not themselves cause a trading plan in this first runtime.
        if event.event_type == SourceEventType.AUXILIARY:
            target = self._read_target_state()
            observer = getattr(self.policy, "observe_market_data", None)
            if target is not None and callable(observer):
                observer(state, target)
                if not self._persist_market_state():
                    self.reconciliation_required = True
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=self.reconciliation_required,
                reason="auxiliary event recorded for sequence continuity",
            )
        return self._plan_from_current_state(sequence_status, state)

    def handle_target_observation(self) -> CoordinationResult | None:
        """Re-plan from the latest ATAS state after an MT5 transaction/snapshot."""

        if not self.source_book.has_synchronized_snapshot:
            # MT5 may start before ATAS. Cache its snapshot in the gateway but
            # never manufacture a source state or plan a target command.
            return None
        state = self.source_book.snapshot()
        return self._plan_from_current_state(SequenceStatus.APPLIED, state)

    def handle_target_receipt(self, receipt: ExecutionReceipt) -> None:
        """Persist the MT5 EA delivery receipt without treating it as fill proof."""

        try:
            operation = self.operation_book.apply_receipt(receipt)
        except KeyError:
            self.reconciliation_required = True
            return
        self._apply_receipt(operation.command, receipt)
        if not self._persist_ledger_and_operations((operation,)):
            self.reconciliation_required = True
            return
        if receipt.status in {OperationStatus.FAILED, OperationStatus.UNKNOWN}:
            self.reconciliation_required = True

    def check_operation_timeouts(self) -> bool:
        """Expire stalled operations even if the MT5 EA stops publishing."""

        expired = self.operation_book.expire_unconfirmed(self.operation_confirmation_timeout)
        if not expired:
            return False
        self._withdraw_expired_commands(expired)
        if not self._persist_ledger_and_operations(expired):
            self.reconciliation_required = True
            return True
        self.reconciliation_required = True
        return True

    def confirm_no_effect_after_operator_check(self, command_id: str) -> CoordinationResult | None:
        """Release one unknown request only after an operator checked MT5.

        The caller must have verified that the terminal's latest complete
        snapshot has no effect from this command.  This is the deliberate
        escape hatch for an ambiguous broker/network outcome; it is never
        called automatically.
        """

        if not self.source_book.has_synchronized_snapshot:
            return None
        target = self._read_target_state()
        if target is None:
            self.reconciliation_required = True
            return None
        if not self._apply_observed_target(target):
            self.reconciliation_required = True
            return None

        record = self.operation_book.records.get(command_id)
        if record is None:
            raise KeyError(f"unknown operation: {command_id}")
        if record.resolved_at_utc is not None:
            return self._plan_from_current_state(SequenceStatus.APPLIED, self.source_book.snapshot())

        released = self.operation_book.confirm_no_effect(command_id)
        self._rearm_leg_after_no_effect(released.command)
        if not self._persist_ledger_and_operations((released,)):
            self.reconciliation_required = True
            return None
        self.reconciliation_required = False
        return self._plan_from_current_state(SequenceStatus.APPLIED, self.source_book.snapshot())

    def reconcile_target(self) -> bool:
        """Refresh target state without dispatching a follow-on command."""

        target = self._read_target_state()
        if target is None:
            self.reconciliation_required = True
            return False
        if not self._apply_observed_target(target):
            self.reconciliation_required = True
            return False
        self.reconciliation_required = self._requires_operator_action()
        return not self.reconciliation_required and not self.operation_book.has_in_flight()

    def _plan_from_current_state(
        self,
        sequence_status: SequenceStatus,
        state: SourceSymbolState,
    ) -> CoordinationResult:
        target = self._read_target_state()
        if target is None:
            self.reconciliation_required = True
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason=self._target_unavailable_reason,
            )
        if not self._apply_observed_target(target):
            self.reconciliation_required = True
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="target observation could not be persisted",
            )

        self.reconciliation_required = self._requires_operator_action()
        if self.reconciliation_required:
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="target command outcome requires operator reconciliation",
            )
        commands = self.policy.plan(state, target, self.ledger)
        if not self._persist_market_state():
            self.reconciliation_required = True
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="market-state persistence failed; target execution is blocked",
            )

        # Normally one unconfirmed target command serializes the symbol.  The
        # sole exception is a source-authoritative cancellation of a pending
        # order for which the EA returned a concrete MT5 ticket.  That command
        # removes risk; holding it behind a stale full snapshot is exactly how
        # a cancelled ATAS order used to survive on MT5.
        if self.operation_book.has_in_flight():
            cancellation = self._select_next_command(
                tuple(command for command in commands if command.command_type == CommandType.CANCEL_PENDING)
            )
            if cancellation is None:
                return CoordinationResult(
                    sequence_status=sequence_status,
                    source_state=state,
                    reconciliation_required=False,
                    reason="waiting for target snapshot to confirm prior command",
                )
            command = self.operation_book.allocate_attempt(cancellation)
            receipt = self._dispatch(command)
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                commands=(command,),
                receipts=(receipt,),
                reconciliation_required=self.reconciliation_required,
                reason="target execution uncertainty" if self.reconciliation_required else None,
            )

        command = self._select_next_command(commands)
        if command is None:
            return CoordinationResult(sequence_status=sequence_status, source_state=state)
        command = self.operation_book.allocate_attempt(command)
        receipt = self._dispatch(command)
        return CoordinationResult(
            sequence_status=sequence_status,
            source_state=state,
            commands=(command,),
            receipts=(receipt,),
            reconciliation_required=self.reconciliation_required,
            reason="target execution uncertainty" if self.reconciliation_required else None,
        )

    def _dispatch(self, command: BridgeCommand) -> ExecutionReceipt:
        # Write-ahead persistence is a hard precondition to an MT5 request.
        operation = self.operation_book.dispatch(command)
        self._mark_command_dispatched(command)
        if not self._persist_ledger_and_operations((operation,)):
            self.reconciliation_required = True
            return ExecutionReceipt(
                command_id=command.command_id,
                status=OperationStatus.UNKNOWN,
                message="operation dispatch and target leg state were not durably recorded",
            )
        receipt = self._execute_safely(command)
        self.journal.record(command, receipt)
        operation = self.operation_book.apply_receipt(receipt)
        self._apply_receipt(command, receipt)
        if not self._persist_ledger_and_operations((operation,)):
            self.reconciliation_required = True
        if receipt.status in {OperationStatus.FAILED, OperationStatus.UNKNOWN}:
            self.reconciliation_required = True
        return receipt

    def _apply_observed_target(self, target: TargetAccountState) -> bool:
        self.ledger.synchronize_target(target)
        self._apply_target_fill_grace(self.source_book.snapshot(), target)
        expired = self.operation_book.expire_unconfirmed(self.operation_confirmation_timeout)
        self._withdraw_expired_commands(expired)
        resolved = self.operation_book.resolve_from_snapshot(target, self.ledger)
        for record in resolved:
            command = record.command
            if command.command_type == CommandType.CANCEL_PENDING and command.leg_id is not None:
                leg = self.ledger.get_leg(command.leg_id)
                if leg is not None:
                    self.ledger.update_leg(replace(leg, state=TargetLegState.CANCELLED, target_order_ticket=None))
        try:
            self.state_store.save_ledger_and_operations(self.target_symbol, self.ledger, (*expired, *resolved))
        except Exception:
            return False
        return True

    def _apply_target_fill_grace(self, source: SourceSymbolState, target: TargetAccountState) -> None:
        """Manage the bounded, durable MT5-first-fill exception.

        Classification runs only after a complete target snapshot.  It cannot
        accidentally hide a missing target order from a source-only callback.
        """

        eligible = getattr(self.policy, "target_fill_grace_eligible", None)
        seconds = int(getattr(self.policy, "target_fill_grace_seconds", 0) or 0)
        if not callable(eligible) or seconds <= 0:
            return
        now = datetime.now(timezone.utc)
        multiplier = getattr(self.policy, "volume_multiplier", 1)
        for leg in tuple(self.ledger.legs.values()):
            intent = self.ledger.intents.get(leg.intent_id)
            if leg.state == TargetLegState.MISSING_ON_TARGET and eligible(source, target, leg):
                self.ledger.update_leg(
                    replace(
                        leg,
                        state=TargetLegState.TARGET_FILL_GRACE,
                        target_fill_grace_started_at_utc=now.isoformat(),
                    )
                )
                continue
            if leg.state != TargetLegState.TARGET_FILL_GRACE:
                continue
            if intent is None or intent.state == IntentState.RETIRED:
                self.ledger.update_leg(
                    replace(leg, state=TargetLegState.CANCELLED, target_fill_grace_started_at_utc=None)
                )
                continue
            # ATAS has caught up at the position layer. Keep this source leg
            # parked until its own terminal callback retires it; recreating a
            # pending MT5 order in that callback-ordering window is unsafe.
            if source.position is not None and source.position_complete and (
                source.position.net_quantity * multiplier == target.net_quantity
            ):
                continue
            started = _parse_utc(leg.target_fill_grace_started_at_utc)
            if started is None or now - started >= timedelta(seconds=seconds):
                # The target fill never reached ATAS.  Release the mapping so
                # normal ATAS-authoritative repair closes the unmatched target
                # quantity, then recreates a fresh pending mirror if needed.
                self.ledger.update_leg(
                    replace(leg, state=TargetLegState.CANCELLED, target_fill_grace_started_at_utc=None)
                )

    def _persist_source(self, event: SourceEvent, state: SourceSymbolState) -> bool:
        is_quote = event.event_type == SourceEventType.AUXILIARY and event.quote is not None
        now = monotonic()
        if is_quote and now - self._last_quote_checkpoint_at < self.quote_checkpoint_interval_seconds:
            # Raw probe JSONL retains every quote for measurement. SQLite only
            # needs a recent quote checkpoint for restart recovery; making it
            # transactional at 20 Hz would delay order processing and bloat
            # the WAL without improving trading correctness.
            return True
        try:
            if not is_quote:
                self.state_store.record_source_event(event)
            self.state_store.save_source_state(state)
        except Exception:
            return False
        if is_quote:
            self._last_quote_checkpoint_at = now
        return True

    def _persist_ledger(self) -> bool:
        try:
            self.state_store.save_ledger(self.target_symbol, self.ledger)
        except Exception:
            return False
        return True

    def _persist_operation(self, operation: OperationRecord) -> bool:
        try:
            self.state_store.save_operation(operation)
        except Exception:
            return False
        return True

    def _persist_ledger_and_operations(self, operations: tuple[OperationRecord, ...]) -> bool:
        try:
            self.state_store.save_ledger_and_operations(self.target_symbol, self.ledger, operations)
        except Exception:
            return False
        return True

    def _persist_market_state(self) -> bool:
        exporter = getattr(self.policy, "export_market_state", None)
        if not callable(exporter):
            return True
        payload = exporter()
        if payload is None:
            return True
        now = monotonic()
        if now - self._last_market_state_persist_at < self.market_state_persist_interval_seconds:
            return True
        try:
            self.state_store.save_market_state(self.target_symbol, payload)
        except Exception:
            return False
        self._last_market_state_persist_at = now
        return True

    def _read_target_state(self) -> TargetAccountState | None:
        try:
            target = self.execution_port.read_state(self.target_symbol)
        except Exception as error:  # MT5 terminal/network may disappear
            self._target_unavailable_reason = f"target snapshot unavailable: {type(error).__name__}: {error}"
            return None
        if target.target_symbol != self.target_symbol:
            self._target_unavailable_reason = "target adapter returned a snapshot for the wrong symbol"
            return None
        if not target.snapshot_complete:
            self._target_unavailable_reason = "target adapter returned an incomplete snapshot"
            return None
        if target.foreign_symbol_objects_present:
            self._target_unavailable_reason = "foreign/manual target-symbol objects detected; bridge is quarantined"
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

    def _withdraw_expired_commands(self, expired: tuple[OperationRecord, ...]) -> None:
        """Tell queue-backed transports not to replay quarantined commands."""

        withdraw = getattr(self.execution_port, "withdraw_command", None)
        if not callable(withdraw):
            return
        for record in expired:
            withdraw(record.command.command_id)

    def _mark_command_dispatched(self, command: BridgeCommand) -> None:
        if command.leg_id is None:
            return
        if command.command_type == CommandType.CREATE_PENDING:
            self.ledger.mark_leg_create_pending(command.leg_id)
        elif command.command_type == CommandType.CANCEL_PENDING:
            self.ledger.mark_leg_cancel_pending(command.leg_id)

    def _apply_receipt(self, command: BridgeCommand, receipt: ExecutionReceipt) -> None:
        if command.leg_id is None:
            return
        if command.command_type == CommandType.CREATE_PENDING and receipt.status in {
            OperationStatus.ACCEPTED,
            OperationStatus.CONFIRMED,
        }:
            ticket = _valid_target_ticket(receipt.target_reference)
            if ticket is not None:
                leg = self.ledger.get_leg(command.leg_id)
                if leg is not None and leg.state == TargetLegState.CREATE_PENDING:
                    # Keep CREATE_PENDING until the complete MT5 snapshot
                    # confirms existence, but retain the ticket so an ATAS
                    # cancellation can be targeted immediately.
                    self.ledger.update_leg(replace(leg, target_order_ticket=ticket))
        if receipt.status == OperationStatus.FAILED:
            self.ledger.mark_leg_failed(command.leg_id)
        elif receipt.status == OperationStatus.UNKNOWN:
            self.ledger.mark_leg_unknown(command.leg_id)

    def _rearm_leg_after_no_effect(self, command: BridgeCommand) -> None:
        """Restore the desired leg state after a verified no-effect outcome."""

        if command.leg_id is None:
            return
        leg = self.ledger.get_leg(command.leg_id)
        if leg is None:
            return
        intent = self.ledger.intents.get(leg.intent_id)
        if command.command_type == CommandType.CREATE_PENDING:
            if leg.state in {TargetLegState.ACTIVE, TargetLegState.PARTIAL_FILL}:
                raise ValueError("target snapshot shows the supposedly absent pending order")
            state = TargetLegState.DESIRED if intent is not None and intent.state == IntentState.ACTIVE else TargetLegState.CANCELLED
            self.ledger.update_leg(replace(leg, state=state, target_order_ticket=None))
        elif command.command_type == CommandType.CANCEL_PENDING:
            state = TargetLegState.ACTIVE if leg.target_order_ticket is not None else TargetLegState.CANCELLED
            self.ledger.update_leg(replace(leg, state=state))

    def _requires_operator_action(self) -> bool:
        return self.operation_book.has_unknown() or self.operation_book.has_failed() or self.ledger.has_unknown_leg() or any(
            leg.state == TargetLegState.FAILED for leg in self.ledger.legs.values()
        )

    @staticmethod
    def _select_next_command(commands: tuple[BridgeCommand, ...]) -> BridgeCommand | None:
        """One command at a time per symbol; cancellation/reduction wins."""

        priority = {
            CommandType.CANCEL_PENDING: 0,
            CommandType.CLOSE_POSITION: 1,
            CommandType.EXECUTE_MARKET: 2,
            CommandType.MODIFY_POSITION_PROTECTION: 3,
            CommandType.CREATE_PENDING: 4,
            CommandType.MODIFY_PENDING: 5,
            CommandType.RECONCILE: 6,
        }
        return min(commands, key=lambda command: priority[command.command_type]) if commands else None


def _valid_target_ticket(value: str | None) -> str | None:
    """Return a non-zero MT5 ticket from an EA receipt, if one was supplied."""

    try:
        ticket = int(str(value or "").strip())
    except ValueError:
        return None
    return str(ticket) if ticket > 0 else None


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
