"""Discovery: run each probe's read-only call once, derive capabilities and identity.

Probes (probes.py) then evaluate the recorded observations; they make no new calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import RpcClient, RpcError, RpcProtocolError, RpcTransportError
from .evidence import sha256_hex
from .models import Capability, Environment, RpcObservation

# Documented (docs.zksync.io zks-rpc) as "ZKsync OS only".
OS_ONLY_METHODS = ("zks_getGenesis", "zks_getBlockMetadataByNumber")
# Documented as available on EraVM chains.
ERAVM_METHODS = ("zks_getBlockDetails", "zks_L1BatchNumber", "zks_getL1BatchBlockRange", "zks_getL1BatchDetails")

_MISSING_TEXT = re.compile(r"method.*(not found|not supported|does not exist|not available)|unknown method", re.I)


class DiscoveryAborted(Exception):
    """The endpoint is unreachable, so no meaningful scan is possible."""


def is_method_missing(rpc_error: dict[str, Any] | None) -> bool:
    if not rpc_error:
        return False
    return rpc_error.get("code") == -32601 or bool(_MISSING_TEXT.search(str(rpc_error.get("message", ""))))


@dataclass(frozen=True)
class CallPlan:
    """What discovery needs to know about a probe. Defined by probes.ProbeSpec."""

    id: str
    method: str
    depends_on: tuple[str, ...]
    params: Callable[[dict[str, Any]], list[Any] | None]  # ctx -> params, None if a dependency is missing
    provides: Callable[[Any], dict[str, Any]]  # result -> ctx additions


@dataclass
class Observed:
    probe_id: str
    method: str
    params: list[Any] = field(default_factory=list)
    observation: RpcObservation | None = None
    error: str | None = None  # transport/protocol failure
    skipped_reason: str | None = None  # dependency missing


@dataclass
class Discovery:
    observed: dict[str, Observed]
    capabilities: dict[str, Capability]
    environment: Environment


def _capability(o: Observed) -> Capability:
    if o.skipped_reason:
        return Capability(method=o.method, supported=None, probe_id=o.probe_id, reason=o.skipped_reason)
    if o.observation is None:
        return Capability(method=o.method, supported=None, probe_id=o.probe_id, reason=o.error)
    if is_method_missing(o.observation.rpc_error):
        return Capability(method=o.method, supported=False, probe_id=o.probe_id, reason="method not found")
    return Capability(method=o.method, supported=True, probe_id=o.probe_id)


def _result(observed: dict[str, Observed], method: str) -> Any:
    for o in observed.values():
        if o.method == method and o.observation and not o.observation.rpc_error:
            return o.observation.result
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def _identify(observed: dict[str, Observed], caps: dict[str, Capability], endpoint: str) -> Environment:
    client_version = _result(observed, "web3_clientVersion")
    client_version = client_version if isinstance(client_version, str) else None
    net_version = _result(observed, "net_version")
    genesis = _result(observed, "zks_getGenesis")
    metadata = _result(observed, "zks_getBlockMetadataByNumber")
    details = _result(observed, "zks_getBlockDetails")
    genesis = genesis if isinstance(genesis, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    details = details if isinstance(details, dict) else {}

    def supported(method: str) -> bool:
        return caps.get(method, Capability(method=method, supported=None, probe_id="")).supported is True

    basis: list[str] = []
    if any(supported(m) for m in OS_ONLY_METHODS):
        kind = "zksync-os"
        basis += [f"{m} supported (documented as ZKsync OS only)" for m in OS_ONLY_METHODS if supported(m)]
    elif client_version and "zksync-os" in client_version.lower():
        kind = "zksync-os"
        basis.append("web3_clientVersion mentions zksync-os")
    elif any(supported(m) for m in ERAVM_METHODS):
        kind = "zksync-eravm"
        basis += [f"{m} supported (documented for EraVM chains)" for m in ERAVM_METHODS if supported(m)]
    elif supported("zks_getBridgehubContract"):
        kind = "zksync-unknown"
        basis.append("only zks_getBridgehubContract supported")
    else:
        kind = "generic-evm"
        basis.append("no zks_ methods supported")

    protocol: dict[str, Any] = {}
    if genesis.get("genesis_root") is not None:
        protocol["genesis_root"] = genesis["genesis_root"]
    if details.get("protocolVersion") is not None:
        protocol["protocol_version"] = details["protocolVersion"]

    return Environment(
        endpoint=endpoint,
        endpoint_fingerprint=sha256_hex(endpoint)[:16],
        chain_id=_to_int(_result(observed, "eth_chainId")),
        net_version=net_version if isinstance(net_version, str) else None,
        client_version=client_version,
        detected_execution_environment=kind,
        detection_basis=basis,
        execution_version=_to_int(genesis.get("execution_version", metadata.get("execution_version"))),
        protocol_metadata=protocol,
    )


def discover(client: RpcClient, plans: list[CallPlan]) -> Discovery:
    """Run plans in order (dependencies first). Raises DiscoveryAborted when the very
    first call cannot reach the endpoint, so a dead URL is not reported as a failing chain."""
    ctx: dict[str, Any] = {}
    observed: dict[str, Observed] = {}
    for index, plan in enumerate(plans):
        params = plan.params(ctx)
        if params is None:
            missing = ", ".join(plan.depends_on) or "dependency"
            observed[plan.id] = Observed(plan.id, plan.method, skipped_reason=f"needs a value from {missing}")
            continue
        entry = Observed(plan.id, plan.method, params=params)
        try:
            entry.observation = client.call(plan.method, params)
        except RpcTransportError as exc:
            if index == 0:
                raise DiscoveryAborted(str(exc)) from None
            entry.error = str(exc)
        except RpcProtocolError as exc:
            entry.error = str(exc)
        except RpcError as exc:  # pragma: no cover - future subclasses
            entry.error = str(exc)
        observed[plan.id] = entry
        if entry.observation and not entry.observation.rpc_error:
            ctx.update(plan.provides(entry.observation.result))

    caps = {o.method: _capability(o) for o in observed.values()}
    return Discovery(observed, caps, _identify(observed, caps, client.redacted_url))
