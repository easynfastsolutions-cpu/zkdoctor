"""Evidence capture (hashes, bounded summaries, shapes) and scan persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import SCHEMA_VERSION, Evidence, RpcObservation, Scan, utc_now

MAX_DEPTH = 4
MAX_EXCERPT_CHARS = 120
MAX_EXCERPT_KEYS = 20


class ScanFileError(Exception):
    """A scan file could not be read, parsed or is an unsupported schema."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_hash(method: str, params: list[Any]) -> str:
    return sha256_hex(canonical_json({"method": method, "params": params}))


def response_hash(result: Any, rpc_error: dict[str, Any] | None) -> str:
    return sha256_hex(canonical_json({"result": result, "error": rpc_error}))


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def shape_of(value: Any, depth: int = 0) -> Any:
    """Structure of a JSON value with no data: scalar type names, dicts of shapes,
    and one-element lists holding the shape of the first item."""
    if depth >= MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        return {k: shape_of(value[k], depth + 1) for k in sorted(value)}
    if isinstance(value, list):
        return [shape_of(value[0], depth + 1)] if value else []
    return type_name(value)


def _clip(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def summarize_result(result: Any) -> dict[str, Any]:
    """type + shape + a bounded excerpt. Never the whole response."""
    summary: dict[str, Any] = {
        "type": type_name(result),
        "shape": shape_of(result),
        "size_bytes": len(canonical_json(result).encode("utf-8")),
    }
    if isinstance(result, dict):
        scalars = {k: v for k, v in result.items() if not isinstance(v, (dict, list))}
        summary["excerpt"] = {
            k: _clip(v, 80) if isinstance(v, str) else v
            for k, v in list(scalars.items())[:MAX_EXCERPT_KEYS]
        }
        summary["keys"] = sorted(result)[:MAX_EXCERPT_KEYS * 2]
    elif isinstance(result, list):
        summary["length"] = len(result)
        summary["excerpt"] = [_clip(json.dumps(x), 80) for x in result[:3]]
    else:
        summary["excerpt"] = _clip(json.dumps(result))
    return summary


def build_evidence(
    probe_id: str,
    method: str,
    params: list[Any],
    observation: RpcObservation | None,
    depends_on: list[str],
    error: str | None = None,
) -> Evidence:
    if observation is None:
        return Evidence(
            timestamp=utc_now(),
            probe_id=probe_id,
            method=method,
            params=params,
            depends_on=depends_on,
            request_hash=request_hash(method, params),
            error=error,
        )
    return Evidence(
        timestamp=observation.timestamp,
        probe_id=probe_id,
        method=method,
        params=params,
        depends_on=depends_on,
        request_hash=request_hash(method, params),
        response_hash=response_hash(observation.result, observation.rpc_error),
        http_status=observation.http_status,
        rpc_error=observation.rpc_error,
        result_summary={} if observation.rpc_error else summarize_result(observation.result),
        latency_ms=observation.latency_ms,
    )


def save_scan(scan: Scan, path: Path) -> None:
    path.write_text(scan.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8")


def load_scan(path: Path) -> Scan:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanFileError(f"{path}: cannot read scan file ({exc})") from None
    if not isinstance(data, dict):
        raise ScanFileError(f"{path}: not a zkdoctor scan")
    version = str(data.get("schema_version", ""))
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ScanFileError(f"{path}: unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")
    try:
        return Scan.model_validate(data)
    except ValidationError as exc:
        raise ScanFileError(f"{path}: invalid scan file ({exc.error_count()} validation errors)") from None
