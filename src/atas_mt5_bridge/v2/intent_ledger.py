from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from hashlib import blake2s
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

        # An absent order is a cancellation only when ATAS supplied a complete
        # inventory.  ChartStrategy recovery snapshots intentionally mark
        # manually-entered orders as incomplete; retiring their target legs on
        # a reconnect would incorrectly cancel live MT5 entries or clear
        # protection.  An explicit callback remains authoritative for that
        # individual source order even with an incomplete inventory.
        for intent_id, intent in tuple(self.intents.items()):
            if intent.state != IntentState.ACTIVE:
                continue
            # ATAS order ids identify the broker order instance and survive a
            # probe-session restart.  A directly observed cancellation may
            # therefore retire the pre-reconnect mapping as well.
            was_explicitly_removed = intent.source_key.source_order_id in source.explicitly_removed_order_ids
            absent_from_complete_inventory = source.orders_complete and intent_id not in active_intent_ids
            if was_explicitly_removed or absent_from_complete_inventory:
                self.intents[intent_id] = replace(intent, state=IntentState.RETIRED)

        # A missing target leg has already been proved absent by a *complete*
        # MT5 snapshot.  It must not indefinitely quarantine a new ATAS probe
        # session merely because the old session could not enumerate every
        # historic source order during reconnect.  There is no target-side
        # risk left to reconcile in this case, so retain the audit record but
        # close the stale leg.  Live ACTIVE/PARTIAL legs are intentionally not
        # touched here: an incomplete ATAS inventory must never cancel them.
        for leg_id, leg in tuple(self.legs.items()):
            if (
                leg.state == TargetLegState.MISSING_ON_TARGET
                and leg.source_key.session_id != source.session_id
            ):
                self.legs[leg_id] = replace(leg, state=TargetLegState.CANCELLED, target_order_ticket=None)

    def ensure_legs(self, intent_id: str, specs: Iterable[TargetLegSpec]) -> tuple[TargetLeg, ...]:
        """Return the live layout, preserving cancelled legs as audit history.

        An unchanged source order keeps its original leg. A changed order
        first retires the active target leg; only after cancellation is
        observed is a new versioned leg created. Desired-but-not-dispatched
        legs can be updated in place because no MT5 request exists yet.
        """

        intent = self.intents[intent_id]
        requested = tuple(specs)
        existing = self.legs_for_intent(intent_id)
        live = tuple(
            leg
            for leg in existing
            if leg.state
            not in {
                TargetLegState.CANCELLED,
                TargetLegState.FILLED,
            }
        )
        if live and _layout_matches(live, requested):
            return live
        if live:
            if all(leg.state == TargetLegState.DESIRED for leg in live) and len(live) == len(requested):
                updated = tuple(_replace_spec(leg, spec) for leg, spec in zip(live, requested, strict=True))
                for leg in updated:
                    self.legs[leg.leg_id] = leg
                return updated
            return live

        created: list[TargetLeg] = []
        starting_index = len(existing) + 1
        for index, spec in enumerate(requested, start=starting_index):
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
                bridge_tag=_bridge_tag(f"{intent_id}:leg:{index}"),
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
        leg_id_by_tag = {leg.bridge_tag: leg.leg_id for leg in self.legs.values() if leg.bridge_tag}
        for order in target.pending_orders:
            if order.leg_id or not order.bridge_tag:
                continue
            leg_id = leg_id_by_tag.get(order.bridge_tag)
            if leg_id is not None:
                orders_by_leg_id[leg_id] = order
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
                # A complete MT5 snapshot may still contain an order while a
                # just-submitted cancellation is in flight.  Preserve that
                # state so the policy does not issue duplicate cancellations
                # on each following snapshot.
                state = (
                    TargetLegState.CANCEL_PENDING
                    if leg.state == TargetLegState.CANCEL_PENDING
                    else TargetLegState.PARTIAL_FILL
                    if order.quantity < leg.quantity
                    else TargetLegState.ACTIVE
                )
                self.legs[leg_id] = replace(
                    leg,
                    state=state,
                    target_order_ticket=order.ticket,
                )
                continue

            if leg.state == TargetLegState.CANCEL_PENDING:
                self.legs[leg_id] = replace(leg, state=TargetLegState.CANCELLED, target_order_ticket=None)
            elif leg.state in {TargetLegState.ACTIVE, TargetLegState.PARTIAL_FILL}:
                intent = self.intents.get(leg.intent_id)
                state = TargetLegState.CANCELLED if intent is not None and intent.state == IntentState.RETIRED else TargetLegState.MISSING_ON_TARGET
                self.legs[leg_id] = replace(leg, state=state, target_order_ticket=None)

    def has_unresolved_leg(self) -> bool:
        return any(
            leg.state
            in {
                TargetLegState.CREATE_PENDING,
                TargetLegState.CANCEL_PENDING,
                TargetLegState.MISSING_ON_TARGET,
                TargetLegState.UNKNOWN,
            }
            for leg in self.legs.values()
        )

    def has_in_flight_leg(self) -> bool:
        return any(
            leg.state in {TargetLegState.CREATE_PENDING, TargetLegState.CANCEL_PENDING}
            for leg in self.legs.values()
        )

    def has_unknown_leg(self) -> bool:
        return any(leg.state in {TargetLegState.UNKNOWN, TargetLegState.MISSING_ON_TARGET} for leg in self.legs.values())

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


def _bridge_tag(leg_id: str) -> str:
    """Fits in the short MT5 order-comment payload with the AB2 prefix."""

    return blake2s(leg_id.encode("utf-8"), digest_size=8).hexdigest()


def _layout_matches(legs: tuple[TargetLeg, ...], specs: tuple[TargetLegSpec, ...]) -> bool:
    return len(legs) == len(specs) and all(_matches_spec(leg, spec) for leg, spec in zip(legs, specs, strict=True))


def _matches_spec(leg: TargetLeg, spec: TargetLegSpec) -> bool:
    return (
        leg.role == spec.role
        and leg.side == spec.side
        and leg.order_type == spec.order_type
        and leg.quantity == spec.quantity
        and leg.price == spec.price
        and leg.trigger_price == spec.trigger_price
    )


def _replace_spec(leg: TargetLeg, spec: TargetLegSpec) -> TargetLeg:
    return replace(
        leg,
        role=spec.role,
        side=spec.side,
        order_type=spec.order_type,
        quantity=spec.quantity,
        price=spec.price,
        trigger_price=spec.trigger_price,
    )
