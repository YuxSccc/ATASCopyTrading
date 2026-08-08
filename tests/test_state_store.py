from decimal import Decimal

from atas_mt5_bridge.core.models import TargetPosition
from atas_mt5_bridge.core.state import FileStateStore


def test_file_state_store_persists_last_seq_and_target(tmp_path):
    state_path = tmp_path / "bridge-state.json"
    first = FileStateStore(state_path)
    target = TargetPosition(
        source_symbol="COMEX Gold August 2026",
        symbol="XAUUSD",
        net_lot=Decimal("0.05"),
        source_seq=7,
    )

    first.record_target(target)
    second = FileStateStore(state_path)

    assert second.is_new_event("COMEX Gold August 2026", 7) is False
    assert second.is_new_event("COMEX Gold August 2026", 8) is True
    assert second.desired_by_symbol["XAUUSD"] == target
    assert second.get_desired("XAUUSD") == target
