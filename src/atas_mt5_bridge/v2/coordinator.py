from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import (
    BridgeCommand,
    CoordinationResult,
    ExecutionReceipt,
    OperationRecord,
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
    """Owns copy rules; the framework deliberately supplies no default rules."""

    def plan(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
    ) -> tuple[BridgeCommand, ...]:
        raise NotImplementedError


@dataclass
class ObserveOnlyPolicy:
    """Safe default used until a separately reviewed policy is installed."""

    def plan(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
    ) -> tuple[BridgeCommand, ...]:
        return ()


@dataclass
class InMemoryOperationJournal:
    records: dict[str, OperationRecord] = field(default_factory=dict)

    def record(self, command: BridgeCommand, receipt: ExecutionReceipt) -> None:
        self.records[command.command_id] = OperationRecord(
            command=command,
            status=receipt.status,
            target_reference=receipt.target_reference,
            message=receipt.message,
        )


@dataclass
class SymbolCoordinator:
    """Single-worker coordinator for one source/target symbol pair.

    The host must call this object from one serialized worker. It never
    retries a failed or unknown command automatically; that state requires a
    target snapshot and an explicit reconciliation policy.
    """

    source_symbol: str
    target_symbol: str
    execution_port: TargetExecutionPort
    policy: CopyPolicy = field(default_factory=ObserveOnlyPolicy)
    source_book: SourceStateBook = field(init=False)
    journal: InMemoryOperationJournal = field(default_factory=InMemoryOperationJournal)
    reconciliation_required: bool = False

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

        if self.reconciliation_required:
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reconciliation_required=True,
                reason="manual reconciliation required after target execution uncertainty",
            )

        # Quotes and lifecycle notices take part in sequence validation but
        # must never cause a copy policy to run.
        if event.event_type == SourceEventType.AUXILIARY:
            return CoordinationResult(
                sequence_status=sequence_status,
                source_state=state,
                reason="auxiliary event recorded for sequence continuity",
            )

        target = self.execution_port.read_state(self.target_symbol)
        commands = self.policy.plan(state, target)
        receipts: list[ExecutionReceipt] = []
        for command in commands:
            receipt = self.execution_port.execute(command)
            self.journal.record(command, receipt)
            receipts.append(receipt)
            if receipt.status in {OperationStatus.FAILED, OperationStatus.UNKNOWN}:
                self.reconciliation_required = True
                break

        return CoordinationResult(
            sequence_status=sequence_status,
            source_state=state,
            commands=commands,
            receipts=tuple(receipts),
            reconciliation_required=self.reconciliation_required,
            reason="target execution uncertainty" if self.reconciliation_required else None,
        )
