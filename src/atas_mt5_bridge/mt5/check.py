from __future__ import annotations

import argparse
from decimal import Decimal
import importlib
import json
from typing import Any, Iterable

from atas_mt5_bridge.mt5.metatrader5_adapter import MetaTrader5Adapter


def build_mt5_status(mt5: Any, symbols: Iterable[str], magic: int) -> dict[str, Any]:
    if not mt5.initialize():
        raise RuntimeError(f"failed to initialize MetaTrader5: {mt5.last_error()}")

    adapter = MetaTrader5Adapter(mt5)
    return {
        "terminal": _public_attrs(mt5.terminal_info()),
        "account": _public_attrs(mt5.account_info(), redact={"login"}),
        "symbols": {
            symbol: _symbol_status(mt5, adapter, symbol, magic)
            for symbol in symbols
        },
    }


def _symbol_status(
    mt5: Any,
    adapter: MetaTrader5Adapter,
    symbol: str,
    magic: int,
) -> dict[str, Any]:
    info = mt5.symbol_info(symbol)
    if info is None:
        return {"exists": False}

    rules = adapter.get_volume_rules(symbol)
    position = adapter.get_net_position(symbol, magic)
    return {
        "exists": True,
        "visible": bool(getattr(info, "visible", False)),
        "trade_mode": getattr(info, "trade_mode", None),
        "filling_mode": getattr(info, "filling_mode", None),
        "digits": getattr(info, "digits", None),
        "point": _string_decimal(getattr(info, "point", None)),
        "trade_stops_level": getattr(info, "trade_stops_level", None),
        "trade_contract_size": _string_decimal(getattr(info, "trade_contract_size", None)),
        "volume_min": str(rules.volume_min),
        "volume_step": str(rules.volume_step),
        "volume_max": str(rules.volume_max),
        "magic_position": {
            "net_lot": str(position.net_lot),
            "long_lot": str(position.long_lot),
            "short_lot": str(position.short_lot),
        },
    }


def _public_attrs(value: Any, redact: set[str] | None = None) -> dict[str, Any] | None:
    if value is None:
        return None

    if hasattr(value, "_asdict"):
        raw = value._asdict()
    elif hasattr(value, "__dict__"):
        raw = vars(value)
    else:
        return {"value": str(value)}

    redacted = redact or set()
    return {
        key: ("<redacted>" if key in redacted else _json_safe(item))
        for key, item in raw.items()
        if not key.startswith("_")
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _string_decimal(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only MetaTrader5 terminal and symbol check.")
    parser.add_argument("--symbols", nargs="+", default=["XAUUSD", "USTEC"])
    parser.add_argument("--magic", type=int, default=880501)
    args = parser.parse_args()

    mt5 = importlib.import_module("MetaTrader5")
    status = build_mt5_status(mt5, args.symbols, args.magic)
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
