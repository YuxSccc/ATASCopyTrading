import csv
import importlib.util
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


module = Path(__file__).resolve().parents[1] / "atas_exness_review" / "exness_sync_history.py"
spec = importlib.util.spec_from_file_location("exness_sync_history", module)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def fill(n, kind, qty, price="100.25", side=None):
    time = f"2026-11-18T18:16:{n:02}.123456+00:00"
    return {"event_time": time, "deal": {"deal_id": str(n), "position_id": "700",
            "instrument": "USTEC", "direction": side or ("buy" if kind == "open" else "sell"),
            "volume": qty, "price": price, "create_time": time, "type": kind,
            "profit": {"amount": "1.23" if kind == "close" else "0"}}}


def page(*items):
    return {"items": list(items), "has_more": False}


def test_preserves_explicit_position_fills_and_partial_close(tmp_path):
    initial = fill(1, "open", "0.50")
    exit_a, exit_b = fill(2, "close", "0.300"), fill(3, "close", "0.2")
    def run(*items):
        target = tmp_path / "sync.csv"
        report = sync.synchronize("123", tmp_path, target, seed=page(*items))
        with target.open() as f:
            return report, list(csv.DictReader(f))
    # Seed a late-arriving close. Retain it for repair without producing an invented entry.
    report, rows = run(exit_a)
    assert report["unmatched_positions"] == 1 and report["unmatched_position_ids"] == ["700"] and not rows
    balance = {"deal": {"type": "balance"}}
    report, rows = run(initial, balance)
    assert report["deals"] == 2 and report["new_deals"] == 1 and report["unmatched_positions"] == 0
    assert [r["lots"] for r in rows] == ["0.3", "0.2"]
    assert rows[0]["opening_time_utc"].endswith(".123456Z") and rows[1]["closing_time_utc"] == ""
    ids = [r["ticket"] for r in rows]
    assert ids[0] == "API-123-700-1"
    report, rows = run(exit_b, initial, exit_a)
    assert [r["ticket"] for r in rows] == ids and all(r["profit"] == "1.23" for r in rows)
    assert run(fill(1, "open", ".500000"), exit_a, exit_b)[0]["new_deals"] == 0


def test_conflict_aborts_before_publishing(tmp_path):
    target = tmp_path / "report.csv"
    sync.synchronize("123", tmp_path, target, seed=page(fill(1, "open", "0.5")))
    previous = target.read_bytes()
    with pytest.raises(ValueError):
        sync.synchronize("123", tmp_path, target, seed=page(fill(2, "close", "0.5"), fill(1, "open", "0.1")))
    assert previous == target.read_bytes()
    with sqlite3.connect(tmp_path / "123.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_windows_paginate_and_restart_after_failure(tmp_path):
    first = {"items": [fill(1,"open","0.5")], "has_more": True, "next_cursor": "abc+/"}
    last = page(fill(2,"close","0.5"))
    reader = sync.Reader.__new__(sync.Reader)
    reader.account, reader.point = "123", "unused"
    calls = []
    def get(_host, path):
        query = parse_qs(urlsplit(path).query)
        calls.append(query)
        if "from" in query:
            return first
        assert query == {"cursor": ["abc+/"], "limit": ["200"]}
        return last
    reader.get = get
    report = sync.synchronize("123",tmp_path,tmp_path / "out.csv",reader=reader)
    assert report["rows"] == 1 and len(calls) == 2
    reader.get = lambda *_: first
    previous = (tmp_path / "out.csv").read_bytes()
    with pytest.raises(ValueError, match="cursor"):
        sync.synchronize("123",tmp_path,tmp_path / "out.csv",reader=reader)
    assert (tmp_path / "out.csv").read_bytes() == previous
