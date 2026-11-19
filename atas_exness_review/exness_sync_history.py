"""Manually pull read-only Exness deals into a durable ledger and chart CSV."""

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit


FIELDS = ("ticket", "opening_time_utc", "closing_time_utc", "type", "lots",
          "original_position_size", "symbol", "opening_price", "closing_price", "profit",
          "close_reason", "commission", "swap")


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("History timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def number(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError("Non-finite number in history")
    return n


def money(deal, key):
    value = deal.get(key)
    if isinstance(value, dict):
        value = value.get("amount")
    return None if value is None else number(value)


def id_text(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("Invalid Exness identifier")
    return value


def origin(value):
    value = value if "://" in value else "https://" + value
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.path.strip("/") or u.query or u.fragment:
        raise ValueError("Expected an HTTPS API host")
    return "https://" + u.netloc


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Reader:
    def __init__(self, credentials):
        import requests
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        raw = bytes.fromhex(credentials["private_key"].strip())
        self.key = Ed25519PrivateKey.from_private_bytes(raw[:32])
        if len(raw) != 64 or self.key.public_key().public_bytes_raw() != raw[32:]:
            raise ValueError("Invalid signing key")
        self.api_key = credentials["api_key"]
        self.account = id_text(credentials["account_id"])
        self.http = requests.Session()
        self.https_proxy = None
        settings = Path(__file__).resolve().with_name("network.json")
        if settings.is_file():
            self.https_proxy = json.loads(settings.read_text(encoding="utf-8-sig")).get("https_proxy")
            if self.https_proxy and urlsplit(self.https_proxy).scheme not in ("http", "https"):
                raise ValueError("Invalid proxy configuration")
        reply = self.get(origin(credentials["gateway"]), "/v1/trading/access-point?" + urlencode({"account_id": self.account}))
        self.point = origin(reply["access_point"])

    def get(self, host, path):
        stamp = time.time_ns() // 1_000_000
        payload = json.dumps(dict(api_key=self.api_key, idempotency_key="", timestamp=stamp, sign_version=1,
                                  method="GET", path=path, body_hash=b64(hashlib.sha256(b"").digest())),
                             separators=(",", ":")).encode()
        headers = {"EXN-API-KEY": self.api_key, "EXN-IDEMPOTENCY-KEY": "", "EXN-TIMESTAMP": str(stamp),
                   "EXN-SIGN-VERSION": "1", "EXN-DATA": b64(payload), "EXN-SIGN": b64(self.key.sign(payload))}
        url = host + path
        proxies = None
        if self.https_proxy:
            from requests.utils import should_bypass_proxies
            if not should_bypass_proxies(url, no_proxy=None):
                proxies = {"https": self.https_proxy}
        r = self.http.get(url, headers=headers, proxies=proxies, allow_redirects=False, timeout=(6, 25))
        if r.status_code != 200:
            raise ValueError(f"Exness returned HTTP {r.status_code}")
        return r.json()

    def history(self, start, stop):
        path = f"/v1/history/accounts/{self.account}/deals?"
        query = {"from": iso(start), "to": iso(stop), "limit": 200}
        seen = set()
        while True:
            page = self.get(self.point, path + urlencode(query))
            yield page
            if page["has_more"] is False:
                return
            cursor = page.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("Missing or repeated pagination cursor")
            seen.add(cursor)
            query = {"cursor": cursor, "limit": 200}


def add_page(db, page):
    if not isinstance(page.get("items"), list) or not isinstance(page.get("has_more"), bool):
        raise ValueError("Invalid history page")
    added = 0
    for item in page["items"]:
        deal = item["deal"]
        kind = deal["type"]
        if kind not in ("open", "close"):
            continue
        qty = number(deal["volume"])
        price = number(deal["price"])
        side = deal["direction"]
        symbol = deal["instrument"]
        if qty <= 0 or price <= 0 or side not in ("buy", "sell") or not isinstance(symbol, str) or not symbol:
            raise ValueError("Invalid execution in history")
        props = dict(id=id_text(deal["deal_id"]), position=id_text(deal["position_id"]), kind=kind, side=side,
                     symbol=symbol.upper(), utc=iso(timestamp(deal.get("create_time") or item["event_time"])),
                     qty=qty, price=price, profit=money(deal, "profit"), commission=money(deal, "commission"),
                     swap=money(deal, "swap"), reason=deal.get("close_reason") or "")
        for field in ("qty", "price", "profit", "commission", "swap"):
            value = props[field]
            if value is not None:
                text = format(value, "f")
                props[field] = text.rstrip("0").rstrip(".") if "." in text else text
        payload = json.dumps(props, separators=(",", ":"))
        old = db.execute("SELECT payload FROM fills WHERE id=?", (props["id"],)).fetchone()
        if old is None:
            db.execute("INSERT INTO fills(id,payload) VALUES(?,?)", (props["id"], payload))
            added += 1
        elif old[0] != payload:
            raise ValueError("Inconsistent executions share one deal ID")
    return added


def position_rows(account, position, fills):
    rows = []
    opening = None
    for fill in sorted(fills, key=lambda f: (f["utc"], int(f["id"]))):
        qty = number(fill["qty"])
        if fill["kind"] == "open":
            if opening is not None:
                return None  # Multiple possible entry lots cannot be linked to a particular exit.
            opening = fill
            remaining = qty
            ticket = f"API-{account}-{position}-{fill['id']}"
            continue
        if opening is None or opening["side"] == fill["side"] or opening["symbol"] != fill["symbol"] or qty > remaining:
            return None
        rows.append(row_for(ticket, opening, qty, remaining, fill))
        remaining -= qty
        if remaining:
            ticket = f"API-{account}-{position}-{opening['id']}-after-{fill['id']}"
        else:
            opening = None
    if opening is not None:
        rows.append(row_for(ticket, opening, remaining, remaining, {}))
    return rows


def row_for(ticket, entry, qty, original, exit_fill):
    fees = []
    if entry["commission"] is not None:
        fees.append(number(entry["commission"]) * qty / number(entry["qty"]))
    if exit_fill.get("commission") is not None:
        fees.append(number(exit_fill["commission"]))
    return dict(ticket=ticket, opening_time_utc=entry["utc"], closing_time_utc=exit_fill.get("utc", ""),
                type=entry["side"], lots=format(qty, "f"), original_position_size=format(original, "f"),
                symbol=entry["symbol"], opening_price=entry["price"], closing_price=exit_fill.get("price", ""),
                profit=exit_fill.get("profit"), close_reason=exit_fill.get("reason", ""),
                commission=format(sum(fees), "f") if fees else "", swap=exit_fill.get("swap"))


def synchronize(account, state_dir, output, reader=None, seed=None):
    state_dir.mkdir(parents=True, exist_ok=True)
    state = state_dir / (account + ".sqlite3")
    with closing(sqlite3.connect(state, timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS sync(singleton INTEGER PRIMARY KEY CHECK(singleton=1), through TEXT NOT NULL)")
        added = 0
        if seed is not None:
            if seed.get("has_more") is not False:
                raise ValueError("Seed page must be a complete history response")
            added = add_page(db, seed)
        else:
            now = datetime.now(timezone.utc)
            previous = db.execute("SELECT through FROM sync WHERE singleton=1").fetchone()
            start = timestamp(previous[0]) - timedelta(hours=1) if previous else now - timedelta(days=1)
            if start >= now:
                raise ValueError("Local clock is behind the previous sync")
            while start < now:
                stop = min(now, start + timedelta(days=1))
                for page in reader.history(start, stop):
                    added += add_page(db, page)
                start = stop
            db.execute("INSERT OR REPLACE INTO sync(singleton,through) VALUES(1,?)", (iso(now),))
        groups = defaultdict(list)
        for payload, in db.execute("SELECT payload FROM fills"):
            fill = json.loads(payload)
            groups[fill["position"]].append(fill)
        rows = []
        unmatched = []
        for position, fills in groups.items():
            result = position_rows(account, position, fills)
            if result is None:
                unmatched.append(position)
            else:
                rows.extend(result)
        rows.sort(key=lambda r: (r["opening_time_utc"], r["ticket"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False) as f:
                tmp = Path(f.name)
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
                f.flush()
                os.fsync(f.fileno())
            db.commit()  # A failed publication is retried by projecting the entire persisted ledger.
            os.replace(tmp, output)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    return dict(rows=len(rows), unmatched_positions=len(unmatched), unmatched_position_ids=sorted(unmatched),
                deals=sum(map(len, groups.values())), new_deals=added, account_id=account)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--import-page", type=Path)
    args = parser.parse_args()
    reader = seed = None
    if args.import_page:
        account = id_text(args.account_id)
        seed = json.loads(args.import_page.read_text(encoding="utf-8-sig"))
    else:
        from exness_credentials import read_credentials
        try:
            credentials = read_credentials()
            if not credentials or not all(credentials.get(k) for k in ("api_key", "private_key", "gateway", "account_id")):
                raise ValueError()
        except (ValueError, KeyError, OSError, RuntimeError):
            print("Exness read-only credentials are unavailable.", file=sys.stderr)
            return 3
        account = id_text(credentials["account_id"])
        if args.account_id and args.account_id != account:
            raise ValueError("Credential account does not match the requested account")
        reader = Reader(credentials)
    directory = args.state_dir or Path(os.environ["LOCALAPPDATA"]) / "AtasExnessReview" / "api" / "state" / account
    report = synchronize(account, directory, args.output, reader, seed)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, InvalidOperation, OSError, KeyError, TypeError, sqlite3.Error, ImportError) as error:
        # Never expose response bodies, signed headers or credentials to host logs.
        reason = str(error) if type(error) is ValueError and str(error).startswith("Exness returned HTTP ") else type(error).__name__
        print("History sync failed: " + reason, file=sys.stderr)
        sys.exit(1)
