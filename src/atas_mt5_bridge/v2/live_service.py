from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
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
from .models import ExecutionReceipt, OperationStatus
from .persistence import SqliteBridgeStateStore
from .policy import AtasAuthoritativePolicy
from .pricing import PriceGuardConfig, PriceMapper
from .test_control import DemoTestController


@dataclass
class LiveBridgeController:
    """Local controller joining ATAS WebSocket input and MT5 EA HTTP input."""

    source_symbol: str
    target_symbol: str
    bridge_magic: int
    database_path: Path
    journal_path: Path
    execution_enabled: bool = False
    volume_multiplier: Decimal = Decimal("1")
    price_guard_config: PriceGuardConfig = field(default_factory=PriceGuardConfig)
    test_controller: DemoTestController | None = None
    gateway: Mt5HttpGateway = field(init=False)
    state_store: SqliteBridgeStateStore = field(init=False)
    coordinator: SymbolCoordinator = field(init=False)
    _source_m1_history: tuple[dict[str, object], ...] = field(default=(), init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.state_store = SqliteBridgeStateStore(self.database_path)
        self.gateway = Mt5HttpGateway(self.target_symbol, self.bridge_magic)
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
                price_mapper=mapper,
                require_price_guard=self.execution_enabled,
            ),
            journal=JsonlOperationJournal(path=self.journal_path),
            state_store=self.state_store,
        )
        for record in self.coordinator.operation_book.records.values():
            if record.resolved_at_utc is None and record.status != OperationStatus.FAILED:
                self.gateway.restore_unresolved_command(record.command)
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
            if _is_basis_collector_event(payload):
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

    def handle_mt5_snapshot(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.gateway.ingest_snapshot(payload)
            self._refresh_history_calibration()
            self.coordinator.handle_target_observation()

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
        self._source_m1_history = tuple(
            {"time_utc": str(item.get("time_utc", "")), "close": str(item.get("close", ""))}
            for item in bars[-240:]
            if isinstance(item, Mapping) and item.get("time_utc") not in (None, "") and item.get("close") not in (None, "")
        )
        self._refresh_history_calibration()

    def _refresh_history_calibration(self) -> None:
        mapper = self.coordinator.policy.price_mapper
        target_bars = self.gateway.latest_m1_history()
        if mapper is not None and self._source_m1_history and target_bars:
            mapper.calibrate_from_minute_history(self._source_m1_history, target_bars)

    def handle_mt5_receipt(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            receipt = self.gateway.acknowledge_command(payload)
            self.coordinator.handle_target_receipt(receipt)

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
                "execution_enabled": self.execution_enabled,
                "source_synchronized": self.coordinator.source_book.has_synchronized_snapshot,
                "source_position_complete": self.coordinator.source_book.snapshot().position_complete
                if self.coordinator.source_book.has_synchronized_snapshot
                else False,
                "target_snapshot_received": target_snapshot["received"],
                "target_snapshot_fresh": target_snapshot["fresh"],
                "target_snapshot_age_ms": target_snapshot["age_ms"],
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
            }

    def next_mt5_command(self) -> str:
        with self._lock:
            commands = self.gateway.next_commands(limit=1)
            return command_to_tsv(commands[0]) if commands else ""


def start_mt5_http_server(controller: LiveBridgeController, host: str, port: int, token: str) -> ThreadingHTTPServer:
    handler = _make_handler(controller, token)
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


def start_test_http_server(
    test_controller: DemoTestController,
    host: str,
    port: int,
    token: str,
) -> ThreadingHTTPServer:
    """Start the separate local test-control API.

    Keeping this on its own port/token means the EA's bridge credential cannot
    be used to create demo orders accidentally.
    """
    handler = _make_test_handler(test_controller, token)
    server = ThreadingHTTPServer((host, port), handler)
    Thread(target=server.serve_forever, name="bridge-test-control", daemon=True).start()
    return server


def _make_handler(controller: LiveBridgeController, token: str):
    class Mt5Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._respond(404, "")
                return
            path = self.path.split("?", 1)[0]
            if path == "/v1/mt5/command":
                self._respond(200, controller.next_mt5_command(), content_type="text/plain; charset=utf-8")
            elif path == "/v1/status":
                self._respond(200, controller.status())
            else:
                self._respond(404, "")

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._respond(403, {"error": "forbidden"})
                return
            try:
                body = self._read_json()
                path = self.path.split("?", 1)[0]
                if path == "/v1/mt5/snapshot":
                    controller.handle_mt5_snapshot(body)
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

        def _authorized(self) -> bool:
            return bool(token) and self.headers.get("X-Bridge-Token", "") == token

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
            self.end_headers()
            self.wfile.write(encoded)

    return Mt5Handler


def _make_test_handler(test_controller: DemoTestController, token: str):
    class TestControlHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._respond(404, "")
                return
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
            if not self._authorized():
                self._respond(403, {"error": "forbidden"})
                return
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

        def _authorized(self) -> bool:
            return bool(token) and self.headers.get("X-Test-Control-Token", "") == token

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
    parser.add_argument("--source-symbol", required=True)
    parser.add_argument("--target-symbol", default="XAUUSD")
    parser.add_argument("--bridge-magic", type=int, required=True)
    parser.add_argument("--token", required=True, help="shared loopback token configured in the MT5 EA")
    parser.add_argument("--state-db", default="bridge-state/bridge.db")
    parser.add_argument("--journal", default="bridge-state/operations.jsonl")
    parser.add_argument("--output-dir", default="probe-data")
    parser.add_argument("--websocket-port", type=int, default=8767)
    parser.add_argument("--mt5-http-port", type=int, default=8768)
    parser.add_argument("--volume-multiplier", type=Decimal, default=Decimal("1"))
    parser.add_argument("--basis-window-seconds", type=int, default=4 * 60 * 60)
    parser.add_argument("--basis-half-life-seconds", type=int, default=15 * 60)
    parser.add_argument("--basis-sample-interval-seconds", type=int, default=60)
    parser.add_argument("--basis-minimum-samples", type=int, default=170)
    parser.add_argument("--quote-max-age-ms", type=int, default=250)
    parser.add_argument("--max-current-basis-deviation", type=Decimal, default=Decimal("2.00"))
    parser.add_argument("--max-basis-dispersion", type=Decimal, default=Decimal("1.00"))
    parser.add_argument("--enable-execution", action="store_true")
    parser.add_argument("--enable-test-control", action="store_true", help="enable the fixed local ATAS demo test scenario")
    parser.add_argument("--test-token", default="", help="separate token shared only with the ATAS test-control setting")
    parser.add_argument("--test-http-port", type=int, default=8769)
    args = parser.parse_args()
    if args.enable_test_control and not args.test_token:
        parser.error("--enable-test-control requires --test-token")
    if args.basis_window_seconds <= 0 or args.basis_half_life_seconds <= 0:
        parser.error("basis window and half-life must be positive")
    if args.basis_minimum_samples <= 0 or args.quote_max_age_ms <= 0 or args.basis_sample_interval_seconds <= 0:
        parser.error("basis sample count, sample interval, and quote maximum age must be positive")
    test_controller = DemoTestController(
        enabled=args.enable_test_control,
        token=args.test_token,
        report_path=Path(args.output_dir) / "test_runs.jsonl",
    )
    controller = LiveBridgeController(
        source_symbol=args.source_symbol,
        target_symbol=args.target_symbol,
        bridge_magic=args.bridge_magic,
        database_path=Path(args.state_db),
        journal_path=Path(args.journal),
        execution_enabled=args.enable_execution,
        volume_multiplier=args.volume_multiplier,
        price_guard_config=PriceGuardConfig(
            basis_window=timedelta(seconds=args.basis_window_seconds),
            basis_half_life=timedelta(seconds=args.basis_half_life_seconds),
            minimum_basis_samples=args.basis_minimum_samples,
            minimum_sample_interval=timedelta(seconds=args.basis_sample_interval_seconds),
            quote_max_age=timedelta(milliseconds=args.quote_max_age_ms),
            max_current_basis_deviation=args.max_current_basis_deviation,
            max_basis_dispersion=args.max_basis_dispersion,
        ),
        test_controller=test_controller,
    )
    server = start_mt5_http_server(controller, "127.0.0.1", args.mt5_http_port, args.token)
    test_server = (
        start_test_http_server(test_controller, "127.0.0.1", args.test_http_port, args.test_token)
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


if __name__ == "__main__":
    raise SystemExit(main())
