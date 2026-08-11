"""Loopback-only, data-driven ATAS demo-test control plane.

The ATAS DLL is a small executor of pre-approved order primitives.  Test
plans, values, sequencing, and reports live here so a new scenario can be
submitted over localhost without rebuilding or reloading ATAS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4


_MARKER_PREFIX = "AB2TEST:"
_ACTIVE_STATES = {"active", "working", "accepted", "pending"}
_DONE_STATES = {"done", "cancelled", "canceled", "filled", "rejected"}
_ACTIONS = {"open_pending", "modify_pending", "cancel_pending"}


@dataclass(frozen=True)
class TestStep:
    step_id: str
    action: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass
class TestRun:
    run_id: str
    scenario: str
    created_at_utc: str
    steps: tuple[TestStep, ...]
    status: str = "queued"
    phase: str = "dispatch"
    step_index: int = 0
    order_refs: dict[str, str] = field(default_factory=dict)
    current_order_id: str | None = None
    evidence_seen: bool = False
    last_error: str | None = None
    history: list[dict[str, object]] = field(default_factory=list)

    @property
    def marker(self) -> str:
        return f"{_MARKER_PREFIX}{self.run_id}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DemoTestController:
    """Execute parameterized, bounded demo pending-order plans.

    A plan can contain only distant limit/stop creation, modifications, and
    cancellation. It cannot submit arbitrary MQL/C# code, trade another
    account, or bypass the quantity/price checks enforced a second time by the
    ATAS strategy.
    """

    scenario = "pending_lifecycle"
    script_scenario = "pending_script"

    def __init__(self, *, enabled: bool, report_path: Path) -> None:
        self._enabled = enabled
        self._report_path = report_path
        self._runs: dict[str, TestRun] = {}
        self._driver_session_id: str | None = None
        self._driver_last_seen_utc: str | None = None
        self._lock = RLock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "scenarios": [self.scenario, self.script_scenario],
                "driver_connected": self._driver_session_id is not None,
                "driver_last_seen_utc": self._driver_last_seen_utc,
                "runs": {status: sum(run.status == status for run in self._runs.values()) for status in ("queued", "running", "completed", "aborted", "failed")},
            }

    def create_run(self, body: Mapping[str, Any]) -> dict[str, object]:
        if not self._enabled:
            raise PermissionError("test control is disabled")
        scenario = str(body.get("scenario", self.scenario)).strip()
        steps = self._parse_steps(scenario, body)
        with self._lock:
            run = TestRun(
                run_id=uuid4().hex[:12],
                scenario=scenario,
                created_at_utc=_utc_now(),
                steps=steps,
            )
            self._runs[run.run_id] = run
            self._append(run, "created", status=run.status, phase=run.phase, steps=[step.step_id for step in steps])
            return run.as_dict()

    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.as_dict() if run else None

    def abort_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in {"completed", "aborted", "failed"}:
                return run.as_dict() if run else None
            if run.current_order_id:
                run.steps = (TestStep("abort", "cancel_pending", {"order_ref": "__current"}),)
                run.step_index = 0
                run.order_refs["__current"] = run.current_order_id
                run.phase = "dispatch"
                run.status = "running"
                run.evidence_seen = False
                self._append(run, "operator abort requested; cancellation queued")
            else:
                run.status = "aborted"
                run.phase = "none"
                self._append(run, "operator abort requested before an order was observed")
            return run.as_dict()

    def command_for_ack(self, session_id: str | None = None) -> dict[str, object] | None:
        if not self._enabled or not session_id:
            return None
        with self._lock:
            if session_id != self._driver_session_id:
                return None
            for run in self._runs.values():
                if run.phase == "dispatch" and run.status not in {"completed", "aborted", "failed"}:
                    return self._command(run)
        return None

    def observe_probe_event(self, payload: Mapping[str, Any]) -> None:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return
        event_type = str(payload.get("event_type", ""))
        session_id = str(payload.get("session_id", "")).strip()
        if event_type in {"test_driver_ready", "test_driver_heartbeat"} and session_id:
            with self._lock:
                self._driver_session_id = session_id
                self._driver_last_seen_utc = _utc_now()
            return
        if event_type == "test_command_receipt":
            self._observe_receipt(data)
        elif event_type in {"order_new", "order_changed"}:
            self._observe_order(data)

    def _parse_steps(self, scenario: str, body: Mapping[str, Any]) -> tuple[TestStep, ...]:
        if scenario == self.scenario:
            unexpected = set(body).difference({"scenario", "parameters"})
            if unexpected:
                raise ValueError(f"unsupported fields: {', '.join(sorted(unexpected))}")
            parameters = _validate_open_parameters(body.get("parameters", {}))
            modify_parameters = {"price_offset_ticks": parameters.pop("modify_offset_ticks", -120)}
            return (
                TestStep("entry", "open_pending", parameters),
                TestStep("move", "modify_pending", {"order_ref": "entry", **modify_parameters}),
                TestStep("cancel", "cancel_pending", {"order_ref": "entry"}),
            )
        if scenario != self.script_scenario:
            raise ValueError(f"scenario must be {self.scenario!r} or {self.script_scenario!r}")
        raw_steps = body.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 20:
            raise ValueError("steps must contain 1 to 20 objects")
        steps: list[TestStep] = []
        identifiers: set[str] = set()
        known_refs: set[str] = set()
        for index, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, Mapping):
                raise ValueError("each step must be an object")
            step_id = str(raw.get("id", "")).strip()
            action = str(raw.get("action", "")).strip()
            parameters = raw.get("parameters", {})
            if not step_id or step_id in identifiers or len(step_id) > 32 or not step_id.replace("_", "").isalnum():
                raise ValueError(f"invalid unique step id at index {index}")
            if action not in _ACTIONS:
                raise ValueError(f"unsupported action at step {step_id}: {action}")
            if not isinstance(parameters, Mapping):
                raise ValueError(f"parameters for {step_id} must be an object")
            if action == "open_pending":
                normalized = _validate_open_parameters(parameters)
                known_refs.add(step_id)
            else:
                normalized = _validate_reference_parameters(action, parameters, known_refs)
            identifiers.add(step_id)
            steps.append(TestStep(step_id, action, normalized))
        return tuple(steps)

    def _observe_receipt(self, data: Mapping[str, Any]) -> None:
        run_id = str(data.get("run_id", "")).strip()
        command = str(data.get("command", "")).strip()
        accepted = bool(data.get("accepted", False))
        message = str(data.get("message", "")).strip()
        with self._lock:
            run = self._runs.get(run_id)
            step = self._current_step(run)
            if run is None or step is None or command != step.action or run.status in {"completed", "aborted", "failed"}:
                return
            if not accepted:
                self._fail(run, f"ATAS rejected {command}: {message or 'unknown error'}")
                return
            if run.evidence_seen:
                self._advance(run, f"ATAS accepted {command} after callback")
            else:
                run.status = "running"
                run.phase = "awaiting_event"
                self._append(run, f"ATAS accepted {command}; waiting for callback")

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
            step = self._current_step(run)
            if run is None or step is None or run.status in {"completed", "aborted", "failed"}:
                return
            if not self._matches_expected_event(run, step, order_id, state, cancelled):
                return
            self._append(run, "order observed", order_id=order_id, state=state, cancelled=cancelled, step=step.step_id)
            run.evidence_seen = True
            if step.action == "open_pending":
                run.order_refs[step.step_id] = order_id
                run.current_order_id = order_id
            elif step.action == "modify_pending":
                reference = str(step.parameters["order_ref"])
                run.order_refs[reference] = order_id
                run.current_order_id = order_id
            if run.phase == "awaiting_event":
                self._advance(run, "expected order callback received")

    def _matches_expected_event(self, run: TestRun, step: TestStep, order_id: str, state: str, cancelled: bool) -> bool:
        if step.action == "open_pending":
            return state in _ACTIVE_STATES
        reference = str(step.parameters["order_ref"])
        expected_order_id = run.order_refs.get(reference)
        if expected_order_id is None:
            self._fail(run, f"step {step.step_id} references unavailable order {reference}")
            return False
        if step.action == "modify_pending":
            return state in _ACTIVE_STATES
        return order_id == expected_order_id and (cancelled or state in _DONE_STATES)

    def _command(self, run: TestRun) -> dict[str, object] | None:
        step = self._current_step(run)
        if step is None:
            return None
        parameters = dict(step.parameters)
        if step.action != "open_pending":
            reference = str(parameters.pop("order_ref"))
            order_id = run.order_refs.get(reference)
            if order_id is None:
                self._fail(run, f"step {step.step_id} references unavailable order {reference}")
                return None
        else:
            order_id = None
        return {
            "command_id": f"{run.run_id}:{run.step_index}:{step.action}",
            "run_id": run.run_id,
            "command": step.action,
            "source_order_id": order_id or "",
            "parameters": parameters,
        }

    @staticmethod
    def _current_step(run: TestRun | None) -> TestStep | None:
        if run is None or run.step_index >= len(run.steps):
            return None
        return run.steps[run.step_index]

    def _advance(self, run: TestRun, reason: str) -> None:
        run.step_index += 1
        run.evidence_seen = False
        if run.step_index >= len(run.steps):
            run.status = "completed"
            run.phase = "none"
            self._append(run, reason, status=run.status, phase=run.phase)
        else:
            run.status = "running"
            run.phase = "dispatch"
            self._append(run, reason, status=run.status, phase=run.phase, next_step=run.steps[run.step_index].step_id)

    def _fail(self, run: TestRun, reason: str) -> None:
        run.status = "failed"
        run.phase = "none"
        run.last_error = reason
        self._append(run, reason, status=run.status, phase=run.phase)

    def _append(self, run: TestRun, message: str, **details: object) -> None:
        run.history.append({"at_utc": _utc_now(), "message": message, **details})
        self._write_report(run)

    def _write_report(self, run: TestRun) -> None:
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        with self._report_path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(run.as_dict(), ensure_ascii=False, separators=(",", ":")))
            output.write("\n")


def _validate_open_parameters(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("open_pending parameters must be an object")
    allowed = {"side", "order_type", "quantity", "price_offset_ticks", "modify_offset_ticks"}
    unexpected = set(value).difference(allowed)
    if unexpected:
        raise ValueError(f"unsupported open_pending parameters: {', '.join(sorted(unexpected))}")
    side = str(value.get("side", "buy")).lower()
    order_type = str(value.get("order_type", "limit")).lower()
    if side not in {"buy", "sell"} or order_type != "limit":
        raise ValueError("open_pending supports buy/sell limit only")
    quantity = _positive_decimal(value.get("quantity", "1"), "quantity")
    offset = _offset(value.get("price_offset_ticks", -100), "price_offset_ticks")
    expected_sign = -1 if (side == "buy") == (order_type == "limit") else 1
    if offset * expected_sign <= 0:
        raise ValueError("price_offset_ticks would create a marketable pending order")
    result: dict[str, object] = {"side": side, "order_type": order_type, "quantity": str(quantity), "price_offset_ticks": offset}
    if "modify_offset_ticks" in value:
        result["modify_offset_ticks"] = _offset(value["modify_offset_ticks"], "modify_offset_ticks")
    return result


def _validate_reference_parameters(action: str, value: Mapping[str, object], known_refs: set[str]) -> dict[str, object]:
    allowed = {"order_ref", "price_offset_ticks"} if action == "modify_pending" else {"order_ref"}
    unexpected = set(value).difference(allowed)
    if unexpected:
        raise ValueError(f"unsupported {action} parameters: {', '.join(sorted(unexpected))}")
    reference = str(value.get("order_ref", "")).strip()
    if reference not in known_refs:
        raise ValueError(f"{action} order_ref must reference an earlier open_pending step")
    result: dict[str, object] = {"order_ref": reference}
    if action == "modify_pending":
        result["price_offset_ticks"] = _offset(value.get("price_offset_ticks", -120), "price_offset_ticks")
    return result


def _positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be decimal") from error
    if number <= 0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _offset(value: object, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an integer") from error
    if number == 0 or abs(number) > 10000:
        raise ValueError(f"{field_name} must be between -10000 and 10000 excluding zero")
    return number


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
