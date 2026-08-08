from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .intent_ledger import IntentLedger, TargetLegSpec
from .models import (
    BridgeCommand,
    CommandType,
    SourceSymbolState,
    TargetAccountModel,
    TargetAccountState,
    TargetLeg,
    TargetLegRole,
    TargetLegState,
)


@dataclass
class AtasAuthoritativePolicy:
    """Initial, intentionally conservative ATAS -> MT5 policy.

    It mirrors active ATAS order instances as direct target pending orders and
    converges only the bridge-owned *net* target exposure to the confirmed
    ATAS net position. It deliberately tolerates hedged long/short tickets
    when their net exposure is already correct.
    """

    volume_multiplier: Decimal = Decimal("1")
    direct_pending_enabled: bool = True
    position_reconciliation_enabled: bool = True
    execution_enabled: bool = False

    def plan(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        ledger: IntentLedger,
    ) -> tuple[BridgeCommand, ...]:
        # This policy deliberately cannot be connected to a future live MT5
        # adapter by accident. Live enablement also requires the separate
        # price-mapping/freshness guard described in the design document.
        if not self.execution_enabled:
            return ()

        ledger.synchronize_target(target)
        commands: list[BridgeCommand] = []

        # First retire target risk which no longer has an ATAS source order.
        for intent in ledger.retired_intents():
            for leg in ledger.active_or_pending_legs(intent.intent_id):
                if leg.state == TargetLegState.DESIRED:
                    ledger.update_leg(_with_state(leg, TargetLegState.CANCELLED))
                elif leg.state == TargetLegState.ACTIVE:
                    commands.append(self._cancel_command(source, target, leg))

        # A confirmed ATAS position change takes priority over creating new
        # target pending orders. This is the fast repair path after asymmetric
        # fills, while cancel commands above remain highest priority.
        if self.position_reconciliation_enabled and source.position is not None:
            position_command = self._position_command(source, target)
            if position_command is not None:
                commands.append(position_command)

        if self.direct_pending_enabled:
            for intent in ledger.active_intents():
                legs = ledger.ensure_legs(intent.intent_id, (self._direct_spec(intent.order),))
                for leg in legs:
                    if leg.state == TargetLegState.DESIRED:
                        commands.append(self._create_command(source, target, leg))

        return tuple(commands)

    def _direct_spec(self, order) -> TargetLegSpec:
        return TargetLegSpec(
            role=_role_for_source_order(order.comment),
            side=order.side,
            order_type=order.order_type,
            quantity=order.unfilled * self.volume_multiplier,
            price=order.price,
            trigger_price=order.trigger_price,
        )

    def _cancel_command(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        leg: TargetLeg,
    ) -> BridgeCommand:
        return BridgeCommand(
            command_id=f"cancel:{leg.leg_id}:{source.last_event_id}",
            command_type=CommandType.CANCEL_PENDING,
            target_symbol=target.target_symbol,
            source_event_id=source.last_event_id,
            source_order_id=leg.source_key.source_order_id,
            intent_id=leg.intent_id,
            leg_id=leg.leg_id,
            payload={"target_order_ticket": leg.target_order_ticket},
        )

    def _create_command(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        leg: TargetLeg,
    ) -> BridgeCommand:
        return BridgeCommand(
            command_id=f"create:{leg.leg_id}:{source.last_event_id}",
            command_type=CommandType.CREATE_PENDING,
            target_symbol=target.target_symbol,
            source_event_id=source.last_event_id,
            source_order_id=leg.source_key.source_order_id,
            intent_id=leg.intent_id,
            leg_id=leg.leg_id,
            payload={
                "side": leg.side,
                "order_type": leg.order_type,
                "quantity": leg.quantity,
                "price": leg.price,
                "trigger_price": leg.trigger_price,
                "role": leg.role.value,
            },
        )

    def _position_command(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
    ) -> BridgeCommand | None:
        assert source.position is not None
        desired_net = source.position.net_quantity * self.volume_multiplier
        delta = desired_net - target.net_quantity
        if delta == 0:
            return None

        # On hedging accounts, use a ticket-specific close before any new
        # market order. This reduces gross exposure without requiring an
        # artificial full flatten before source pending orders are mirrored.
        close_side = "short" if delta > 0 else "long"
        opposite_positions = tuple(position for position in target.positions if position.side.lower() == close_side)
        if opposite_positions:
            position = opposite_positions[0]
            quantity = min(abs(delta), position.quantity)
            return BridgeCommand(
                command_id=f"close:{position.ticket}:{source.last_event_id}",
                command_type=CommandType.CLOSE_POSITION,
                target_symbol=target.target_symbol,
                source_event_id=source.last_event_id,
                payload={
                    "position_ticket": position.ticket,
                    "side": "buy" if close_side == "short" else "sell",
                    "quantity": quantity,
                    "reason": "atas_net_reconcile",
                },
            )

        # A hedging snapshot without position tickets cannot safely promise a
        # reduce-only close. The MT5 adapter must first provide the tickets.
        if target.model == TargetAccountModel.HEDGING and (
            (delta > 0 and target.short_quantity > 0) or (delta < 0 and target.long_quantity > 0)
        ):
            return None

        return BridgeCommand(
            command_id=f"market:{source.last_event_id}:{'buy' if delta > 0 else 'sell'}",
            command_type=CommandType.EXECUTE_MARKET,
            target_symbol=target.target_symbol,
            source_event_id=source.last_event_id,
            payload={
                "side": "buy" if delta > 0 else "sell",
                "quantity": abs(delta),
                "reason": "atas_net_reconcile",
            },
        )


def _role_for_source_order(comment: str) -> TargetLegRole:
    """Use comments only as a temporary hint; mapping never depends on them."""

    normalized = comment.strip().upper()
    if normalized == "TP":
        return TargetLegRole.PROTECTIVE_TP
    if normalized == "SL":
        return TargetLegRole.PROTECTIVE_SL
    return TargetLegRole.ENTRY_PENDING


def _with_state(leg: TargetLeg, state: TargetLegState) -> TargetLeg:
    from dataclasses import replace

    return replace(leg, state=state)
