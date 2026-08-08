from __future__ import annotations

from typing import Any


def bridge_result_to_json(result: Any) -> dict[str, Any]:
    plan = result.plan
    return {
        "target": {
            "source_symbol": plan.target.source_symbol,
            "symbol": plan.target.symbol,
            "net_lot": str(plan.target.net_lot),
            "source_seq": plan.target.source_seq,
        },
        "actual": {
            "symbol": plan.actual.symbol,
            "net_lot": str(plan.actual.net_lot),
            "long_lot": str(plan.actual.long_lot),
            "short_lot": str(plan.actual.short_lot),
        },
        "dry_run": plan.dry_run,
        "ignored_reason": plan.ignored_reason,
        "orders": [
            {
                "symbol": order.symbol,
                "side": order.side.value,
                "volume": str(order.volume),
                "magic": order.magic,
                "comment": order.comment,
            }
            for order in plan.orders
        ],
        "executions": [
            {
                "accepted": execution.accepted,
                "message": execution.message,
                "external_id": execution.external_id,
            }
            for execution in result.executions
        ],
    }
