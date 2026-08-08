from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Iterable, Mapping

from .models import (
    BridgeIntent,
    IntentState,
    SourceOrderKey,
    SourceSymbolState,
    TargetAccountState,
    TargetLeg,
    TargetLegRole,
    TargetLegState,
)


@dataclass(frozen=True)
class TargetLegSpec:
    """A policy's declarative target leg. The ledger owns its identity and state."""

    role: TargetLegRole
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trigger_price: Decimal | None


@dataclass
class IntentLedger:
    """The durable-domain model for source intentions and 1..N target legs.

    The ledger never identifies a replacement order by price or timing. A new
    ATAS order instance creates a new intent. A policy may, however, choose to
    compile one intent into several target legs.
    """

    intents: dict[str, BridgeIntent] = field(default_factory=dict)
    legs: dict[str, TargetLeg] = field(default_factory=dict)

    def synchronize_source(self, source: SourceSymbolState) -> None:
        active_intent_ids: set[str] = set()
        for order in source.active_orders.values():
            key = SourceOrderKey(source.session_id, source.source_symbol, order.source_order_id)
            intent_id = key.value
            active_intent_ids.add(intent_id)
            existing = self.intents.get(intent_id)
            if existing is None:
                self.intents[intent_id] = BridgeIntent(
                    intent_id=intent_id,
                    source_key=key,
                    order=order,
                    state=IntentState.ACTIVE,
                )
            else:
                self.intents[intent_id] = replace(existing, order=order, state=IntentState.ACTIVE)

        # A full source state is authoritative. Any previously active source
        # instance absent from it has been cancelled, filled, or otherwise retired.
        for intent_id, intent in tuple(self.intents.items()):
            if intent.state != IntentState.ACTIVE:
                continue
            if intent.source_key.session_id != source.session_id or intent_id not in active_intent_ids:
                self.intents[intent_id] = replace(intent, state=IntentState.RETIRED)

    def ensure_legs(self, intent_id: str, specs: Iterable[TargetLegSpec]) -> tuple[TargetLeg, ...]:
        """Create legs only once for an intent and return their current state.

        Replacing a desired layout is intentionally a separate policy action;
        silent replacement would lose the audit trail for an already submitted
        target order.
        """

        intent = self.intents[intent_id]
        existing = self.legs_for_intent(intent_id)
        if existing:
            return existing

        created: list[TargetLeg] = []
        for index, spec in enumerate(specs, start=1):
            if spec.quantity <= 0:
                raise ValueError("target leg quantity must be positive")
            leg = TargetLeg(
                leg_id=f"{intent_id}:leg:{index}",
                intent_id=intent_id,
                source_key=intent.source_key,
                role=spec.role,
                side=spec.side,
                order_type=spec.order_type,
                quantity=spec.quantity,
                price=spec.price,
                trigger_price=spec.trigger_price,
            )
            self.legs[leg.leg_id] = leg
            created.append(leg)
        return tuple(created)

    def legs_for_intent(self, intent_id: str) -> tuple[TargetLeg, ...]:
        return tuple(leg for leg in self.legs.values() if leg.intent_id == intent_id)

    def active_intents(self) -> tuple[BridgeIntent, ...]:
        return tuple(intent for intent in self.intents.values() if intent.state == IntentState.ACTIVE)

    def retired_intents(self) -> tuple[BridgeIntent, ...]:
        return tuple(intent for intent in self.intents.values() if intent.state == IntentState.RETIRED)

    def get_leg(self, leg_id: str) -> TargetLeg | None:
        return self.legs.get(leg_id)

    def update_leg(self, leg: TargetLeg) -> None:
        if leg.leg_id not in self.legs:
            raise KeyError(f"unknown target leg: {leg.leg_id}")
        self.legs[leg.leg_id] = leg

    def mark_leg_create_pending(self, leg_id: str) -> None:
        leg = self._require_leg(leg_id)
        self.update_leg(replace(leg, state=TargetLegState.CREATE_PENDING))

    def mark_leg_cancel_pending(self, leg_id: str) -> None:
        leg = self._require_leg(leg_id)
        self.update_leg(replace(leg, state=TargetLegState.CANCEL_PENDING))

    def mark_leg_failed(self, leg_id: str) -> None:
        leg = self._require_leg(leg_id)
        self.update_leg(replace(leg, state=TargetLegState.FAILED))

    def mark_leg_unknown(self, leg_id: str) -> None:
        leg = self._require_leg(leg_id)
        self.update_leg(replace(leg, state=TargetLegState.UNKNOWN))

    def synchronize_target(self, target: TargetAccountState) -> None:
        """Apply a confirmed complete target snapshot to target-leg state.

        A target order must carry ``leg_id``. The legacy source-order fallback
        is permitted only when that source intent has exactly one target leg;
        otherwise a one-to-many mapping would be ambiguous.
        """

        if not target.snapshot_complete:
            return

        orders_by_leg_id = {order.leg_id: order for order in target.pending_orders if order.leg_id}
        single_leg_by_source_order = self._single_leg_by_source_order()
        for order in target.pending_orders:
            if order.leg_id or order.source_order_id is None:
                continue
            leg = single_leg_by_source_order.get(order.source_order_id)
            if leg is not None:
                orders_by_leg_id[leg.leg_id] = order

        for leg_id, leg in tuple(self.legs.items()):
            order = orders_by_leg_id.get(leg_id)
            if order is not None:
                self.legs[leg_id] = replace(
                    leg,
                    state=TargetLegState.ACTIVE,
                    target_order_ticket=order.ticket,
                )
                continue

            if leg.state == TargetLegState.CANCEL_PENDING:
                self.legs[leg_id] = replace(leg, state=TargetLegState.CANCELLED, target_order_ticket=None)

    def has_unresolved_leg(self) -> bool:
        return any(
            leg.state in {TargetLegState.CREATE_PENDING, TargetLegState.CANCEL_PENDING, TargetLegState.UNKNOWN}
            for leg in self.legs.values()
        )

    def has_in_flight_leg(self) -> bool:
        return any(
            leg.state in {TargetLegState.CREATE_PENDING, TargetLegState.CANCEL_PENDING}
            for leg in self.legs.values()
        )

    def has_unknown_leg(self) -> bool:
        return any(leg.state == TargetLegState.UNKNOWN for leg in self.legs.values())

    def active_or_pending_legs(self, intent_id: str) -> tuple[TargetLeg, ...]:
        return tuple(
            leg
            for leg in self.legs_for_intent(intent_id)
            if leg.state
            in {
                TargetLegState.DESIRED,
                TargetLegState.CREATE_PENDING,
                TargetLegState.ACTIVE,
                TargetLegState.CANCEL_PENDING,
            }
        )

    def _single_leg_by_source_order(self) -> Mapping[str, TargetLeg]:
        candidates: dict[str, list[TargetLeg]] = {}
        for leg in self.legs.values():
            candidates.setdefault(leg.source_key.source_order_id, []).append(leg)
        return {source_id: legs[0] for source_id, legs in candidates.items() if len(legs) == 1}

    def _require_leg(self, leg_id: str) -> TargetLeg:
        leg = self.legs.get(leg_id)
        if leg is None:
            raise KeyError(f"unknown target leg: {leg_id}")
        return leg
