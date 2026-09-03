"""Outbound-polling Host Agent with R0 policy and bounded registered R1 tasks."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import ssl
import stat
import subprocess
import signal
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .controller import CAPABILITIES, EXECUTOR_CAPABILITY, R1_CAPABILITY

MAX_FILE_BYTES = 1_000_000
MAX_LOG_BYTES = 1_000_000
MAX_DIFF_BYTES = 1_000_000
# Two streams capped at 64 KiB each remain safely below the Controller's 1 MiB
# serialized-result limit even when invalid UTF-8 expands during JSON encoding.
MAX_EXECUTION_STREAM_BYTES = 64 * 1024
INITIAL_RETRY_SECONDS = 1
MAX_RETRY_SECONDS = 30


class PolicyError(Exception):
    def __init__(self, message: str, code: str = "policy_rejected"):
        super().__init__(message); self.code = code
class ControllerUnavailable(Exception): pass


def bounded(data: bytes, maximum: int) -> dict[str, Any]:
    return {"text": data[:maximum].decode("utf-8", "replace"), "truncated": len(data) > maximum, "bytes_available": len(data)}


class AgentPolicy:
    def __init__(self, config: dict[str, Any]):
        self.roots = [Path(p).resolve(strict=True) for p in config.get("allowed_roots", [])]
        self.repos = {key: Path(value["path"]).resolve(strict=True) for key, value in config.get("repos", {}).items()}
        self.services = set(config.get("services", [])); self.filesystems = list(config.get("filesystems", ["/"]))
        self.allow_command_line = bool(config.get("allow_command_line", False))
        self.max_file = min(int(config.get("max_file_bytes", MAX_FILE_BYTES)), MAX_FILE_BYTES)
        self.max_logs = min(int(config.get("max_log_bytes", MAX_LOG_BYTES)), MAX_LOG_BYTES)
        self.tasks = self._load_tasks(config.get("registered_tasks", {}))
        self.executor_credentials = self._load_executor_credentials(config.get("executor_credential_profiles", {}))
        self.executor_networks = self._load_executor_networks(config.get("executor_network_profiles", {}))
        self.executor_profiles = self._load_executor_profiles(config.get("executor_profiles", {}))
        self._execution_lock = threading.Lock()
        self._last_execution: dict[str, float] = {}
        self.execution_state_file = Path(config.get("execution_state_file", "agent-execution-state.json"))
        self.execution_retention_seconds = int(config.get("execution_result_retention_seconds", 3600))
        if not 60 <= self.execution_retention_seconds <= 86400: raise ValueError("invalid execution result retention")
        self._execution_expiry: dict[str, int] = {}
        self._pending_delivery: dict[str, dict[str, Any]] = {}
        self._completed_executions: dict[str, dict[str, Any]] = self._load_execution_state() if (self.tasks or self.executor_profiles) else {}

    def _load_execution_state(self) -> dict[str, dict[str, Any]]:
        try: raw = json.loads(self.execution_state_file.read_text())
        except FileNotFoundError: return {}
        except (OSError, json.JSONDecodeError): return {}
        current = time.time()
        completed = {key: value["result"] for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict) and value.get("expires_at", 0) > current and isinstance(value.get("result"), dict)}
        self._execution_expiry = {key: int(value["expires_at"]) for key, value in raw.items() if key in completed}
        self._pending_delivery = {key: completed[key] for key, value in raw.items() if key in completed and not value.get("delivered", False)}
        self._persist_execution_state(completed)
        return completed

    def _persist_execution_state(self, completed: dict[str, dict[str, Any]]) -> None:
        if not (self.tasks or self.executor_profiles): return
        current = int(time.time())
        expired = [key for key in completed if self._execution_expiry.get(key, 0) <= current]
        for key in expired:
            completed.pop(key, None); self._execution_expiry.pop(key, None); self._pending_delivery.pop(key, None)
        payload = {key: {"expires_at": self._execution_expiry.get(key, current + self.execution_retention_seconds), "result": value, "delivered": key not in self._pending_delivery} for key, value in completed.items()}
        temporary = self.execution_state_file.with_suffix(self.execution_state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")))
        os.chmod(temporary, 0o600); temporary.replace(self.execution_state_file)

    def _load_tasks(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict): raise ValueError("registered_tasks must be an object")
        tasks: dict[str, dict[str, Any]] = {}
        for task_id, value in raw.items():
            if not isinstance(task_id, str) or not task_id or not isinstance(value, dict): raise ValueError("invalid registered task")
            required = {"description", "executable", "argv", "working_directory", "environment", "timeout_seconds", "max_stdout_bytes", "max_stderr_bytes", "max_concurrent", "cooldown_seconds", "idempotent", "run_as"}
            if set(value) != required: raise ValueError("registered task has unsupported or missing policy fields")
            executable = Path(value["executable"])
            cwd = Path(value["working_directory"])
            if not executable.is_absolute() or not cwd.is_absolute() or not isinstance(value["argv"], list) or not value["argv"] or value["argv"][0] != str(executable) or not all(isinstance(arg, str) for arg in value["argv"]): raise ValueError("registered task argv must be fixed and absolute")
            try: canonical_cwd = cwd.resolve(strict=True)
            except OSError as exc: raise ValueError("registered task working directory must exist") from exc
            if not any(canonical_cwd.is_relative_to(root) for root in self.roots): raise ValueError("registered task working directory is outside allowed roots")
            if not isinstance(value["description"], str) or not value["description"] or not isinstance(value["environment"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value["environment"].items()): raise ValueError("invalid registered task metadata")
            if value["run_as"] != "agent" or value["idempotent"] is not True or value["max_concurrent"] != 1: raise ValueError("registered tasks must be idempotent, single-concurrent, and run as agent")
            for name, maximum, minimum in (("timeout_seconds", 3600, 1), ("max_stdout_bytes", MAX_EXECUTION_STREAM_BYTES, 1), ("max_stderr_bytes", MAX_EXECUTION_STREAM_BYTES, 1), ("cooldown_seconds", 86400, 0)):
                if not isinstance(value[name], int) or not minimum <= value[name] <= maximum: raise ValueError("invalid registered task limit")
            normalized = {**value, "working_directory": str(canonical_cwd), "task_identity": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}
            tasks[task_id] = normalized
        return tasks

    def _load_executor_credentials(self, raw: Any) -> dict[str, dict[str, str]]:
        """Load opaque, Agent-local secret mount declarations.

        These declarations deliberately reveal neither secret values nor provider
        semantics to the Controller.  The only supported transport is a single
        protected file mounted read-only at a fixed container path.
        """
        if not isinstance(raw, dict): raise ValueError("executor_credential_profiles must be an object")
        profiles: dict[str, dict[str, str]] = {}
        for profile_id, value in raw.items():
            if not isinstance(profile_id, str) or not profile_id or not isinstance(value, dict) or set(value) != {"secret_file", "mount_path", "environment_variable"}:
                raise ValueError("invalid executor credential profile")
            secret = Path(value["secret_file"])
            mount_path = value["mount_path"]
            variable = value["environment_variable"]
            if not secret.is_absolute() or not mount_path.startswith("/") or not isinstance(variable, str) or not variable or not variable.replace("_", "").isalnum():
                raise ValueError("invalid executor credential profile")
            try: secret_stat = secret.stat(); mode = stat.S_IMODE(secret_stat.st_mode)
            except OSError as exc: raise ValueError("executor credential file is unavailable") from exc
            if secret_stat.st_uid != os.geteuid() or mode & 0o077 or any(secret.resolve().is_relative_to(root) for root in self.roots):
                raise ValueError("executor credential file is not protected")
            profiles[profile_id] = {"secret_file": str(secret.resolve()), "mount_path": mount_path, "environment_variable": variable}
        return profiles

    def _load_executor_networks(self, raw: Any) -> dict[str, list[str]]:
        """Load fixed, deployment-local Podman arguments for a named egress bridge.

        The arguments are selected only by Agent policy.  They must mount one
        pre-existing Unix socket and set a fixed proxy environment variable; no
        client input reaches this layer and no provider hostname lives in RelayMe.
        """
        if not isinstance(raw, dict): raise ValueError("executor_network_profiles must be an object")
        profiles: dict[str, list[str]] = {}
        for profile_id, value in raw.items():
            if not isinstance(profile_id, str) or not profile_id or not isinstance(value, dict) or set(value) != {"proxy_socket", "container_socket", "proxy_url"}:
                raise ValueError("invalid executor network profile")
            socket = Path(value["proxy_socket"])
            if not socket.is_absolute() or not socket.exists() or not value["container_socket"].startswith("/") or not isinstance(value["proxy_url"], str) or not value["proxy_url"].startswith("http://"):
                raise ValueError("invalid executor network profile")
            # The local deployment bridge is responsible for provider allowlisting.
            profiles[profile_id] = ["-v", f"{socket.resolve()}:{value['container_socket']}:rw", "-e", f"HTTPS_PROXY={value['proxy_url']}", "-e", f"HTTP_PROXY={value['proxy_url']}"]
        return profiles

    def _load_executor_profiles(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict): raise ValueError("executor_profiles must be an object")
        profiles: dict[str, dict[str, Any]] = {}
        required = {"description", "repository_id", "base_revision", "podman_executable", "image", "container_argv", "disposable_worktree_root", "task_output_root", "credential_profile_id", "network_policy_id", "environment", "timeout_seconds", "max_transcript_bytes", "max_result_bytes", "max_artifact_bytes", "max_concurrent", "cooldown_seconds", "allowed_task_spec_ids", "pids_limit", "memory_limit", "cpus"}
        optional = {"user_namespace", "execution_identity"}
        for profile_id, value in raw.items():
            if not isinstance(profile_id, str) or not profile_id or not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional: raise ValueError("invalid executor profile")
            value = {**value, "user_namespace": value.get("user_namespace", "none"), "execution_identity": value.get("execution_identity", "image")}
            if value["repository_id"] not in self.repos or value["allowed_task_spec_ids"] != ["patch-and-test"]: raise ValueError("executor profile has unsupported repository or task spec")
            if not all(isinstance(value[key], str) and value[key] for key in ("description", "base_revision", "podman_executable", "image", "credential_profile_id", "network_policy_id", "memory_limit")): raise ValueError("invalid executor profile metadata")
            if not Path(value["podman_executable"]).is_absolute() or not isinstance(value["container_argv"], list) or not value["container_argv"] or not all(isinstance(part, str) and part for part in value["container_argv"]): raise ValueError("executor launcher must be fixed")
            if not isinstance(value["environment"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value["environment"].items()): raise ValueError("invalid executor environment")
            try:
                worktree_root, output_root = Path(value["disposable_worktree_root"]).resolve(strict=True), Path(value["task_output_root"]).resolve(strict=True)
            except OSError as exc: raise ValueError("executor roots must exist") from exc
            if not all(any(root.is_relative_to(allowed) for allowed in self.roots) for root in (worktree_root, output_root)): raise ValueError("executor roots must be within allowed roots")
            for key, low, high in (("timeout_seconds", 1, 3600), ("max_transcript_bytes", 1, MAX_EXECUTION_STREAM_BYTES), ("max_result_bytes", 256, 262144), ("max_artifact_bytes", 1, MAX_FILE_BYTES), ("max_concurrent", 1, 1), ("cooldown_seconds", 0, 86400), ("pids_limit", 1, 256)):
                if not isinstance(value[key], int) or not low <= value[key] <= high: raise ValueError("invalid executor limit")
            if not isinstance(value["cpus"], (int, float)) or not 0 < float(value["cpus"]) <= 8: raise ValueError("invalid executor cpu limit")
            if value["user_namespace"] not in {"none", "keep-id"}: raise ValueError("unsupported executor user namespace")
            if not isinstance(value["execution_identity"], str) or value["execution_identity"] not in {"image", "agent"}: raise ValueError("unsupported executor execution identity")
            if value["execution_identity"] == "agent" and value["user_namespace"] != "keep-id": raise ValueError("agent executor identity requires keep-id user namespace")
            if value["credential_profile_id"] != "none" and value["credential_profile_id"] not in self.executor_credentials: raise ValueError("executor credential profile is unavailable")
            if value["network_policy_id"] != "none" and value["network_policy_id"] not in self.executor_networks: raise ValueError("executor network profile is unavailable")
            profiles[profile_id] = {**value, "worktree_root": str(worktree_root), "output_root": str(output_root), "task_identity": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}
        return profiles

    def read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested = arguments.get("path")
        if not isinstance(requested, str): raise PolicyError("path is required")
        try: path = Path(requested).resolve(strict=True)
        except (OSError, RuntimeError): raise PolicyError("path does not exist or cannot be resolved")
        if not any(path.is_relative_to(root) for root in self.roots): raise PolicyError("path is outside allowed roots", "file_outside_allowed_roots")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode): raise PolicyError("path is not a regular file")
        size = path.stat().st_size
        if size > self.max_file: raise PolicyError("file exceeds maximum allowed size")
        return {"path": str(path), **bounded(path.read_bytes(), self.max_file)}

    def read_logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        service = arguments.get("service")
        if service not in self.services: raise PolicyError("service is not registered", "unregistered_service")
        max_bytes = min(int(arguments.get("max_bytes", self.max_logs)), self.max_logs)
        if max_bytes < 1: raise PolicyError("max_bytes must be positive")
        last_n, since = arguments.get("last_n"), arguments.get("since")
        if last_n is None and since is None: last_n = 100
        if last_n is not None:
            if not isinstance(last_n, int) or not 1 <= last_n <= 10_000: raise PolicyError("last_n must be between 1 and 10000")
        if since is not None and (not isinstance(since, str) or not 1 <= len(since) <= 64): raise PolicyError("invalid since selector")
        args = ["journalctl", "--no-pager", "--unit", service]
        if last_n is not None: args += ["--lines", str(last_n)]
        if since is not None: args += ["--since", since]
        try: run = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc: raise PolicyError(f"cannot read logs: {exc}")
        if run.returncode: raise PolicyError(bounded(run.stderr, 4096)["text"])
        return {"service": service, **bounded(run.stdout, max_bytes)}

    def git_diff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        repo = arguments.get("repo")
        if repo not in self.repos: raise PolicyError("repository is not registered", "unregistered_repository")
        try:
            run = subprocess.run(["git", "-C", str(self.repos[repo]), "diff", "--no-ext-diff", "--no-color"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc: raise PolicyError(f"cannot inspect git diff: {exc}")
        if run.returncode: raise PolicyError(bounded(run.stderr, 4096)["text"])
        return {"repo": repo, **bounded(run.stdout, MAX_DIFF_BYTES)}

    def host_status(self) -> dict[str, Any]:
        memory: dict[str, int] = {}
        try:
            pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE"); memory["total_bytes"] = pages * size
        except (ValueError, OSError): pass
        disks = []
        for filesystem in self.filesystems:
            try:
                usage = os.statvfs(filesystem); disks.append({"path": filesystem, "total_bytes": usage.f_blocks * usage.f_frsize, "available_bytes": usage.f_bavail * usage.f_frsize})
            except OSError: continue
        uptime = None
        try: uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
        except OSError: pass
        return {"hostname": platform.node(), "os": platform.platform(), "kernel": platform.release(), "uptime_seconds": uptime,
                "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [], "cpu_count": os.cpu_count(), "memory": memory,
                "disk_usage": disks, "agent_version": __version__}

    def process_list(self) -> dict[str, Any]:
        # `etimes` is a stable, single-token process start-age representation.
        fields = "pid,user,comm,%cpu,%mem,state,etimes"
        args = ["ps", "-eo", fields, "--no-headers"]
        if self.allow_command_line: args = ["ps", "-eo", fields + ",args", "--no-headers"]
        try: output = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError) as exc: raise PolicyError(f"cannot list processes: {exc}")
        items = []
        for line in output.splitlines()[:4096]:
            parts = line.split(None, 7 if self.allow_command_line else 6)
            if len(parts) < 7: continue
            item = {"pid": int(parts[0]), "user": parts[1], "name": parts[2], "cpu": parts[3], "memory": parts[4], "state": parts[5], "start_time": {"elapsed_seconds": int(parts[6])}}
            if self.allow_command_line and len(parts) > 7: item["command_line"] = parts[7][:512]
            items.append(item)
        return {"processes": items, "truncated": len(output.splitlines()) > 4096}

    def _capture(self, stream: Any, maximum: int, output: dict[str, Any], key: str) -> None:
        digest, count, data = hashlib.sha256(), 0, bytearray()
        while True:
            chunk = stream.read(8192)
            if not chunk: break
            digest.update(chunk); count += len(chunk)
            if len(data) < maximum: data.extend(chunk[:maximum - len(data)])
        output[key] = {"text": bytes(data).decode("utf-8", "replace"), "bytes": count, "truncated": count > maximum, "hash": digest.hexdigest()}

    def run_registered_task(self, arguments: dict[str, Any], execution_id: str) -> dict[str, Any]:
        if set(arguments) - {"task_id", "idempotency_key"} or not isinstance(arguments.get("task_id"), str): raise PolicyError("invalid registered-task request", "invalid_registered_task_request")
        task_id = arguments["task_id"]
        if task_id not in self.tasks: raise PolicyError("registered task is not available", "unregistered_task")
        if not execution_id: raise PolicyError("execution_id is required", "invalid_execution_id")
        if os.geteuid() == 0: raise PolicyError("registered tasks must not execute as root", "root_execution_forbidden")
        if execution_id in self._completed_executions: return self._completed_executions[execution_id]
        task = self.tasks[task_id]
        with self._execution_lock:
            if execution_id in self._completed_executions: return self._completed_executions[execution_id]
            last = self._last_execution.get(task_id, 0)
            if time.monotonic() - last < task["cooldown_seconds"]: raise PolicyError("registered task is in cooldown", "task_cooldown")
            self._last_execution[task_id] = time.monotonic()
            started = time.monotonic(); output: dict[str, Any] = {}
            try:
                process = subprocess.Popen(task["argv"], shell=False, cwd=task["working_directory"], env=dict(task["environment"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            except OSError as exc: raise PolicyError(f"cannot start registered task: {exc}", "task_start_failed") from exc
            assert process.stdout and process.stderr
            readers = [threading.Thread(target=self._capture, args=(process.stdout, task["max_stdout_bytes"], output, "stdout")), threading.Thread(target=self._capture, args=(process.stderr, task["max_stderr_bytes"], output, "stderr"))]
            for reader in readers: reader.start()
            state, signal_number = "succeeded", None
            try: process.wait(timeout=task["timeout_seconds"])
            except subprocess.TimeoutExpired:
                state = "timed_out"
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
            for reader in readers: reader.join(timeout=5)
            process.stdout.close(); process.stderr.close()
            exit_code = process.returncode if process.returncode is not None and process.returncode >= 0 else None
            if process.returncode is not None and process.returncode < 0: signal_number = -process.returncode
            if state != "timed_out" and (exit_code is None or exit_code != 0): state = "failed"
            result = {"execution_id": execution_id, "task_id": task_id, "task_identity": task["task_identity"], "state": state,
                      "exit_code": exit_code, "terminating_signal": signal_number, "duration_ms": int((time.monotonic() - started) * 1000), "timeout_seconds": task["timeout_seconds"],
                      "stdout": output["stdout"]["text"], "stderr": output["stderr"]["text"], "stdout_bytes": output["stdout"]["bytes"], "stderr_bytes": output["stderr"]["bytes"],
                      "stdout_truncated": output["stdout"]["truncated"], "stderr_truncated": output["stderr"]["truncated"], "stdout_hash": output["stdout"]["hash"], "stderr_hash": output["stderr"]["hash"]}
            self._completed_executions[execution_id] = result
            self._execution_expiry[execution_id] = int(time.time()) + self.execution_retention_seconds
            self._pending_delivery[execution_id] = result
            self._persist_execution_state(self._completed_executions)
            return result

    def _run_executor_process(self, profile: dict[str, Any], command: list[str], output: dict[str, Any]) -> tuple[str, int | None, int | None]:
        try:
            process = subprocess.Popen(command, shell=False, cwd=profile["worktree_root"], env=dict(profile["environment"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc: raise PolicyError(f"cannot start executor sandbox: {exc}", "executor_start_failed") from exc
        assert process.stdout and process.stderr
        readers = [threading.Thread(target=self._capture, args=(process.stdout, profile["max_transcript_bytes"], output, "stdout")), threading.Thread(target=self._capture, args=(process.stderr, profile["max_transcript_bytes"], output, "stderr"))]
        for reader in readers: reader.start()
        state, signal_number = "SUCCEEDED", None
        try: process.wait(timeout=profile["timeout_seconds"])
        except subprocess.TimeoutExpired:
            state = "TIMED_OUT"; os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait()
        for reader in readers: reader.join(timeout=5)
        process.stdout.close(); process.stderr.close()
        exit_code = process.returncode if process.returncode is not None and process.returncode >= 0 else None
        if process.returncode is not None and process.returncode < 0: signal_number = -process.returncode
        if state != "TIMED_OUT" and exit_code != 0: state = "FAILED"
        return state, exit_code, signal_number

    @staticmethod
    def _artifact(path: Path, artifact_type: str, maximum: int) -> dict[str, Any] | None:
        if not path.is_file() or path.stat().st_size > maximum: return None
        raw = path.read_bytes()
        return {"name": path.name, "type": artifact_type, "path": path.name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "mime_type": "application/json" if path.suffix == ".json" else "text/plain"}

    def start_registered_executor_task(self, arguments: dict[str, Any], execution_id: str) -> dict[str, Any]:
        allowed = {"executor_profile_id", "task_spec_id", "brief", "idempotency_key"}
        if set(arguments) - allowed or arguments.get("task_spec_id") != "patch-and-test" or not isinstance(arguments.get("executor_profile_id"), str) or not isinstance(arguments.get("brief"), str) or not 1 <= len(arguments["brief"]) <= 8192: raise PolicyError("invalid executor-task request", "invalid_executor_task_request")
        profile_id, brief = arguments["executor_profile_id"], arguments["brief"]
        if profile_id not in self.executor_profiles: raise PolicyError("executor profile is not registered", "unregistered_executor_profile")
        try:
            if str(uuid.UUID(execution_id)) != execution_id: raise ValueError
        except (ValueError, AttributeError): raise PolicyError("execution_id is invalid", "invalid_execution_id") from None
        if os.geteuid() == 0: raise PolicyError("executor tasks must not execute as root", "root_execution_forbidden")
        if execution_id in self._completed_executions: return self._completed_executions[execution_id]
        profile = self.executor_profiles[profile_id]
        with self._execution_lock:
            if execution_id in self._completed_executions: return self._completed_executions[execution_id]
            last = self._last_execution.get(profile_id, 0)
            if time.monotonic() - last < profile["cooldown_seconds"]: raise PolicyError("executor profile is in cooldown", "executor_cooldown")
            self._last_execution[profile_id] = time.monotonic()
            started, started_at, output = time.monotonic(), int(time.time()), {}
            repo = self.repos[profile["repository_id"]]
            worktree, task_output = Path(profile["worktree_root"]) / execution_id, Path(profile["output_root"]) / execution_id
            created, brief_path, name = False, task_output / "brief.txt", "relayme-executor-" + execution_id
            try:
                worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                task_output.mkdir(mode=0o700, parents=True, exist_ok=False)
                brief_path.write_text(brief, encoding="utf-8"); os.chmod(brief_path, 0o600)
                base = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", profile["base_revision"] + "^{commit}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
                if base.returncode: raise PolicyError("registered base revision cannot be resolved", "invalid_base_revision")
                base_revision = base.stdout.decode().strip()
                subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), base_revision], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60); created = True
                command = [profile["podman_executable"], "run", "--rm", "--name", name, "--network", "none", "--read-only", "--pids-limit", str(profile["pids_limit"]), "--memory", profile["memory_limit"], "--cpus", str(profile["cpus"]), "--workdir", "/workspace", "-v", str(worktree) + ":/workspace:rw", "-v", str(task_output) + ":/output:rw", "-v", str(brief_path) + ":/input/brief.txt:ro"]
                if profile["user_namespace"] == "keep-id": command.append("--userns=keep-id")
                if profile["execution_identity"] == "agent":
                    agent_uid, agent_gid = os.geteuid(), os.getegid()
                    if agent_uid == 0 or agent_gid == 0: raise PolicyError("executor tasks must not execute with a root agent identity", "root_execution_forbidden")
                    command.extend(["--user", f"{agent_uid}:{agent_gid}"])
                credential_id = profile["credential_profile_id"]
                if credential_id != "none":
                    credential = self.executor_credentials[credential_id]
                    command.extend(["-v", credential["secret_file"] + ":" + credential["mount_path"] + ":ro", "-e", credential["environment_variable"] + "_FILE=" + credential["mount_path"]])
                network_id = profile["network_policy_id"]
                if network_id != "none": command.extend(self.executor_networks[network_id])
                command.extend([profile["image"], *profile["container_argv"]])
                state, exit_code, signal_number = self._run_executor_process(profile, command, output)
                subprocess.run([profile["podman_executable"], "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=15)
                diff = subprocess.run(["git", "-C", str(worktree), "diff", "--no-ext-diff", "--no-color"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30).stdout[:profile["max_artifact_bytes"]]
                (task_output / "patch.diff").write_bytes(diff)
                changed = subprocess.run(["git", "-C", str(worktree), "diff", "--name-only"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30).stdout.decode("utf-8", "replace").splitlines()[:256]
                artifact_limits = {"patch.diff": profile["max_artifact_bytes"], "result.json": min(profile["max_result_bytes"], profile["max_artifact_bytes"]), "test.log": profile["max_artifact_bytes"], "analysis.md": profile["max_artifact_bytes"]}
                for child in task_output.iterdir():
                    if child.name == "brief.txt": continue
                    if child.name not in artifact_limits or not child.is_file() or child.stat().st_size > artifact_limits[child.name]:
                        if child.is_dir(): shutil.rmtree(child, ignore_errors=True)
                        else: child.unlink(missing_ok=True)
                artifacts = [item for item in (self._artifact(task_output / name, typ, profile["max_artifact_bytes"]) for name, typ in (("patch.diff", "patch"), ("result.json", "result"), ("test.log", "test-log"), ("analysis.md", "analysis"))) if item]
                summary, warnings, tests = "executor completed", [], []
                result_file = task_output / "result.json"
                if result_file.is_file() and result_file.stat().st_size <= profile["max_result_bytes"]:
                    try:
                        local_result = json.loads(result_file.read_text())
                        if isinstance(local_result, dict): summary = str(local_result.get("summary", summary))[:4096]; warnings = [str(x)[:512] for x in local_result.get("warnings", [])[:32] if isinstance(x, str)]; tests = local_result.get("tests", [])[:32] if isinstance(local_result.get("tests", []), list) else []
                    except (OSError, json.JSONDecodeError): warnings.append("invalid structured result")
                result = {"execution_id": execution_id, "task_id": execution_id, "state": state, "executor_profile_id": profile_id, "task_spec_id": "patch-and-test", "task_identity": profile["task_identity"], "base_revision": base_revision, "summary": summary, "changed_files": changed, "tests_run": tests, "warnings": warnings, "suggested_reviewer_evidence": [item["name"] for item in artifacts], "started_at": started_at, "finished_at": int(time.time()), "duration_ms": int((time.monotonic() - started) * 1000), "timeout_seconds": profile["timeout_seconds"], "exit_code": exit_code, "terminating_signal": signal_number, "stdout": output["stdout"]["text"], "stderr": output["stderr"]["text"], "stdout_bytes": output["stdout"]["bytes"], "stderr_bytes": output["stderr"]["bytes"], "stdout_truncated": output["stdout"]["truncated"], "stderr_truncated": output["stderr"]["truncated"], "stdout_hash": output["stdout"]["hash"], "stderr_hash": output["stderr"]["hash"], "transcript_truncated": output["stdout"]["truncated"] or output["stderr"]["truncated"], "artifacts": artifacts, "reviewable": state in {"SUCCEEDED", "FAILED", "TIMED_OUT"}}
            finally:
                brief_path.unlink(missing_ok=True)
                if created: subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60)
                shutil.rmtree(worktree, ignore_errors=True)
            self._completed_executions[execution_id] = result; self._execution_expiry[execution_id] = int(time.time()) + self.execution_retention_seconds; self._pending_delivery[execution_id] = result; self._persist_execution_state(self._completed_executions)
            return result

    def pending_delivery_payloads(self) -> list[dict[str, Any]]:
        current = int(time.time())
        if any(expiry <= current for expiry in self._execution_expiry.values()):
            self._persist_execution_state(self._completed_executions)
        return [{"task_id": execution_id, "status": "SUCCEEDED" if result.get("state") in {"succeeded", "SUCCEEDED"} else "FAILED", "result": result, "error": None}
                for execution_id, result in self._pending_delivery.items()]

    def mark_delivery_confirmed(self, execution_id: str) -> None:
        if execution_id in self._pending_delivery:
            del self._pending_delivery[execution_id]
            self._persist_execution_state(self._completed_executions)

    def execute(self, capability: str, arguments: dict[str, Any], execution_id: str | None = None) -> dict[str, Any]:
        if capability not in CAPABILITIES - {"list_hosts", "list_host_resources"}: raise PolicyError("capability is not authorized for agents")
        if capability == "host_status": return self.host_status()
        if capability == "process_list": return self.process_list()
        if capability == "read_file": return self.read_file(arguments)
        if capability == "read_logs": return self.read_logs(arguments)
        if capability == "git_diff": return self.git_diff(arguments)
        if capability == R1_CAPABILITY: return self.run_registered_task(arguments, execution_id or "")
        if capability == EXECUTOR_CAPABILITY: return self.start_registered_executor_task(arguments, execution_id or "")
        raise PolicyError("unsupported capability")

    def resources(self) -> dict[str, Any]:
        return {"services": sorted(self.services), "repositories": sorted(self.repos),
                "allowed_file_roots": [str(root) for root in self.roots],
                "registered_tasks": [{"task_id": key, "description": task["description"], "max_timeout_seconds": task["timeout_seconds"]} for key, task in sorted(self.tasks.items())],
                "executor_profiles": [{"executor_profile_id": key, "description": value["description"], "repository_id": value["repository_id"], "allowed_task_spec_ids": value["allowed_task_spec_ids"]} for key, value in sorted(self.executor_profiles.items())]}


def request(url: str, method: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None, ca_cert: str | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        context = ssl.create_default_context(cafile=ca_cert) if ca_cert else None
        with urlopen(req, timeout=35, context=context) as response: return json.loads(response.read())
    except HTTPError as exc:
        if exc.code >= 500: raise ControllerUnavailable(f"controller returned {exc.code}")
        raise RuntimeError(f"controller returned {exc.code}: {exc.read().decode()}")
    except (URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
        raise ControllerUnavailable(f"controller unavailable: {exc}") from exc


def credentials(config: dict[str, Any]) -> dict[str, str]:
    path = Path(config.get("agent_credential_file", "agent-credential.json"))
    if path.exists(): return json.loads(path.read_text())
    enrolled = request(config["controller_url"] + "/v1/agents/enroll", "POST", {"enrollment_token": config["enrollment_token"], "hostname": config.get("hostname", platform.node()), "metadata": {"agent_version": __version__}}, ca_cert=config.get("controller_ca_cert"))
    path.write_text(json.dumps(enrolled)); os.chmod(path, 0o600)
    return enrolled


def run(config: dict[str, Any]) -> None:
    policy, cred, url = AgentPolicy(config), credentials(config), config["controller_url"].rstrip("/")
    ca_cert = config.get("controller_ca_cert")
    headers = {"Authorization": "Bearer " + cred["credential"], "X-RelayMe-Host": cred["host_id"]}
    last_heartbeat = 0.0
    retry_seconds = INITIAL_RETRY_SECONDS
    pending_result: dict[str, Any] | None = None
    pending_r1 = policy.pending_delivery_payloads()
    while True:
        try:
            pending_r1 = policy.pending_delivery_payloads()
            if pending_result:
                request(url + "/v1/agent/tasks/result", "POST", pending_result, headers, ca_cert)
                pending_result = None
            if pending_r1:
                delivery = pending_r1[0]
                request(url + "/v1/agent/tasks/result", "POST", delivery, headers, ca_cert)
                policy.mark_delivery_confirmed(delivery["task_id"])
                pending_r1 = policy.pending_delivery_payloads()
            if time.monotonic() - last_heartbeat >= 30:
                request(url + "/v1/agent/heartbeat", "POST", {"metadata": {"agent_version": __version__, "resources": policy.resources()}}, headers, ca_cert); last_heartbeat = time.monotonic()
            payload = request(url + "/v1/agent/tasks/next?wait_seconds=20", "GET", headers=headers, ca_cert=ca_cert)
            retry_seconds = INITIAL_RETRY_SECONDS
        except ControllerUnavailable:
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, MAX_RETRY_SECONDS)
            continue
        task = payload.get("task")
        if not task: continue
        try:
            result = policy.execute(task["capability"], task["arguments"], task["task_id"]) if task["capability"] in {R1_CAPABILITY, EXECUTOR_CAPABILITY} else policy.execute(task["capability"], task["arguments"])
            if task["capability"] in {R1_CAPABILITY, EXECUTOR_CAPABILITY}:
                status, error = ("SUCCEEDED" if result.get("state") in {"succeeded", "SUCCEEDED"} else "FAILED"), None
            else:
                status, error = "SUCCEEDED", None
        except PolicyError as exc:
            if task["capability"] in {R1_CAPABILITY, EXECUTOR_CAPABILITY}:
                result, status = {"execution_id": task["task_id"], "task_id": task["arguments"].get("task_id", task["task_id"]), "state": "FAILED", "stdout": "", "stderr": "", "stdout_bytes": 0, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False}, "FAILED"
            else: result, status = None, "FAILED"
            error = {"code": exc.code, "message": str(exc)[:4096]}
        except Exception as exc: result, status, error = None, "FAILED", str(exc)[:4096]
        if task["capability"] in {R1_CAPABILITY, EXECUTOR_CAPABILITY} and isinstance(result, dict) and result.get("execution_id") == task["task_id"] and error is None:
            pending_r1 = policy.pending_delivery_payloads()
            if not any(delivery["task_id"] == task["task_id"] for delivery in pending_r1):
                pending_result = {"task_id": task["task_id"], "status": status, "result": result, "error": error}
        else:
            pending_result = {"task_id": task["task_id"], "status": status, "result": result, "error": error}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True); args = parser.parse_args()
    run(json.loads(Path(args.config).read_text()))

if __name__ == "__main__": main()
