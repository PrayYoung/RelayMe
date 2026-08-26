import asyncio
import importlib.util
import unittest
from pathlib import Path

from adapters.mcp import server


class ControllerDouble:
    def __init__(self, terminal=None):
        self.calls = []
        self.terminal = terminal or {"task": {"status": "SUCCEEDED", "result": {"ok": True}}}

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/v1/hosts":
            return {"hosts": [{"id": "example-host"}]}
        if method == "POST":
            return {"task_id": "task-1"}
        return self.terminal


class McpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.double = ControllerDouble()
        config = server.AdapterConfig("https://controller.example.invalid", "example-token")
        self.adapter = server.RelayMeMcpAdapter(server.ControllerClient(config, self.double))

    def test_exposes_exactly_the_six_r0_tools(self):
        self.assertEqual(set(server.TOOL_NAMES), {
            "relayme_list_hosts", "relayme_host_status", "relayme_process_list",
            "relayme_read_logs", "relayme_read_file", "relayme_git_diff",
        })
        self.assertEqual(set(server.TOOL_SCHEMAS), set(server.TOOL_NAMES))

    def test_each_tool_maps_to_existing_controller_request(self):
        cases = [
            ("relayme_list_hosts", {}, ("GET", "/v1/hosts", None)),
            ("relayme_host_status", {"host": "host-1"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "host_status", "arguments": {}})),
            ("relayme_process_list", {"host": "host-1"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "process_list", "arguments": {}})),
            ("relayme_read_logs", {"host": "host-1", "service": "example.service", "last_n": 10, "since": "-1h", "max_bytes": 128}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "read_logs", "arguments": {"service": "example.service", "last_n": 10, "since": "-1h", "max_bytes": 128}})),
            ("relayme_read_file", {"host": "host-1", "path": "/srv/example-app/config.json"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "read_file", "arguments": {"path": "/srv/example-app/config.json"}})),
            ("relayme_git_diff", {"host": "host-1", "repo": "example-app"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "git_diff", "arguments": {"repo": "example-app"}})),
        ]
        for tool, arguments, expected in cases:
            with self.subTest(tool=tool):
                self.double.calls.clear()
                result = self.adapter.invoke(tool, arguments)
                self.assertEqual(result["task"]["status"], "SUCCEEDED") if tool != "relayme_list_hosts" else self.assertEqual(result["hosts"][0]["id"], "example-host")
                self.assertEqual(self.double.calls[0], expected)

    def test_controller_authorization_error_is_preserved(self):
        def denied(method, path, body):
            raise server.ControllerError(403, "client token does not allow host_status")
        adapter = server.RelayMeMcpAdapter(server.ControllerClient(server.AdapterConfig("https://controller.example.invalid", "token"), denied))
        with self.assertRaisesRegex(server.ControllerError, "does not allow") as raised:
            adapter.invoke("relayme_host_status", {"host": "host-1"})
        self.assertEqual(raised.exception.status, 403)

    def test_host_agent_policy_rejection_is_returned_unmodified(self):
        rejected = {"task": {"status": "FAILED", "error": "path is outside allowed roots"}}
        double = ControllerDouble(rejected)
        adapter = server.RelayMeMcpAdapter(server.ControllerClient(server.AdapterConfig("https://controller.example.invalid", "token"), double))
        self.assertEqual(adapter.invoke("relayme_read_file", {"host": "host-1", "path": "/etc/shadow"}), rejected)

    def test_unsupported_or_invalid_tool_calls_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.adapter.invoke("relayme_restart_service", {"host": "host-1"})
        with self.assertRaisesRegex(ValueError, "host must"):
            self.adapter.invoke("relayme_host_status", {})
        with self.assertRaisesRegex(ValueError, "accepts no arguments"):
            self.adapter.invoke("relayme_list_hosts", {"host": "host-1"})

    def test_missing_or_invalid_configuration_fails_clearly(self):
        with self.assertRaisesRegex(server.ConfigurationError, "RELAYME_URL"):
            server.AdapterConfig.from_env({})
        with self.assertRaisesRegex(server.ConfigurationError, "integer"):
            server.AdapterConfig.from_env({"RELAYME_URL": "https://controller.example.invalid", "RELAYME_TOKEN": "token", "RELAYME_WAIT_SECONDS": "fast"})

    def test_adapter_contains_no_ssh_or_process_execution_fallback(self):
        source = Path(server.__file__).read_text().lower()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("ssh", source)
        self.assertNotIn("os.system", source)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK is not installed")
    def test_mcp_server_exposes_schemas_and_surfaces_controller_errors(self):
        mcp_server = server.create_mcp_server(self.adapter)
        tools = asyncio.run(mcp_server.list_tools())
        self.assertEqual({tool.name for tool in tools}, set(server.TOOL_NAMES))
        self.assertIn("host", next(tool for tool in tools if tool.name == "relayme_host_status").inputSchema["properties"])

        def denied(method, path, body):
            raise server.ControllerError(403, "scope denied")
        denied_adapter = server.RelayMeMcpAdapter(server.ControllerClient(server.AdapterConfig("https://controller.example.invalid", "token"), denied))
        _, response = asyncio.run(server.create_mcp_server(denied_adapter).call_tool("relayme_host_status", {"host": "host-1"}))
        self.assertEqual(response["error"], {"status": 403, "message": "scope denied"})
