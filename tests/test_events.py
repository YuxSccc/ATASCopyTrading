from decimal import Decimal

from atas_mt5_bridge.bridge.events import parse_atas_position_event


def test_parse_atas_position_event_from_json_dict():
    event = parse_atas_position_event(
        {
            "seq": 42,
            "source_symbol": "COMEX Gold August 2026",
            "target_net_contracts": "1",
            "avg_price": "2400.5",
            "timestamp": "2026-06-03T10:00:00Z",
        }
    )

    assert event.seq == 42
    assert event.source_symbol == "COMEX Gold August 2026"
    assert event.target_net_contracts == Decimal("1")
    assert event.avg_price == Decimal("2400.5")
