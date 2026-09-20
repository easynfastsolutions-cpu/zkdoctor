import json

import httpx
import pytest

from zkdoctor.client import (
    RpcClient,
    RpcProtocolError,
    RpcTransportError,
    redact_url,
)


def client_for(handler, url="http://node.test:3050"):
    return RpcClient(url, transport=httpx.MockTransport(handler))


def test_successful_call_records_request_and_latency():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x10"})

    obs = client_for(handler).call("eth_chainId")
    assert seen["body"]["method"] == "eth_chainId"
    assert seen["body"]["params"] == []
    assert seen["body"]["id"] == 1
    assert obs.result == "0x10"
    assert obs.http_status == 200
    assert obs.rpc_error is None
    assert obs.latency_ms >= 0
    assert obs.timestamp.endswith("Z")


def test_request_ids_increment():
    ids = []

    def handler(request):
        ids.append(json.loads(request.content)["id"])
        return httpx.Response(200, json={"result": None})

    c = client_for(handler)
    c.call("eth_chainId")
    c.call("eth_blockNumber")
    assert ids == [1, 2]


def test_jsonrpc_error_is_an_observation_not_an_exception():
    def handler(request):
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
        )

    obs = client_for(handler).call("zks_getGenesis")
    assert obs.rpc_error == {"code": -32601, "message": "Method not found"}
    assert obs.result is None


def test_http_error_with_jsonrpc_body_is_an_observation():
    def handler(request):
        return httpx.Response(500, json={"error": {"code": -32000, "message": "boom"}})

    obs = client_for(handler).call("eth_chainId")
    assert obs.http_status == 500
    assert obs.rpc_error["code"] == -32000


def test_http_error_without_jsonrpc_body_raises_protocol_error():
    def handler(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(RpcProtocolError) as info:
        client_for(handler).call("eth_chainId")
    assert info.value.http_status == 502


def test_malformed_json_raises_protocol_error():
    def handler(request):
        return httpx.Response(200, content=b"{not json")

    with pytest.raises(RpcProtocolError):
        client_for(handler).call("eth_chainId")


def test_json_that_is_not_a_rpc_response_raises_protocol_error():
    def handler(request):
        return httpx.Response(200, json=["nope"])

    with pytest.raises(RpcProtocolError):
        client_for(handler).call("eth_chainId")


def test_timeout_raises_transport_error():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(RpcTransportError):
        client_for(handler).call("eth_chainId")


def test_mutating_methods_are_refused_without_a_request():
    def handler(request):
        raise AssertionError("no request should be sent")

    c = client_for(handler)
    for method in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_signTransaction", "personal_sign"):
        with pytest.raises(ValueError):
            c.call(method)


def test_invalid_url_rejected():
    with pytest.raises(ValueError):
        RpcClient("not a url")
    with pytest.raises(ValueError):
        RpcClient("ftp://host")


SECRET_URL = "https://alice:hunter2@node.test:8545/v2/abcdefghijklmnopqrstuvwxyz123456?apikey=SECRETVAL&x=1"


def test_redact_url_removes_credentials_tokens_and_keys():
    red = redact_url(SECRET_URL)
    for secret in ("alice", "hunter2", "abcdefghijklmnopqrstuvwxyz123456", "SECRETVAL"):
        assert secret not in red
    assert "node.test:8545" in red
    assert "x=1" in red


def test_transport_error_message_never_contains_secrets():
    def handler(request):
        raise httpx.ConnectError(f"failed to reach {SECRET_URL} as hunter2", request=request)

    with pytest.raises(RpcTransportError) as info:
        client_for(handler, url=SECRET_URL).call("eth_chainId")
    message = str(info.value)
    for secret in ("alice", "hunter2", "abcdefghijklmnopqrstuvwxyz123456", "SECRETVAL"):
        assert secret not in message
