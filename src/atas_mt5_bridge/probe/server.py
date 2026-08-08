from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from atas_mt5_bridge.probe.recorder import ProbeProtocolError, ProbeRecorder, parse_probe_event
from atas_mt5_bridge.v2.runtime import BridgeRuntime


def log_server(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


async def serve(
    output_directory: Path,
    host: str,
    port: int,
    *,
    bridge_mode: str = "recorder",
    source_symbol: str | None = None,
    target_symbol: str = "XAUUSD",
    runtime_output_directory: Path | None = None,
) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets package is required to run the probe server") from exc

    recorder = ProbeRecorder(output_directory=output_directory)
    runtime = None
    if bridge_mode != "recorder":
        if not source_symbol:
            raise ValueError("--source-symbol is required when bridge mode is observe or simulate")
        runtime = BridgeRuntime(
            source_symbol=source_symbol,
            target_symbol=target_symbol,
            mode=bridge_mode,
            output_directory=runtime_output_directory or output_directory / "runtime",
        )

    async def handler(websocket: Any) -> None:
        remote = getattr(websocket, "remote_address", None)
        log_server(f"probe client connected: {remote}")
        try:
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                    event = parse_probe_event(payload)
                    acknowledgement = recorder.record(event)
                    if runtime is not None:
                        try:
                            result = runtime.handle_probe_message(payload)
                            log_server(
                                f"runtime={bridge_mode} event={event.event_id} "
                                f"commands={len(result.commands)} reconcile={result.reconciliation_required}"
                            )
                        except Exception as runtime_error:
                            # Recorder availability and ATAS callbacks must not
                            # depend on a non-live planning/simulation failure.
                            log_server(
                                f"runtime error event={event.event_id}: "
                                f"{type(runtime_error).__name__}: {runtime_error}"
                            )
                    await websocket.send(json.dumps(acknowledgement, ensure_ascii=False))
                    log_server(
                        f"event={event.event_id} type={event.event_type} "
                        f"symbol={event.source_symbol} mode={event.delivery_mode}"
                    )
                except (json.JSONDecodeError, ProbeProtocolError, ValueError) as exc:
                    response = {"type": "probe_error", "message": str(exc)}
                    await websocket.send(json.dumps(response, ensure_ascii=False))
                    log_server(f"probe protocol error from {remote}: {exc}")
        except websockets.exceptions.ConnectionClosed as exc:
            log_server(f"probe client disconnected: {remote} code={exc.code} reason={exc.reason!r}")

    output_directory.mkdir(parents=True, exist_ok=True)
    log_server(f"ATAS bridge probe listening on ws://{host}:{port}; output={output_directory}")
    async with websockets.serve(handler, host, port, ping_interval=20, ping_timeout=10):
        await asyncio.Future()


def main() -> int:
    parser = argparse.ArgumentParser(description="Record events from AtasMt5BridgeProbe.")
    parser.add_argument("--output-dir", default="probe-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--bridge-mode",
        choices=("recorder", "observe", "simulate"),
        default="recorder",
        help="recorder is read-only; observe plans nothing; simulate never contacts MT5",
    )
    parser.add_argument("--source-symbol", help="exact ATAS source symbol for observe/simulate")
    parser.add_argument("--target-symbol", default="XAUUSD")
    parser.add_argument("--runtime-output-dir")
    args = parser.parse_args()
    asyncio.run(
        serve(
            Path(args.output_dir),
            args.host,
            args.port,
            bridge_mode=args.bridge_mode,
            source_symbol=args.source_symbol,
            target_symbol=args.target_symbol,
            runtime_output_directory=Path(args.runtime_output_dir) if args.runtime_output_dir else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
