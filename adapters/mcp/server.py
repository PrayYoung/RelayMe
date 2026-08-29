"""Standard stdio MCP adapter for RelayMe v0.3.

This module translates MCP tool calls into the existing RelayMe Controller HTTP/JSON
API. It never contacts managed hosts directly and leaves all authorization and host
policy decisions to RelayMe.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TOOL_NAMES = (
    "list_hosts",
    "list_host_resources",
    "host_status",
    "process_list",
    "read_logs",
    "read_file",
    "git_diff",
    "run_registered_task",
)

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_hosts": {"description": "List RelayMe hosts visible to this token. Call first when the host identity is unknown; host-scoped tools accept host_id or a unique hostname.", "parameters": {}},
    "list_host_resources": {"description": "List only registered services, repositories, and allowed file roots for one host. Call before resource-scoped tools when identifiers are unknown. Host accepts host_id or a unique hostname.", "parameters": {"host": str}},
    "host_status": {"description": "Read bounded host health and resource status. Host accepts host_id or a unique hostname.", "parameters": {"host": str}},
    "process_list": {"description": "List processes on one host. Read-only; host accepts host_id or a unique hostname.", "parameters": {"host": str}},
    "read_logs": {"description": "Read bounded logs for a service registered by the Host Agent. Call list_host_resources first if its identifier is unknown; host accepts host_id or unique hostname.", "parameters": {"host": str, "service": str, "last_n": (int, type(None)), "since": (str, type(None)), "max_bytes": (int, type(None))}},
    "read_file": {"description": "Read a file only within Agent-allowed roots. Call list_host_resources first if its allowed root is unknown; host accepts host_id or unique hostname.", "parameters": {"host": str, "path": str}},
    "git_diff": {"description": "Read the Git diff for a repository registered by the Host Agent. Call list_host_resources first if its identifier is unknown; host accepts host_id or unique hostname.", "parameters": {"host": str, "repo": str}},
    "run_registered_task": {"description": "Run one idempotent, bounded task registered locally by the Host Agent. Call list_host_resources first to discover valid task IDs. The task has fixed local command, environment, working directory, and limits; no arguments can be supplied.", "parameters": {"host": str, "task_id": str, "idempotency_key": (str, type(None))}},
}


class ConfigurationError(ValueError): pass


class ControllerError(RuntimeError):
    def __init__(self, status: int, message: Any):
        super().__init__(message); self.status, self.message = status, message


@dataclass(frozen=True)
class AdapterConfig:
    controller_url: str
    client_token: str
    ca_cert: str | None = None
    wait_seconds: int = 35

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AdapterConfig":
        values = os.environ if env is None else env
        url, token = values.get("RELAYME_URL"), values.get("RELAYME_TOKEN")
        if not url or not token:
            raise ConfigurationError("RELAYME_URL and RELAYME_TOKEN are required")
        try: wait = int(values.get("RELAYME_WAIT_SECONDS", "35"))
        except ValueError as exc: raise ConfigurationError("RELAYME_WAIT_SECONDS must be an integer") from exc
        if wait < 1 or wait > 300: raise ConfigurationError("RELAYME_WAIT_SECONDS must be between 1 and 300")
        return cls(url.rstrip("/"), token, values.get("RELAYME_CA_CERT") or None, wait)


class ControllerClient:
    """Minimal client for the existing Controller API; no host or policy logic."""
    def __init__(self, config: AdapterConfig, request_fn: Callable[[str, str, dict[str, Any] | None], dict[str, Any]] | None = None):
        self.config, self._request_fn = config, request_fn

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._request_fn: return self._request_fn(method, path, body)
        request = Request(self.config.controller_url + path, data=json.dumps(body).encode() if body is not None else None,
                          method=method, headers={"Authorization": "Bearer " + self.config.client_token, "Content-Type": "application/json"})
        try:
            context = ssl.create_default_context(cafile=self.config.ca_cert) if self.config.ca_cert else None
            with urlopen(request, timeout=self.config.wait_seconds, context=context) as response: return json.loads(response.read())
        except HTTPError as exc:
            try: message = json.loads(exc.read())["error"]
            except (json.JSONDecodeError, KeyError): message = str(exc)
            raise ControllerError(exc.code, message) from exc
        except URLError as exc: raise ControllerError(502, f"Controller connection failed: {exc.reason}") from exc

    def list_hosts(self) -> dict[str, Any]: return self.request("GET", "/v1/hosts")
    def list_host_resources(self, host: str) -> dict[str, Any]: return self.request("GET", "/v1/hosts/" + host + "/resources")

    def run_task(self, host: str, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        created = self.request("POST", "/v1/tasks", {"host": host, "capability": capability, "arguments": arguments})
        task_id, deadline = created["task_id"], time.monotonic() + self.config.wait_seconds
        while True:
            value = self.request("GET", "/v1/tasks/" + task_id)
            if value["task"]["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}: return value
            if time.monotonic() >= deadline: raise ControllerError(504, f"RelayMe task {task_id} did not finish within {self.config.wait_seconds} seconds")
            time.sleep(1)


class RelayMeMcpAdapter:
    def __init__(self, client: ControllerClient): self.client = client

    @staticmethod
    def _string(arguments: dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value: raise ValueError(f"{name} must be a non-empty string")
        return value

    def invoke(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool not in TOOL_NAMES: raise ValueError(f"unsupported RelayMe MCP tool: {tool}")
        arguments = arguments or {}
        if not isinstance(arguments, dict): raise ValueError("tool arguments must be an object")
        if tool == "list_hosts":
            if arguments: raise ValueError("list_hosts accepts no arguments")
            return self._result(self.client.list_hosts())
        host = self._string(arguments, "host")
        if tool == "list_host_resources": return self._result(self.client.list_host_resources(host))
        if tool == "run_registered_task":
            task_args = {"task_id": self._string(arguments, "task_id")}
            if arguments.get("idempotency_key") is not None: task_args["idempotency_key"] = self._string(arguments, "idempotency_key")
            if set(arguments) - {"host", "task_id", "idempotency_key"}: raise ValueError("run_registered_task accepts only host, task_id, and idempotency_key")
            return self._result(self.client.run_task(host, "run_registered_task", task_args))
        mapping = {
            "host_status": ("host_status", {}),
            "process_list": ("process_list", {}),
            "read_file": ("read_file", {"path": self._string(arguments, "path")} if tool == "read_file" else {}),
            "git_diff": ("git_diff", {"repo": self._string(arguments, "repo")} if tool == "git_diff" else {}),
        }
        if tool == "read_logs":
            task_args: dict[str, Any] = {"service": self._string(arguments, "service")}
            for name in ("last_n", "since", "max_bytes"):
                if name in arguments and arguments[name] is not None:
                    expected = str if name == "since" else int
                    if isinstance(arguments[name], bool) or not isinstance(arguments[name], expected): raise ValueError(f"{name} has an invalid type")
                    task_args[name] = arguments[name]
            return self._result(self.client.run_task(host, "read_logs", task_args))
        capability, task_args = mapping[tool]
        return self._result(self.client.run_task(host, capability, task_args))

    @staticmethod
    def _result(value: dict[str, Any]) -> dict[str, Any]: return value


def create_mcp_server(adapter: RelayMeMcpAdapter):
    """Create an MCP server lazily so translation logic remains directly testable."""
    try: from mcp.server.fastmcp import FastMCP
    except ImportError as exc: raise ConfigurationError("MCP SDK is required; install RelayMe with its MCP dependency") from exc
    server = FastMCP("RelayMe", instructions="RelayMe v0.3 exposes seven read-only R0 observation/discovery tools and one bounded R1 registered-task tool. R1 runs only locally registered fixed tasks; it provides no arbitrary shell or mutation access.", json_response=True)

    def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return adapter.invoke(name, arguments)
        except ControllerError as exc:
            error = {"status": exc.status}
            if isinstance(exc.message, dict):
                error.update(exc.message)
            else:
                error["message"] = exc.message
            return {"error": error}
        except ValueError as exc:
            return {"error": {"status": 400, "message": str(exc)}}

    @server.tool(name="list_hosts", description=TOOL_SCHEMAS["list_hosts"]["description"])
    def list_hosts() -> dict[str, Any]: return invoke("list_hosts", {})

    @server.tool(name="list_host_resources", description=TOOL_SCHEMAS["list_host_resources"]["description"])
    def list_host_resources(host: str) -> dict[str, Any]: return invoke("list_host_resources", {"host": host})

    @server.tool(name="host_status", description=TOOL_SCHEMAS["host_status"]["description"])
    def host_status(host: str) -> dict[str, Any]: return invoke("host_status", {"host": host})

    @server.tool(name="process_list", description=TOOL_SCHEMAS["process_list"]["description"])
    def process_list(host: str) -> dict[str, Any]: return invoke("process_list", {"host": host})

    @server.tool(name="read_logs", description=TOOL_SCHEMAS["read_logs"]["description"])
    def read_logs(host: str, service: str, last_n: int | None = None, since: str | None = None, max_bytes: int | None = None) -> dict[str, Any]:
        return invoke("read_logs", {"host": host, "service": service, "last_n": last_n, "since": since, "max_bytes": max_bytes})

    @server.tool(name="read_file", description=TOOL_SCHEMAS["read_file"]["description"])
    def read_file(host: str, path: str) -> dict[str, Any]: return invoke("read_file", {"host": host, "path": path})

    @server.tool(name="git_diff", description=TOOL_SCHEMAS["git_diff"]["description"])
    def git_diff(host: str, repo: str) -> dict[str, Any]: return invoke("git_diff", {"host": host, "repo": repo})

    @server.tool(name="run_registered_task", description=TOOL_SCHEMAS["run_registered_task"]["description"])
    def run_registered_task(host: str, task_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        return invoke("run_registered_task", {"host": host, "task_id": task_id, "idempotency_key": idempotency_key})

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="RelayMe v0.3 MCP adapter for R0 observation and bounded R1 tasks")
    parser.add_argument("--transport", choices=("stdio",), default="stdio", help="MCP transport (stdio is suitable for local clients and tunnel launchers)")
    args = parser.parse_args()
    server = create_mcp_server(RelayMeMcpAdapter(ControllerClient(AdapterConfig.from_env())))
    server.run(transport=args.transport)


if __name__ == "__main__": main()
