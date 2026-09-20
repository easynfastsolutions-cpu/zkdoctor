"""Data models shared by every stage. The scan JSON is these models, serialised."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.1"


def utc_now() -> str:
    """UTC ISO-8601 timestamp, millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    ERROR = "ERROR"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Change(StrEnum):
    UNCHANGED = "UNCHANGED"
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    CHANGED = "CHANGED"


class Impact(StrEnum):
    BREAKING = "BREAKING"
    WARNING = "WARNING"
    NONE = "NONE"


class RpcObservation(BaseModel):
    """What one JSON-RPC call returned. Enough to reproduce the call."""

    method: str
    params: list[Any] = Field(default_factory=list)
    timestamp: str
    http_status: int | None = None
    rpc_error: dict[str, Any] | None = None
    result: Any = None
    latency_ms: float


class Evidence(BaseModel):
    """Bounded, hash-backed record of one probe's RPC call (never the raw response)."""

    timestamp: str
    probe_id: str
    method: str
    params: list[Any]
    depends_on: list[str] = Field(default_factory=list)
    request_hash: str
    response_hash: str | None = None
    http_status: int | None = None
    rpc_error: dict[str, Any] | None = None
    error: str | None = None  # transport/protocol failure, no RPC response to hash
    result_summary: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None


class ProbeResult(BaseModel):
    probe_id: str
    name: str
    category: str
    status: Status
    severity: Severity
    summary: str
    details: list[str] = Field(default_factory=list)
    evidence: Evidence | None = None


class Capability(BaseModel):
    method: str
    supported: bool | None  # None = could not be determined
    probe_id: str
    reason: str | None = None


class Environment(BaseModel):
    endpoint: str  # credentials redacted
    endpoint_fingerprint: str
    chain_id: int | None = None
    net_version: str | None = None
    client_version: str | None = None
    detected_execution_environment: str = "unknown"
    detection_basis: list[str] = Field(default_factory=list)
    execution_version: int | None = None
    protocol_metadata: dict[str, Any] = Field(default_factory=dict)


class Summary(BaseModel):
    passed: int = Field(0, serialization_alias="pass", validation_alias="pass")
    warn: int = 0
    fail: int = 0
    skip: int = 0
    error: int = 0

    model_config = {"populate_by_name": True}


class Scan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tool_version: str
    scan_id: str
    timestamp: str
    environment: Environment
    capabilities: dict[str, Capability]
    results: list[ProbeResult]
    summary: Summary


class Difference(BaseModel):
    subject: str  # "environment", "capability", or a probe id
    path: str | None = None
    change: Change
    impact: Impact
    message: str
    baseline: Any = None
    target: Any = None


class ComparisonSide(BaseModel):
    scan_id: str
    timestamp: str
    endpoint: str
    tool_version: str


class Comparison(BaseModel):
    schema_version: str = SCHEMA_VERSION
    baseline: ComparisonSide
    target: ComparisonSide
    differences: list[Difference]
    unchanged_probes: list[str]
    summary: dict[str, int]
