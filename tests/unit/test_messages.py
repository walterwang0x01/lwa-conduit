"""单元测试：acp.messages 的纯函数逻辑。"""

from __future__ import annotations

import pytest

from kiro_conduit.acp.messages import (
    ACP_PROTOCOL_VERSION,
    AcpError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
)


class TestJsonRpcRequest:
    def test_to_wire_with_id(self) -> None:
        req = JsonRpcRequest(method="ping", params={"a": 1}, id=42)
        wire = req.to_wire()
        assert wire == {
            "jsonrpc": "2.0",
            "method": "ping",
            "params": {"a": 1},
            "id": 42,
        }

    def test_to_wire_without_id_omits_id(self) -> None:
        """notification 形式：id=None 时不应出现在线上格式里。"""
        req = JsonRpcRequest(method="notify", params={}, id=None)
        wire = req.to_wire()
        assert "id" not in wire
        assert wire["method"] == "notify"

    def test_default_params_is_empty_dict(self) -> None:
        req = JsonRpcRequest(method="ping")
        assert req.params == {}


class TestJsonRpcResponse:
    def test_from_wire_with_result(self) -> None:
        resp = JsonRpcResponse.from_wire({"id": 1, "result": {"ok": True}})
        assert resp.id == 1
        assert resp.result == {"ok": True}
        assert resp.error is None
        assert not resp.is_error

    def test_from_wire_with_error(self) -> None:
        resp = JsonRpcResponse.from_wire(
            {"id": 1, "error": {"code": -1, "message": "boom"}}
        )
        assert resp.is_error
        assert resp.error == {"code": -1, "message": "boom"}


class TestJsonRpcNotification:
    def test_from_wire(self) -> None:
        notif = JsonRpcNotification.from_wire(
            {"method": "session/update", "params": {"sessionId": "s1"}}
        )
        assert notif.method == "session/update"
        assert notif.params == {"sessionId": "s1"}

    def test_from_wire_missing_params_defaults_empty(self) -> None:
        notif = JsonRpcNotification.from_wire({"method": "evt"})
        assert notif.params == {}


class TestAcpError:
    def test_from_wire(self) -> None:
        err = AcpError.from_wire({"code": -32601, "message": "Method not found"})
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data is None

    def test_from_wire_with_data(self) -> None:
        err = AcpError.from_wire(
            {"code": 1, "message": "x", "data": {"detail": "y"}}
        )
        assert err.data == {"detail": "y"}

    def test_str_includes_code_and_message(self) -> None:
        err = AcpError(code=-1, message="oops")
        assert "oops" in str(err)
        assert "-1" in str(err)

    def test_is_exception(self) -> None:
        with pytest.raises(AcpError):
            raise AcpError(code=1, message="bang")


def test_protocol_version_is_int() -> None:
    """实测得到 Kiro 的协议版本是整数 1，不是日期串。"""
    assert ACP_PROTOCOL_VERSION == 1
    assert isinstance(ACP_PROTOCOL_VERSION, int)
