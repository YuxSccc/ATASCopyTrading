"""Deliberately small, loopback-only control plane for demo integration tests.

This module does *not* expose a general order API.  It issues a fixed pending
order lifecycle to the ATAS strategy and advances only from events reported by
ATAS.  That makes it useful for exercising the transport and order callbacks
without turning the bridge service into an unauthenticated trading terminal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4


_MARKER_PREFIX = "AB2TEST:"
_ACTIVE_STATES = {"active", "working", "accepted", "pending"}
_DONE_STATES = {"done", "cancelled", "canceled", "filled", "rejected"}


@dataclass
class TestRun:
    run_id: str
    scenario: str
    created_at_utc: str
    status: str = "queued"
    phase: str = "open_pending"
    current_order_id: str | None = None
    last_error: str | None = None
    history: list[dict[str, object]] = field(default_factory=list)

    @property
    def marker(self) -> str:
        return f"{_MARKER_PREFIX}{self.run_id}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DemoTestController:
    """State machine for the one safe automated ATAS demo scenario.

    Commands are carried in normal ATAS WebSocket acknowledgements.  The
    strategy independently requires its test-control switch and token before
    it will act on any of them.
    """

    scenario = "pending_lifecycle"

    def __init__(self, *, enabled: bool, token: str, report_path: Path) -> None:
        self._enabled = enabled
        self._token = token
        self._report_path = report_path
        self._runs: dict[str, TestRun] = {}
        self._driver_session_id: str | None = None
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def create_run(self, body: Mapping[str, Any]) -> dict[str, object]:
        if not self._enabled:
            raise PermissionError("test control is disabled")
        scenario = str(body.get("scenario", self.scenario)).strip()
        if scenario != self.scenario:
            raise ValueError(f"scenario must be {self.scenario!r}")
        # Intentionally reject knobs such as price, side, or quantity.  Those
        # remain constrained ATAS-side strategy parameters, where the latest
        # quote and the account's portfolio/security are available.
        unexpected = set(body).difference({"scenario"})
        if unexpected:
            raise ValueError(f"unsupported test options: {', '.join(sorted(unexpected))}")
        with self._lock:
            run = TestRun(
                run_id=uuid4().hex[:12],
                scenario=scenario,
                created_at_utc=_utc_now(),
            )
            self._runs[run.run_id] = run
            self._transition(run, "queued", "open_pending", "created")
            return run.as_dict()

    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.as_dict() if run else None

    def abort_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            if run.status in {"completed", "aborted", "failed"}:
                return run.as_dict()
            if run.current_order_id:
                self._transition(run, "cancelling", "cancel_pending", "operator abort requested")
            else:
                self._transition(run, "aborted", "none", "operator abort requested before an order was observed")
            return run.as_dict()

    def command_for_ack(self, session_id: str | None = None) -> dict[str, object] | None:
        """Return the currently outstanding fixed command, if any.

        It is deliberately re-sent until the strategy reports its command
        receipt.  The strategy deduplicates command IDs, making reconnects
        safe and preventing an acknowledgement loss from stranding a test.
        """
        if not self._enabled or not session_id:
            return None
        with self._lock:
            if session_id != self._driver_session_id:
                return None
            for run in self._runs.values():
                command = self._command_for_run(run)
                if command is not None:
                    return command
        return None

    def observe_probe_event(self, payload: Mapping[str, Any]) -> None:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return
        event_type = str(payload.get("event_type", ""))
        if event_type == "test_driver_ready":
            session_id = str(payload.get("session_id", "")).strip()
            if session_id:
                with self._lock:
                    self._driver_session_id = session_id
        elif event_type == "test_command_receipt":
            self._observe_receipt(data)
        elif event_type in {"order_new", "order_changed"}:
            self._observe_order(data)

    def _observe_receipt(self, data: Mapping[str, Any]) -> None:
        run_id = str(data.get("run_id", "")).strip()
        command = str(data.get("command", "")).strip()
        accepted = bool(data.get("accepted", False))
        message = str(data.get("message", "")).strip()
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in {"completed", "aborted", "failed"}:
                return
            if not accepted:
                self._transition(run, "failed", "none", f"ATAS rejected {command}: {message or 'unknown error'}")
                return
            if command == "open_pending" and run.phase == "open_pending":
                self._transition(run, "waiting_for_open", "waiting_for_open", "ATAS accepted open command")
            elif command == "modify_pending" and run.phase == "modify_pending":
                self._transition(run, "waiting_for_modify", "waiting_for_modify", "ATAS accepted modify command")
            elif command == "cancel_pending" and run.phase == "cancel_pending":
                self._transition(run, "waiting_for_cancel", "waiting_for_cancel", "ATAS accepted cancel command")

    def _observe_order(self, data: Mapping[str, Any]) -> None:
        comment = str(data.get("comment", ""))
        if not comment.startswith(_MARKER_PREFIX):
            return
        run_id = comment[len(_MARKER_PREFIX) :].strip()
        order_id = str(data.get("id", "")).strip()
        state = str(data.get("state", "")).strip().lower()
        cancelled = bool(data.get("canceled", False))
        if not order_id:
            return
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in {"completed", "aborted", "failed"}:
                return
            self._append(run, "order observed", order_id=order_id, state=state, cancelled=cancelled)
            if run.phase == "waiting_for_open" and state in _ACTIVE_STATES:
                run.current_order_id = order_id
                self._transition(run, "modifying", "modify_pending", "pending order active")
            elif run.phase == "waiting_for_modify" and state in _ACTIVE_STATES:
                # ATAS may keep the order ID or implement a move as
                # cancel/recreate; retain whichever active ID it reports.
                run.current_order_id = order_id
                self._transition(run, "cancelling", "cancel_pending", "modified pending order active")
            elif run.phase == "waiting_for_cancel" and (cancelled or state in _DONE_STATES):
                self._transition(run, "completed", "none", "pending order terminal after cancel")

    def _command_for_run(self, run: TestRun) -> dict[str, object] | None:
        if run.phase == "open_pending":
            return self._command(run, "open_pending")
        if run.phase == "modify_pending" and run.current_order_id:
            return self._command(run, "modify_pending", source_order_id=run.current_order_id)
        if run.phase == "cancel_pending" and run.current_order_id:
            return self._command(run, "cancel_pending", source_order_id=run.current_order_id)
        return None

    def _command(self, run: TestRun, command: str, **extra: str) -> dict[str, object]:
        return {
            "command_id": f"{run.run_id}:{command}",
            "run_id": run.run_id,
            "command": command,
            "token": self._token,
            **extra,
        }

    def _transition(self, run: TestRun, status: str, phase: str, reason: str) -> None:
        run.status = status
        run.phase = phase
        self._append(run, reason, status=status, phase=phase)
        self._write_report(run)

    def _append(self, run: TestRun, message: str, **details: object) -> None:
        run.history.append({"at_utc": _utc_now(), "message": message, **details})
        self._write_report(run)

    def _write_report(self, run: TestRun) -> None:
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        with self._report_path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(run.as_dict(), ensure_ascii=False, separators=(",", ":")))
            output.write("\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
