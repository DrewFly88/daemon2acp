"""端到端测试 — 验证 HTTP 端点和 ACP JSON-RPC 端点

运行方式：
    D:\QwenPaw\python.exe tests/test_endpoints.py
"""

import asyncio
import io
import json
import sys
import os

# Windows 控制台 GBK 编码兼容 — 切换 stdout 为 UTF-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 添加项目根到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import web

# 直接 import 顶层模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util


def _load_server_module():
    spec = importlib.util.spec_from_file_location(
        "server", os.path.join(os.path.dirname(__file__), "..", "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server_mod = _load_server_module()


class TestEndpoints(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return server_mod.create_app()

    @unittest_run_loop
    async def test_health(self):
        resp = await self.client.request("GET", "/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "daemon2acp"
        print("[OK] health check passed")

    @unittest_run_loop
    async def test_acp_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1"},
        }
        resp = await self.client.request(
            "POST", "/acp", json=req,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        assert data["result"]["protocolVersion"] == "1"
        assert "agentInfo" in data["result"]
        assert "agentCapabilities" in data["result"]
        print("[OK] ACP initialize passed")

    @unittest_run_loop
    async def test_session_lifecycle(self):
        # new
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "id": 2, "method": "session/new"},
        )
        data = await resp.json()
        session_id = data["result"]["session"]["id"]
        assert session_id, "session id should not be empty"
        print(f"[OK] session/new passed (id={session_id[:8]})")

        # list
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "id": 3, "method": "session/list"},
        )
        data = await resp.json()
        assert len(data["result"]["sessions"]) >= 1
        print("[OK] session/list passed")

        # load
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "id": 4, "method": "session/load",
                  "params": {"sessionId": session_id}},
        )
        data = await resp.json()
        assert data["result"]["session"]["id"] == session_id
        print("[OK] session/load passed")

        # delete
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "id": 5, "method": "session/delete",
                  "params": {"sessionId": session_id}},
        )
        data = await resp.json()
        assert data["result"]["deleted"] is True
        print("[OK] session/delete passed")

    @unittest_run_loop
    async def test_rest_chat_sse(self):
        # 先创建会话
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "id": 10, "method": "session/new"},
        )
        sid = (await resp.json())["result"]["session"]["id"]

        # 发送 chat，读取 SSE 流
        resp = await self.client.request(
            "POST", "/chat",
            json={"sessionId": sid, "message": "hello world"},
        )
        assert resp.status == 200
        body = await resp.text()
        assert "event: text" in body
        assert "event: turn_end" in body
        print("[OK] REST /chat SSE passed")

    @unittest_run_loop
    async def test_acp_method_not_found(self):
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "id": 99, "method": "unknown/method"},
        )
        data = await resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601
        print("[OK] method not found error passed")

    @unittest_run_loop
    async def test_notification_no_response(self):
        # 通知无 id，应返回 204
        resp = await self.client.request(
            "POST", "/acp",
            json={"jsonrpc": "2.0", "method": "session/cancel",
                  "params": {"sessionId": "nonexistent"}},
        )
        assert resp.status == 204
        print("[OK] notification (no response) passed")


if __name__ == "__main__":
    import unittest
    unittest.main()
