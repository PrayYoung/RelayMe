"""Small JSON CLI client for RelayMe R0 and bounded R1 tasks."""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def call(url: str, token: str, method: str, path: str, body: dict | None = None, ca_cert: str | None = None) -> dict:
    request = Request(url.rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        context = ssl.create_default_context(cafile=ca_cert) if ca_cert else None
        with urlopen(request, timeout=35, context=context) as response: return json.loads(response.read())
    except HTTPError as exc: raise SystemExit(f"HTTP {exc.code}: {exc.read().decode()}")
    except URLError as exc: raise SystemExit(f"Controller connection failed: {exc.reason}")


def wait_for_task(url: str, token: str, task_id: str, ca_cert: str | None, wait_seconds: int) -> dict:
    deadline = time.monotonic() + wait_seconds
    while True:
        value = call(url, token, "GET", "/v1/tasks/" + task_id, ca_cert=ca_cert)
        if value["task"]["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "AGENT_INTERRUPTED", "AGENT_LOST"}: return value
        if time.monotonic() >= deadline: raise SystemExit(f"Task {task_id} did not finish within {wait_seconds} seconds")
        time.sleep(1)


def main() -> None:
    if sys.argv[1:2] == ["admin"]:
        from .admin import main as admin_main
        admin_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("RELAYME_URL"), help="Controller URL (or RELAYME_URL)")
    parser.add_argument("--token", default=os.environ.get("RELAYME_TOKEN"), help="Scoped client token (or RELAYME_TOKEN)")
    parser.add_argument("--ca-cert", default=os.environ.get("RELAYME_CA_CERT"), help="CA certificate path for HTTPS (or RELAYME_CA_CERT)")
    parser.add_argument("--wait-seconds", type=int, default=35, help="Maximum time to poll an R0 task")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-hosts"); commands.add_parser("hosts")
    resources = commands.add_parser("list-host-resources"); resources.add_argument("host", help="Canonical host_id or a unique hostname")
    enroll = commands.add_parser("create-enrollment-token"); enroll.add_argument("--ttl-seconds", type=int, default=600)
    task = commands.add_parser("task"); task.add_argument("host"); task.add_argument("capability"); task.add_argument("arguments", help="JSON object")
    get = commands.add_parser("get-task"); get.add_argument("task_id")
    status = commands.add_parser("status"); status.add_argument("host")
    processes = commands.add_parser("processes"); processes.add_argument("host")
    logs = commands.add_parser("logs"); logs.add_argument("host"); logs.add_argument("service"); logs.add_argument("--last-n", type=int); logs.add_argument("--since"); logs.add_argument("--max-bytes", type=int)
    read_file = commands.add_parser("read-file"); read_file.add_argument("host"); read_file.add_argument("path")
    git_diff = commands.add_parser("git-diff"); git_diff.add_argument("host"); git_diff.add_argument("repo")
    run_task = commands.add_parser("run-task"); run_task.add_argument("host"); run_task.add_argument("task_id"); run_task.add_argument("--idempotency-key")
    executor = commands.add_parser("start-executor-task"); executor.add_argument("host"); executor.add_argument("executor_profile_id"); executor.add_argument("task_spec_id"); executor.add_argument("brief"); executor.add_argument("--idempotency-key")
    reviewable = commands.add_parser("list-reviewable-tasks")
    result = commands.add_parser("task-result"); result.add_argument("task_id")
    artifact = commands.add_parser("task-artifact"); artifact.add_argument("task_id"); artifact.add_argument("artifact_name")
    args = parser.parse_args()
    if not args.url or not args.token: parser.error("--url/RELAYME_URL and --token/RELAYME_TOKEN are required")
    if args.command in {"list-hosts", "hosts"}: value = call(args.url, args.token, "GET", "/v1/hosts", ca_cert=args.ca_cert)
    elif args.command == "list-host-resources": value = call(args.url, args.token, "GET", "/v1/hosts/" + args.host + "/resources", ca_cert=args.ca_cert)
    elif args.command == "create-enrollment-token": value = call(args.url, args.token, "POST", "/v1/admin/enrollment-tokens", {"ttl_seconds": args.ttl_seconds}, args.ca_cert)
    elif args.command == "task": value = call(args.url, args.token, "POST", "/v1/tasks", {"host": args.host, "capability": args.capability, "arguments": json.loads(args.arguments)}, args.ca_cert)
    elif args.command == "get-task": value = call(args.url, args.token, "GET", "/v1/tasks/" + args.task_id, ca_cert=args.ca_cert)
    elif args.command == "run-task":
        arguments = {"task_id": args.task_id}
        if args.idempotency_key: arguments["idempotency_key"] = args.idempotency_key
        created = call(args.url, args.token, "POST", "/v1/tasks", {"host": args.host, "capability": "run_registered_task", "arguments": arguments}, args.ca_cert)
        value = wait_for_task(args.url, args.token, created["task_id"], args.ca_cert, args.wait_seconds)
    elif args.command == "start-executor-task":
        arguments = {"executor_profile_id": args.executor_profile_id, "task_spec_id": args.task_spec_id, "brief": args.brief}
        if args.idempotency_key: arguments["idempotency_key"] = args.idempotency_key
        created = call(args.url, args.token, "POST", "/v1/tasks", {"host": args.host, "capability": "start_registered_executor_task", "arguments": arguments}, args.ca_cert)
        value = wait_for_task(args.url, args.token, created["task_id"], args.ca_cert, args.wait_seconds)
    elif args.command == "list-reviewable-tasks": value = call(args.url, args.token, "GET", "/v1/reviewable-tasks", ca_cert=args.ca_cert)
    elif args.command == "task-result": value = call(args.url, args.token, "GET", "/v1/tasks/" + args.task_id + "/result", ca_cert=args.ca_cert)
    elif args.command == "task-artifact": value = call(args.url, args.token, "GET", "/v1/tasks/" + args.task_id + "/artifacts/" + args.artifact_name, ca_cert=args.ca_cert)
    else:
        if args.command == "status": host, capability, arguments = args.host, "host_status", {}
        elif args.command == "processes": host, capability, arguments = args.host, "process_list", {}
        elif args.command == "logs":
            host, capability = args.host, "read_logs"
            arguments = {key: value for key, value in {"service": args.service, "last_n": args.last_n, "since": args.since, "max_bytes": args.max_bytes}.items() if value is not None}
        elif args.command == "read-file": host, capability, arguments = args.host, "read_file", {"path": args.path}
        else: host, capability, arguments = args.host, "git_diff", {"repo": args.repo}
        created = call(args.url, args.token, "POST", "/v1/tasks", {"host": host, "capability": capability, "arguments": arguments}, args.ca_cert)
        value = wait_for_task(args.url, args.token, created["task_id"], args.ca_cert, args.wait_seconds)
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__": main()
