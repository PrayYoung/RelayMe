"""Outbound-polling Host Agent with the v0.1 local read-only policy."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import stat
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .controller import CAPABILITIES

MAX_FILE_BYTES = 1_000_000
MAX_LOG_BYTES = 1_000_000
MAX_DIFF_BYTES = 1_000_000
INITIAL_RETRY_SECONDS = 1
MAX_RETRY_SECONDS = 30


class PolicyError(Exception): pass
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

    def read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested = arguments.get("path")
        if not isinstance(requested, str): raise PolicyError("path is required")
        try: path = Path(requested).resolve(strict=True)
        except (OSError, RuntimeError): raise PolicyError("path does not exist or cannot be resolved")
        if not any(path.is_relative_to(root) for root in self.roots): raise PolicyError("path is outside allowed roots")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode): raise PolicyError("path is not a regular file")
        size = path.stat().st_size
        if size > self.max_file: raise PolicyError("file exceeds maximum allowed size")
        return {"path": str(path), **bounded(path.read_bytes(), self.max_file)}

    def read_logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        service = arguments.get("service")
        if service not in self.services: raise PolicyError("service is not registered")
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
        if repo not in self.repos: raise PolicyError("repository is not registered")
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

    def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if capability not in CAPABILITIES - {"list_hosts"}: raise PolicyError("capability is not authorized for agents")
        if capability == "host_status": return self.host_status()
        if capability == "process_list": return self.process_list()
        if capability == "read_file": return self.read_file(arguments)
        if capability == "read_logs": return self.read_logs(arguments)
        if capability == "git_diff": return self.git_diff(arguments)
        raise PolicyError("unsupported capability")


def request(url: str, method: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=35) as response: return json.loads(response.read())
    except HTTPError as exc:
        if exc.code >= 500: raise ControllerUnavailable(f"controller returned {exc.code}")
        raise RuntimeError(f"controller returned {exc.code}: {exc.read().decode()}")
    except (URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
        raise ControllerUnavailable(f"controller unavailable: {exc}") from exc


def credentials(config: dict[str, Any]) -> dict[str, str]:
    path = Path(config.get("agent_credential_file", "agent-credential.json"))
    if path.exists(): return json.loads(path.read_text())
    enrolled = request(config["controller_url"] + "/v1/agents/enroll", "POST", {"enrollment_token": config["enrollment_token"], "hostname": config.get("hostname", platform.node()), "metadata": {"agent_version": __version__}})
    path.write_text(json.dumps(enrolled)); os.chmod(path, 0o600)
    return enrolled


def run(config: dict[str, Any]) -> None:
    policy, cred, url = AgentPolicy(config), credentials(config), config["controller_url"].rstrip("/")
    headers = {"Authorization": "Bearer " + cred["credential"], "X-RelayMe-Host": cred["host_id"]}
    last_heartbeat = 0.0
    retry_seconds = INITIAL_RETRY_SECONDS
    pending_result: dict[str, Any] | None = None
    while True:
        try:
            if pending_result:
                request(url + "/v1/agent/tasks/result", "POST", pending_result, headers)
                pending_result = None
            if time.monotonic() - last_heartbeat >= 30:
                request(url + "/v1/agent/heartbeat", "POST", {"metadata": {"agent_version": __version__}}, headers); last_heartbeat = time.monotonic()
            payload = request(url + "/v1/agent/tasks/next?wait_seconds=20", "GET", headers=headers)
            retry_seconds = INITIAL_RETRY_SECONDS
        except ControllerUnavailable:
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, MAX_RETRY_SECONDS)
            continue
        task = payload.get("task")
        if not task: continue
        try: result, status, error = policy.execute(task["capability"], task["arguments"]), "SUCCEEDED", None
        except Exception as exc: result, status, error = None, "FAILED", str(exc)[:4096]
        pending_result = {"task_id": task["task_id"], "status": status, "result": result, "error": error}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True); args = parser.parse_args()
    run(json.loads(Path(args.config).read_text()))

if __name__ == "__main__": main()
