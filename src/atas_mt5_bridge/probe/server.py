from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from atas_mt5_bridge.probe.recorder import ProbeProtocolError, ProbeRecorder, parse_probe_event


def log_server(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


async def serve(output_directory: Path, host: str, port: int) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets package is required to run the probe server") from exc

    recorder = ProbeRecorder(output_directory=output_directory)

    async def handler(websocket: Any) -> None:
        remote = getattr(websocket, "remote_address", None)
        log_server(f"probe client connected: {remote}")
        try:
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                    event = parse_probe_event(payload)
                    acknowledgement = recorder.record(event)
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
    args = parser.parse_args()
    asyncio.run(serve(Path(args.output_dir), args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
