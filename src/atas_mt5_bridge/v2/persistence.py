from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterable, Iterator, Protocol

from .intent_ledger import IntentLedger
from .models import OperationRecord, SourceEvent, SourceSymbolState
from .state_codec import (
    ledger_from_data,
    ledger_to_data,
    operation_record_from_data,
    operation_record_to_data,
    source_state_from_data,
    source_state_to_data,
)


class BridgeStateStore(Protocol):
    """Durable boundary; every target command must be recorded before send."""

    def record_source_event(self, event: SourceEvent) -> None:
        raise NotImplementedError

    def save_source_state(self, state: SourceSymbolState) -> None:
        raise NotImplementedError

    def load_source_state(self, source_symbol: str) -> SourceSymbolState | None:
        raise NotImplementedError

    def save_ledger(self, target_symbol: str, ledger: IntentLedger) -> None:
        raise NotImplementedError

    def load_ledger(self, target_symbol: str) -> IntentLedger | None:
        raise NotImplementedError

    def save_operation(self, record: OperationRecord) -> None:
        raise NotImplementedError

    def save_ledger_and_operations(
        self,
        target_symbol: str,
        ledger: IntentLedger,
        operations: Iterable[OperationRecord],
    ) -> None:
        raise NotImplementedError

    def load_operations(self, target_symbol: str) -> tuple[OperationRecord, ...]:
        raise NotImplementedError

    def save_market_state(self, target_symbol: str, payload: dict) -> None:
        raise NotImplementedError

    def load_market_state(self, target_symbol: str) -> dict | None:
        raise NotImplementedError


@dataclass
class NullBridgeStateStore:
    """Explicit non-persistent store for unit tests and the legacy recorder."""

    def record_source_event(self, event: SourceEvent) -> None:
        return None

    def save_source_state(self, state: SourceSymbolState) -> None:
        return None

    def load_source_state(self, source_symbol: str) -> SourceSymbolState | None:
        return None

    def save_ledger(self, target_symbol: str, ledger: IntentLedger) -> None:
        return None

    def load_ledger(self, target_symbol: str) -> IntentLedger | None:
        return None

    def save_operation(self, record: OperationRecord) -> None:
        return None

    def save_ledger_and_operations(
        self,
        target_symbol: str,
        ledger: IntentLedger,
        operations: Iterable[OperationRecord],
    ) -> None:
        return None

    def load_operations(self, target_symbol: str) -> tuple[OperationRecord, ...]:
        return ()

    def save_market_state(self, target_symbol: str, payload: dict) -> None:
        return None

    def load_market_state(self, target_symbol: str) -> dict | None:
        return None


class SqliteBridgeStateStore:
    """SQLite state store for restart-safe ATAS/MT5 reconciliation.

    SQLite is deliberately local and synchronous. A command dispatch is first
    committed to this database; only then may an MT5 adapter receive it. WAL
    mode lets the recorder and a diagnostic reader coexist without weakening
    that ordering guarantee.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def record_source_event(self, event: SourceEvent) -> None:
        payload = {
            "session_id": event.session_id,
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "source_symbol": event.source_symbol,
            "occurred_at_utc": event.occurred_at_utc,
        }
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO source_events
                    (source_symbol, session_id, event_id, payload_json, recorded_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.source_symbol,
                    event.session_id,
                    event.event_id,
                    _encode(payload),
                    _now(),
                ),
            )

    def save_source_state(self, state: SourceSymbolState) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_checkpoints (source_symbol, payload_json, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(source_symbol) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (state.source_symbol, _encode(source_state_to_data(state)), _now()),
            )

    def load_source_state(self, source_symbol: str) -> SourceSymbolState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM source_checkpoints WHERE source_symbol=?", (source_symbol,)
            ).fetchone()
        return source_state_from_data(_decode(row["payload_json"])) if row else None

    def save_ledger(self, target_symbol: str, ledger: IntentLedger) -> None:
        with self._transaction() as connection:
            self._save_ledger(connection, target_symbol, ledger)

    def load_ledger(self, target_symbol: str) -> IntentLedger | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM intent_ledgers WHERE target_symbol=?", (target_symbol,)
            ).fetchone()
        return ledger_from_data(_decode(row["payload_json"])) if row else None

    def save_operation(self, record: OperationRecord) -> None:
        with self._transaction() as connection:
            self._save_operation(connection, record)

    def save_ledger_and_operations(
        self,
        target_symbol: str,
        ledger: IntentLedger,
        operations: Iterable[OperationRecord],
    ) -> None:
        with self._transaction() as connection:
            self._save_ledger(connection, target_symbol, ledger)
            for operation in operations:
                self._save_operation(connection, operation)

    def load_operations(self, target_symbol: str) -> tuple[OperationRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM operations WHERE target_symbol=? ORDER BY updated_at_utc, command_id",
                (target_symbol,),
            ).fetchall()
        return tuple(operation_record_from_data(_decode(row["payload_json"])) for row in rows)

    def save_market_state(self, target_symbol: str, payload: dict) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO market_state (target_symbol, payload_json, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(target_symbol) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (target_symbol, _encode(payload), _now()),
            )

    def load_market_state(self, target_symbol: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM market_state WHERE target_symbol=?", (target_symbol,)
            ).fetchone()
        return _decode(row["payload_json"]) if row else None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _migrate(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_events (
                    source_symbol TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    PRIMARY KEY (source_symbol, session_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS source_checkpoints (
                    source_symbol TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_ledgers (
                    target_symbol TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    command_id TEXT PRIMARY KEY,
                    target_symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operations_target_status_idx
                    ON operations (target_symbol, status);
                CREATE TABLE IF NOT EXISTS market_state (
                    target_symbol TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _save_ledger(connection: sqlite3.Connection, target_symbol: str, ledger: IntentLedger) -> None:
        connection.execute(
            """
            INSERT INTO intent_ledgers (target_symbol, payload_json, updated_at_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(target_symbol) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at_utc=excluded.updated_at_utc
            """,
            (target_symbol, _encode(ledger_to_data(ledger)), _now()),
        )

    @staticmethod
    def _save_operation(connection: sqlite3.Connection, record: OperationRecord) -> None:
        connection.execute(
            """
            INSERT INTO operations
                (command_id, target_symbol, status, payload_json, updated_at_utc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                status=excluded.status,
                payload_json=excluded.payload_json,
                updated_at_utc=excluded.updated_at_utc
            """,
            (
                record.command.command_id,
                record.command.target_symbol,
                record.status.value,
                _encode(operation_record_to_data(record)),
                _now(),
            ),
        )


def _encode(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(payload: str) -> dict:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("stored JSON payload must be an object")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
