"""Probe definitions, schema validation and scan orchestration.

Response schemas follow https://docs.zksync.io/zksync-protocol/api/zks-rpc and
.../ethereum-rpc. ZKsync OS endpoints are documented as unstable, so:
  * `required` fields missing/mistyped -> FAIL; `documented` fields mistyped or absent
    -> WARN (docs and node disagree); `optional` fields mistyped -> WARN, absent ignored;
  * fields the docs do not mention are always ignored.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import __version__
from .client import RpcClient
from .discovery import CallPlan, Discovery, Observed, discover, is_method_missing
from .evidence import build_evidence, type_name
from .models import ProbeResult, Scan, Severity, Status, Summary, utc_now

# ---------------------------------------------------------------- field checks

_HEX = re.compile(r"^0x[0-9a-fA-F]+$")


def _is_uint(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v >= 0
    return isinstance(v, str) and bool(_HEX.match(v) or v.isdigit())


def _is_quantity(v: Any) -> bool:
    return isinstance(v, str) and bool(_HEX.match(v))


CHECKS: dict[str, Callable[[Any], bool]] = {
    "uint": _is_uint,
    "quantity": _is_quantity,
    "hash32": lambda v: isinstance(v, str) and bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", v)),
    "address": lambda v: isinstance(v, str) and bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", v)),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


@dataclass
class Validation:
    problems: list[str] = field(default_factory=list)  # -> FAIL
    warnings: list[str] = field(default_factory=list)  # -> WARN


def check_object(
    result: Any,
    required: dict[str, str],
    optional: dict[str, str] | None = None,
    documented: dict[str, str] | None = None,
) -> Validation:
    v = Validation()
    if not isinstance(result, dict):
        v.problems.append(f"expected object, got {type_name(result)}")
        return v
    for name, kind in required.items():
        if name not in result:
            v.problems.append(f"missing required field '{name}'")
        elif not CHECKS[kind](result[name]):
            v.problems.append(f"field '{name}' should be {kind}, got {type_name(result[name])}")
    for name, kind in (optional or {}).items():
        # null is legitimate for not-yet-committed batches; absence is tolerated.
        if result.get(name) is not None and not CHECKS[kind](result[name]):
            v.warnings.append(f"field '{name}' should be {kind}, got {type_name(result[name])}")
    for name, kind in (documented or {}).items():
        # Documented but not required: a real node may omit it. Report the docs/node gap as WARN.
        if name not in result:
            v.warnings.append(f"documented field '{name}' is absent")
        elif result[name] is not None and not CHECKS[kind](result[name]):
            v.warnings.append(f"field '{name}' should be {kind}, got {type_name(result[name])}")
    return v


def check_scalar(result: Any, kind: str) -> Validation:
    v = Validation()
    if not CHECKS[kind](result):
        v.problems.append(f"expected {kind}, got {type_name(result)}: {str(result)[:60]!r}")
    return v


# --------------------------------------------------------------------- probes


@dataclass(frozen=True)
class ProbeSpec:
    id: str
    name: str
    category: str  # "generic" | "zksync"
    method: str
    validate: Callable[[Any], Validation]
    params: Callable[[dict[str, Any]], list[Any] | None] = lambda ctx: []
    provides: Callable[[Any], dict[str, Any]] = lambda result: {}
    depends_on: tuple[str, ...] = ()
    required: bool = False  # missing method => FAIL (otherwise SKIP)
    os_documented: bool = False  # missing on a ZKsync OS node => WARN (otherwise SKIP)
    nullable: bool = False  # a null result is a WARN, not a schema FAIL


def _hex_to_int(value: Any) -> int | None:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


def _block_int(ctx: dict[str, Any]) -> list[Any] | None:
    n = _hex_to_int(ctx.get("block_number"))
    return [n] if n is not None else None


def _batch_int(ctx: dict[str, Any]) -> list[Any] | None:
    n = _hex_to_int(ctx.get("l1_batch_number"))
    return [n] if n is not None else None


def _validate_net_version(result: Any) -> Validation:
    v = check_scalar(result, "string")
    if not v.problems and not result.isdigit():
        v.warnings.append(f"net_version is not a decimal string: {result[:40]!r}")
    return v


def _validate_block_range(result: Any) -> Validation:
    v = Validation()
    if not isinstance(result, list) or len(result) != 2:
        v.problems.append(f"expected array of 2 quantities, got {type_name(result)}")
    elif not all(_is_quantity(x) for x in result):
        v.problems.append("range entries should be hex quantities")
    return v


def _validate_genesis(result: Any) -> Validation:
    """Structure the genesis is unusable without -> FAIL. Deployment differences from the docs -> WARN.

    `additional_storage` is documented as an array. Observed: an array on the public testnet
    (zksync-os/v0.24.0) but an object on local zksync-os v0.20.12 and v0.23.0. Both are usable, so
    an object is a WARN; the observed shape is kept in the evidence. `execution_version` is
    documented but absent from real responses (WARN).
    """
    v = check_object(
        result,
        {"initial_contracts": "array", "genesis_root": "hash32"},
        documented={"execution_version": "uint"},
    )
    if not isinstance(result, dict):
        return v
    if "additional_storage" not in result:
        v.warnings.append("documented field 'additional_storage' is absent")
    elif isinstance(result["additional_storage"], dict):
        v.warnings.append("field 'additional_storage' documented as array, observed object")
    elif not isinstance(result["additional_storage"], list):
        v.problems.append(f"field 'additional_storage' should be array, got {type_name(result['additional_storage'])}")
    return v


def _validate_bridgehub(result: Any) -> Validation:
    # Response shape not confirmed in the docs; accept a string and warn if not an address.
    v = check_scalar(result, "string")
    if not v.problems and not CHECKS["address"](result):
        v.warnings.append("value is not a 20-byte hex address")
    return v


PROBES: list[ProbeSpec] = [
    ProbeSpec("RPC-001", "chain ID", "generic", "eth_chainId", lambda r: check_scalar(r, "quantity"), required=True),
    ProbeSpec(
        "RPC-002", "block number", "generic", "eth_blockNumber", lambda r: check_scalar(r, "quantity"),
        provides=lambda r: {"block_number": r} if _is_quantity(r) else {}, required=True,
    ),
    ProbeSpec("RPC-003", "client version", "generic", "web3_clientVersion", lambda r: check_scalar(r, "string")),
    ProbeSpec("RPC-004", "net version", "generic", "net_version", _validate_net_version),
    ProbeSpec("ZKS-001", "genesis", "zksync", "zks_getGenesis", _validate_genesis, os_documented=True),
    ProbeSpec(
        "ZKS-002", "block metadata", "zksync", "zks_getBlockMetadataByNumber",
        lambda r: check_object(
            r, {"pubdata_price_per_byte": "uint", "native_price": "uint"}, documented={"execution_version": "uint"}
        ),
        params=_block_int, depends_on=("RPC-002",), os_documented=True,  # server wants a JSON integer, not hex
    ),
    ProbeSpec(
        "ZKS-003", "block details", "zksync", "zks_getBlockDetails",
        lambda r: check_object(
            r,
            {"number": "uint", "timestamp": "uint"},
            {
                "l1BatchNumber": "uint", "l1TxCount": "uint", "l2TxCount": "uint", "rootHash": "hash32",
                "status": "string", "l1GasPrice": "uint", "l2FairGasPrice": "uint",
                "baseSystemContractsHashes": "object", "operatorAddress": "address", "protocolVersion": "string",
            },
        ),
        params=_block_int, depends_on=("RPC-002",), nullable=True,
    ),
    ProbeSpec(
        "ZKS-004", "L1 batch number", "zksync", "zks_L1BatchNumber", lambda r: check_scalar(r, "quantity"),
        provides=lambda r: {"l1_batch_number": r} if _is_quantity(r) else {},
    ),
    ProbeSpec(
        "ZKS-005", "L1 batch block range", "zksync", "zks_getL1BatchBlockRange", _validate_block_range,
        params=_batch_int, depends_on=("ZKS-004",), nullable=True,
    ),
    ProbeSpec(
        "ZKS-006", "L1 batch details", "zksync", "zks_getL1BatchDetails",
        lambda r: check_object(
            r,
            {"number": "uint"},
            {
                "timestamp": "uint", "l1TxCount": "uint", "l2TxCount": "uint", "rootHash": "hash32",
                "status": "string", "commitTxHash": "hash32", "proveTxHash": "hash32", "executeTxHash": "hash32",
                "l1GasPrice": "uint", "l2FairGasPrice": "uint", "baseSystemContractsHashes": "object",
            },
        ),
        params=_batch_int, depends_on=("ZKS-004",), nullable=True,
    ),
    ProbeSpec("ZKS-007", "bridgehub contract", "zksync", "zks_getBridgehubContract", _validate_bridgehub),
]


def _severity(status: Status) -> Severity:
    return {
        Status.PASS: Severity.INFO,
        Status.SKIP: Severity.INFO,
        Status.WARN: Severity.WARNING,
        Status.FAIL: Severity.CRITICAL,
        Status.ERROR: Severity.CRITICAL,
    }[status]


def evaluate(spec: ProbeSpec, o: Observed, environment_kind: str, require: frozenset[str]) -> ProbeResult:
    def result(status: Status, summary: str, details: list[str] | None = None) -> ProbeResult:
        evidence = None
        if not o.skipped_reason:
            evidence = build_evidence(spec.id, spec.method, o.params, o.observation, list(spec.depends_on), o.error)
        return ProbeResult(
            probe_id=spec.id, name=spec.name, category=spec.category, status=status,
            severity=_severity(status), summary=summary, details=details or [], evidence=evidence,
        )

    if o.skipped_reason:
        return result(Status.SKIP, f"skipped: {o.skipped_reason}")
    if o.observation is None:
        return result(Status.ERROR, "no usable response", [o.error or "unknown error"])

    obs = o.observation
    required = spec.required or spec.method in require
    if is_method_missing(obs.rpc_error):
        if required:
            return result(Status.FAIL, "required method unsupported", [str(obs.rpc_error)])
        if spec.os_documented and environment_kind == "zksync-os":
            return result(Status.WARN, "unsupported (documented for ZKsync OS; interface may be unstable)")
        return result(Status.SKIP, "unsupported")
    if obs.rpc_error:
        status = Status.FAIL if required else Status.WARN
        return result(status, "RPC error on a supported method", [str(obs.rpc_error)])
    if obs.result is None and spec.nullable:
        return result(Status.WARN, "returned null for the requested value")

    check = spec.validate(obs.result)
    if check.problems:
        return result(Status.FAIL, "response does not match documented schema", check.problems + check.warnings)
    if check.warnings:
        return result(Status.WARN, "response matches schema with deviations", check.warnings)
    return result(Status.PASS, "ok")


def run_scan(client: RpcClient, require: frozenset[str] = frozenset()) -> Scan:
    """Discovery, then probe evaluation. Raises DiscoveryAborted if unreachable."""
    plans = [CallPlan(s.id, s.method, s.depends_on, s.params, s.provides) for s in PROBES]
    found: Discovery = discover(client, plans)
    results = [
        evaluate(spec, found.observed[spec.id], found.environment.detected_execution_environment, require)
        for spec in PROBES
    ]
    counts = {s: sum(1 for r in results if r.status == s) for s in Status}
    return Scan(
        tool_version=__version__,
        scan_id=uuid.uuid4().hex,
        timestamp=utc_now(),
        environment=found.environment,
        capabilities=found.capabilities,
        results=results,
        summary=Summary(
            passed=counts[Status.PASS], warn=counts[Status.WARN], fail=counts[Status.FAIL],
            skip=counts[Status.SKIP], error=counts[Status.ERROR],
        ),
    )
