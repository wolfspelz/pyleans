"""Wire protocol for the distributed grain directory's inter-silo RPCs.

The directory runs four operations across silos: ``REGISTER``,
``LOOKUP``, ``UNREGISTER``, ``RESOLVE_OR_ACTIVATE`` — each carried as
one :attr:`MessageType.REQUEST` frame with a per-op header tag and a
JSON body. Two fire-and-forget one-way messages round out the
set — ``HANDOFF`` (owner-change propagation) and ``DEACTIVATE``
(split-view loser notification).

Body schema is JSON for readability and to match the snapshot
broadcast in task-02-11; values are plain dicts — no binary framing
at this level beyond the transport's own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from pyleans.cluster.directory import DirectoryEntry
from pyleans.cluster.placement import PlacementStrategy
from pyleans.identity import GrainId, SiloAddress

DIRECTORY_HEADER_PREFIX: Final[bytes] = b"pyleans/directory/v1/"

DIRECTORY_REGISTER_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"register"
DIRECTORY_LOOKUP_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"lookup"
DIRECTORY_UNREGISTER_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"unregister"
DIRECTORY_RESOLVE_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"resolve"
DIRECTORY_HANDOFF_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"handoff"
DIRECTORY_DEACTIVATE_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"deactivate"
DIRECTORY_REBUILD_QUERY_HEADER: Final[bytes] = DIRECTORY_HEADER_PREFIX + b"rebuild-query"


def is_directory_header(header: bytes) -> bool:
    return header.startswith(DIRECTORY_HEADER_PREFIX)


@dataclass(frozen=True)
class RegisterRequest:
    grain_id: GrainId
    silo: SiloAddress


@dataclass(frozen=True)
class LookupRequest:
    grain_id: GrainId


@dataclass(frozen=True)
class UnregisterRequest:
    grain_id: GrainId
    silo: SiloAddress


@dataclass(frozen=True)
class ResolveRequest:
    grain_id: GrainId
    placement_class: str
    caller: SiloAddress | None


@dataclass(frozen=True)
class HandoffRequest:
    entry: DirectoryEntry


@dataclass(frozen=True)
class DeactivateRequest:
    grain_id: GrainId
    activation_epoch: int


@dataclass(frozen=True)
class Arc:
    """Closed-open arc on the 64-bit hash ring; wraps when ``start > end``."""

    start: int
    end: int

    def contains(self, hash_position: int) -> bool:
        if self.start <= self.end:
            return self.start < hash_position <= self.end
        return hash_position > self.start or hash_position <= self.end


@dataclass(frozen=True)
class RebuildQueryRequest:
    arcs: list[Arc]


@dataclass(frozen=True)
class RebuildQueryResponse:
    entries: list[DirectoryEntry]


# ---- Codec helpers -----------------------------------------------------------


def _grain_id_to_dict(g: GrainId) -> dict[str, str]:
    return {"grain_type": g.grain_type, "key": g.key}


def _grain_id_from_dict(obj: Any) -> GrainId:
    if not isinstance(obj, dict):
        raise ValueError("grain_id must be an object")
    try:
        return GrainId(grain_type=str(obj["grain_type"]), key=str(obj["key"]))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"grain_id fields: {exc}") from exc


def _silo_to_str(s: SiloAddress) -> str:
    return s.silo_id


def _silo_from_str(value: Any) -> SiloAddress:
    if not isinstance(value, str):
        raise ValueError("silo must be a silo-id string")
    return SiloAddress.from_silo_id(value)


def _entry_to_dict(entry: DirectoryEntry) -> dict[str, Any]:
    return {
        "grain_id": _grain_id_to_dict(entry.grain_id),
        "silo": _silo_to_str(entry.silo),
        "activation_epoch": entry.activation_epoch,
    }


def _entry_from_dict(obj: Any) -> DirectoryEntry:
    if not isinstance(obj, dict):
        raise ValueError("entry must be an object")
    try:
        return DirectoryEntry(
            grain_id=_grain_id_from_dict(obj["grain_id"]),
            silo=_silo_from_str(obj["silo"]),
            activation_epoch=int(obj["activation_epoch"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"entry fields: {exc}") from exc


def encode_register(req: RegisterRequest) -> bytes:
    return _dump(
        {
            "grain_id": _grain_id_to_dict(req.grain_id),
            "silo": _silo_to_str(req.silo),
        }
    )


def decode_register(data: bytes) -> RegisterRequest:
    obj = _load(data)
    return RegisterRequest(
        grain_id=_grain_id_from_dict(obj["grain_id"]),
        silo=_silo_from_str(obj["silo"]),
    )


def encode_lookup(req: LookupRequest) -> bytes:
    return _dump({"grain_id": _grain_id_to_dict(req.grain_id)})


def decode_lookup(data: bytes) -> LookupRequest:
    obj = _load(data)
    return LookupRequest(grain_id=_grain_id_from_dict(obj["grain_id"]))


def encode_unregister(req: UnregisterRequest) -> bytes:
    return _dump(
        {
            "grain_id": _grain_id_to_dict(req.grain_id),
            "silo": _silo_to_str(req.silo),
        }
    )


def decode_unregister(data: bytes) -> UnregisterRequest:
    obj = _load(data)
    return UnregisterRequest(
        grain_id=_grain_id_from_dict(obj["grain_id"]),
        silo=_silo_from_str(obj["silo"]),
    )


def encode_resolve(req: ResolveRequest) -> bytes:
    return _dump(
        {
            "grain_id": _grain_id_to_dict(req.grain_id),
            "placement_class": req.placement_class,
            "caller": _silo_to_str(req.caller) if req.caller is not None else None,
        }
    )


def decode_resolve(data: bytes) -> ResolveRequest:
    obj = _load(data)
    caller_raw = obj.get("caller")
    return ResolveRequest(
        grain_id=_grain_id_from_dict(obj["grain_id"]),
        placement_class=str(obj["placement_class"]),
        caller=_silo_from_str(caller_raw) if caller_raw is not None else None,
    )


def encode_entry(entry: DirectoryEntry | None) -> bytes:
    if entry is None:
        return _dump({"entry": None})
    return _dump({"entry": _entry_to_dict(entry)})


def decode_entry(data: bytes) -> DirectoryEntry | None:
    obj = _load(data)
    raw = obj.get("entry")
    if raw is None:
        return None
    return _entry_from_dict(raw)


def encode_none() -> bytes:
    return _dump({"ok": True})


def decode_none(data: bytes) -> None:
    obj = _load(data)
    if not obj.get("ok", False):
        raise ValueError("response missing ok=true")


def encode_handoff(req: HandoffRequest) -> bytes:
    return _dump({"entry": _entry_to_dict(req.entry)})


def decode_handoff(data: bytes) -> HandoffRequest:
    obj = _load(data)
    return HandoffRequest(entry=_entry_from_dict(obj["entry"]))


def encode_deactivate(req: DeactivateRequest) -> bytes:
    return _dump(
        {
            "grain_id": _grain_id_to_dict(req.grain_id),
            "activation_epoch": req.activation_epoch,
        }
    )


def decode_deactivate(data: bytes) -> DeactivateRequest:
    obj = _load(data)
    return DeactivateRequest(
        grain_id=_grain_id_from_dict(obj["grain_id"]),
        activation_epoch=int(obj["activation_epoch"]),
    )


def placement_class_name(strategy: PlacementStrategy) -> str:
    return type(strategy).__name__


def encode_rebuild_query(req: RebuildQueryRequest) -> bytes:
    return _dump(
        {
            "arcs": [{"start": a.start, "end": a.end} for a in req.arcs],
        }
    )


def decode_rebuild_query(data: bytes) -> RebuildQueryRequest:
    obj = _load(data)
    raw_arcs = obj.get("arcs", [])
    if not isinstance(raw_arcs, list):
        raise ValueError("arcs must be a list")
    arcs = [Arc(start=int(a["start"]), end=int(a["end"])) for a in raw_arcs if isinstance(a, dict)]
    return RebuildQueryRequest(arcs=arcs)


def encode_rebuild_response(resp: RebuildQueryResponse) -> bytes:
    return _dump(
        {"entries": [_entry_to_dict(e) for e in resp.entries]},
    )


def decode_rebuild_response(data: bytes) -> RebuildQueryResponse:
    obj = _load(data)
    raw = obj.get("entries", [])
    if not isinstance(raw, list):
        raise ValueError("entries must be a list")
    return RebuildQueryResponse(entries=[_entry_from_dict(e) for e in raw])


def _dump(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _load(data: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid directory-rpc bytes: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("directory-rpc body must be a JSON object")
    return obj
