from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .coordinator import TargetExecutionPort
from .models import (
    BridgeCommand,
    CommandType,
    ExecutionReceipt,
    OperationStatus,
    TargetAccountModel,
    TargetAccountState,
    TargetOrderState,
    TargetPositionState,
)


@dataclass
class SimulatedTargetExecutionPort(TargetExecutionPort):
    """In-process target for deterministic safe testing; it is never MT5.

    The simulator carries bridge tags exactly as a real adapter must do. It is
    useful for testing source event ordering and intent/leg cleanup without a
    trading terminal or broker connection.
    """

    target_symbol: str
    model: TargetAccountModel = TargetAccountModel.HEDGING
    _orders: dict[str, TargetOrderState] = field(default_factory=dict)
    _positions: dict[str, TargetPositionState] = field(default_factory=dict)
    _ticket_counter: int = 0

    def read_state(self, target_symbol: str) -> TargetAccountState:
        if target_symbol != self.target_symbol:
            raise ValueError(f"unexpected target symbol: {target_symbol}")
        long_quantity = sum(
            (position.quantity for position in self._positions.values() if position.side == "long"),
            start=Decimal("0"),
        )
        short_quantity = sum(
            (position.quantity for position in self._positions.values() if position.side == "short"),
            start=Decimal("0"),
        )
        return TargetAccountState(
            target_symbol=self.target_symbol,
            model=self.model,
            long_quantity=long_quantity,
            short_quantity=short_quantity,
            pending_orders=tuple(self._orders.values()),
            positions=tuple(self._positions.values()),
        )

    def execute(self, command: BridgeCommand) -> ExecutionReceipt:
        if command.target_symbol != self.target_symbol:
            return ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="wrong target symbol")
        if command.command_type == CommandType.CREATE_PENDING:
            return self._create_pending(command)
        if command.command_type == CommandType.CANCEL_PENDING:
            return self._cancel_pending(command)
        if command.command_type == CommandType.EXECUTE_MARKET:
            return self._execute_market(command)
        if command.command_type == CommandType.CLOSE_POSITION:
            return self._close_position(command)
        return ExecutionReceipt(command.command_id, OperationStatus.CONFIRMED, message="simulated no-op")

    def _create_pending(self, command: BridgeCommand) -> ExecutionReceipt:
        if command.leg_id is None:
            return ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="missing leg id")
        ticket = self._ticket("order")
        self._orders[ticket] = TargetOrderState(
            ticket=ticket,
            side=str(command.payload["side"]),
            order_type=str(command.payload["order_type"]),
            quantity=_decimal(command.payload["quantity"]),
            price=_optional_decimal(command.payload.get("price")),
            trigger_price=_optional_decimal(command.payload.get("trigger_price")),
            source_order_id=command.source_order_id,
            leg_id=command.leg_id,
        )
        return ExecutionReceipt(command.command_id, OperationStatus.CONFIRMED, target_reference=ticket)

    def _cancel_pending(self, command: BridgeCommand) -> ExecutionReceipt:
        ticket = command.payload.get("target_order_ticket")
        if ticket is not None:
            self._orders.pop(str(ticket), None)
        elif command.leg_id is not None:
            for existing_ticket, order in tuple(self._orders.items()):
                if order.leg_id == command.leg_id:
                    del self._orders[existing_ticket]
        return ExecutionReceipt(command.command_id, OperationStatus.CONFIRMED)

    def _execute_market(self, command: BridgeCommand) -> ExecutionReceipt:
        side = str(command.payload["side"]).lower()
        ticket = self._ticket("position")
        self._positions[ticket] = TargetPositionState(
            ticket=ticket,
            side="long" if side == "buy" else "short",
            quantity=_decimal(command.payload["quantity"]),
        )
        return ExecutionReceipt(command.command_id, OperationStatus.CONFIRMED, target_reference=ticket)

    def _close_position(self, command: BridgeCommand) -> ExecutionReceipt:
        ticket = str(command.payload["position_ticket"])
        position = self._positions.get(ticket)
        if position is None:
            return ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="position ticket not found")
        quantity = _decimal(command.payload["quantity"])
        if quantity <= 0 or quantity > position.quantity:
            return ExecutionReceipt(command.command_id, OperationStatus.FAILED, message="invalid close quantity")
        remaining = position.quantity - quantity
        if remaining == 0:
            del self._positions[ticket]
        else:
            self._positions[ticket] = TargetPositionState(ticket, position.side, remaining)
        return ExecutionReceipt(command.command_id, OperationStatus.CONFIRMED, target_reference=ticket)

    def _ticket(self, prefix: str) -> str:
        self._ticket_counter += 1
        return f"sim-{prefix}-{self._ticket_counter}"


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else _decimal(value)
