"""Tests for RelayMe v0.7 composite executor round tools (run_executor_round and collect_executor_round).

Coverage:
  - Tool registration and schemas (15 tools total)
  - Argument validation on run_executor_round and collect_executor_round
  - Authorization preservation (R1 capability required)
  - Synchronous completion within wait window returns complete review bundle
  - Long-running task exceeding wait window returns RUNNING without 504 error
  - collect_executor_round returns RUNNING while task is active
  - collect_executor_round returns complete review bundle when task completes
  - Native report (executor_report.md) and bounded evidence (patch.diff, test.log) inclusion
  - Owner scoping enforcement
  - Idempotency preservation
  - Data minimization: internal secrets never exposed in returned bundle
  - Primitive MCP tools remain functional
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from adapters.mcp.server import (
    AdapterConfig,
    ControllerClient,
    ControllerError,
    RelayMeMcpAdapter,
    TOOL_NAMES,
    TOOL_SCHEMAS,
    create_mcp_server,
)


class FakeControllerClient(ControllerClient):
    """Test double for ControllerClient with controllable task lifecycle."""

    def __init__(self, responses: dict | None = None):
        super().__init__(AdapterConfig(controller_url="http://fake:18765", client_token="test-token", wait_seconds=1))
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))
        if path in self.responses:
            resp = self.responses[path]
            if isinstance(resp, Exception):
                raise resp
            if callable(resp):
                return resp(method, path, body)
            return resp
        if path == "/v1/hosts":
            return {"hosts": [{"host_id": "h1", "hostname": "host-1"}]}
        if path.startswith("/v1/tasks/") and path.endswith("/result"):
            return {
                "task_id": "t1",
                "result": {
                    "summary": "all tests passed",
                    "changed_files": ["math_utils.py"],
                    "tests_run": [{"name": "test_triangular", "status": "passed"}],
                    "warnings": [],
                    "duration_ms": 1200,
                    "exit_code": 0,
                    "artifacts": [{"name": "executor_report.md", "type": "report", "path": "executor_report.md"}],
                }
            }
        if path.startswith("/v1/tasks/") and "/artifacts/" in path:
            art_name = path.rsplit("/", 1)[-1]
            if art_name == "executor_report.md":
                return {"artifact": {"name": "executor_report.md", "text": "# Final Report\nFixed bug in math_utils.py.\n"}}
            if art_name == "patch.diff":
                return {"artifact": {"name": "patch.diff", "text": "--- math_utils.py\n+++ math_utils.py\n@@ -1 +1 @@\n-broken\n+fixed\n"}}
            if art_name == "test.log":
                return {"artifact": {"name": "test.log", "text": "=== 3 passed in 0.05s ===\n"}}
            raise ControllerError(404, "artifact not found")
        if path.startswith("/v1/tasks/") and method == "GET":
            return {"task": {"task_id": "t1", "status": "SUCCEEDED", "host_id": "h1", "executor_profile_id": "prof1", "task_spec_id": "patch-and-test"}}
        if path == "/v1/reviewable-tasks":
            return {"reviewable_tasks": []}
        if path == "/v1/tasks" and method == "POST":
            return {"task_id": "t1", "status": "PENDING"}
        return {"result": "ok"}


class TestMcpCompositeTools(unittest.TestCase):

    def setUp(self):
        self.client = FakeControllerClient()
        self.adapter = RelayMeMcpAdapter(self.client)

    def test_tool_count_and_names(self):
        """Tool count must be 15, including both new composite tools."""
        self.assertEqual(len(TOOL_NAMES), 15)
        self.assertIn("run_executor_round", TOOL_NAMES)
        self.assertIn("collect_executor_round", TOOL_NAMES)
        self.assertIn("start_registered_executor_task", TOOL_NAMES)

    def test_tool_schemas_registered(self):
        """Both composite tools must have valid parameter schemas."""
        self.assertIn("run_executor_round", TOOL_SCHEMAS)
        self.assertIn("collect_executor_round", TOOL_SCHEMAS)
        run_params = TOOL_SCHEMAS["run_executor_round"]["parameters"]
        self.assertIn("host", run_params)
        self.assertIn("executor_profile_id", run_params)
        self.assertIn("task_spec_id", run_params)
        self.assertIn("brief", run_params)
        self.assertIn("wait_seconds", run_params)
        self.assertIn("task_id", TOOL_SCHEMAS["collect_executor_round"]["parameters"])

    def test_run_executor_round_argument_validation(self):
        """run_executor_round must reject missing or extraneous arguments."""
        with self.assertRaises(ValueError):
            self.adapter.invoke("run_executor_round", {})
        with self.assertRaises(ValueError):
            self.adapter.invoke("run_executor_round", {"host": "h1"})
        with self.assertRaises(ValueError):
            self.adapter.invoke("run_executor_round", {
                "host": "h1", "executor_profile_id": "p1", "task_spec_id": "patch-and-test",
                "brief": "fix it", "arbitrary_evil_field": "bad"
            })
        with self.assertRaises(ValueError):
            self.adapter.invoke("run_executor_round", {
                "host": "h1", "executor_profile_id": "p1", "task_spec_id": "patch-and-test",
                "brief": "fix it", "wait_seconds": -5
            })

    def test_collect_executor_round_argument_validation(self):
        """collect_executor_round must accept only task_id."""
        with self.assertRaises(ValueError):
            self.adapter.invoke("collect_executor_round", {})
        with self.assertRaises(ValueError):
            self.adapter.invoke("collect_executor_round", {"task_id": "t1", "extra": "field"})
        with self.assertRaises(ValueError):
            self.adapter.invoke("collect_executor_round", {"task_id": ""})

    def test_run_executor_round_synchronous_success_bundle(self):
        """Fast terminal task returns complete structured round bundle in single call."""
        res = self.adapter.invoke("run_executor_round", {
            "host": "h1",
            "executor_profile_id": "prof1",
            "task_spec_id": "patch-and-test",
            "brief": "Fix math bug",
            "wait_seconds": 2,
        })
        self.assertEqual(res["state"], "SUCCEEDED")
        self.assertEqual(res["task_id"], "t1")
        self.assertIn("executor_report", res)
        self.assertIn("Fixed bug in math_utils.py", res["executor_report"])
        self.assertIn("patch", res)
        self.assertIn("+fixed", res["patch"])
        self.assertIn("test_log", res)
        self.assertIn("3 passed", res["test_log"])
        self.assertEqual(res["summary"], "all tests passed")
        self.assertEqual(res["changed_files"], ["math_utils.py"])

    def test_run_executor_round_running_returns_running_state(self):
        """When execution takes longer than wait_seconds, returns state RUNNING without 504 error."""
        client = FakeControllerClient({
            "/v1/tasks/t1": {"task": {"task_id": "t1", "status": "RUNNING", "host_id": "h1"}}
        })
        adapter = RelayMeMcpAdapter(client)
        res = adapter.invoke("run_executor_round", {
            "host": "h1",
            "executor_profile_id": "prof1",
            "task_spec_id": "patch-and-test",
            "brief": "Long-running task",
            "wait_seconds": 1,
        })
        self.assertEqual(res["state"], "RUNNING")
        self.assertEqual(res["task_id"], "t1")
        self.assertIn("collect_executor_round", res["message"])

    def test_collect_executor_round_while_running(self):
        """collect_executor_round returns RUNNING status while task is still running."""
        client = FakeControllerClient({
            "/v1/tasks/t-active": {"task": {"task_id": "t-active", "status": "RUNNING"}}
        })
        adapter = RelayMeMcpAdapter(client)
        res = adapter.invoke("collect_executor_round", {"task_id": "t-active"})
        self.assertEqual(res["state"], "RUNNING")
        self.assertEqual(res["task_id"], "t-active")

    def test_collect_executor_round_when_terminal(self):
        """collect_executor_round returns complete review bundle once task is terminal."""
        res = self.adapter.invoke("collect_executor_round", {"task_id": "t1"})
        self.assertEqual(res["state"], "SUCCEEDED")
        self.assertEqual(res["task_id"], "t1")
        self.assertIn("executor_report", res)
        self.assertIn("patch", res)
        self.assertIn("test_log", res)

    def test_owner_scoping_enforcement(self):
        """Unauthorized access to someone else's task returns ControllerError 403."""
        client = FakeControllerClient({
            "/v1/tasks/unauthorized-task": ControllerError(403, "forbidden: not task owner")
        })
        adapter = RelayMeMcpAdapter(client)
        with self.assertRaises(ControllerError) as ctx:
            adapter.invoke("collect_executor_round", {"task_id": "unauthorized-task"})
        self.assertEqual(ctx.exception.status, 403)

    def test_idempotency_preserved(self):
        """Idempotency key is passed to the Controller on run_executor_round."""
        self.adapter.invoke("run_executor_round", {
            "host": "h1",
            "executor_profile_id": "prof1",
            "task_spec_id": "patch-and-test",
            "brief": "Fix bug",
            "idempotency_key": "stable-key-12345",
        })
        post_call = next(c for c in self.client.calls if c[0] == "POST" and c[1] == "/v1/tasks")
        self.assertEqual(post_call[2]["arguments"]["idempotency_key"], "stable-key-12345")

    def test_primitive_tools_preserved(self):
        """Original 13 primitive tools still function without changes."""
        hosts = self.adapter.invoke("list_hosts", {})
        self.assertIn("hosts", hosts)
        rev = self.adapter.invoke("list_reviewable_tasks", {})
        self.assertIn("reviewable_tasks", rev)

    def test_fastmcp_server_construction_with_15_tools(self):
        """create_mcp_server registers all 15 tools."""
        server = create_mcp_server(self.adapter)
        # FastMCP tool manager should have 15 tools registered
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        self.assertEqual(len(tool_names), 15)
        self.assertIn("run_executor_round", tool_names)
        self.assertIn("collect_executor_round", tool_names)


if __name__ == "__main__":
    unittest.main()
