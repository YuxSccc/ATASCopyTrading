from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .intent_ledger import IntentLedger
from .models import (
    BridgeCommand,
    BridgeIntent,
    IntentState,
    OperationRecord,
    OperationStatus,
    SourceOrder,
    SourceOrderKey,
    SourceOrderState,
    SourcePosition,
    SourceQuote,
    SourceSymbolState,
    TargetLeg,
    TargetLegRole,
    TargetLegState,
)


def source_state_to_data(state: SourceSymbolState) -> dict[str, Any]:
    return {
        "source_symbol": state.source_symbol,
        "session_id": state.session_id,
        "last_event_id": state.last_event_id,
        "position": _position_to_data(state.position),
        "active_orders": [_order_to_data(order) for order in state.active_orders.values()],
        "synchronized": state.synchronized,
        "quote": _quote_to_data(state.quote),
        "position_complete": state.position_complete,
        "source_account": state.source_account,
        "orders_complete": state.orders_complete,
        "explicitly_removed_order_ids": sorted(state.explicitly_removed_order_ids),
    }


def source_state_from_data(data: Mapping[str, Any]) -> SourceSymbolState:
    orders = tuple(_order_from_data(item) for item in _list(data, "active_orders"))
    return SourceSymbolState(
        source_symbol=_text(data, "source_symbol"),
        session_id=_text(data, "session_id"),
        last_event_id=int(data["last_event_id"]),
        position=_position_from_data(data.get("position")),
        active_orders={order.source_order_id: order for order in orders},
        synchronized=bool(data["synchronized"]),
        quote=_quote_from_data(data.get("quote")),
        position_complete=bool(data.get("position_complete", data.get("position") is not None)),
        source_account=str(data.get("source_account", "")).strip(),
        # Checkpoints produced before order-inventory provenance existed are
        # deliberately treated as incomplete.  The safer failure mode is to
        # preserve target orders/protection until ATAS proves their removal.
        orders_complete=bool(data.get("orders_complete", False)),
        explicitly_removed_order_ids=frozenset(str(item) for item in data.get("explicitly_removed_order_ids", []) if str(item)),
    )


def ledger_to_data(ledger: IntentLedger) -> dict[str, Any]:
    return {
        "intents": [
            {
                "intent_id": intent.intent_id,
                "source_key": _source_key_to_data(intent.source_key),
                "order": _order_to_data(intent.order),
                "state": intent.state.value,
            }
            for intent in ledger.intents.values()
        ],
        "legs": [
            {
                "leg_id": leg.leg_id,
                "intent_id": leg.intent_id,
                "source_key": _source_key_to_data(leg.source_key),
                "role": leg.role.value,
                "side": leg.side,
                "order_type": leg.order_type,
                "quantity": str(leg.quantity),
                "price": _optional_decimal_to_data(leg.price),
                "trigger_price": _optional_decimal_to_data(leg.trigger_price),
                "state": leg.state.value,
                "target_order_ticket": leg.target_order_ticket,
                "target_position_ticket": leg.target_position_ticket,
                "bridge_tag": leg.bridge_tag,
                "target_fill_grace_started_at_utc": leg.target_fill_grace_started_at_utc,
            }
            for leg in ledger.legs.values()
        ],
    }


def ledger_from_data(data: Mapping[str, Any]) -> IntentLedger:
    ledger = IntentLedger()
    for item in _list(data, "intents"):
        source_key = _source_key_from_data(_mapping(item, "source_key"))
        intent = BridgeIntent(
            intent_id=_text(item, "intent_id"),
            source_key=source_key,
            order=_order_from_data(_mapping(item, "order")),
            state=IntentState(_text(item, "state")),
        )
        ledger.intents[intent.intent_id] = intent
    for item in _list(data, "legs"):
        leg = TargetLeg(
            leg_id=_text(item, "leg_id"),
            intent_id=_text(item, "intent_id"),
            source_key=_source_key_from_data(_mapping(item, "source_key")),
            role=TargetLegRole(_text(item, "role")),
            side=_text(item, "side"),
            order_type=_text(item, "order_type"),
            quantity=Decimal(str(item["quantity"])),
            price=_optional_decimal_from_data(item.get("price")),
            trigger_price=_optional_decimal_from_data(item.get("trigger_price")),
            state=TargetLegState(_text(item, "state")),
            target_order_ticket=_optional_text(item.get("target_order_ticket")),
            target_position_ticket=_optional_text(item.get("target_position_ticket")),
            bridge_tag=str(item.get("bridge_tag", "")),
            target_fill_grace_started_at_utc=_optional_text(item.get("target_fill_grace_started_at_utc")),
        )
        ledger.legs[leg.leg_id] = leg
    return ledger


def operation_record_to_data(record: OperationRecord) -> dict[str, Any]:
    return {
        "command": {
            "command_id": record.command.command_id,
            "command_type": record.command.command_type.value,
            "target_symbol": record.command.target_symbol,
            "source_event_id": record.command.source_event_id,
            "source_order_id": record.command.source_order_id,
            "intent_id": record.command.intent_id,
            "leg_id": record.command.leg_id,
            "payload": _normalize(record.command.payload),
        },
        "status": record.status.value,
        "target_reference": record.target_reference,
        "message": record.message,
        "dispatched_at_utc": record.dispatched_at_utc,
        "resolved_at_utc": record.resolved_at_utc,
    }


def operation_record_from_data(data: Mapping[str, Any]) -> OperationRecord:
    from .models import CommandType

    command = _mapping(data, "command")
    return OperationRecord(
        command=BridgeCommand(
            command_id=_text(command, "command_id"),
            command_type=CommandType(_text(command, "command_type")),
            target_symbol=_text(command, "target_symbol"),
            source_event_id=int(command["source_event_id"]),
            source_order_id=_optional_text(command.get("source_order_id")),
            intent_id=_optional_text(command.get("intent_id")),
            leg_id=_optional_text(command.get("leg_id")),
            payload=_denormalize(command.get("payload", {})),
        ),
        status=OperationStatus(_text(data, "status")),
        target_reference=_optional_text(data.get("target_reference")),
        message=str(data.get("message", "")),
        dispatched_at_utc=_optional_text(data.get("dispatched_at_utc")),
        resolved_at_utc=_optional_text(data.get("resolved_at_utc")),
    )


def _order_to_data(order: SourceOrder) -> dict[str, Any]:
    return {
        "source_order_id": order.source_order_id,
        "source_external_id": order.source_external_id,
        "source_symbol": order.source_symbol,
        "side": order.side,
        "order_type": order.order_type,
        "state": order.state.value,
        "quantity": str(order.quantity),
        "unfilled": str(order.unfilled),
        "price": _optional_decimal_to_data(order.price),
        "trigger_price": _optional_decimal_to_data(order.trigger_price),
        "cancelled": order.cancelled,
        "comment": order.comment,
        "metadata": _normalize(order.metadata),
    }


def _order_from_data(data: Mapping[str, Any]) -> SourceOrder:
    return SourceOrder(
        source_order_id=_text(data, "source_order_id"),
        source_external_id=_optional_text(data.get("source_external_id")),
        source_symbol=_text(data, "source_symbol"),
        side=_text(data, "side"),
        order_type=_text(data, "order_type"),
        state=SourceOrderState(_text(data, "state")),
        quantity=Decimal(str(data["quantity"])),
        unfilled=Decimal(str(data["unfilled"])),
        price=_optional_decimal_from_data(data.get("price")),
        trigger_price=_optional_decimal_from_data(data.get("trigger_price")),
        cancelled=bool(data["cancelled"]),
        comment=str(data.get("comment", "")),
        metadata=_denormalize(data.get("metadata", {})),
    )


def _position_to_data(position: SourcePosition | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {
        "source_symbol": position.source_symbol,
        "net_quantity": str(position.net_quantity),
        "average_price": _optional_decimal_to_data(position.average_price),
    }


def _position_from_data(data: object) -> SourcePosition | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise ValueError("position must be an object")
    return SourcePosition(
        source_symbol=_text(data, "source_symbol"),
        net_quantity=Decimal(str(data["net_quantity"])),
        average_price=_optional_decimal_from_data(data.get("average_price")),
    )


def _quote_to_data(quote: SourceQuote | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {
        "source_symbol": quote.source_symbol,
        "bid": _optional_decimal_to_data(quote.bid),
        "ask": _optional_decimal_to_data(quote.ask),
        "occurred_at_utc": quote.occurred_at_utc,
    }


def _quote_from_data(data: object) -> SourceQuote | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise ValueError("quote must be an object")
    return SourceQuote(
        source_symbol=_text(data, "source_symbol"),
        bid=_optional_decimal_from_data(data.get("bid")),
        ask=_optional_decimal_from_data(data.get("ask")),
        occurred_at_utc=_text(data, "occurred_at_utc"),
    )


def _source_key_to_data(key: SourceOrderKey) -> dict[str, str]:
    return {
        "session_id": key.session_id,
        "source_symbol": key.source_symbol,
        "source_order_id": key.source_order_id,
    }


def _source_key_from_data(data: Mapping[str, Any]) -> SourceOrderKey:
    return SourceOrderKey(_text(data, "session_id"), _text(data, "source_symbol"), _text(data, "source_order_id"))


def _optional_decimal_to_data(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal_from_data(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _denormalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {"__decimal__"}:
            return Decimal(str(value["__decimal__"]))
        return {str(key): _denormalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_denormalize(item) for item in value]
    return value


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _list(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{key} must be a list of objects")
    return value


def _text(data: Mapping[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
