import json
import os
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from relayme.agent import AgentPolicy, ControllerUnavailable, PolicyError, run
from relayme import admin, cli
from relayme.controller import ControllerServer, Store


class AgentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name) / "allowed"; root.mkdir()
        (root / "ok.txt").write_text("ok")
        (root / "out").symlink_to("/etc/passwd")
        self.policy = AgentPolicy({"allowed_roots": [str(root)], "repos": {}, "services": ["safe.service"]})
        self.root = root
    def tearDown(self): self.temp.cleanup()
    def test_read_file_blocks_parent_escape_and_shadow(self):
        with self.assertRaises(PolicyError): self.policy.read_file({"path": str(self.root / ".." / ".." / "etc" / "shadow")})
        with self.assertRaises(PolicyError): self.policy.read_file({"path": "/etc/shadow"})
    def test_read_file_blocks_symlink_and_nonregular(self):
        with self.assertRaises(PolicyError): self.policy.read_file({"path": str(self.root / "out")})
        with self.assertRaises(PolicyError): self.policy.read_file({"path": str(self.root)})
    def test_large_file_is_rejected(self):
        self.policy.max_file = 3; (self.root / "large.txt").write_text("four")
        with self.assertRaises(PolicyError): self.policy.read_file({"path": str(self.root / "large.txt")})
    def test_logs_service_and_bounds_enforced_without_shell(self):
        with self.assertRaises(PolicyError): self.policy.read_logs({"service": "evil.service"})
        fake = type("Run", (), {"returncode": 0, "stdout": b"x" * 10, "stderr": b""})()
        with patch("relayme.agent.subprocess.run", return_value=fake) as run:
            result = self.policy.read_logs({"service": "safe.service", "last_n": 2, "max_bytes": 3})
        self.assertTrue(result["truncated"]); self.assertEqual(result["text"], "xxx")
        self.assertIsInstance(run.call_args.args[0], list); self.assertNotIn("shell", run.call_args.kwargs)
    def test_repo_id_and_unauthorized_capability_rejected(self):
        with self.assertRaises(PolicyError): self.policy.git_diff({"repo": "/etc"})
        with self.assertRaises(PolicyError): self.policy.execute("run_shell", {"command": "id"})
    @patch("relayme.agent.Path.read_text", return_value="123.9 0.0")
    @patch("relayme.agent.platform.node", return_value="test-host")
    def test_host_status_reports_elapsed_uptime(self, _node, _uptime):
        self.assertEqual(self.policy.host_status()["uptime_seconds"], 123)
    def test_agent_retries_after_controller_interruption_without_reenrolling(self):
        calls = [{"ok": True}, ControllerUnavailable("offline"), {"task": None}, KeyboardInterrupt()]
        with patch("relayme.agent.credentials", return_value={"host_id": "host", "credential": "existing"}) as credentials, \
             patch("relayme.agent.request", side_effect=calls), \
             patch("relayme.agent.time.sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt): run({"controller_url": "http://controller", "allowed_roots": []})
        credentials.assert_called_once()
        sleep.assert_called_once_with(1)
    def test_agent_retries_pending_result_after_controller_recovery(self):
        task = {"task_id": "task", "capability": "host_status", "arguments": {}}
        calls = [{"ok": True}, {"task": task}, ControllerUnavailable("offline"), {"ok": True}, KeyboardInterrupt()]
        with patch("relayme.agent.credentials", return_value={"host_id": "host", "credential": "existing"}), \
             patch("relayme.agent.request", side_effect=calls) as request, \
             patch.object(AgentPolicy, "execute", return_value={"ok": True}) as execute, \
             patch("relayme.agent.time.sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt): run({"controller_url": "http://controller", "allowed_roots": []})
        execute.assert_called_once_with("host_status", {})
        result_calls = [call for call in request.call_args_list if call.args[0].endswith("/agent/tasks/result")]
        self.assertEqual(len(result_calls), 2)
        sleep.assert_called_once_with(1)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(str(Path(self.temp.name) / "db.sqlite"))
        self.server = ControllerServer(("127.0.0.1", 0), self.store, "admin")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.store.db.close(); self.temp.cleanup()
    def call(self, method, path, body=None, token="admin", agent=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if agent: headers["X-RelayMe-Host"] = agent
        req = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        with urlopen(req) as response: return json.loads(response.read())
    def test_auth_task_delivery_audit_and_duplicate_prevention(self):
        enrollment = self.call("POST", "/v1/admin/enrollment-tokens", {"ttl_seconds": 60})["token"]
        agent = self.call("POST", "/v1/agents/enroll", {"enrollment_token": enrollment, "hostname": "example-host", "metadata": {}})
        headers = {"Authorization": "Bearer " + agent["credential"], "X-RelayMe-Host": agent["host_id"]}
        self.call("POST", "/v1/agent/heartbeat", {"metadata": {}}, agent=agent["host_id"], token=agent["credential"])
        token = self.store.create_client("local-cli", {"hosts": [agent["host_id"]], "capabilities": ["host_status"]}, None)
        task = self.call("POST", "/v1/tasks", {"host": agent["host_id"], "capability": "host_status", "arguments": {}}, token)["task_id"]
        delivered = self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token=agent["credential"], agent=agent["host_id"])["task"]
        self.assertEqual(delivered["task_id"], task)
        self.assertIsNone(self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token=agent["credential"], agent=agent["host_id"])["task"])
        self.call("POST", "/v1/agent/tasks/result", {"task_id": task, "status": "SUCCEEDED", "result": {"ok": True}}, token=agent["credential"], agent=agent["host_id"])
        audit = self.call("GET", "/v1/tasks/" + task, token)["task"]
        self.assertEqual(audit["status"], "SUCCEEDED"); self.assertEqual(audit["result_bytes"], 11); self.assertIsNotNone(audit["result_hash"]); self.assertEqual(audit["result"], {"ok": True})
    def test_invalid_credentials_are_rejected(self):
        with self.assertRaises(HTTPError) as client: self.call("GET", "/v1/hosts", token="bad")
        self.assertEqual(client.exception.code, 401)
        client.exception.close()
        with self.assertRaises(HTTPError) as agent: self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token="bad", agent="not-a-host")
        self.assertEqual(agent.exception.code, 401)
        agent.exception.close()
    def test_expired_client_token_is_rejected(self):
        expired = self.store.create_client("expired", {"hosts": ["*"], "capabilities": ["list_hosts"]}, -1)
        with self.assertRaises(HTTPError) as client: self.call("GET", "/v1/hosts", token=expired)
        self.assertEqual(client.exception.code, 401)
        client.exception.close()
    def test_scoped_client_cannot_create_out_of_scope_task(self):
        token = self.store.create_client("limited", {"hosts": ["example-host"], "capabilities": ["host_status"]}, None)
        with self.assertRaises(HTTPError) as client:
            self.call("POST", "/v1/tasks", {"host": "example-host", "capability": "process_list", "arguments": {}}, token)
        self.assertEqual(client.exception.code, 403)
        client.exception.close()

    def test_remote_client_token_creation_is_not_exposed(self):
        with self.assertRaises(HTTPError) as client:
            self.call("POST", "/v1/admin/client-tokens", {"identity": "remote", "scopes": {"hosts": ["*"], "capabilities": ["list_hosts"]}})
        self.assertEqual(client.exception.code, 404)
        client.exception.close()


class LocalAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "controller.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def test_local_admin_creates_exact_scoped_client_and_audit(self):
        capabilities = ["list_hosts", "host_status", "process_list", "read_logs", "read_file", "git_diff"]
        arguments = [value for capability in capabilities for value in ("--capability", capability)]
        with patch("sys.stdout", new_callable=StringIO) as output:
            admin.main(["--db", self.database, "create-client", "--name", "opencode", *arguments])
        token = output.getvalue().strip()
        store = Store(self.database)
        try:
            identity, scope = store.authenticate_client(token)
            self.assertEqual(identity, "opencode")
            self.assertEqual(scope, {"hosts": ["*"], "capabilities": capabilities})
            audit = store.db.execute("SELECT event, identity, token_hash FROM client_token_audit").fetchone()
            self.assertEqual((audit["event"], audit["identity"]), ("CREATED", "opencode"))
            self.assertNotEqual(audit["token_hash"], token)
        finally:
            store.db.close()

    def test_local_admin_rejects_unauthorized_capability(self):
        with self.assertRaises(SystemExit):
            admin.main(["--db", self.database, "create-client", "--name", "opencode", "--capability", "run_shell"])

    def test_relayme_admin_dispatches_to_local_admin(self):
        with patch("sys.argv", ["relayme", "admin", "--db", self.database, "create-client", "--name", "local", "--capability", "list_hosts"]), \
             patch("sys.stdout", new_callable=StringIO) as output:
            cli.main()
        self.assertGreaterEqual(len(output.getvalue().strip()), 24)

    def test_local_admin_revokes_client_and_records_audit(self):
        store = Store(self.database)
        token = store.create_client("opencode", {"hosts": ["*"], "capabilities": ["list_hosts"]}, None)
        store.db.close()
        with patch("sys.stdout", new_callable=StringIO) as output:
            admin.main(["--db", self.database, "revoke-client", "--name", "opencode"])
        self.assertEqual(output.getvalue().strip(), "1")
        store = Store(self.database)
        try:
            self.assertIsNone(store.authenticate_client(token))
            events = [row["event"] for row in store.db.execute("SELECT event FROM client_token_audit ORDER BY event_id")]
            self.assertEqual(events, ["CREATED", "REVOKED"])
        finally:
            store.db.close()


class ExternalCliTests(unittest.TestCase):
    def test_named_r0_command_uses_environment_configuration(self):
        created = {"task_id": "task"}; completed = {"task": {"status": "SUCCEEDED", "result": {"ok": True}}}
        with patch.dict("os.environ", {"RELAYME_URL": "https://controller.example.invalid", "RELAYME_TOKEN": "scoped"}, clear=True), \
             patch("sys.argv", ["relayme", "read-file", "host", "/srv/example-app/config.json"]), \
             patch("relayme.cli.call", side_effect=[created, completed]) as call, \
             patch("sys.stdout", new_callable=StringIO):
            cli.main()
        self.assertEqual(call.call_args_list[0].args[2:4], ("POST", "/v1/tasks"))
        self.assertEqual(call.call_args_list[0].args[4]["capability"], "read_file")
        self.assertEqual(call.call_args_list[0].args[4]["arguments"], {"path": "/srv/example-app/config.json"})


if __name__ == "__main__": unittest.main()
