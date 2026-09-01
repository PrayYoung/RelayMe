import json
import os
import tempfile
import threading
import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from relayme.agent import AgentPolicy, ControllerUnavailable, MAX_EXECUTION_STREAM_BYTES, PolicyError, run
from relayme import admin, cli, controller
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

    def test_agent_forwards_successful_r1_result_with_terminal_status(self):
        task = {"task_id": "execution", "capability": "run_registered_task", "arguments": {"task_id": "safe"}}
        result = {"execution_id": "execution", "task_id": "safe", "state": "succeeded"}
        calls = [{"ok": True}, {"task": task}, {"ok": True}, KeyboardInterrupt()]
        with patch("relayme.agent.credentials", return_value={"host_id": "host", "credential": "existing"}), \
             patch("relayme.agent.request", side_effect=calls) as request, \
             patch.object(AgentPolicy, "execute", return_value=result):
            with self.assertRaises(KeyboardInterrupt): run({"controller_url": "http://controller", "allowed_roots": []})
        posted = [call for call in request.call_args_list if call.args[0].endswith("/agent/tasks/result")][0]
        self.assertEqual(posted.args[2]["status"], "SUCCEEDED")


class RegisteredTaskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "allowed"; self.root.mkdir()
        self.config = {
            "allowed_roots": [str(self.root)], "repos": {}, "services": [],
            "execution_state_file": str(Path(self.temp.name) / "execution-state.json"),
            "registered_tasks": {"demo-health-check": {
                "description": "Deterministic demo check", "executable": sys.executable,
                "argv": [sys.executable, "-c", "import os; print(os.getenv('SECRET', 'clean'))"],
                "working_directory": str(self.root), "environment": {"LANG": "C.UTF-8"},
                "timeout_seconds": 2, "max_stdout_bytes": 64, "max_stderr_bytes": 64,
                "max_concurrent": 1, "cooldown_seconds": 0, "idempotent": True, "run_as": "agent"
            }}
        }
    def tearDown(self): self.temp.cleanup()
    def test_fixed_policy_clean_environment_and_duplicate_execution(self):
        with patch.dict(os.environ, {"SECRET": "leak"}, clear=True):
            policy = AgentPolicy(self.config)
            result = policy.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-1")
        self.assertEqual(result["state"], "succeeded"); self.assertEqual(result["stdout"].strip(), "clean")
        self.assertEqual(policy.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-1"), result)
        self.assertEqual(AgentPolicy(self.config).execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-1"), result)
        self.assertNotIn(sys.executable, policy.resources()["registered_tasks"][0].values())
    def test_unregistered_task_client_fields_and_root_are_rejected(self):
        policy = AgentPolicy(self.config)
        with self.assertRaisesRegex(PolicyError, "not available"): policy.execute("run_registered_task", {"task_id": "missing"}, "execution-2")
        with self.assertRaisesRegex(PolicyError, "invalid"): policy.execute("run_registered_task", {"task_id": "demo-health-check", "argv": ["evil"]}, "execution-3")
        with patch("relayme.agent.os.geteuid", return_value=0):
            with self.assertRaisesRegex(PolicyError, "root"): policy.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-4")
    def test_cwd_output_limits_cooldown_and_timeout(self):
        self.config["registered_tasks"]["demo-health-check"]["argv"] = [sys.executable, "-c", "import sys; print('x'*200); print('y'*200, file=sys.stderr)"]
        self.config["registered_tasks"]["demo-health-check"]["max_stdout_bytes"] = 10
        self.config["registered_tasks"]["demo-health-check"]["max_stderr_bytes"] = 10
        self.config["registered_tasks"]["demo-health-check"]["cooldown_seconds"] = 60
        policy = AgentPolicy(self.config)
        result = policy.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-5")
        self.assertTrue(result["stdout_truncated"]); self.assertTrue(result["stderr_truncated"])
        with self.assertRaisesRegex(PolicyError, "cooldown"): policy.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-6")
        self.config["registered_tasks"]["demo-health-check"].update({"argv": [sys.executable, "-c", "import time; time.sleep(10)"], "timeout_seconds": 1, "cooldown_seconds": 0})
        timed = AgentPolicy(self.config).execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-7")
        self.assertEqual(timed["state"], "timed_out")
    def test_task_policy_requires_canonical_allowed_cwd_and_fixed_identity(self):
        bad = json.loads(json.dumps(self.config)); bad["registered_tasks"]["demo-health-check"]["working_directory"] = "/"
        with self.assertRaisesRegex(ValueError, "outside allowed roots"): AgentPolicy(bad)
        bad = json.loads(json.dumps(self.config)); bad["registered_tasks"]["demo-health-check"]["idempotent"] = False
        with self.assertRaisesRegex(ValueError, "idempotent"): AgentPolicy(bad)

    def test_completed_result_survives_restart_and_is_redelivered_without_rerun(self):
        counter = self.root / "execution-count"
        self.config["registered_tasks"]["demo-health-check"]["argv"] = [sys.executable, "-c", f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')"]
        first = AgentPolicy(self.config)
        result = first.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-restart")
        self.assertEqual(counter.read_text(), "1")
        restarted = AgentPolicy(self.config)
        self.assertEqual(len(restarted.pending_delivery_payloads()), 1)
        self.assertEqual(restarted.execute("run_registered_task", {"task_id": "demo-health-check"}, "execution-restart"), result)
        self.assertEqual(counter.read_text(), "1")
        calls = [{"ok": True}, KeyboardInterrupt()]
        config = {**self.config, "controller_url": "http://controller"}
        with patch("relayme.agent.credentials", return_value={"host_id": "host", "credential": "existing"}), patch("relayme.agent.request", side_effect=calls) as request:
            with self.assertRaises(KeyboardInterrupt): run(config)
        self.assertTrue(request.call_args_list[0].args[0].endswith("/agent/tasks/result"))
        self.assertEqual(AgentPolicy(self.config).pending_delivery_payloads(), [])
        self.assertEqual(counter.read_text(), "1")

    def test_execution_stream_caps_fit_controller_contract(self):
        bad = json.loads(json.dumps(self.config)); bad["registered_tasks"]["demo-health-check"]["max_stdout_bytes"] = MAX_EXECUTION_STREAM_BYTES + 1
        with self.assertRaisesRegex(ValueError, "limit"): AgentPolicy(bad)

    def test_configured_maximum_output_is_accepted(self):
        self.config["registered_tasks"]["demo-health-check"].update({"argv": [sys.executable, "-c", f"print('x' * {MAX_EXECUTION_STREAM_BYTES - 1})"], "max_stdout_bytes": MAX_EXECUTION_STREAM_BYTES})
        result = AgentPolicy(self.config).execute("run_registered_task", {"task_id": "demo-health-check"}, "maximum-output")
        self.assertEqual(result["state"], "succeeded")
        self.assertFalse(result["stdout_truncated"])
        self.assertEqual(result["stdout_bytes"], MAX_EXECUTION_STREAM_BYTES)


class ExecutorTaskPolicyTests(unittest.TestCase):
    """Synthetic acceptance coverage for the fixed patch-and-test profile only."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "allowed"; self.root.mkdir()
        self.repo = self.root / "synthetic-repo"; self.repo.mkdir()
        for command in (["git", "init", "-q", str(self.repo)], ["git", "-C", str(self.repo), "config", "user.email", "example@example.invalid"], ["git", "-C", str(self.repo), "config", "user.name", "RelayMe Test"]):
            __import__("subprocess").run(command, check=True)
        (self.repo / "app.txt").write_text("before\n")
        __import__("subprocess").run(["git", "-C", str(self.repo), "add", "app.txt"], check=True)
        __import__("subprocess").run(["git", "-C", str(self.repo), "commit", "-qm", "synthetic base"], check=True)
        self.worktrees = self.root / "worktrees"; self.worktrees.mkdir()
        self.outputs = self.root / "outputs"; self.outputs.mkdir()
        self.launcher = self.root / "fake-podman"
        self.launcher.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == 'rm': raise SystemExit(0)
mounts = [args[index + 1] for index, item in enumerate(args[:-1]) if item == '-v']
worktree = Path(next(item.split(':', 1)[0] for item in mounts if ':/workspace:' in item))
output = Path(next(item.split(':', 1)[0] for item in mounts if ':/output:' in item))
(worktree / 'app.txt').write_text('after\\n')
(output / 'result.json').write_text(json.dumps({'summary': 'synthetic patch applied', 'tests': [{'name': 'synthetic', 'status': 'passed'}]}))
(output / 'test.log').write_text('synthetic test passed\\n')
(output / 'analysis.md').write_text('synthetic analysis\\n')
print(json.dumps({'argv': args}))
""")
        self.launcher.chmod(0o755)
        self.config = {
            "allowed_roots": [str(self.root)], "repos": {"synthetic-repo": {"path": str(self.repo)}}, "services": [],
            "execution_state_file": str(Path(self.temp.name) / "executor-state.json"),
            "executor_profiles": {"research-patch-test": {
                "description": "Synthetic fixed patch-and-test profile", "repository_id": "synthetic-repo", "base_revision": "HEAD",
                "podman_executable": str(self.launcher), "image": "synthetic-image", "container_argv": ["/fixed-synthetic-launcher"],
                "disposable_worktree_root": str(self.worktrees), "task_output_root": str(self.outputs),
                "credential_profile_id": "none", "network_policy_id": "none", "environment": {"PATH": os.environ["PATH"]},
                "timeout_seconds": 10, "max_transcript_bytes": 4096, "max_result_bytes": 4096, "max_artifact_bytes": 4096,
                "max_concurrent": 1, "cooldown_seconds": 0, "allowed_task_spec_ids": ["patch-and-test"],
                "pids_limit": 32, "memory_limit": "128m", "cpus": 0.5
            }}
        }

    def tearDown(self): self.temp.cleanup()

    def test_synthetic_executor_is_fixed_isolated_and_reviewable(self):
        policy = AgentPolicy(self.config)
        execution_id = "11111111-1111-4111-8111-111111111111"
        result = policy.execute("start_registered_executor_task", {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "Change the synthetic fixture only."}, execution_id)
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual((self.repo / "app.txt").read_text(), "before\n")
        self.assertFalse((self.worktrees / execution_id).exists())
        self.assertTrue((self.outputs / execution_id / "patch.diff").is_file())
        self.assertFalse((self.outputs / execution_id / "brief.txt").exists())
        self.assertEqual({item["name"] for item in result["artifacts"]}, {"patch.diff", "result.json", "test.log", "analysis.md"})
        invocation = json.loads(result["stdout"])["argv"]
        self.assertIn("--network", invocation); self.assertEqual(invocation[invocation.index("--network") + 1], "none")
        self.assertIn("--read-only", invocation); self.assertIn("--pids-limit", invocation); self.assertIn("--memory", invocation); self.assertIn("--cpus", invocation)
        self.assertNotIn("Change the synthetic fixture only.", result["stdout"])
        self.assertEqual(policy.execute("start_registered_executor_task", {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "different text is ignored after delivery"}, execution_id), result)
        resources = policy.resources()["executor_profiles"]
        self.assertEqual(resources, [{"executor_profile_id": "research-patch-test", "description": "Synthetic fixed patch-and-test profile", "repository_id": "synthetic-repo", "allowed_task_spec_ids": ["patch-and-test"]}])
        self.assertNotIn(str(self.launcher), json.dumps(resources))

    def test_executor_rejects_client_control_and_unavailable_profile(self):
        policy = AgentPolicy(self.config)
        execution_id = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(PolicyError, "invalid"):
            policy.execute("start_registered_executor_task", {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "x", "argv": ["/bin/sh"]}, execution_id)
        with self.assertRaisesRegex(PolicyError, "not registered"):
            policy.execute("start_registered_executor_task", {"executor_profile_id": "unknown", "task_spec_id": "patch-and-test", "brief": "x"}, execution_id)
        with self.assertRaisesRegex(PolicyError, "invalid"):
            policy.execute("start_registered_executor_task", {"executor_profile_id": "research-patch-test", "task_spec_id": "anything-else", "brief": "x"}, execution_id)
        with patch("relayme.agent.os.geteuid", return_value=0), self.assertRaisesRegex(PolicyError, "root"):
            policy.execute("start_registered_executor_task", {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "x"}, execution_id)

    def test_opaque_credential_profile_is_local_only_and_cannot_use_allowed_root(self):
        invalid = json.loads(json.dumps(self.config))
        invalid["executor_profiles"]["research-patch-test"]["credential_profile_id"] = "synthetic-secret"
        invalid["executor_credential_profiles"] = {"synthetic-secret": {"secret_file": str(self.root / "not-secret"), "mount_path": "/run/secrets/value", "environment_variable": "SYNTHETIC_TOKEN"}}
        (self.root / "not-secret").write_text("not-a-real-secret"); (self.root / "not-secret").chmod(0o600)
        with self.assertRaisesRegex(ValueError, "not protected"): AgentPolicy(invalid)


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

    def enroll(self, hostname="example-host", metadata=None):
        enrollment = self.call("POST", "/v1/admin/enrollment-tokens", {"ttl_seconds": 60})["token"]
        return self.call("POST", "/v1/agents/enroll", {"enrollment_token": enrollment, "hostname": hostname, "metadata": metadata or {}})
    def test_auth_task_delivery_audit_and_duplicate_prevention(self):
        agent = self.enroll()
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
        agent = self.enroll()
        token = self.store.create_client("limited", {"hosts": [agent["host_id"]], "capabilities": ["host_status"]}, None)
        with self.assertRaises(HTTPError) as client:
            self.call("POST", "/v1/tasks", {"host": agent["host_id"], "capability": "process_list", "arguments": {}}, token)
        self.assertEqual(client.exception.code, 403)
        client.exception.close()

    def test_r1_requires_exact_task_scope_and_idempotency_reuses_execution(self):
        agent = self.enroll()
        missing = self.store.create_client("missing", {"hosts": [agent["host_id"]], "capabilities": ["host_status"]}, None)
        with self.assertRaises(HTTPError) as denied:
            self.call("POST", "/v1/tasks", {"host": agent["host_id"], "capability": "run_registered_task", "arguments": {"task_id": "demo-health-check"}}, missing)
        self.assertEqual(denied.exception.code, 403); denied.exception.close()
        token = self.store.create_client("runner", {"hosts": [agent["host_id"]], "capabilities": ["run_registered_task:demo-health-check"]}, None)
        body = {"host": agent["host_id"], "capability": "run_registered_task", "arguments": {"task_id": "demo-health-check", "idempotency_key": "opaque-key"}}
        first = self.call("POST", "/v1/tasks", body, token)["task_id"]
        self.assertEqual(self.call("POST", "/v1/tasks", body, token)["task_id"], first)
        delivered = self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token=agent["credential"], agent=agent["host_id"])["task"]
        self.assertEqual(delivered["task_id"], first)
        self.assertIsNone(self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token=agent["credential"], agent=agent["host_id"])["task"])
        result = {"execution_id": first, "task_id": "demo-health-check", "task_identity": "policy-hash", "state": "succeeded", "exit_code": 0, "duration_ms": 1, "timeout_seconds": 2, "stdout": "ok", "stderr": "", "stdout_bytes": 2, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False, "stdout_hash": "a", "stderr_hash": "b", "terminating_signal": None}
        self.call("POST", "/v1/agent/tasks/result", {"task_id": first, "status": "SUCCEEDED", "result": result}, token=agent["credential"], agent=agent["host_id"])
        audit = self.store.db.execute("SELECT task_id, idempotency_key_hash, stdout_hash FROM execution_audit WHERE execution_id=?", (first,)).fetchone()
        self.assertEqual((audit["task_id"], audit["stdout_hash"]), ("demo-health-check", "a")); self.assertNotEqual(audit["idempotency_key_hash"], "opaque-key")

    def test_r1_rejects_client_controlled_execution_fields(self):
        agent = self.enroll()
        token = self.store.create_client("runner", {"hosts": [agent["host_id"]], "capabilities": ["run_registered_task:demo-health-check"]}, None)
        with self.assertRaises(HTTPError) as rejected:
            self.call("POST", "/v1/tasks", {"host": agent["host_id"], "capability": "run_registered_task", "arguments": {"task_id": "demo-health-check", "argv": ["/bin/sh"]}}, token)
        self.assertEqual(rejected.exception.code, 400); rejected.exception.close()

    def test_r1_concurrent_idempotency_is_atomic_and_database_backed(self):
        agent = self.enroll()
        barrier = threading.Barrier(8)
        def create() -> str:
            barrier.wait()
            return self.store.create_task("runner", agent["host_id"], "run_registered_task", {"task_id": "demo-health-check", "idempotency_key": "same-key"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            task_ids = list(pool.map(lambda _: create(), range(8)))
        self.assertEqual(len(set(task_ids)), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE capability='run_registered_task'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM r1_idempotency").fetchone()[0], 1)

    def test_r1_replay_retains_result_and_duplicate_delivery_is_harmless(self):
        agent = self.enroll()
        task_id = self.store.create_task("runner", agent["host_id"], "run_registered_task", {"task_id": "demo-health-check", "idempotency_key": "replay-key"})
        self.assertIsNotNone(self.store.next_task(agent["host_id"], 0))
        result = {"execution_id": task_id, "task_id": "demo-health-check", "task_identity": "policy", "state": "succeeded", "exit_code": 0, "duration_ms": 1, "timeout_seconds": 2, "stdout": "ok", "stderr": "", "stdout_bytes": 2, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False, "stdout_hash": "a", "stderr_hash": "b", "terminating_signal": None}
        self.assertTrue(self.store.finish_task(agent["host_id"], task_id, "SUCCEEDED", result, None))
        self.assertEqual(self.store.task(task_id)["result"], result)
        self.assertEqual(self.store.task(task_id)["result"], result)
        self.assertTrue(self.store.finish_task(agent["host_id"], task_id, "SUCCEEDED", result, None))
        self.assertEqual(self.store.create_task("runner", agent["host_id"], "run_registered_task", {"task_id": "demo-health-check", "idempotency_key": "replay-key"}), task_id)

    def test_expired_r1_result_returns_stable_expired_state(self):
        agent = self.enroll()
        task_id = self.store.create_task("runner", agent["host_id"], "run_registered_task", {"task_id": "demo-health-check"})
        self.assertIsNotNone(self.store.next_task(agent["host_id"], 0))
        result = {"execution_id": task_id, "task_id": "demo-health-check", "state": "succeeded", "stdout": "ok", "stderr": "", "stdout_bytes": 2, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False}
        self.assertTrue(self.store.finish_task(agent["host_id"], task_id, "SUCCEEDED", result, None))
        self.store.db.execute("UPDATE r1_results SET expires_at=0 WHERE execution_id=?", (task_id,)); self.store.db.commit()
        observed = self.store.task(task_id)
        self.assertTrue(observed["result_expired"])
        self.assertEqual(observed["result"]["state"], "result_expired")

    def test_running_r1_task_is_not_redelivered_after_controller_restart(self):
        agent = self.enroll()
        task_id = self.store.create_task("runner", agent["host_id"], "run_registered_task", {"task_id": "demo-health-check"})
        self.assertEqual(self.store.next_task(agent["host_id"], 0)["task_id"], task_id)
        database = self.store.db.execute("PRAGMA database_list").fetchone()[2]
        restarted = Store(database)
        try: self.assertIsNone(restarted.next_task(agent["host_id"], 0))
        finally: restarted.db.close()

    def test_oversize_r1_result_is_audited_with_structured_result(self):
        agent = self.enroll()
        task_id = self.store.create_task("runner", agent["host_id"], "run_registered_task", {"task_id": "demo-health-check"})
        self.assertIsNotNone(self.store.next_task(agent["host_id"], 0))
        result = {"execution_id": task_id, "task_id": "demo-health-check", "state": "succeeded", "stdout": "x" * 1_000_000, "stderr": "", "stdout_bytes": 1_000_000, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False}
        self.assertTrue(self.store.finish_task(agent["host_id"], task_id, "SUCCEEDED", result, None))
        observed = self.store.task(task_id)
        self.assertEqual(observed["result"]["state"], "result_rejected_oversize")
        self.assertEqual(observed["error"]["code"], "result_too_large")
        self.assertIsNotNone(self.store.db.execute("SELECT execution_id FROM execution_audit WHERE execution_id=?", (task_id,)).fetchone())

    def test_remote_client_token_creation_is_not_exposed(self):
        with self.assertRaises(HTTPError) as client:
            self.call("POST", "/v1/admin/client-tokens", {"identity": "remote", "scopes": {"hosts": ["*"], "capabilities": ["list_hosts"]}})
        self.assertEqual(client.exception.code, 404)
        client.exception.close()

    def test_list_host_resources_exposes_only_registered_metadata(self):
        agent = self.enroll(metadata={"resources": {"services": ["example.service"], "repositories": ["example-app"], "allowed_file_roots": ["/srv/example-app"]}, "secret": "not-a-resource"})
        self.call("POST", "/v1/agent/heartbeat", {"metadata": {"agent_version": "0.2.0", "resources": {"services": ["example.service"], "repositories": ["example-app"], "allowed_file_roots": ["/srv/example-app"]}}}, token=agent["credential"], agent=agent["host_id"])
        token = self.store.create_client("reader", {"hosts": [agent["host_id"]], "capabilities": ["list_hosts", "list_host_resources"]}, None)
        resources = self.call("GET", "/v1/hosts/" + agent["host_id"] + "/resources", token=token)["resources"]
        self.assertEqual(resources, {"host_id": agent["host_id"], "hostname": "example-host", "services": ["example.service"], "repositories": ["example-app"], "allowed_file_roots": ["/srv/example-app"], "registered_tasks": [], "executor_profiles": []})
        self.assertNotIn("resources", self.call("GET", "/v1/hosts", token=token)["hosts"][0]["metadata"])

    def test_hostname_and_host_id_resolve_immediately_for_tasks(self):
        agent = self.enroll()
        token = self.store.create_client("reader", {"hosts": [agent["host_id"]], "capabilities": ["host_status"]}, None)
        created = self.call("POST", "/v1/tasks", {"host": "example-host", "capability": "host_status", "arguments": {}}, token)["task_id"]
        delivered = self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token=agent["credential"], agent=agent["host_id"])["task"]
        self.assertEqual(delivered["task_id"], created)
        self.call("POST", "/v1/agent/tasks/result", {"task_id": created, "status": "SUCCEEDED", "result": {}}, token=agent["credential"], agent=agent["host_id"])
        self.assertEqual(self.call("POST", "/v1/tasks", {"host": agent["host_id"], "capability": "host_status", "arguments": {}}, token)["status"], "QUEUED")

    def test_unknown_and_ambiguous_host_identifiers_fail_explicitly(self):
        token = self.store.create_client("reader", {"hosts": ["*"], "capabilities": ["host_status"]}, None)
        with self.assertRaises(HTTPError) as unknown:
            self.call("POST", "/v1/tasks", {"host": "missing", "capability": "host_status", "arguments": {}}, token)
        self.assertEqual(unknown.exception.code, 404)
        self.assertEqual(json.loads(unknown.exception.read())["error"]["code"], "unknown_host")
        unknown.exception.close()
        self.enroll("duplicate"); self.enroll("duplicate")
        with self.assertRaises(HTTPError) as ambiguous:
            self.call("POST", "/v1/tasks", {"host": "duplicate", "capability": "host_status", "arguments": {}}, token)
        self.assertEqual(ambiguous.exception.code, 409)
        self.assertEqual(json.loads(ambiguous.exception.read())["error"]["code"], "ambiguous_hostname")
        ambiguous.exception.close()

    def test_agent_policy_errors_are_structured_in_task_results(self):
        agent = self.enroll()
        task = self.store.create_task("reader", agent["host_id"], "read_file", {"path": "/etc/shadow"})
        self.assertIsNotNone(self.store.next_task(agent["host_id"], 0))
        self.assertTrue(self.store.finish_task(agent["host_id"], task, "FAILED", None, {"code": "file_outside_allowed_roots", "message": "path is outside allowed roots"}))
        self.assertEqual(self.store.task(task)["error"], {"code": "file_outside_allowed_roots", "message": "path is outside allowed roots"})

    def test_executor_requires_exact_scope_and_retains_reviewable_result(self):
        agent = self.enroll(metadata={"resources": {"executor_profiles": [{"executor_profile_id": "research-patch-test", "description": "synthetic", "repository_id": "synthetic-repo", "allowed_task_spec_ids": ["patch-and-test"]}]}})
        denied = self.store.create_client("reader", {"hosts": [agent["host_id"]], "capabilities": ["run_registered_task:research-patch-test"]}, None)
        body = {"host": agent["host_id"], "capability": "start_registered_executor_task", "arguments": {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "Synthetic change only.", "idempotency_key": "stable-key"}}
        with self.assertRaises(HTTPError) as rejected: self.call("POST", "/v1/tasks", body, denied)
        self.assertEqual(rejected.exception.code, 403); rejected.exception.close()
        token = self.store.create_client("executor", {"hosts": [agent["host_id"]], "capabilities": ["run_executor_task:research-patch-test"]}, None)
        first = self.call("POST", "/v1/tasks", body, token)["task_id"]
        self.assertEqual(self.call("POST", "/v1/tasks", body, token)["task_id"], first)
        delivered = self.call("GET", "/v1/agent/tasks/next?wait_seconds=0", token=agent["credential"], agent=agent["host_id"])["task"]
        self.assertEqual(delivered["task_id"], first)
        result = {"execution_id": first, "task_id": first, "task_identity": "profile-hash", "state": "SUCCEEDED", "exit_code": 0, "duration_ms": 5, "timeout_seconds": 10, "stdout": "", "stderr": "", "stdout_bytes": 0, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False, "stdout_hash": "a", "stderr_hash": "b", "terminating_signal": None, "executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "reviewable": True, "artifacts": [{"name": "patch.diff", "sha256": "c"}]}
        self.assertTrue(self.store.finish_task(agent["host_id"], first, "SUCCEEDED", result, None))
        self.assertEqual(self.call("GET", "/v1/tasks/" + first + "/result", token)["result"], result)
        reviewable = self.call("GET", "/v1/reviewable-tasks", token)["tasks"]
        self.assertEqual(reviewable[0]["task_id"], first)
        self.assertEqual(reviewable[0]["executor_profile_id"], "research-patch-test")

    def test_executor_rejects_any_client_selected_execution_surface(self):
        agent = self.enroll()
        token = self.store.create_client("executor", {"hosts": [agent["host_id"]], "capabilities": ["run_executor_task:research-patch-test"]}, None)
        body = {"host": agent["host_id"], "capability": "start_registered_executor_task", "arguments": {"executor_profile_id": "research-patch-test", "task_spec_id": "patch-and-test", "brief": "x", "environment": {"SECRET": "x"}}}
        with self.assertRaises(HTTPError) as rejected: self.call("POST", "/v1/tasks", body, token)
        self.assertEqual(rejected.exception.code, 400); rejected.exception.close()


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


class ControllerBootstrapTokenTests(unittest.TestCase):
    def test_loads_bootstrap_token_from_protected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "bootstrap.token"
            token_file.write_text("local-only-token\n")
            self.assertEqual(controller.load_bootstrap_token(None, str(token_file)), "local-only-token")

    def test_requires_exactly_one_bootstrap_token_source(self):
        with self.assertRaises(ValueError): controller.load_bootstrap_token(None, None)
        with self.assertRaises(ValueError): controller.load_bootstrap_token("inline", "protected-file")


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
