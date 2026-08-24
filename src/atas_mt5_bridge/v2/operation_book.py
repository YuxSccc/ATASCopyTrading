from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from .intent_ledger import IntentLedger
from .models import (
    BridgeCommand,
    CommandType,
    ExecutionReceipt,
    OperationRecord,
    OperationStatus,
    TargetAccountState,
    TargetLegState,
    SourceSymbolState,
)


@dataclass
class OperationBook:
    """Durable command state machine, including market and close commands.

    A sent command is not forgotten merely because it has no source-order leg.
    This prevents a stale MT5 snapshot from causing a repeated market repair.
    """

    records: dict[str, OperationRecord] = field(default_factory=dict)

    def restore(self, records: Iterable[OperationRecord]) -> None:
        self.records = {record.command.command_id: record for record in records}

    def dispatch(self, command: BridgeCommand) -> OperationRecord:
        existing = self.records.get(command.command_id)
        if existing is not None:
            raise ValueError(f"command ID is already present: {command.command_id}")
        record = OperationRecord(
            command=command,
            status=OperationStatus.PENDING,
            dispatched_at_utc=_now(),
        )
        self.records[command.command_id] = record
        return record

    def allocate_attempt(self, command: BridgeCommand) -> BridgeCommand:
        """Return a durable new command ID when a released action is retried."""

        existing = self.records.get(command.command_id)
        if existing is None:
            return command
        if existing.resolved_at_utc is None:
            raise ValueError(f"cannot create a second in-flight attempt for {command.command_id}")
        attempt = 2
        while f"{command.command_id}:attempt:{attempt}" in self.records:
            attempt += 1
        return replace(command, command_id=f"{command.command_id}:attempt:{attempt}")

    def apply_receipt(self, receipt: ExecutionReceipt) -> OperationRecord:
        record = self._require(receipt.command_id)
        if record.resolved_at_utc is not None:
            # The EA can retry a receipt after its HTTP response was lost.
            # Once a complete target snapshot resolved the command, a late
            # delivery acknowledgement must not reopen that operation.
            return record
        # A local queue may only acknowledge delivery later. Receipt status is
        # retained, but every successful outcome still needs an observed MT5
        # snapshot or transaction before it is considered resolved.
        updated = replace(
            record,
            status=receipt.status,
            target_reference=receipt.target_reference,
            message=receipt.message,
        )
        self.records[receipt.command_id] = updated
        return updated

    def resolve_from_snapshot(self, target: TargetAccountState, ledger: IntentLedger) -> tuple[OperationRecord, ...]:
        if not target.snapshot_complete:
            return ()
        resolved: list[OperationRecord] = []
        for command_id, record in tuple(self.records.items()):
            observable_statuses = {
                OperationStatus.PENDING,
                OperationStatus.ACCEPTED,
                OperationStatus.CONFIRMED,
                OperationStatus.UNKNOWN,
            }
            # A failed ticket-specific cancellation or close is normally
            # quarantined.  A later *complete* snapshot can, however, prove
            # that the exact target object is already gone.  In that case it
            # is safe to release the stale failure and let normal
            # reconciliation evaluate the current source state; retaining it
            # would indefinitely block unrelated later source orders.
            #
            # This deliberately excludes market opens and protection changes:
            # absence of an object is not evidence that those commands had no
            # target-side effect.
            if record.status == OperationStatus.FAILED and record.command.command_type in {
                CommandType.CANCEL_PENDING,
                CommandType.CLOSE_POSITION,
            }:
                observable_statuses.add(OperationStatus.FAILED)
            if record.status not in observable_statuses:
                continue
            if not self._is_observed(record.command, target, ledger):
                continue
            updated = replace(record, status=OperationStatus.CONFIRMED, resolved_at_utc=_now())
            self.records[command_id] = updated
            resolved.append(updated)
        return tuple(resolved)

    def resolve_creates_superseded_by_cancellation(
        self,
        target: TargetAccountState,
        ledger: IntentLedger,
        resolved_operations: Iterable[OperationRecord],
    ) -> tuple[OperationRecord, ...]:
        """Close an unobserved create once its exact later cancel is proved.

        An EA can accept a create, then receive an ATAS-driven cancellation
        before the next complete account snapshot.  The order may therefore
        never appear in a snapshot at all.  A *confirmed ticket-specific*
        cancellation plus a complete snapshot without the leg proves there is
        no remaining target pending-order risk, so retaining the create as
        ``UNKNOWN`` would be a false quarantine.
        """

        if not target.snapshot_complete:
            return ()
        cancelled_leg_ids = {
            record.command.leg_id
            for record in resolved_operations
            if record.command.command_type == CommandType.CANCEL_PENDING and record.command.leg_id
        }
        cancelled_leg_ids.update(
            record.command.leg_id
            for record in self.records.values()
            if record.command.command_type == CommandType.CANCEL_PENDING
            and record.status == OperationStatus.CONFIRMED
            and record.resolved_at_utc is not None
            and record.command.leg_id
        )
        if not cancelled_leg_ids:
            return ()
        resolved: list[OperationRecord] = []
        for command_id, record in tuple(self.records.items()):
            command = record.command
            if (
                command.command_type != CommandType.CREATE_PENDING
                or command.leg_id not in cancelled_leg_ids
                or record.resolved_at_utc is not None
                or _has_target_order_for_leg(command.leg_id, ledger, target)
            ):
                continue
            updated = replace(
                record,
                status=OperationStatus.CONFIRMED,
                message="create was safely superseded by a confirmed ticket-specific cancellation",
                resolved_at_utc=_now(),
            )
            self.records[command_id] = updated
            resolved.append(updated)
        return tuple(resolved)

    def resolve_failed_creates_retired_at_source(
        self,
        target: TargetAccountState,
        ledger: IntentLedger,
        source: SourceSymbolState,
    ) -> tuple[OperationRecord, ...]:
        """Release a failed create whose source order has explicitly ended.

        This is intentionally narrower than a general failed-create retry. An
        ATAS must explicitly report the exact source order as done/cancelled,
        or establish a newer probe session. A complete MT5 snapshot must also
        show no matching pending order. Together those facts prove there is no
        residual target order to protect and no source intent left to retry.
        """

        if not target.snapshot_complete:
            return ()
        resolved: list[OperationRecord] = []
        for command_id, record in tuple(self.records.items()):
            command = record.command
            leg = ledger.get_leg(command.leg_id) if command.leg_id is not None else None
            source_order_ended = command.source_order_id in source.explicitly_removed_order_ids
            source_session_replaced = leg is not None and leg.source_key.session_id != source.session_id
            if (
                record.status != OperationStatus.FAILED
                or record.resolved_at_utc is not None
                or command.command_type != CommandType.CREATE_PENDING
                or command.leg_id is None
                or not (source_order_ended or source_session_replaced)
                or _has_target_order_for_leg(command.leg_id, ledger, target)
            ):
                continue
            updated = replace(
                record,
                status=OperationStatus.CONFIRMED,
                message="failed create was safely retired after explicit ATAS removal and an absent complete MT5 snapshot",
                resolved_at_utc=_now(),
            )
            self.records[command_id] = updated
            resolved.append(updated)
        return tuple(resolved)

    def expire_unconfirmed(self, timeout: timedelta, now: datetime | None = None) -> tuple[OperationRecord, ...]:
        """Quarantine requests that were accepted but never become observable.

        This intentionally does *not* retry a possibly delivered MT5 request.
        A late snapshot can still resolve the resulting ``UNKNOWN`` record.
        """

        if timeout <= timedelta(0):
            raise ValueError("operation confirmation timeout must be positive")
        now = now or datetime.now(timezone.utc)
        expired: list[OperationRecord] = []
        for command_id, record in tuple(self.records.items()):
            if record.status not in {OperationStatus.PENDING, OperationStatus.ACCEPTED, OperationStatus.CONFIRMED}:
                continue
            if record.resolved_at_utc is not None:
                continue
            dispatched_at = _parse_time(record.dispatched_at_utc)
            if now - dispatched_at < timeout:
                continue
            updated = replace(
                record,
                status=OperationStatus.UNKNOWN,
                message="target request was not confirmed by a complete MT5 snapshot before timeout",
            )
            self.records[command_id] = updated
            expired.append(updated)
        return tuple(expired)

    def confirm_no_effect(self, command_id: str) -> OperationRecord:
        """Close an unknown operation after a human verifies the MT5 state.

        This is intentionally explicit.  A bridge restart or a timeout is not
        evidence that a broker did not receive an asynchronous request.
        """

        record = self._require(command_id)
        if record.status not in {OperationStatus.UNKNOWN, OperationStatus.FAILED} or record.resolved_at_utc is not None:
            raise ValueError("only an unresolved unknown or failed operation can be released")
        updated = replace(
            record,
            status=OperationStatus.OBSERVED_NO_EFFECT,
            message="operator confirmed no target-side effect after reviewing a complete snapshot",
            resolved_at_utc=_now(),
        )
        self.records[command_id] = updated
        return updated

    def discard_before_delivery(self, command_id: str) -> OperationRecord:
        """Resolve a command proved never to have been offered to the EA.

        This is narrower than :meth:`confirm_no_effect`: the local gateway
        establishes that no HTTP poll has returned the command, so there is no
        broker-side ambiguity and no operator action is required.
        """

        record = self._require(command_id)
        if record.status != OperationStatus.PENDING or record.resolved_at_utc is not None:
            raise ValueError("only an undelivered pending command can be discarded")
        updated = replace(
            record,
            status=OperationStatus.OBSERVED_NO_EFFECT,
            message="source intent was superseded before the command was offered to the MT5 EA",
            resolved_at_utc=_now(),
        )
        self.records[command_id] = updated
        return updated

    def has_in_flight(self) -> bool:
        return any(
            record.status in {OperationStatus.PENDING, OperationStatus.ACCEPTED, OperationStatus.CONFIRMED, OperationStatus.UNKNOWN}
            and record.resolved_at_utc is None
            for record in self.records.values()
        )

    def has_unknown(self) -> bool:
        return any(
            record.status == OperationStatus.UNKNOWN and record.resolved_at_utc is None
            for record in self.records.values()
        )

    def has_failed(self) -> bool:
        return any(record.status == OperationStatus.FAILED for record in self.records.values())

    def _is_observed(self, command: BridgeCommand, target: TargetAccountState, ledger: IntentLedger) -> bool:
        if command.command_type == CommandType.RECONCILE:
            return True
        if command.command_type == CommandType.CREATE_PENDING and command.leg_id:
            leg = ledger.get_leg(command.leg_id)
            return leg is not None and leg.state in {TargetLegState.ACTIVE, TargetLegState.PARTIAL_FILL}
        if command.command_type == CommandType.CANCEL_PENDING and command.leg_id:
            return not _has_target_order_for_leg(command.leg_id, ledger, target)
        if command.command_type == CommandType.CLOSE_POSITION:
            ticket = str(command.payload.get("position_ticket", ""))
            expected_remaining = _decimal(command.payload.get("expected_position_remaining"))
            position = next((item for item in target.positions if item.ticket == ticket), None)
            return position is None if expected_remaining == 0 else position is not None and position.quantity <= expected_remaining
        if command.command_type == CommandType.EXECUTE_MARKET:
            expected_net = _decimal(command.payload.get("expected_target_net"))
            return target.net_quantity == expected_net
        if command.command_type == CommandType.MODIFY_POSITION_PROTECTION:
            ticket = str(command.payload.get("position_ticket", ""))
            position = next((item for item in target.positions if item.ticket == ticket), None)
            if position is None:
                return False
            return (
                position.stop_loss == _optional_decimal(command.payload.get("stop_loss"))
                and position.take_profit == _optional_decimal(command.payload.get("take_profit"))
            )
        return False

    def _require(self, command_id: str) -> OperationRecord:
        record = self.records.get(command_id)
        if record is None:
            raise KeyError(f"unknown operation: {command_id}")
        return record


def _decimal(value: object) -> Decimal:
    if value is None:
        raise ValueError("expected decimal is missing")
    return Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _has_target_order_for_leg(leg_id: str, ledger: IntentLedger, target: TargetAccountState) -> bool:
    leg = ledger.get_leg(leg_id)
    return any(
        order.leg_id == leg_id or (leg is not None and bool(leg.bridge_tag) and order.bridge_tag == leg.bridge_tag)
        for order in target.pending_orders
    )
