from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .models import SourceEvent, SourceEventType, SourceOrder, SourceOrderState, SourcePosition, SourceQuote


def parse_probe_message(message: Mapping[str, Any]) -> SourceEvent:
    """Convert the read-only probe v1 schema into a canonical v2 event.

    Quotes and lifecycle messages become auxiliary events. They do not enter a
    copy policy, but they must be retained for global event-ID continuity.
    """

    event_type = str(message.get("event_type", ""))
    data = message.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("probe event data must be an object")

    common = {
        "session_id": _required_text(message, "session_id"),
        "event_id": int(message["event_id"]),
        "source_symbol": _required_text(message, "source_symbol"),
        "occurred_at_utc": _required_text(message, "atas_timestamp_utc"),
    }
    if event_type in {"order_new", "order_changed"}:
        return SourceEvent(event_type=SourceEventType.ORDER, order=_parse_order(data), **common)
    if event_type == "position_changed":
        return SourceEvent(event_type=SourceEventType.POSITION, position=_parse_position(common["source_symbol"], data), **common)
    if event_type == "state_snapshot":
        raw_orders = data.get("orders", ())
        if not isinstance(raw_orders, list):
            raise ValueError("state snapshot orders must be a list")
        raw_position = data.get("last_observed_position")
        position = _parse_position(common["source_symbol"], raw_position) if isinstance(raw_position, Mapping) else None
        return SourceEvent(
            event_type=SourceEventType.SNAPSHOT,
            position=position,
            snapshot_orders=tuple(_parse_order(order) for order in raw_orders if isinstance(order, Mapping)),
            snapshot_orders_complete=bool(data.get("orders_complete", True)),
            position_complete=bool(data.get("position_known", position is not None)),
            **common,
        )
    if event_type == "quote":
        return SourceEvent(
            event_type=SourceEventType.AUXILIARY,
            quote=SourceQuote(
                source_symbol=common["source_symbol"],
                bid=_optional_decimal(data.get("best_bid")),
                ask=_optional_decimal(data.get("best_ask")),
                occurred_at_utc=common["occurred_at_utc"],
            ),
            **common,
        )
    return SourceEvent(event_type=SourceEventType.AUXILIARY, **common)


def _parse_order(data: Mapping[str, Any]) -> SourceOrder:
    state_text = str(data.get("state", "unknown")).lower()
    state = SourceOrderState(state_text) if state_text in SourceOrderState._value2member_map_ else SourceOrderState.UNKNOWN
    return SourceOrder(
        source_order_id=_required_text(data, "id"),
        source_external_id=_optional_text(data.get("ext_id")),
        source_symbol=_required_text(data, "security"),
        side=_required_text(data, "direction").lower(),
        order_type=_required_text(data, "order_type").lower(),
        state=state,
        quantity=_decimal(data.get("quantity_to_fill")),
        unfilled=_decimal(data.get("unfilled")),
        price=_optional_decimal(data.get("price")),
        trigger_price=_optional_decimal(data.get("trigger_price")),
        cancelled=bool(data.get("canceled", False)),
        comment=str(data.get("comment", "")),
        metadata={
            "user_ext_id": data.get("user_ext_id"),
            "oco_group": data.get("oco_group"),
            "is_attached": data.get("is_attached"),
        },
    )


def _parse_position(source_symbol: str, data: Mapping[str, Any]) -> SourcePosition:
    return SourcePosition(
        source_symbol=source_symbol,
        net_quantity=_decimal(data.get("volume")),
        average_price=_optional_decimal(data.get("average_price")),
    )


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _decimal(value: object) -> Decimal:
    if value is None:
        raise ValueError("decimal value is required")
    return Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))
