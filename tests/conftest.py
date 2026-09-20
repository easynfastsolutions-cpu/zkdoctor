"""Fake JSON-RPC chain used by unit tests (in-process transport) and CLI tests (real local HTTP)."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RpcFail:
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


class FakeChain:
    """methods maps name -> result, or callable(params) -> result / RpcFail."""

    def __init__(self, methods: dict[str, Any]):
        self.methods = methods
        self.calls: list[tuple[str, list]] = []

    def respond(self, payload: dict) -> tuple[int, dict]:
        method, params = payload["method"], payload.get("params", [])
        self.calls.append((method, params))
        base = {"jsonrpc": "2.0", "id": payload.get("id")}
        if method not in self.methods:
            return 200, {**base, "error": load("rpc_method_missing.json")["error"]}
        value = self.methods[method]
        if callable(value):
            value = value(params)
        if isinstance(value, RpcFail):
            return 200, {**base, "error": {"code": value.code, "message": value.message}}
        return 200, {**base, "result": value}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            status, body = self.respond(json.loads(request.content))
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handler)

    @contextmanager
    def serve(self):
        chain = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, body = chain.respond(payload)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()


def generic_methods(block: int = 1000, chain_id: int = 300, client: str = "reth/v1.0") -> dict[str, Any]:
    return {
        "eth_chainId": hex(chain_id),
        "eth_blockNumber": hex(block),
        "web3_clientVersion": client,
        "net_version": str(chain_id),
    }


def _metadata_needing_int(result: Any):
    """Like the real server: the block parameter must be a JSON integer (u64), hex strings are rejected."""

    def handler(params: list) -> Any:
        if not params or not isinstance(params[0], int) or isinstance(params[0], bool):
            return RpcFail(-32602, f"Invalid params: invalid type {params[:1]!r}, expected u64")
        return result

    return handler


def os_methods(block: int = 1000, **kw: Any) -> dict[str, Any]:
    """Docs-conformant ZKsync OS style: OS-only methods plus bridgehub; no L1-batch methods."""
    kw.setdefault("client", "zksync-os/0.4.0")
    return {
        **generic_methods(block, **kw),
        "zks_getGenesis": load("genesis.json"),
        "zks_getBlockMetadataByNumber": _metadata_needing_int(load("block_metadata.json")),
        "zks_getBridgehubContract": "0x" + "ab" * 20,
    }


def real_testnet_methods(block: int = 504553) -> dict[str, Any]:
    """Mirrors what https://zksync-os-testnet-alpha.zksync.dev/ (zksync-os/v0.24.0) actually returned on
    2026-09-20: hex net_version, no execution_version anywhere, extra undocumented genesis fields."""
    return {
        "eth_chainId": "0x7a6b31",
        "eth_blockNumber": hex(block),
        "web3_clientVersion": "zksync-os/v0.24.0",
        "net_version": "0x7a6b31",
        "zks_getGenesis": load("genesis_real_shape.json"),
        "zks_getBlockMetadataByNumber": _metadata_needing_int(load("block_metadata_real.json")),
        "zks_getBridgehubContract": "0xc4fd2580c3487bba18d63f50301020132342fdbd",
    }


def era_methods(block: int = 1000, batch: int = 40, **kw: Any) -> dict[str, Any]:
    kw.setdefault("client", "ZKsync/v2.0")
    return {
        **generic_methods(block, **kw),
        "zks_getBlockDetails": lambda p: {
            "number": p[0], "l1BatchNumber": batch, "timestamp": 1_700_000_000 + p[0], "l1TxCount": 0,
            "l2TxCount": 3, "rootHash": "0x" + "22" * 32, "status": "verified", "commitTxHash": None,
            "operatorAddress": "0x" + "cd" * 20, "protocolVersion": "Version27",
        },
        "zks_L1BatchNumber": hex(batch),
        "zks_getL1BatchBlockRange": lambda p: [hex(p[0] * 10), hex(p[0] * 10 + 9)],
        "zks_getL1BatchDetails": lambda p: {
            "number": p[0], "timestamp": 1_700_000_000, "l1TxCount": 0, "l2TxCount": 30,
            "rootHash": "0x" + "33" * 32, "status": "verified", "commitTxHash": None,
        },
    }
