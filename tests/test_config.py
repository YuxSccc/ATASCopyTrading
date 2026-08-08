from decimal import Decimal

from atas_mt5_bridge.bridge.config import load_bridge_config
from atas_mt5_bridge.core.models import BridgeMode


def test_load_bridge_config_from_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "bridge": {
            "mode": "dry_run",
            "magic": 880501,
            "comment_prefix": "ATAS-MT5",
            "reject_hedged_same_magic": true
          },
          "symbols": {
            "COMEX Gold August 2026": {
              "target_symbol": "XAUUSD",
              "lot_ratio": "0.05",
              "max_abs_target_lot": "1.00",
              "max_order_lot": "0.20"
            }
          }
        }
        """,
        encoding="utf-8",
    )

    config = load_bridge_config(path)

    assert config.risk.mode == BridgeMode.DRY_RUN
    assert config.risk.magic == 880501
    assert config.mappings["COMEX Gold August 2026"].target_symbol == "XAUUSD"
    assert config.mappings["COMEX Gold August 2026"].lot_ratio == Decimal("0.05")


def test_kill_switch_overrides_configured_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "bridge": {
            "mode": "live",
            "kill_switch": true,
            "magic": 880501
          },
          "symbols": {
            "COMEX Gold August 2026": {
              "target_symbol": "XAUUSD",
              "lot_ratio": "0.05",
              "max_abs_target_lot": "1.00",
              "max_order_lot": "0.20"
            }
          }
        }
        """,
        encoding="utf-8",
    )

    config = load_bridge_config(path)

    assert config.risk.mode == BridgeMode.KILL_SWITCH
