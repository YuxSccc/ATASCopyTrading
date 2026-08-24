from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import RLock, Thread
from typing import Any, Mapping

from atas_mt5_bridge.probe.recorder import ProbeProtocolError, ProbeRecorder, parse_probe_event

from .atas_adapter import parse_probe_message
from .coordinator import SymbolCoordinator
from .journal import JsonlOperationJournal
from .mt5_gateway import Mt5HttpGateway, command_to_tsv
from .models import BridgeCommand, CommandType, ExecutionReceipt, IntentState, OperationStatus, TargetLegState
from .persistence import SqliteBridgeStateStore
from .policy import AtasAuthoritativePolicy
from .pricing import PriceGuardConfig, PriceMapper
from .symbol_aliases import source_symbol_matches
from .test_control import DemoTestController


@dataclass
class LiveBridgeController:
    """Local controller joining ATAS WebSocket input and MT5 EA HTTP input."""

    source_symbol: str
    target_symbol: str
    bridge_magic: int
    database_path: Path
    journal_path: Path
    pair_tag: str = ""
    basis_recording_directory: Path | None = None
    execution_enabled: bool = False
    volume_multiplier: Decimal = Decimal("1")
    target_fill_grace_seconds: int = 30
    price_guard_config: PriceGuardConfig = field(default_factory=PriceGuardConfig)
    test_controller: DemoTestController | None = None
    gateway: Mt5HttpGateway = field(init=False)
    state_store: SqliteBridgeStateStore = field(init=False)
    coordinator: SymbolCoordinator = field(init=False)
    _source_m1_history: tuple[dict[str, object], ...] = field(default=(), init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.state_store = SqliteBridgeStateStore(self.database_path)
        self.gateway = Mt5HttpGateway(self.target_symbol, self.bridge_magic, pair_tag=self.pair_tag or None)
        # Calibration must run in observation mode too. Otherwise the first
        # live order has neither a measured GC/XAUUSD basis nor evidence that
        # the chosen guard thresholds match the broker's feed.
        # Do not restore any prior basis from SQLite.  Every process starts
        # uncalibrated and must receive matching recent closed M1 windows from
        # ATAS and MT5 before it is allowed to map a pending price.
        mapper = PriceMapper.from_data(None, config=self.price_guard_config)
        self.coordinator = SymbolCoordinator(
            source_symbol=self.source_symbol,
            target_symbol=self.target_symbol,
            execution_port=self.gateway,
            policy=AtasAuthoritativePolicy(
                volume_multiplier=self.volume_multiplier,
                execution_enabled=self.execution_enabled,
                target_fill_grace_seconds=self.target_fill_grace_seconds,
                price_mapper=mapper,
                require_price_guard=self.execution_enabled,
            ),
            journal=JsonlOperationJournal(path=self.journal_path),
            state_store=self.state_store,
        )
        self._restore_source_history_from_recordings()
        for record in self.coordinator.operation_book.records.values():
            if record.resolved_at_utc is None:
                self.gateway.remember_quarantined_command(record.command)
            elif record.resolved_at_utc is not None:
                # A lost HTTP response can make an EA resend an old receipt.
                # Treat it as idempotent without reopening the completed op.
                receipt_status = OperationStatus.FAILED if record.status == OperationStatus.FAILED else OperationStatus.ACCEPTED
                self.gateway.restore_acknowledged_receipt(
                    ExecutionReceipt(
                        command_id=record.command.command_id,
                        status=receipt_status,
                        target_reference=record.target_reference,
                    )
                )

    def close(self) -> None:
        with self._lock:
            self.state_store.close()

    def handle_atas_message(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            # ATAS' display/security text changes with connector, contract
            # month and account.  Pair routing has already selected this
            # runtime (by tag or alias); normalize the envelope before the
            # canonical v2 parser sees it so a new Rithmic account cannot
            # create a second logical source symbol.
            payload = self._normalize_atas_payload(payload)
            if _is_basis_collector_event(payload):
                if _is_embedded_basis_event(payload):
                    # Embedded basis events use the Probe's source sequence;
                    # recording them as auxiliary events prevents a later
                    # order/quote event from appearing to have a sequence gap.
                    self.coordinator.handle(parse_probe_message(payload))
                self._handle_basis_collector_message(payload)
                return
            # The demo driver uses the same authenticated WebSocket transport
            # as the observation probe, but it is not a second source of
            # trade intent.  Feeding its separate session into SourceStateBook
            # would reset the live ATAS session on every test callback.
            if self.test_controller is not None and _is_test_driver_event(payload):
                self.test_controller.observe_probe_event(payload)
                return
            event = parse_probe_message(payload)
            self.coordinator.handle(event)
            if self.test_controller is not None:
                self.test_controller.observe_probe_event(payload)

    def probe_bridge_status(self) -> dict[str, object]:
        """Return the compact per-pair status displayed by the ATAS probe."""

        with self._lock:
            operations = tuple(self.coordinator.operation_book.records.values())
            if not self.coordinator.source_book.has_synchronized_snapshot:
                return {
                    "state": "waiting",
                    "message": f"{self.source_symbol}->{self.target_symbol}: waiting for synchronized ATAS state",
                    "reconciliation_required": False,
                }
            target_snapshot = self.gateway.snapshot_status()
            if not target_snapshot["received"] or not target_snapshot["fresh"]:
                return {
                    "state": "waiting",
                    "message": f"{self.source_symbol}->{self.target_symbol}: waiting for a fresh MT5 snapshot",
                    "reconciliation_required": False,
                }
            if self.coordinator.reconciliation_required:
                unknown = next(
                    (
                        record
                        for record in operations
                        if record.resolved_at_utc is None and record.status in {OperationStatus.UNKNOWN, OperationStatus.FAILED}
                    ),
                    None,
                )
                if unknown is not None:
                    return {
                        "state": "blocked",
                        "message": (
                            f"{self.source_symbol}->{self.target_symbol}: MT5 {unknown.command.command_type.value} "
                            f"outcome is unconfirmed; automatic mapping for this pair is stopped"
                        ),
                        "reconciliation_required": True,
                    }
                return {
                    "state": "blocked",
                    "message": f"{self.source_symbol}->{self.target_symbol}: reconciliation is required; automatic mapping for this pair is stopped",
                    "reconciliation_required": True,
                }
            if any(record.resolved_at_utc is None for record in operations):
                return {
                    "state": "waiting",
                    "message": f"{self.source_symbol}->{self.target_symbol}: waiting for MT5 snapshot confirmation",
                    "reconciliation_required": False,
                }
            return {
                "state": "ready",
                "message": f"{self.source_symbol}->{self.target_symbol}: bridge is ready",
                "reconciliation_required": False,
            }

    def _normalize_atas_payload(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Replace a connector display name with this runtime's source key.

        The original nested security text remains available in the raw JSONL
        recorder.  The v2 domain only needs the stable configured key for
        state-book and intent-ledger identity.  A shallow copy keeps caller
        payloads immutable and avoids mutating the recorder's object.
        """

        raw_symbol = str(payload.get("source_symbol", "")).strip()
        if not raw_symbol or raw_symbol.casefold() == self.source_symbol.strip().casefold():
            return payload
        # A supplied PairTag is authoritative for an unknown display name;
        # for known aliases the router has already verified the same runtime.
        # This also lets a future connector provide a new display name before
        # its alias is added.
        return {**payload, "source_symbol": self.source_symbol}

    def handle_mt5_snapshot(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.gateway.ingest_snapshot(payload)
            self._refresh_history_calibration()
            self.coordinator.handle_target_observation()

    def handle_mt5_snapshot_and_next_command(self, payload: Mapping[str, Any]) -> str:
        """Ingest one complete snapshot and return the next command in-band.

        The EA used to POST a snapshot and then make a second HTTP request to
        poll for a command.  Both requests are loopback calls, but MQL5's
        ``WebRequest`` is synchronous, so the second round trip directly adds
        to the observable order latency.  Keeping the operation under the
        same controller lock also means the command is selected from the
        snapshot that just reached the bridge.
        """

        with self._lock:
            self.gateway.ingest_snapshot(payload)
            self._refresh_history_calibration()
            self.coordinator.handle_target_observation()
            return self._next_mt5_command_locked()

    def handle_mt5_heartbeat(self, payload: Mapping[str, Any]) -> None:
        """Ingest only the fast MT5 quote/liveness message.

        A heartbeat must not be treated as a position/order observation.  In
        particular, it cannot confirm a create or cancel operation and it
        must not cause the full reconciliation planner to run on every
        50-millisecond timer tick.  It may still feed the in-memory basis
        estimator so a source order arriving between complete snapshots uses
        the latest target quote.
        """

        with self._lock:
            target = self.gateway.ingest_heartbeat(payload)
            if target is not None and self.coordinator.source_book.has_synchronized_snapshot:
                source = self.coordinator.source_book.snapshot()
                observer = getattr(self.coordinator.policy, "observe_market_data", None)
                if callable(observer):
                    observer(source, target)

    def handle_mt5_heartbeat_and_next_command(self, payload: Mapping[str, Any]) -> str:
        """Update quote/liveness and return one queued command in-band."""

        with self._lock:
            target = self.gateway.ingest_heartbeat(payload)
            if target is not None and self.coordinator.source_book.has_synchronized_snapshot:
                source = self.coordinator.source_book.snapshot()
                observer = getattr(self.coordinator.policy, "observe_market_data", None)
                if callable(observer):
                    observer(source, target)
            return self._next_mt5_command_locked()

    def _handle_basis_collector_message(self, payload: Mapping[str, Any]) -> None:
        if str(payload.get("event_type", "")) != "basis_history":
            return
        if str(payload.get("source_symbol", "")).strip() != self.source_symbol:
            return
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return
        bars = data.get("bars")
        if not isinstance(bars, list):
            return
        self._source_m1_history = _merge_minute_bars(self._source_m1_history, bars)
        self._refresh_history_calibration()

    def _restore_source_history_from_recordings(self) -> None:
        """Recover recent source M1 closes from this service's own raw logs.

        The ATAS cumulative-trade API can return only its initial historical
        segment immediately after an ATAS or service restart. Probe JSONL is
        already written for diagnosis; recovering its recent closed bars keeps
        a restart from discarding otherwise valid intraday calibration. Fresh
        bid/ask and current-basis guards remain mandatory before execution.
        """

        directory = self.basis_recording_directory
        if directory is None or not directory.is_dir():
            return
        recovered: tuple[dict[str, object], ...] = ()
        for path in directory.glob("atas_probe_*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        record = json.loads(line)
                        event = record.get("event")
                        if not isinstance(event, Mapping):
                            continue
                        if event.get("event_type") != "basis_history" or str(event.get("source_symbol", "")).strip() != self.source_symbol:
                            continue
                        data = event.get("data")
                        bars = data.get("bars") if isinstance(data, Mapping) else None
                        if not isinstance(bars, list):
                            continue
                        # A newly restarted collector often has only the
                        # current minute in addition to a pre-halt response.
                        # Merge every recorded response by minute instead of
                        # selecting the numerically longest (possibly older)
                        # one, then keep the rolling newest 240 below.
                        recovered = _merge_minute_bars(recovered, bars)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        self._source_m1_history = recovered

    def _refresh_history_calibration(self) -> None:
        mapper = self.coordinator.policy.price_mapper
        target_bars = self.gateway.latest_m1_history()
        if mapper is not None and self._source_m1_history and target_bars:
            mapper.calibrate_from_minute_history(self._source_m1_history, target_bars)

    def handle_mt5_receipt(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            receipt = self.gateway.acknowledge_command(payload)
            self.coordinator.handle_target_receipt(receipt)
            # A receipt with a concrete MT5 pending-order ticket can make a
            # source cancellation dispatchable before the next full account
            # snapshot.  Re-plan now; the coordinator still permits only the
            # risk-reducing cancellation while another operation is in flight.
            self.coordinator.handle_target_observation()

    def confirm_no_effect_after_operator_check(self, command_id: str) -> None:
        with self._lock:
            self.coordinator.confirm_no_effect_after_operator_check(command_id)

    def tick(self) -> None:
        with self._lock:
            self.coordinator.check_operation_timeouts()

    def status(self) -> dict[str, object]:
        with self._lock:
            operations = tuple(self.coordinator.operation_book.records.values())
            target_snapshot = self.gateway.snapshot_status()
            source_state = self.coordinator.source_book.snapshot() if self.coordinator.source_book.has_synchronized_snapshot else None
            target_state = self.gateway.latest_state()
            mapper = self.coordinator.policy.price_mapper
            return {
                "pair_tag": self.pair_tag,
                "source_symbol": self.source_symbol,
                "source_account": source_state.source_account if source_state is not None else "",
                "target_symbol": self.target_symbol,
                "bridge_magic": self.bridge_magic,
                "volume_multiplier": str(self.volume_multiplier),
                "target_fill_grace_seconds": self.target_fill_grace_seconds,
                "execution_enabled": self.execution_enabled,
                "source_synchronized": self.coordinator.source_book.has_synchronized_snapshot,
                "source_position_complete": self.coordinator.source_book.snapshot().position_complete
                if self.coordinator.source_book.has_synchronized_snapshot
                else False,
                "target_snapshot_received": target_snapshot["received"],
                "target_snapshot_fresh": target_snapshot["fresh"],
                "target_snapshot_age_ms": target_snapshot["age_ms"],
                "target_heartbeat_received": target_snapshot["heartbeat_received"],
                "target_heartbeat_age_ms": target_snapshot["heartbeat_age_ms"],
                "target_transport_alive": target_snapshot["transport_alive"],
                "reconciliation_required": self.coordinator.reconciliation_required,
                "in_flight_operations": sum(record.resolved_at_utc is None for record in operations),
                "unknown_or_failed_operations": sum(
                    record.resolved_at_utc is None and record.status in {OperationStatus.UNKNOWN, OperationStatus.FAILED}
                    for record in operations
                ),
                "test_control_enabled": bool(self.test_controller and self.test_controller.enabled),
                "basis": mapper.diagnostics(
                    source_state.quote if source_state is not None else None,
                    target_state.quote if target_state is not None else None,
                )
                if mapper is not None
                else None,
                "basis_history": {
                    "source_m1_bars": len(self._source_m1_history),
                    "target_m1_bars": len(self.gateway.latest_m1_history()),
                },
                "latency": {
                    "mt5_local_transport": self.gateway.transport_status(),
                    "note": "transport ends at EA receipt; operation confirmation is target-snapshot based",
                },
            }

    def next_mt5_command(self) -> str:
        with self._lock:
            return self._next_mt5_command_locked()

    def _next_mt5_command_locked(self) -> str:
        # A durable local queue is intentionally at-least-once, but a source
        # cancellation can arrive before its earlier create is polled by the
        # EA.  Never let such a superseded create turn into a delayed MT5
        # pending order.  We only discard commands that the gateway proves
        # were *not yet offered*; offered commands remain conservative and
        # go through ordinary receipt/snapshot reconciliation.
        while True:
            candidate = self.gateway.next_queued_command()
            if candidate is None:
                return ""
            if self._command_is_still_deliverable(candidate):
                commands = self.gateway.next_commands(limit=1)
                return command_to_tsv(commands[0]) if commands else ""
            if not self.gateway.discard_undelivered_command(candidate.command_id):
                return ""
            discarded = self.coordinator.operation_book.discard_before_delivery(candidate.command_id)
            if candidate.leg_id is not None:
                leg = self.coordinator.ledger.get_leg(candidate.leg_id)
                if leg is not None and leg.state == TargetLegState.CREATE_PENDING:
                    self.coordinator.ledger.update_leg(replace(leg, state=TargetLegState.CANCELLED))
            self.coordinator.state_store.save_ledger_and_operations(
                self.target_symbol,
                self.coordinator.ledger,
                (discarded,),
            )

    def _command_is_still_deliverable(self, command: BridgeCommand) -> bool:
        """Validate delayed queue entries against the newest ATAS intent."""

        if command.command_type != CommandType.CREATE_PENDING:
            return True
        if command.intent_id is None or command.leg_id is None or command.source_order_id is None:
            return False
        intent = self.coordinator.ledger.intents.get(command.intent_id)
        leg = self.coordinator.ledger.get_leg(command.leg_id)
        if intent is None or leg is None or intent.state != IntentState.ACTIVE:
            return False
        if leg.state != TargetLegState.CREATE_PENDING:
            return False
        if not self.coordinator.source_book.has_synchronized_snapshot:
            return False
        source = self.coordinator.source_book.snapshot()
        return (
            intent.source_key.session_id == source.session_id
            and command.source_order_id in source.active_orders
        )


@dataclass
class PairRoutedBridgeController:
    """Route several isolated symbol runtimes through one local transport.

    ``pair_tag`` is the preferred routing key.  During rollout, clients that
    have not yet been rebuilt may omit it; ATAS then matches a unique source
    symbol and MT5 matches the unique target-symbol/magic tuple.  Ambiguous
    messages are rejected instead of being sent to the wrong account leg.
    """

    runtimes: dict[str, LiveBridgeController]
    test_controller: DemoTestController | None = None
    default_pair_tag: str | None = None

    def __post_init__(self) -> None:
        if not self.runtimes:
            raise ValueError("at least one pair runtime is required")
        self.runtimes = {_pair_key(tag): runtime for tag, runtime in self.runtimes.items()}
        if self.default_pair_tag is None:
            self.default_pair_tag = next(iter(self.runtimes))

    def _resolve(self, payload: Mapping[str, Any], kind: str) -> LiveBridgeController:
        supplied = str(payload.get("pair_tag", "")).strip()
        if supplied:
            runtime = self.runtimes.get(_pair_key(supplied))
            if runtime is None:
                raise ValueError(f"unknown pair_tag: {supplied}")
            if kind == "atas":
                # A stale tag must never silently send a recognizable source
                # instrument to another pair.  If the display name resolves
                # uniquely, require it to agree with the supplied tag; an
                # unknown future display name may still use the explicit tag
                # while the alias table is being extended.
                value = str(payload.get("source_symbol", "")).strip()
                candidates = [
                    item
                    for item in self.runtimes.values()
                    if source_symbol_matches(item.source_symbol, value)
                ]
                if len(candidates) == 1 and candidates[0] is not runtime:
                    raise ValueError(
                        f"pair_tag/source_symbol conflict: {supplied}/{value}"
                    )
            return runtime

        if kind == "atas":
            value = str(payload.get("source_symbol", "")).strip()
            candidates = [
                item
                for item in self.runtimes.values()
                if source_symbol_matches(item.source_symbol, value)
            ]
        elif kind == "mt5":
            symbol = str(payload.get("target_symbol", "")).strip().lower()
            try:
                magic = int(payload.get("bridge_magic", -1))
            except (TypeError, ValueError):
                magic = -1
            candidates = [
                item for item in self.runtimes.values()
                if item.target_symbol.strip().lower() == symbol and item.bridge_magic == magic
            ]
        else:
            command_id = str(payload.get("command_id", "")).strip()
            candidates = [
                item for item in self.runtimes.values()
                if command_id in item.coordinator.operation_book.records
                or command_id in item.gateway._queued_commands
                or command_id in item.gateway._quarantined_commands
            ]

        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(f"cannot route {kind} message without pair_tag")
        raise ValueError(f"ambiguous {kind} message; pair_tag is required")

    def handle_atas_message(self, payload: Mapping[str, Any]) -> None:
        self._resolve(payload, "atas").handle_atas_message(payload)

    def probe_bridge_status(self, payload: Mapping[str, Any]) -> dict[str, object]:
        return self._resolve(payload, "atas").probe_bridge_status()

    def handle_mt5_snapshot(self, payload: Mapping[str, Any]) -> None:
        self._resolve(payload, "mt5").handle_mt5_snapshot(payload)

    def handle_mt5_snapshot_and_next_command(self, payload: Mapping[str, Any]) -> str:
        return self._resolve(payload, "mt5").handle_mt5_snapshot_and_next_command(payload)

    def handle_mt5_heartbeat(self, payload: Mapping[str, Any]) -> None:
        self._resolve(payload, "mt5").handle_mt5_heartbeat(payload)

    def handle_mt5_heartbeat_and_next_command(self, payload: Mapping[str, Any]) -> str:
        return self._resolve(payload, "mt5").handle_mt5_heartbeat_and_next_command(payload)

    def handle_mt5_receipt(self, payload: Mapping[str, Any]) -> None:
        self._resolve(payload, "receipt").handle_mt5_receipt(payload)

    def confirm_no_effect_after_operator_check(self, command_id: str) -> None:
        self._resolve({"command_id": command_id}, "receipt").confirm_no_effect_after_operator_check(command_id)

    def next_mt5_command(self) -> str:
        # The legacy GET endpoint has no symbol in its request. Keep it useful
        # for a one-pair setup; multi-pair EAs use the in-band POST response.
        runtime = self.runtimes[_pair_key(self.default_pair_tag or "")]
        return runtime.next_mt5_command()

    def tick(self) -> None:
        for runtime in self.runtimes.values():
            runtime.tick()

    def status(self) -> dict[str, object]:
        pairs = {tag: runtime.status() for tag, runtime in self.runtimes.items()}
        default = pairs[_pair_key(self.default_pair_tag or "")]
        return {**default, "pairs": pairs, "pair_count": len(pairs)}

    def close(self) -> None:
        for runtime in self.runtimes.values():
            runtime.close()


def _pair_key(value: str) -> str:
    return str(value).strip().lower()


def start_mt5_http_server(controller: object, host: str, port: int) -> ThreadingHTTPServer:
    handler = _make_handler(controller)
    server = ThreadingHTTPServer((host, port), handler)
    Thread(target=server.serve_forever, name="mt5-http", daemon=True).start()
    return server


def _is_test_driver_event(payload: Mapping[str, Any]) -> bool:
    event_type = str(payload.get("event_type", ""))
    if event_type in {"test_driver_ready", "test_driver_heartbeat", "test_command_receipt", "test_driver_error"}:
        return True
    data = payload.get("data")
    return isinstance(data, Mapping) and str(data.get("comment", "")).startswith("AB2TEST:")


def _is_basis_collector_event(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("event_type", "")) in {"basis_collector_ready", "basis_history"}


def _is_embedded_basis_event(payload: Mapping[str, Any]) -> bool:
    data = payload.get("data")
    return isinstance(data, Mapping) and bool(data.get("embedded_in_probe", False))


def _merge_minute_bars(
    existing: tuple[dict[str, object], ...],
    incoming: list[object],
) -> tuple[dict[str, object], ...]:
    """Return the newest 240 valid closed M1 bars keyed by exact UTC text."""

    merged: dict[str, dict[str, object]] = {}
    for item in (*existing, *incoming):
        if not isinstance(item, Mapping):
            continue
        time_utc = str(item.get("time_utc", "")).strip()
        close = item.get("close")
        if not time_utc or close in (None, ""):
            continue
        merged[time_utc] = {"time_utc": time_utc, "close": str(close)}
    return tuple(merged[key] for key in sorted(merged)[-240:])


def start_test_http_server(
    test_controller: DemoTestController,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    """Start the separate local test-control API.

    Keeping this on its own loopback-only port means the EA cannot create demo
    orders accidentally.
    """
    handler = _make_test_handler(test_controller)
    server = ThreadingHTTPServer((host, port), handler)
    Thread(target=server.serve_forever, name="bridge-test-control", daemon=True).start()
    return server


def _make_handler(controller: LiveBridgeController):
    class Mt5Handler(BaseHTTPRequestHandler):
        # Explicit HTTP/1.1 framing plus Connection: close avoids terminal
        # builds attempting to reuse a loopback connection after a response.
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/v1/mt5/command":
                self._respond(200, controller.next_mt5_command(), content_type="text/plain; charset=utf-8")
            elif path == "/v1/status":
                self._respond(200, controller.status())
            else:
                self._respond(404, "")

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = self._read_json()
                path = self.path.split("?", 1)[0]
                if path == "/v1/mt5/snapshot":
                    # ``poll=1`` is the low-latency EA path.  The response is
                    # either an empty body or the same TSV command formerly
                    # returned by /v1/mt5/command.  The old JSON response is
                    # retained for existing EAs and diagnostic clients.
                    query = self.path.split("?", 1)[1] if "?" in self.path else ""
                    if "poll=1" in query.split("&"):
                        command = controller.handle_mt5_snapshot_and_next_command(body)
                        self._respond(200, command, content_type="text/plain; charset=utf-8")
                        return
                    controller.handle_mt5_snapshot(body)
                elif path == "/v1/mt5/heartbeat":
                    # Heartbeats carry only quote/liveness.  They never
                    # replace the complete state used for confirmation.
                    query = self.path.split("?", 1)[1] if "?" in self.path else ""
                    if "poll=1" in query.split("&"):
                        command = controller.handle_mt5_heartbeat_and_next_command(body)
                        self._respond(200, command, content_type="text/plain; charset=utf-8")
                        return
                    controller.handle_mt5_heartbeat(body)
                elif path == "/v1/mt5/command":
                    self._respond(200, controller.next_mt5_command(), content_type="text/plain; charset=utf-8")
                    return
                elif path == "/v1/mt5/receipt":
                    controller.handle_mt5_receipt(body)
                elif path == "/v1/operator/confirm-no-effect":
                    command_id = str(body.get("command_id", "")).strip()
                    if not command_id:
                        raise ValueError("command_id is required")
                    controller.confirm_no_effect_after_operator_check(command_id)
                else:
                    self._respond(404, {"error": "unknown endpoint"})
                    return
            except Exception as error:
                self._respond(400, {"error": f"{type(error).__name__}: {error}"})
                return
            self._respond(200, {"ok": True})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> Mapping[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length)
            # MQL5 WebRequest sends the terminating NUL added by
            # StringToCharArray and includes it in Content-Length. Accept that
            # transport detail while still requiring one valid JSON object.
            value = json.loads(raw.decode("utf-8").rstrip("\x00"))
            if not isinstance(value, Mapping):
                raise ValueError("JSON body must be an object")
            return value

        def _respond(self, status: int, body: object, content_type: str = "application/json; charset=utf-8") -> None:
            payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            encoded = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)

    return Mt5Handler


def _make_test_handler(test_controller: DemoTestController):
    class TestControlHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if len(parts) == 4 and parts[:3] == ["v1", "test", "runs"]:
                run = test_controller.get_run(parts[3])
                if run is None:
                    self._respond(404, {"error": "test run not found"})
                else:
                    self._respond(200, run)
            elif parts == ["v1", "test", "status"]:
                self._respond(200, test_controller.status())
            else:
                self._respond(404, {"error": "unknown endpoint"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = self._read_json()
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if parts == ["v1", "test", "runs"]:
                    response = test_controller.create_run(body)
                    self._respond(201, response)
                    return
                if len(parts) == 5 and parts[:3] == ["v1", "test", "runs"] and parts[4] == "abort":
                    response = test_controller.abort_run(parts[3])
                    if response is None:
                        self._respond(404, {"error": "test run not found"})
                    else:
                        self._respond(200, response)
                    return
                self._respond(404, {"error": "unknown endpoint"})
            except PermissionError as error:
                self._respond(403, {"error": str(error)})
            except (ValueError, json.JSONDecodeError) as error:
                self._respond(400, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> Mapping[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("JSON body must be an object")
            return value

        def _respond(self, status: int, body: object, content_type: str = "application/json; charset=utf-8") -> None:
            payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            encoded = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return TestControlHandler


async def serve(
    controller: LiveBridgeController,
    *,
    output_directory: Path,
    websocket_host: str,
    websocket_port: int,
) -> None:
    try:
        import websockets
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:
        raise RuntimeError("websockets package is required") from exc
    recorder = ProbeRecorder(output_directory)

    async def handler(websocket: Any) -> None:
        try:
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                    event = parse_probe_event(payload)
                    acknowledgement = recorder.record(event)
                    controller.handle_atas_message(payload)
                    probe_status = getattr(controller, "probe_bridge_status", None)
                    if callable(probe_status):
                        acknowledgement["bridge_status"] = probe_status(payload)
                    if controller.test_controller is not None:
                        test_command = controller.test_controller.command_for_ack(event.session_id)
                        if test_command is not None:
                            acknowledgement["test_command"] = test_command
                    await websocket.send(json.dumps(acknowledgement, ensure_ascii=False))
                except (json.JSONDecodeError, ProbeProtocolError, ValueError) as error:
                    await websocket.send(json.dumps({"type": "probe_error", "message": str(error)}))
        except ConnectionClosed:
            # ATAS reconnects its local WebSocket after transient failures.
            # A peer that disappears without a close frame is not a server error.
            return

    output_directory.mkdir(parents=True, exist_ok=True)
    async with websockets.serve(handler, websocket_host, websocket_port, ping_interval=20, ping_timeout=10):
        async def watchdog() -> None:
            while True:
                controller.tick()
                await asyncio.sleep(0.1)

        watchdog_task = asyncio.create_task(watchdog(), name="bridge-operation-watchdog")
        try:
            await asyncio.Future()
        finally:
            watchdog_task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local ATAS/MT5 bridge service.")
    parser.add_argument("--source-symbol", required=False)
    parser.add_argument("--target-symbol", default="XAUUSD")
    parser.add_argument("--bridge-magic", type=int, required=False)
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="TAG=SOURCE:TARGET:MAGIC[:MULTIPLIER]",
        help="repeat for one-port multi-pair routing; multiplier is target volume per source unit",
    )
    parser.add_argument("--state-db", default="bridge-state/bridge.db")
    parser.add_argument("--journal", default="bridge-state/operations.jsonl")
    parser.add_argument("--output-dir", default="probe-data")
    parser.add_argument("--websocket-port", type=int, default=8767)
    parser.add_argument("--mt5-http-port", type=int, default=8768)
    parser.add_argument("--volume-multiplier", type=Decimal, default=Decimal("1"))
    parser.add_argument(
        "--target-fill-grace-seconds",
        type=int,
        default=30,
        help="retain a known MT5-first pending fill before ATAS-authoritative net repair (0 disables)",
    )
    parser.add_argument("--basis-window-seconds", type=int, default=4 * 60 * 60)
    parser.add_argument("--basis-half-life-seconds", type=int, default=15 * 60)
    parser.add_argument("--basis-sample-interval-seconds", type=int, default=60)
    parser.add_argument("--basis-minimum-samples", type=int, default=170)
    parser.add_argument("--quote-max-age-ms", type=int, default=3000)
    parser.add_argument("--max-current-basis-deviation", type=Decimal, default=Decimal("2.00"))
    parser.add_argument("--max-basis-dispersion", type=Decimal, default=Decimal("1.00"))
    parser.add_argument("--enable-execution", action="store_true")
    parser.add_argument("--enable-test-control", action="store_true", help="enable the fixed local ATAS demo test scenario")
    parser.add_argument("--test-http-port", type=int, default=8769)
    args = parser.parse_args()
    if args.basis_window_seconds <= 0 or args.basis_half_life_seconds <= 0:
        parser.error("basis window and half-life must be positive")
    if args.basis_minimum_samples <= 0 or args.quote_max_age_ms <= 0 or args.basis_sample_interval_seconds <= 0:
        parser.error("basis sample count, sample interval, and quote maximum age must be positive")
    if args.target_fill_grace_seconds < 0:
        parser.error("--target-fill-grace-seconds must be zero or positive")
    if not _valid_multiplier(args.volume_multiplier):
        parser.error("--volume-multiplier must be a finite positive decimal")
    try:
        pair_specs = _parse_pair_specs(args)
    except ValueError as error:
        parser.error(str(error))
    test_controller = DemoTestController(
        enabled=args.enable_test_control,
        report_path=Path(args.output_dir) / "test_runs.jsonl",
    )
    price_guard_config = PriceGuardConfig(
        basis_window=timedelta(seconds=args.basis_window_seconds),
        basis_half_life=timedelta(seconds=args.basis_half_life_seconds),
        minimum_basis_samples=args.basis_minimum_samples,
        minimum_sample_interval=timedelta(seconds=args.basis_sample_interval_seconds),
        quote_max_age=timedelta(milliseconds=args.quote_max_age_ms),
        max_current_basis_deviation=args.max_current_basis_deviation,
        max_basis_dispersion=args.max_basis_dispersion,
    )
    runtimes = {
        tag: LiveBridgeController(
            source_symbol=source_symbol,
            target_symbol=target_symbol,
            bridge_magic=bridge_magic,
            database_path=Path(args.state_db),
            journal_path=Path(args.journal),
            pair_tag=tag,
            basis_recording_directory=Path(args.output_dir),
            execution_enabled=args.enable_execution,
            volume_multiplier=volume_multiplier,
            target_fill_grace_seconds=args.target_fill_grace_seconds,
            price_guard_config=price_guard_config,
            test_controller=test_controller,
        )
        for tag, source_symbol, target_symbol, bridge_magic, volume_multiplier in pair_specs
    }
    controller: PairRoutedBridgeController | LiveBridgeController
    controller = PairRoutedBridgeController(runtimes, test_controller=test_controller) if len(runtimes) > 1 else next(iter(runtimes.values()))
    server = start_mt5_http_server(controller, "127.0.0.1", args.mt5_http_port)
    test_server = (
        start_test_http_server(test_controller, "127.0.0.1", args.test_http_port)
        if args.enable_test_control
        else None
    )
    try:
        asyncio.run(
            serve(
                controller,
                output_directory=Path(args.output_dir),
                websocket_host="127.0.0.1",
                websocket_port=args.websocket_port,
            )
        )
    finally:
        server.shutdown()
        if test_server is not None:
            test_server.shutdown()
        server.server_close()
        if test_server is not None:
            test_server.server_close()
        controller.close()
    return 0


def _parse_pair_specs(args: argparse.Namespace) -> list[tuple[str, str, str, int, Decimal]]:
    if args.pair:
        if args.source_symbol or args.bridge_magic is not None:
            raise ValueError("use either --pair or legacy --source-symbol/--bridge-magic, not both")
        result: list[tuple[str, str, str, int, Decimal]] = []
        seen: set[str] = set()
        for raw in args.pair:
            if "=" not in raw:
                raise ValueError(f"invalid --pair {raw!r}; expected TAG=SOURCE:TARGET:MAGIC[:MULTIPLIER]")
            tag, details = raw.split("=", 1)
            parts = details.split(":")
            if len(parts) not in {3, 4} or not all(part.strip() for part in parts):
                raise ValueError(f"invalid --pair {raw!r}; expected TAG=SOURCE:TARGET:MAGIC[:MULTIPLIER]")
            source, target, magic_text = (part.strip() for part in parts[:3])
            multiplier_text = parts[3].strip() if len(parts) == 4 else str(args.volume_multiplier)
            try:
                magic = int(magic_text)
            except ValueError as error:
                raise ValueError(f"invalid bridge magic in --pair {raw!r}") from error
            try:
                multiplier = Decimal(multiplier_text)
            except (InvalidOperation, ValueError) as error:
                raise ValueError(f"invalid volume multiplier in --pair {raw!r}") from error
            if not _valid_multiplier(multiplier):
                raise ValueError(f"volume multiplier must be finite and positive in --pair {raw!r}")
            key = _pair_key(tag)
            if not key or key in seen:
                raise ValueError(f"duplicate/empty pair tag in --pair {raw!r}")
            seen.add(key)
            result.append((tag.strip(), source, target, magic, multiplier))
        return result
    if not args.source_symbol or args.bridge_magic is None:
        raise ValueError("--source-symbol and --bridge-magic are required when --pair is not used")
    return [("", args.source_symbol, args.target_symbol, args.bridge_magic, args.volume_multiplier)]


def _valid_multiplier(value: Decimal) -> bool:
    return value.is_finite() and value > 0


if __name__ == "__main__":
    raise SystemExit(main())
