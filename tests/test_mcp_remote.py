import asyncio
import os
import unittest
from contextvars import ContextVar
from pathlib import Path

from starlette.testclient import TestClient

from adapters.mcp import remote, server
try:
    from tests.test_mcp_adapter import ControllerDouble
except ModuleNotFoundError:
    from test_mcp_adapter import ControllerDouble


class CapturingControllerDouble(ControllerDouble):
    def __init__(self, terminal=None):
        super().__init__(terminal)
        self.captured_tokens = []

    def __call__(self, method, path, body):
        self.captured_tokens.append(server.current_token.get())
        return super().__call__(method, path, body)


class RemoteMcpUnitTests(unittest.TestCase):
    def test_remote_config_validation(self):
        # Missing controller URL
        with self.assertRaisesRegex(server.ConfigurationError, "RELAYME_URL"):
            remote.RemoteConfig.from_env({})

        # Valid env parsing
        cfg = remote.RemoteConfig.from_env({
            "RELAYME_URL": "http://controller.example.invalid:8765",
            "RELAYME_TOKEN": "default-tok",
            "RELAYME_MCP_HOST": "127.0.0.1",
            "RELAYME_MCP_PORT": "9000",
            "RELAYME_WAIT_SECONDS": "45",
            "RELAYME_MCP_TRANSPORT": "streamable-http",
        })
        self.assertEqual(cfg.controller_url, "http://controller.example.invalid:8765")
        self.assertEqual(cfg.default_token, "default-tok")
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.wait_seconds, 45)
        self.assertEqual(cfg.transport, "streamable-http")

        # Invalid port
        with self.assertRaisesRegex(server.ConfigurationError, "integer"):
            remote.RemoteConfig.from_env({
                "RELAYME_URL": "http://controller.example.invalid",
                "RELAYME_MCP_PORT": "invalid",
            })

        # Non-loopback without TLS or allow_http_insecure is rejected
        insecure_cfg = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            host="0.0.0.0",
            port=8000,
        )
        with self.assertRaisesRegex(server.ConfigurationError, "Non-loopback binding"):
            remote.validate_remote_config(insecure_cfg)

        # Non-loopback with allow_http_insecure is accepted
        allowed_insecure = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            host="0.0.0.0",
            port=8000,
            allow_http_insecure=True,
        )
        remote.validate_remote_config(allowed_insecure)

        # Incomplete TLS (cert without key) is rejected
        incomplete_tls = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            host="127.0.0.1",
            tls_cert="/path/to/cert.pem",
        )
        with self.assertRaisesRegex(server.ConfigurationError, "Both --tls-cert and --tls-key"):
            remote.validate_remote_config(incomplete_tls)

    def test_health_and_ping_endpoints_unauthenticated(self):
        cfg = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            transport="streamable-http",
        )
        app = remote.create_remote_app(cfg)
        with TestClient(app, base_url="http://localhost") as client:
            res = client.get("/health")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"status": "ok", "service": "relayme-mcp-remote"})

            res_ping = client.get("/ping")
            self.assertEqual(res_ping.status_code, 200)
            self.assertEqual(res_ping.json(), {"status": "ok"})

    def test_unauthenticated_request_rejected_without_default_token(self):
        cfg = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            transport="streamable-http",
            default_token=None,
        )
        app = remote.create_remote_app(cfg)
        with TestClient(app, base_url="http://localhost") as client:
            res = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
            self.assertEqual(res.status_code, 401)
            self.assertIn("WWW-Authenticate", res.headers)
            self.assertEqual(res.headers["WWW-Authenticate"], "Bearer")
            self.assertIn("error", res.json())

    def test_request_with_bearer_token_sets_token_in_context(self):
        double = CapturingControllerDouble()
        cfg = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            transport="streamable-http",
            default_token="fallback-token",
        )
        adapter = server.RelayMeMcpAdapter(
            server.ControllerClient(
                server.AdapterConfig(cfg.controller_url, cfg.default_token),
                double,
            )
        )
        app = remote.create_remote_app(cfg, adapter=adapter)

        with TestClient(app, base_url="http://localhost") as client:
            # When Authorization header is passed, caller token is bound
            res = client.get("/health", headers={"Authorization": "Bearer caller-token-123"})
            self.assertEqual(res.status_code, 200)

    def test_tool_parity_with_stdio(self):
        """Verify the remote MCP server exposes the exact same 13 tools with matching schemas."""
        double = CapturingControllerDouble()
        cfg = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            transport="streamable-http",
            default_token="test-token",
        )
        adapter = server.RelayMeMcpAdapter(
            server.ControllerClient(
                server.AdapterConfig(cfg.controller_url, cfg.default_token),
                double,
            )
        )

        from mcp.server.fastmcp import FastMCP
        fastmcp = FastMCP("RelayMeTest")
        server.create_mcp_server(adapter, server=fastmcp)
        tools = asyncio.run(fastmcp.list_tools())
        tool_names = {t.name for t in tools}

        self.assertEqual(tool_names, set(server.TOOL_NAMES))
        self.assertEqual(len(tool_names), 13)
        for t in tools:
            expected_desc = server.TOOL_SCHEMAS[t.name]["description"]
            self.assertEqual(t.description, expected_desc)


class RemoteMcpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import uvicorn
        self.double = CapturingControllerDouble({
            "task": {
                "status": "SUCCEEDED",
                "result": {"summary": "task completed", "exit_code": 0},
            }
        })
        self.cfg = remote.RemoteConfig(
            controller_url="http://controller.example.invalid",
            host="127.0.0.1",
            port=18765,
            default_token="default-token",
            transport="both",
        )
        self.adapter = server.RelayMeMcpAdapter(
            server.ControllerClient(
                server.AdapterConfig(self.cfg.controller_url, self.cfg.default_token),
                self.double,
            )
        )
        self.app = remote.create_remote_app(self.cfg, adapter=self.adapter)

        uconfig = uvicorn.Config(self.app, host="127.0.0.1", port=18765, log_level="error")
        self.server = uvicorn.Server(uconfig)
        self.server_task = asyncio.create_task(self.server.serve())
        await asyncio.sleep(0.5)

    async def asyncTearDown(self):
        self.server.should_exit = True
        await self.server_task

    async def test_streamable_http_discovery_and_task_execution(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": "Bearer per-request-client-token"}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client("http://127.0.0.1:18765/mcp", http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # 1. Tool listing parity
                    tools = await session.list_tools()
                    tool_names = {t.name for t in tools.tools}
                    self.assertEqual(tool_names, set(server.TOOL_NAMES))

                    # 2. R0 Tool Call: list_hosts
                    res = await session.call_tool("list_hosts", {})
                    self.assertFalse(res.isError)
                    self.assertEqual(self.double.captured_tokens[-1], "per-request-client-token")

                    # 3. R0 Tool Call: list_host_resources
                    res = await session.call_tool("list_host_resources", {"host": "host-1"})
                    self.assertFalse(res.isError)
                    self.assertEqual(self.double.captured_tokens[-1], "per-request-client-token")

                    # 4. R1 Tool Call: start_registered_executor_task
                    exec_args = {
                        "host": "host-1",
                        "executor_profile_id": "research-patch-test",
                        "task_spec_id": "patch-and-test",
                        "brief": "Remote MCP synthetic test",
                        "idempotency_key": "remote-key-1",
                    }
                    res = await session.call_tool("start_registered_executor_task", exec_args)
                    self.assertFalse(res.isError)
                    self.assertEqual(self.double.captured_tokens[-1], "per-request-client-token")
                    self.assertEqual(self.double.calls[-1][0], "GET")
                    self.assertTrue(self.double.calls[-1][1].startswith("/v1/tasks/"))

                    # 5. Reviewable evidence tool: get_task_artifact
                    res = await session.call_tool("get_task_artifact", {"task_id": "task-1", "artifact_name": "patch.diff"})
                    self.assertFalse(res.isError)
                    self.assertEqual(self.double.captured_tokens[-1], "per-request-client-token")

    async def test_sse_discovery_and_auth_override(self):
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        headers = {"Authorization": "Bearer sse-scoped-token"}
        async with sse_client("http://127.0.0.1:18765/sse", headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                self.assertEqual(len(tools.tools), 13)

                res = await session.call_tool("list_hosts", {})
                self.assertFalse(res.isError)
                self.assertEqual(self.double.captured_tokens[-1], "sse-scoped-token")

    async def test_controller_error_propagation_through_remote_mcp(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        def error_request(method, path, body):
            raise server.ControllerError(403, "client token does not allow host_status")

        self.adapter.client._request_fn = error_request

        headers = {"Authorization": "Bearer forbidden-token"}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client("http://127.0.0.1:18765/mcp", http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    res = await session.call_tool("host_status", {"host": "host-1"})
                    self.assertIn("does not allow host_status", str(res.content))


if __name__ == "__main__":
    unittest.main()
