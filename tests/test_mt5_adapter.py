from decimal import Decimal
from types import SimpleNamespace

from atas_mt5_bridge.core.models import MarketOrderIntent, OrderSide
from atas_mt5_bridge.mt5.metatrader5_adapter import MetaTrader5Adapter


class FakeMt5Module:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.sent_requests = []
        self.positions = []
        self.symbol = SimpleNamespace(
            name="XAUUSD",
            visible=True,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            filling_mode=2,
        )
        self.tick = SimpleNamespace(ask=2401.2, bid=2401.0)

    def symbol_info(self, symbol):
        return self.symbol if symbol == "XAUUSD" else None

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return self.tick

    def positions_get(self, symbol=None):
        if symbol is None:
            return self.positions
        return [position for position in self.positions if position.symbol == symbol]

    def order_send(self, request):
        self.sent_requests.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done", order=12345)

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(trade_allowed=True, trade_expert=True)


def test_mt5_adapter_reads_same_magic_net_position():
    mt5 = FakeMt5Module()
    mt5.positions = [
        SimpleNamespace(symbol="XAUUSD", magic=880501, type=mt5.POSITION_TYPE_BUY, volume=0.05),
        SimpleNamespace(symbol="XAUUSD", magic=880501, type=mt5.POSITION_TYPE_SELL, volume=0.02),
        SimpleNamespace(symbol="XAUUSD", magic=123, type=mt5.POSITION_TYPE_BUY, volume=9.0),
    ]
    adapter = MetaTrader5Adapter(mt5)

    position = adapter.get_net_position("XAUUSD", 880501)

    assert position.long_lot == Decimal("0.05")
    assert position.short_lot == Decimal("0.02")
    assert position.net_lot == Decimal("0.03")


def test_mt5_adapter_builds_market_order_request_with_magic_and_comment():
    mt5 = FakeMt5Module()
    adapter = MetaTrader5Adapter(mt5)
    order = MarketOrderIntent(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.05"),
        magic=880501,
        comment="ATAS-MT5:target-net",
    )

    result = adapter.execute_market_order(order)

    assert result.accepted is True
    assert result.external_id == "12345"
    assert "retcode=10009" in result.message
    assert mt5.sent_requests[0]["action"] == mt5.TRADE_ACTION_DEAL
    assert mt5.sent_requests[0]["symbol"] == "XAUUSD"
    assert mt5.sent_requests[0]["volume"] == 0.05
    assert mt5.sent_requests[0]["type"] == mt5.ORDER_TYPE_BUY
    assert mt5.sent_requests[0]["type_filling"] == mt5.ORDER_FILLING_IOC
    assert mt5.sent_requests[0]["magic"] == 880501
    assert mt5.sent_requests[0]["comment"] == "ATAS-MT5:target-net"


def test_mt5_adapter_uses_fok_when_symbol_only_allows_fok():
    mt5 = FakeMt5Module()
    mt5.symbol.filling_mode = 1
    adapter = MetaTrader5Adapter(mt5)
    order = MarketOrderIntent(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.05"),
        magic=880501,
        comment="ATAS-MT5:target-net",
    )

    adapter.execute_market_order(order)

    assert mt5.sent_requests[0]["type_filling"] == mt5.ORDER_FILLING_FOK


def test_mt5_adapter_rejects_when_terminal_trading_is_disabled():
    mt5 = FakeMt5Module()
    mt5.terminal_info = lambda: SimpleNamespace(trade_allowed=False)
    adapter = MetaTrader5Adapter(mt5)
    order = MarketOrderIntent(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.05"),
        magic=880501,
        comment="ATAS-MT5:target-net",
    )

    result = adapter.execute_market_order(order)

    assert result.accepted is False
    assert "trade_allowed is false" in result.message
    assert mt5.sent_requests == []


def test_mt5_adapter_closes_same_magic_long_position_by_ticket_before_opening_short():
    mt5 = FakeMt5Module()
    mt5.positions = [
        SimpleNamespace(
            ticket=101,
            symbol="XAUUSD",
            magic=880501,
            type=mt5.POSITION_TYPE_BUY,
            volume=0.05,
        )
    ]
    adapter = MetaTrader5Adapter(mt5)
    order = MarketOrderIntent(
        symbol="XAUUSD",
        side=OrderSide.SELL,
        volume=Decimal("0.05"),
        magic=880501,
        comment="ATAS-MT5:target-net",
    )

    result = adapter.execute_market_order(order)

    assert result.accepted is True
    assert len(mt5.sent_requests) == 1
    assert mt5.sent_requests[0]["position"] == 101
    assert mt5.sent_requests[0]["type"] == mt5.ORDER_TYPE_SELL
    assert mt5.sent_requests[0]["volume"] == 0.05


def test_mt5_adapter_closes_opposite_positions_then_opens_remaining_reversal():
    mt5 = FakeMt5Module()
    mt5.positions = [
        SimpleNamespace(
            ticket=202,
            symbol="XAUUSD",
            magic=880501,
            type=mt5.POSITION_TYPE_SELL,
            volume=0.05,
        ),
        SimpleNamespace(
            ticket=303,
            symbol="XAUUSD",
            magic=123,
            type=mt5.POSITION_TYPE_SELL,
            volume=9.0,
        ),
    ]
    adapter = MetaTrader5Adapter(mt5)
    order = MarketOrderIntent(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=Decimal("0.08"),
        magic=880501,
        comment="ATAS-MT5:target-net",
    )

    result = adapter.execute_market_order(order)

    assert result.accepted is True
    assert len(mt5.sent_requests) == 2
    assert mt5.sent_requests[0]["position"] == 202
    assert mt5.sent_requests[0]["volume"] == 0.05
    assert "position" not in mt5.sent_requests[1]
    assert mt5.sent_requests[1]["type"] == mt5.ORDER_TYPE_BUY
    assert mt5.sent_requests[1]["volume"] == 0.03
