"""Grain-call RPC encoding and dispatch helpers.

The protocol for an inter-silo grain call rides in a single
:attr:`MessageType.REQUEST` with header ``b"grain-call/v1"``. The
request body is a JSON-encoded envelope; the response is either a
success envelope carrying the serialised return value or a failure
envelope carrying the remote exception class name, message, and
truncated traceback.

Exception classes are **never** reconstructed on the caller — the
caller raises :class:`RemoteGrainException`, preserving metadata as
strings. See task-02-16 for the security rationale.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass
from typing import Any, Final

from pyleans.identity import GrainId

logger = logging.getLogger(__name__)

GRAIN_CALL_HEADER: Final[bytes] = b"pyleans/grain-call/v1"
GRAIN_CALL_SCHEMA_VERSION: Final[int] = 1

_MAX_EXCEPTION_MESSAGE_LEN: Final[int] = 8 * 1024
_MAX_TRACEBACK_LEN: Final[int] = 32 * 1024


@dataclass(frozen=True)
class GrainCallRequest:
    grain_id: GrainId
    method: str
    args: list[Any]
    kwargs: dict[str, Any]
    caller_silo: str | None
    call_id: int
    deadline: float | None


@dataclass(frozen=True)
class GrainCallSuccess:
    result: Any


@dataclass(frozen=True)
class GrainCallFailure:
    exception_type: str
    message: str
    remote_traceback: str | None


def encode_request(req: GrainCallRequest) -> bytes:
    body = {
        "v": GRAIN_CALL_SCHEMA_VERSION,
        "grain_type": req.grain_id.grain_type,
        "grain_key": req.grain_id.key,
        "method": req.method,
        "args": list(req.args),
        "kwargs": dict(req.kwargs),
        "caller": req.caller_silo,
        "call_id": req.call_id,
        "deadline": req.deadline,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def decode_request(data: bytes) -> GrainCallRequest:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid grain-call bytes: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("grain-call body must be a JSON object")
    version = obj.get("v")
    if version != GRAIN_CALL_SCHEMA_VERSION:
        raise ValueError(f"unsupported grain-call schema v{version!r}")
    try:
        grain_id = GrainId(grain_type=str(obj["grain_type"]), key=str(obj["grain_key"]))
        method = str(obj["method"])
        raw_args = obj.get("args", [])
        raw_kwargs = obj.get("kwargs", {})
        if not isinstance(raw_args, list):
            raise ValueError("args must be a list")
        if not isinstance(raw_kwargs, dict):
            raise ValueError("kwargs must be an object")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"grain-call fields: {exc}") from exc
    caller_raw = obj.get("caller")
    deadline_raw = obj.get("deadline")
    return GrainCallRequest(
        grain_id=grain_id,
        method=method,
        args=list(raw_args),
        kwargs=dict(raw_kwargs),
        caller_silo=str(caller_raw) if caller_raw is not None else None,
        call_id=int(obj.get("call_id", 0)),
        deadline=float(deadline_raw) if deadline_raw is not None else None,
    )


def encode_success(result: Any) -> bytes:
    return json.dumps({"ok": True, "result": result}, separators=(",", ":")).encode("utf-8")


def encode_failure(
    exception_type: str,
    message: str,
    remote_traceback: str | None = None,
) -> bytes:
    truncated_message = message[:_MAX_EXCEPTION_MESSAGE_LEN]
    truncated_tb = remote_traceback[:_MAX_TRACEBACK_LEN] if remote_traceback is not None else None
    return json.dumps(
        {
            "ok": False,
            "exception_type": exception_type,
            "message": truncated_message,
            "traceback": truncated_tb,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def decode_response(data: bytes) -> GrainCallSuccess | GrainCallFailure:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid grain-call response: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("grain-call response must be a JSON object")
    if obj.get("ok"):
        return GrainCallSuccess(result=obj.get("result"))
    return GrainCallFailure(
        exception_type=str(obj.get("exception_type", "Exception")),
        message=str(obj.get("message", "")),
        remote_traceback=(str(obj["traceback"]) if obj.get("traceback") is not None else None),
    )


def format_exception_for_wire(exc: BaseException) -> tuple[str, str, str]:
    """Return ``(exception_type, message, traceback)`` strings for ``exc``."""
    exc_type = type(exc).__name__
    message = str(exc)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return exc_type, message, tb
