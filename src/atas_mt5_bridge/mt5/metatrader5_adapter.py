from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atas_mt5_bridge.bridge.service import ExecutionResult
from atas_mt5_bridge.core.models import MarketOrderIntent, Mt5NetPosition, OrderSide
from atas_mt5_bridge.core.volume import SymbolVolumeRules


class Mt5AdapterError(RuntimeError):
    pass


@dataclass
class MetaTrader5Adapter:
    mt5: Any
    deviation_points: int = 20

    def get_volume_rules(self, symbol: str) -> SymbolVolumeRules:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise Mt5AdapterError(f"MT5 symbol does not exist: {symbol}")
        return SymbolVolumeRules(
            symbol=symbol,
            volume_min=Decimal(str(info.volume_min)),
            volume_step=Decimal(str(info.volume_step)),
            volume_max=Decimal(str(info.volume_max)),
        )

    def get_net_position(self, symbol: str, magic: int) -> Mt5NetPosition:
        positions = self.mt5.positions_get(symbol=symbol)
        if positions is None:
            raise Mt5AdapterError(f"failed to read MT5 positions: {self.mt5.last_error()}")

        long_lot = Decimal("0")
        short_lot = Decimal("0")
        for position in positions:
            if int(getattr(position, "magic", 0) or 0) != magic:
                continue
            volume = Decimal(str(position.volume))
            if int(position.type) == self.mt5.POSITION_TYPE_BUY:
                long_lot += volume
            elif int(position.type) == self.mt5.POSITION_TYPE_SELL:
                short_lot += volume

        return Mt5NetPosition(
            symbol=symbol,
            net_lot=long_lot - short_lot,
            long_lot=long_lot,
            short_lot=short_lot,
        )

    def execute_market_order(self, order: MarketOrderIntent) -> ExecutionResult:
        terminal = self.mt5.terminal_info()
        if terminal is not None and not bool(getattr(terminal, "trade_allowed", True)):
            return ExecutionResult(
                order=order,
                accepted=False,
                message="terminal trade_allowed is false; enable Algo Trading in MT5",
            )

        account = self.mt5.account_info()
        if account is not None:
            if not bool(getattr(account, "trade_allowed", True)):
                return ExecutionResult(
                    order=order,
                    accepted=False,
                    message="account trade_allowed is false",
                )
            if not bool(getattr(account, "trade_expert", True)):
                return ExecutionResult(
                    order=order,
                    accepted=False,
                    message="account trade_expert is false; check MT5 Expert Advisor trading permissions",
                )

        info = self.mt5.symbol_info(order.symbol)
        if info is None:
            raise Mt5AdapterError(f"MT5 symbol does not exist: {order.symbol}")
        if not getattr(info, "visible", False):
            if not self.mt5.symbol_select(order.symbol, True):
                raise Mt5AdapterError(f"MT5 symbol is not selectable: {order.symbol}")

        tick = self.mt5.symbol_info_tick(order.symbol)
        if tick is None:
            raise Mt5AdapterError(f"MT5 tick is unavailable: {order.symbol}")

        results = []
        remaining = order.volume
        for position in self._iter_positions_to_close(order):
            if remaining <= 0:
                break

            close_volume = min(remaining, Decimal(str(position.volume)))
            result = self._send_market_request(
                order=order,
                info=info,
                tick=tick,
                volume=close_volume,
                position_ticket=int(position.ticket),
            )
            results.append(result)
            if not self._is_accepted(result):
                return ExecutionResult(
                    order=order,
                    accepted=False,
                    message=_format_multiple_order_send_results(results),
                    external_id=str(getattr(result, "order", "")) or None,
                )
            remaining -= close_volume

        if remaining > 0:
            result = self._send_market_request(
                order=order,
                info=info,
                tick=tick,
                volume=remaining,
                position_ticket=None,
            )
            results.append(result)

        if not results:
            return ExecutionResult(
                order=order,
                accepted=False,
                message="no order_send request was generated",
            )

        accepted = all(result is not None and self._is_accepted(result) for result in results)
        return ExecutionResult(
            order=order,
            accepted=accepted,
            message=_format_multiple_order_send_results(results),
            external_id=",".join(
                str(getattr(result, "order", ""))
                for result in results
                if result is not None and str(getattr(result, "order", ""))
            ) or None,
        )

    def _send_market_request(
        self,
        order: MarketOrderIntent,
        info: Any,
        tick: Any,
        volume: Decimal,
        position_ticket: int | None,
    ) -> Any:
        order_type = self.mt5.ORDER_TYPE_BUY if order.side == OrderSide.BUY else self.mt5.ORDER_TYPE_SELL
        price = tick.ask if order.side == OrderSide.BUY else tick.bid
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "deviation": int(self.deviation_points),
            "magic": int(order.magic),
            "comment": order.comment,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(info),
        }
        if position_ticket is not None:
            request["position"] = int(position_ticket)

        return self.mt5.order_send(request)

    def _iter_positions_to_close(self, order: MarketOrderIntent) -> list[Any]:
        positions = self.mt5.positions_get(symbol=order.symbol)
        if positions is None:
            raise Mt5AdapterError(f"failed to read MT5 positions before close: {self.mt5.last_error()}")

        opposite_type = (
            self.mt5.POSITION_TYPE_SELL
            if order.side == OrderSide.BUY
            else self.mt5.POSITION_TYPE_BUY
        )
        return [
            position
            for position in positions
            if int(getattr(position, "magic", 0) or 0) == int(order.magic)
            and int(position.type) == int(opposite_type)
        ]

    def _is_accepted(self, result: Any) -> bool:
        if result is None:
            return False

        accepted_ret_codes = {int(self.mt5.TRADE_RETCODE_DONE)}
        partial = getattr(self.mt5, "TRADE_RETCODE_DONE_PARTIAL", None)
        if partial is not None:
            accepted_ret_codes.add(int(partial))

        return int(result.retcode) in accepted_ret_codes

    def _get_filling_mode(self, info: Any) -> int:
        symbol_filling_flags = int(getattr(info, "filling_mode", 0) or 0)

        # MT5 symbol filling flags are not the same numeric values as order
        # filling constants. SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2.
        if symbol_filling_flags & 2:
            return self.mt5.ORDER_FILLING_IOC
        if symbol_filling_flags & 1:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN


def _format_order_send_result(result: Any) -> str:
    if result is None:
        return "order_send returned None"

    parts = [
        f"retcode={getattr(result, 'retcode', '')}",
        f"comment={getattr(result, 'comment', '')}",
    ]
    for name in ("order", "deal", "request_id", "volume", "price"):
        value = getattr(result, name, None)
        if value not in (None, "", 0):
            parts.append(f"{name}={value}")
    return ", ".join(parts)


def _format_multiple_order_send_results(results: list[Any]) -> str:
    if not results:
        return "no order_send results"
    return " | ".join(_format_order_send_result(result) for result in results)
