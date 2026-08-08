from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Dict
from typing import Optional

from .models import TargetPosition


class StateStore(ABC):
    @abstractmethod
    def is_new_event(self, source_symbol: str, seq: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def record_target(self, target: TargetPosition) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_desired(self, symbol: str) -> Optional[TargetPosition]:
        raise NotImplementedError


@dataclass
class InMemoryStateStore(StateStore):
    last_seq_by_source: Dict[str, int] = field(default_factory=dict)
    desired_by_symbol: Dict[str, TargetPosition] = field(default_factory=dict)

    def is_new_event(self, source_symbol: str, seq: int) -> bool:
        return seq > self.last_seq_by_source.get(source_symbol, 0)

    def record_target(self, target: TargetPosition) -> None:
        self.last_seq_by_source[target.source_symbol] = target.source_seq
        self.desired_by_symbol[target.symbol] = target

    def get_desired(self, symbol: str) -> Optional[TargetPosition]:
        return self.desired_by_symbol.get(symbol)


class FileStateStore(InMemoryStateStore):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        super().__init__()
        self._load()

    def record_target(self, target: TargetPosition) -> None:
        super().record_target(target)
        self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        self.last_seq_by_source = {
            str(source): int(seq)
            for source, seq in data.get("last_seq_by_source", {}).items()
        }
        self.desired_by_symbol = {
            str(symbol): TargetPosition(
                source_symbol=str(item["source_symbol"]),
                symbol=str(item["symbol"]),
                net_lot=Decimal(str(item["net_lot"])),
                source_seq=int(item["source_seq"]),
            )
            for symbol, item in data.get("desired_by_symbol", {}).items()
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_seq_by_source": self.last_seq_by_source,
            "desired_by_symbol": {
                symbol: {
                    "source_symbol": target.source_symbol,
                    "symbol": target.symbol,
                    "net_lot": str(target.net_lot),
                    "source_seq": target.source_seq,
                }
                for symbol, target in self.desired_by_symbol.items()
            },
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, self.path)
