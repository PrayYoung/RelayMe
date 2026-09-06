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
        if path == "/v1/hosts": return {"hosts": [{"host_id": "host-1", "hostname": "example-host"}]}
        if path == "/v1/hosts/host-1/resources":
            return {"resources": {"host_id": "host-1", "services": ["example.service"], "repositories": ["example-app"], "allowed_file_roots": ["/srv/example-app"]}}
        if method == "POST": return {"task_id": "task-1"}
        return self.terminal


class McpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.double = ControllerDouble()
        config = server.AdapterConfig("https://controller.example.invalid", "example-token")
        self.adapter = server.RelayMeMcpAdapter(server.ControllerClient(config, self.double))

    def test_exposes_exactly_the_r0_and_bounded_r1_tools_without_a_server_prefix(self):
        self.assertEqual(set(server.TOOL_NAMES), {"list_hosts", "list_host_resources", "host_status", "process_list", "read_logs", "read_file", "git_diff", "run_registered_task", "start_registered_executor_task", "get_task", "task_result", "list_reviewable_tasks", "get_task_artifact", "run_executor_round", "collect_executor_round"})
        self.assertEqual(set(server.TOOL_SCHEMAS), set(server.TOOL_NAMES))
        self.assertNotIn("relayme_list_hosts", server.TOOL_NAMES)

    def test_each_tool_maps_to_existing_controller_request(self):
        cases = [
            ("list_hosts", {}, ("GET", "/v1/hosts", None)),
            ("list_host_resources", {"host": "host-1"}, ("GET", "/v1/hosts/host-1/resources", None)),
            ("host_status", {"host": "host-1"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "host_status", "arguments": {}})),
            ("process_list", {"host": "host-1"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "process_list", "arguments": {}})),
            ("read_logs", {"host": "host-1", "service": "example.service", "last_n": 10, "since": "-1h", "max_bytes": 128}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "read_logs", "arguments": {"service": "example.service", "last_n": 10, "since": "-1h", "max_bytes": 128}})),
            ("read_file", {"host": "host-1", "path": "/srv/example-app/config.json"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "read_file", "arguments": {"path": "/srv/example-app/config.json"}})),
            ("git_diff", {"host": "host-1", "repo": "example-app"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "git_diff", "arguments": {"repo": "example-app"}})),
            ("run_registered_task", {"host": "host-1", "task_id": "demo-health-check", "idempotency_key": "once"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "run_registered_task", "arguments": {"task_id": "demo-health-check", "idempotency_key": "once"}})),
            ("start_registered_executor_task", {"host": "host-1", "executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "Synthetic task", "idempotency_key": "once"}, ("POST", "/v1/tasks", {"host": "host-1", "capability": "start_registered_executor_task", "arguments": {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "Synthetic task", "idempotency_key": "once"}})),
            ("get_task", {"task_id": "task-1"}, ("GET", "/v1/tasks/task-1", None)),
            ("task_result", {"task_id": "task-1"}, ("GET", "/v1/tasks/task-1/result", None)),
            ("list_reviewable_tasks", {}, ("GET", "/v1/reviewable-tasks", None)),
            ("get_task_artifact", {"task_id": "task-1", "artifact_name": "patch.diff"}, ("GET", "/v1/tasks/task-1/artifacts/patch.diff", None)),
        ]
        for tool, arguments, expected in cases:
            with self.subTest(tool=tool):
                self.double.calls.clear()
                result = self.adapter.invoke(tool, arguments)
                self.assertTrue(result)
                self.assertEqual(self.double.calls[0], expected)

    def test_controller_authorization_error_is_preserved(self):
        def denied(method, path, body): raise server.ControllerError(403, "client token does not allow host_status")
        adapter = server.RelayMeMcpAdapter(server.ControllerClient(server.AdapterConfig("https://controller.example.invalid", "token"), denied))
        with self.assertRaisesRegex(server.ControllerError, "does not allow") as raised: adapter.invoke("host_status", {"host": "host-1"})
        self.assertEqual(raised.exception.status, 403)

    def test_host_agent_policy_rejection_is_returned_unmodified(self):
        rejected = {"task": {"status": "FAILED", "error": {"code": "file_outside_allowed_roots", "message": "path is outside allowed roots"}}}
        double = ControllerDouble(rejected)
        adapter = server.RelayMeMcpAdapter(server.ControllerClient(server.AdapterConfig("https://controller.example.invalid", "token"), double))
        self.assertEqual(adapter.invoke("read_file", {"host": "host-1", "path": "/etc/shadow"}), rejected)

    def test_unsupported_or_invalid_tool_calls_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"): self.adapter.invoke("restart_service", {"host": "host-1"})
        with self.assertRaisesRegex(ValueError, "host must"): self.adapter.invoke("host_status", {})
        with self.assertRaisesRegex(ValueError, "accepts no arguments"): self.adapter.invoke("list_hosts", {"host": "host-1"})
        with self.assertRaisesRegex(ValueError, "only host"):
            self.adapter.invoke("run_registered_task", {"host": "host-1", "task_id": "safe", "argv": ["evil"]})
        with self.assertRaisesRegex(ValueError, "only host"):
            self.adapter.invoke("start_registered_executor_task", {"host": "host-1", "executor_profile_id": "safe", "task_spec_id": "patch-and-test", "brief": "x", "argv": ["evil"]})
        with self.assertRaisesRegex(ValueError, "only task_id and artifact_name"):
            self.adapter.invoke("get_task_artifact", {"task_id": "task-1"})

    def test_missing_or_invalid_configuration_fails_clearly(self):
        with self.assertRaisesRegex(server.ConfigurationError, "RELAYME_URL"): server.AdapterConfig.from_env({})
        with self.assertRaisesRegex(server.ConfigurationError, "integer"):
            server.AdapterConfig.from_env({"RELAYME_URL": "https://controller.example.invalid", "RELAYME_TOKEN": "token", "RELAYME_WAIT_SECONDS": "fast"})

    def test_adapter_contains_no_ssh_or_process_execution_fallback(self):
        source = Path(server.__file__).read_text().lower()
        self.assertNotIn("subprocess", source); self.assertNotIn("ssh", source); self.assertNotIn("os.system", source)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK is not installed")
    def test_mcp_server_exposes_schemas_and_surfaces_controller_errors(self):
        mcp_server = server.create_mcp_server(self.adapter)
        tools = asyncio.run(mcp_server.list_tools())
        self.assertEqual({tool.name for tool in tools}, set(server.TOOL_NAMES))
        self.assertIn("host", next(tool for tool in tools if tool.name == "host_status").inputSchema["properties"])

        def denied(method, path, body): raise server.ControllerError(403, "scope denied")
        denied_adapter = server.RelayMeMcpAdapter(server.ControllerClient(server.AdapterConfig("https://controller.example.invalid", "token"), denied))
        _, response = asyncio.run(server.create_mcp_server(denied_adapter).call_tool("host_status", {"host": "host-1"}))
        self.assertEqual(response["error"], {"status": 403, "message": "scope denied"})
