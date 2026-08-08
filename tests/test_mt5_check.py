from types import SimpleNamespace

from atas_mt5_bridge.mt5.check import build_mt5_status


class FakeMetaTrader5Module:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self):
        self.initialize_called = False
        self.order_send_called = False
        self.positions = [
            SimpleNamespace(symbol="XAUUSD", magic=880501, type=self.POSITION_TYPE_BUY, volume=0.05),
            SimpleNamespace(symbol="XAUUSD", magic=123, type=self.POSITION_TYPE_BUY, volume=9.0),
        ]

    def initialize(self):
        self.initialize_called = True
        return True

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return SimpleNamespace(name="MetaTrader 5", connected=True)

    def account_info(self):
        return SimpleNamespace(login=277719854, server="demo")

    def symbol_info(self, symbol):
        if symbol != "XAUUSD":
            return None
        return SimpleNamespace(
            visible=True,
            trade_mode=4,
            filling_mode=2,
            digits=2,
            point=0.01,
            trade_stops_level=0,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
        )

    def positions_get(self, symbol=None):
        if symbol is None:
            return self.positions
        return [position for position in self.positions if position.symbol == symbol]

    def order_send(self, request):
        self.order_send_called = True


def test_build_mt5_status_is_read_only_and_reports_symbol_state():
    mt5 = FakeMetaTrader5Module()

    status = build_mt5_status(mt5, ["XAUUSD", "MISSING"], 880501)

    assert mt5.initialize_called is True
    assert mt5.order_send_called is False
    assert status["account"]["login"] == "<redacted>"
    assert status["symbols"]["XAUUSD"]["exists"] is True
    assert status["symbols"]["XAUUSD"]["volume_step"] == "0.01"
    assert status["symbols"]["XAUUSD"]["magic_position"]["net_lot"] == "0.05"
    assert status["symbols"]["MISSING"]["exists"] is False
