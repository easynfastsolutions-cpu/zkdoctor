"""Minimal read-only JSON-RPC client."""

from __future__ import annotations

import itertools
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .models import RpcObservation, utc_now

# The client is for observation only. Refuse anything that could mutate state or
# touches keys, even if a future probe asks for it by mistake.
_FORBIDDEN = re.compile(r"^(eth_(send|sign)|personal_|admin_|miner_|debug_|engine_)", re.I)
_SENSITIVE_QUERY = re.compile(r"(key|token|secret|auth|pass|sig)", re.I)
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]{24,}$")


class RpcError(Exception):
    """Base class for client failures."""


class RpcTransportError(RpcError):
    """No HTTP response: timeout, connection refused, DNS, TLS."""


class RpcProtocolError(RpcError):
    """An HTTP response arrived but was not a usable JSON-RPC response."""

    def __init__(self, message: str, http_status: int | None, latency_ms: float):
        super().__init__(message)
        self.http_status = http_status
        self.latency_ms = latency_ms


def redact_url(url: str) -> str:
    """Drop userinfo, blank sensitive query values and key-like path segments."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    path = "/".join("***" if _TOKEN_SEGMENT.match(s) else s for s in parts.path.split("/"))
    query = urlencode(
        [(k, "***" if _SENSITIVE_QUERY.search(k) else v) for k, v in parse_qsl(parts.query)],
        safe="*",
    )
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _secrets_in(url: str) -> list[str]:
    parts = urlsplit(url)
    found = [parts.username, parts.password]
    found += [v for k, v in parse_qsl(parts.query) if _SENSITIVE_QUERY.search(k)]
    found += [s for s in parts.path.split("/") if _TOKEN_SEGMENT.match(s)]
    return [s for s in found if s]


def scrub(text: str, url: str) -> str:
    """Remove the endpoint and any secret parts of it from an error message."""
    text = text.replace(url, redact_url(url))
    for secret in _secrets_in(url):
        text = text.replace(secret, "***")
    return text


class RpcClient:
    def __init__(
        self,
        url: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("RPC URL must be http(s)://host[:port][/path]")
        self._url = url
        self.redacted_url = redact_url(url)
        self._ids = itertools.count(1)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, params: list[Any] | None = None) -> RpcObservation:
        """One JSON-RPC call. Returns an observation for any well-formed JSON-RPC
        reply (including error replies); raises RpcTransportError / RpcProtocolError
        otherwise."""
        if _FORBIDDEN.match(method):
            raise ValueError(f"refusing non-read-only method: {method}")
        params = list(params or [])
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        stamp = utc_now()
        started = time.perf_counter()
        try:
            response = self._http.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise RpcTransportError(
                scrub(f"{type(exc).__name__}: {exc}", self._url)
            ) from None
        latency = round((time.perf_counter() - started) * 1000, 2)

        try:
            body = response.json()
        except ValueError:
            raise RpcProtocolError(
                f"HTTP {response.status_code}: response is not JSON", response.status_code, latency
            ) from None
        if not isinstance(body, dict) or ("result" not in body and "error" not in body):
            raise RpcProtocolError(
                f"HTTP {response.status_code}: not a JSON-RPC response object",
                response.status_code,
                latency,
            )

        error = body.get("error")
        if error is not None and not isinstance(error, dict):
            error = {"message": str(error)}
        return RpcObservation(
            method=method,
            params=params,
            timestamp=stamp,
            http_status=response.status_code,
            rpc_error=error,
            result=body.get("result"),
            latency_ms=latency,
        )
