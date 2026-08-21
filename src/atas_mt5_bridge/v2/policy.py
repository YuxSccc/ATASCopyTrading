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
from .pricing import PriceMapper


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
    price_mapper: PriceMapper | None = None
    require_price_guard: bool = False
    # Retain a bridge-owned MT5 pending fill briefly while ATAS reports the
    # source order as still active. This avoids an unnecessary MT5 close and
    # re-entry solely because ATAS's fill callback arrived later.
    target_fill_grace_seconds: int = 30

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

        self.observe_market_data(source, target)

        ledger.synchronize_target(target)
        commands: list[BridgeCommand] = []

        # First retire target risk which no longer has an ATAS source order.
        for intent in ledger.retired_intents():
            for leg in ledger.active_or_pending_legs(intent.intent_id):
                if leg.state == TargetLegState.DESIRED:
                    ledger.update_leg(_with_state(leg, TargetLegState.CANCELLED))
                elif leg.state == TargetLegState.ACTIVE:
                    commands.append(self._cancel_command(source, target, leg))
                elif leg.state == TargetLegState.CREATE_PENDING and leg.target_order_ticket is not None:
                    # The EA has synchronously accepted the pending order and
                    # returned its MT5 ticket, but the next full snapshot has
                    # not arrived yet.  A later ATAS cancellation must be
                    # able to remove that exact order immediately rather than
                    # waiting for recovery polling.
                    commands.append(self._cancel_command(source, target, leg))

        # The user elected an aggressive partial-fill rule: any target order
        # with a reduced remaining volume is treated as asymmetric execution.
        # Cancel its remainder first; the next complete target snapshot then
        # reconciles bridge net exposure back to ATAS' confirmed net position.
        for leg in ledger.legs.values():
            if leg.state == TargetLegState.PARTIAL_FILL:
                commands.append(self._cancel_command(source, target, leg))

        # A confirmed ATAS position change takes priority over creating new
        # target pending orders. This is the fast repair path after asymmetric
        # fills, while cancel commands above remain highest priority.
        if self.position_reconciliation_enabled and source.position_complete and source.position is not None:
            position_command = self._position_command(
                source,
                target,
                self._reserved_target_fill_quantity(source, target, ledger),
            )
            if position_command is not None:
                commands.append(position_command)

        if self.direct_pending_enabled:
            for intent in ledger.active_intents():
                # A reconnect with an incomplete ATAS order inventory must not
                # replay a pending order from an earlier probe session.  It is
                # retained in the ledger only to preserve its existing MT5
                # mapping until ATAS can explicitly cancel or fully enumerate
                # it.
                if intent.source_key.session_id != source.session_id:
                    continue
                if intent.order.source_order_id not in source.dispatchable_order_ids:
                    continue
                if _role_for_source_order(intent.order.comment) in {
                    TargetLegRole.PROTECTIVE_TP,
                    TargetLegRole.PROTECTIVE_SL,
                }:
                    continue
                spec = self._direct_spec(intent.order, source, target)
                if spec is None:
                    continue
                legs = ledger.ensure_legs(intent.intent_id, (spec,))
                for leg in legs:
                    if _leg_matches_spec(leg, spec) and leg.state == TargetLegState.DESIRED:
                        commands.append(self._create_command(source, target, leg))
                    elif not _leg_matches_spec(leg, spec) and leg.state == TargetLegState.ACTIVE:
                        commands.append(self._cancel_command(source, target, leg))

        commands.extend(self._protection_commands(source, target))

        return tuple(commands)

    def observe_market_data(self, source: SourceSymbolState, target: TargetAccountState) -> None:
        if self.price_mapper is not None:
            self.price_mapper.record_pair(source.quote, target.quote)

    def export_market_state(self) -> dict | None:
        return self.price_mapper.to_data() if self.price_mapper is not None else None

    def _direct_spec(
        self,
        order,
        source: SourceSymbolState,
        target: TargetAccountState,
    ) -> TargetLegSpec | None:
        if order.side.lower() not in {"buy", "sell"} or order.order_type.lower() not in {"limit", "stop"}:
            # Do not compress ATAS StopLimit or a future order type into a
            # different MT5 order class. Unsupported semantics must remain
            # observable but non-executable until explicitly modelled.
            return None
        price = order.trigger_price if "stop" in order.order_type.lower() else order.price
        trigger_price = None
        target_quantity = order.unfilled * self.volume_multiplier
        if self.price_mapper is not None:
            decision = self.price_mapper.map_pending(order, source.quote, target, target_quantity=target_quantity)
            if not decision.allowed:
                return None
            price = decision.price
            trigger_price = decision.trigger_price
        elif self.require_price_guard:
            return None
        return TargetLegSpec(
            role=_role_for_source_order(order.comment),
            side=order.side,
            order_type=order.order_type,
            quantity=target_quantity,
            price=price,
            trigger_price=trigger_price,
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
                "bridge_tag": leg.bridge_tag,
            },
        )

    def _position_command(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        reserved_target_fill_quantity: Decimal = Decimal("0"),
    ) -> BridgeCommand | None:
        assert source.position is not None
        desired_net = source.position.net_quantity * self.volume_multiplier
        delta = desired_net - target.net_quantity
        # Reserve only the known MT5-first fill. Any excess mismatch keeps
        # reconciling immediately, so this exception never blocks the symbol.
        if delta < 0:
            delta += min(-delta, reserved_target_fill_quantity)
        elif delta > 0:
            delta -= min(delta, reserved_target_fill_quantity)
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
            if not _valid_target_volume(quantity, target, self.require_price_guard):
                return None
            return BridgeCommand(
                command_id=f"close:{position.ticket}:{source.last_event_id}",
                command_type=CommandType.CLOSE_POSITION,
                target_symbol=target.target_symbol,
                source_event_id=source.last_event_id,
                payload={
                    "position_ticket": position.ticket,
                    "side": "buy" if close_side == "short" else "sell",
                    "quantity": quantity,
                    "expected_position_remaining": position.quantity - quantity,
                    "reason": "atas_net_reconcile",
                },
            )

        # A hedging snapshot without position tickets cannot safely promise a
        # reduce-only close. The MT5 adapter must first provide the tickets.
        if target.model == TargetAccountModel.HEDGING and (
            (delta > 0 and target.short_quantity > 0) or (delta < 0 and target.long_quantity > 0)
        ):
            return None

        if not _valid_target_volume(abs(delta), target, self.require_price_guard):
            return None

        return BridgeCommand(
            command_id=f"market:{source.last_event_id}:{'buy' if delta > 0 else 'sell'}",
            command_type=CommandType.EXECUTE_MARKET,
            target_symbol=target.target_symbol,
            source_event_id=source.last_event_id,
            payload={
                "side": "buy" if delta > 0 else "sell",
                "quantity": abs(delta),
                "expected_target_net": target.net_quantity + delta,
                "reason": "atas_net_reconcile",
            },
        )

    def target_fill_grace_eligible(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        leg: TargetLeg,
    ) -> bool:
        """Recognize a missing pending order as a known target fill only.

        An absent order by itself can be a manual MT5 cancellation. Grace is
        allowed only when a complete snapshot also shows the exact directional
        target excess implied by this still-active ATAS order.
        """

        if self.target_fill_grace_seconds <= 0 or leg.role != TargetLegRole.ENTRY_PENDING:
            return False
        if leg.state not in {TargetLegState.MISSING_ON_TARGET, TargetLegState.TARGET_FILL_GRACE}:
            return False
        source_order = source.active_orders.get(leg.source_key.source_order_id)
        if source_order is None or source.position is None or not source.position_complete:
            return False
        if source_order.side.lower() != leg.side.lower() or source_order.unfilled <= 0:
            return False
        desired_net = source.position.net_quantity * self.volume_multiplier
        directional_excess = target.net_quantity - desired_net
        if leg.side.lower() == "sell":
            directional_excess = -directional_excess
        return directional_excess >= leg.quantity

    def _reserved_target_fill_quantity(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
        ledger: IntentLedger,
    ) -> Decimal:
        if source.position is None:
            return Decimal("0")
        delta = source.position.net_quantity * self.volume_multiplier - target.net_quantity
        if delta == 0:
            return Decimal("0")
        protected_side = "buy" if delta < 0 else "sell"
        return sum(
            (
                leg.quantity
                for leg in ledger.legs.values()
                if leg.state == TargetLegState.TARGET_FILL_GRACE and leg.side.lower() == protected_side
            ),
            Decimal("0"),
        )

    def _protection_commands(
        self,
        source: SourceSymbolState,
        target: TargetAccountState,
    ) -> tuple[BridgeCommand, ...]:
        """Mirror ATAS's single TP/SL pair as native MT5 position protection.

        Native protection is only safe when the bridge-owned target positions
        are all in the ATAS net direction.  Opposing hedge tickets are
        deliberately left untouched rather than assigning a long TP/SL to a
        short ticket merely because the *net* happens to match.
        """

        if source.position is None or source.position.net_quantity == 0 or target.has_opposing_exposure:
            return ()
        expected_side = "long" if source.position.net_quantity > 0 else "short"
        if any(position.side.lower() != expected_side for position in target.positions):
            return ()

        tp_orders = tuple(
            order
            for order in source.active_orders.values()
            if order.source_order_id in source.dispatchable_order_ids
            and _role_for_source_order(order.comment) == TargetLegRole.PROTECTIVE_TP
        )
        sl_orders = tuple(
            order
            for order in source.active_orders.values()
            if order.source_order_id in source.dispatchable_order_ids
            and _role_for_source_order(order.comment) == TargetLegRole.PROTECTIVE_SL
        )
        if len(tp_orders) > 1 or len(sl_orders) > 1:
            return ()
        expected_exit_side = "sell" if expected_side == "long" else "buy"
        all_protection_orders = (*tp_orders, *sl_orders)
        if any(order.side.lower() != expected_exit_side for order in all_protection_orders):
            return ()
        required_quantity = abs(source.position.net_quantity) * self.volume_multiplier
        if any(order.unfilled * self.volume_multiplier != required_quantity for order in all_protection_orders):
            return ()

        stop_loss = self._protection_price(sl_orders[0], source, target) if sl_orders else None
        take_profit = self._protection_price(tp_orders[0], source, target) if tp_orders else None
        if (sl_orders and stop_loss is None) or (tp_orders and take_profit is None):
            return ()
        if not _valid_native_protection(expected_side, stop_loss, take_profit, target):
            return ()

        commands: list[BridgeCommand] = []
        for position in target.positions:
            # A callback-derived source inventory can prove an add/move, but
            # cannot prove that an unseen TP/SL was removed after a strategy
            # reconnect.  Preserve the corresponding native MT5 field until
            # ATAS sends a complete inventory or an explicit removal event.
            desired_stop_loss = stop_loss if sl_orders or source.orders_complete else position.stop_loss
            desired_take_profit = take_profit if tp_orders or source.orders_complete else position.take_profit
            if not _valid_native_protection(expected_side, desired_stop_loss, desired_take_profit, target):
                continue
            if position.stop_loss == desired_stop_loss and position.take_profit == desired_take_profit:
                continue
            commands.append(
                BridgeCommand(
                    command_id=(
                        f"protection:{position.ticket}:{source.last_event_id}:"
                        f"{_command_price(desired_stop_loss)}:{_command_price(desired_take_profit)}"
                    ),
                    command_type=CommandType.MODIFY_POSITION_PROTECTION,
                    target_symbol=target.target_symbol,
                    source_event_id=source.last_event_id,
                    source_order_id=(tp_orders[0].source_order_id if tp_orders else sl_orders[0].source_order_id if sl_orders else None),
                    payload={
                        "position_ticket": position.ticket,
                        "stop_loss": desired_stop_loss,
                        "take_profit": desired_take_profit,
                    },
                )
            )
        return tuple(commands)

    def _protection_price(self, order, source: SourceSymbolState, target: TargetAccountState) -> Decimal | None:
        if self.price_mapper is not None:
            decision = self.price_mapper.map_protection(order, source.quote, target)
            return decision.price if decision.allowed else None
        if self.require_price_guard:
            return None
        return order.trigger_price if "stop" in order.order_type.lower() else order.price


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


def _valid_native_protection(
    position_side: str,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    target: TargetAccountState,
) -> bool:
    if stop_loss is None and take_profit is None:
        return True
    if target.quote is None or target.symbol_spec is None:
        return False
    quote = target.quote
    if quote.bid is None or quote.ask is None:
        return False
    minimum_distance = max(target.symbol_spec.stops_level, target.symbol_spec.freeze_level)
    if position_side == "long":
        return (stop_loss is None or stop_loss <= quote.bid - minimum_distance) and (
            take_profit is None or take_profit >= quote.bid + minimum_distance
        )
    return (stop_loss is None or stop_loss >= quote.ask + minimum_distance) and (
        take_profit is None or take_profit <= quote.ask - minimum_distance
    )


def _command_price(value: Decimal | None) -> str:
    return "clear" if value is None else str(value)


def _valid_target_volume(quantity: Decimal, target: TargetAccountState, require_specification: bool) -> bool:
    specification = target.symbol_spec
    if specification is None:
        return not require_specification
    if quantity < specification.volume_min or quantity > specification.volume_max or specification.volume_step <= 0:
        return False
    steps = (quantity - specification.volume_min) / specification.volume_step
    return steps == steps.to_integral_value()


def _leg_matches_spec(leg: TargetLeg, spec: TargetLegSpec) -> bool:
    return (
        leg.role == spec.role
        and leg.side == spec.side
        and leg.order_type == spec.order_type
        and leg.quantity == spec.quantity
        and leg.price == spec.price
        and leg.trigger_price == spec.trigger_price
    )
