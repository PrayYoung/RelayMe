"""Small JSON CLI client for RelayMe v0.1."""
from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def call(url: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    request = Request(url.rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=35) as response: return json.loads(response.read())
    except HTTPError as exc: raise SystemExit(f"HTTP {exc.code}: {exc.read().decode()}")


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--url", required=True); parser.add_argument("--token", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-hosts")
    create = commands.add_parser("create-client-token"); create.add_argument("--identity", required=True); create.add_argument("--capability", action="append", required=True); create.add_argument("--host", action="append", default=["*"]); create.add_argument("--ttl-seconds", type=int)
    enroll = commands.add_parser("create-enrollment-token"); enroll.add_argument("--ttl-seconds", type=int, default=600)
    task = commands.add_parser("task"); task.add_argument("host"); task.add_argument("capability"); task.add_argument("arguments", help="JSON object")
    get = commands.add_parser("get-task"); get.add_argument("task_id")
    args = parser.parse_args()
    if args.command == "list-hosts": value = call(args.url, args.token, "GET", "/v1/hosts")
    elif args.command == "create-client-token": value = call(args.url, args.token, "POST", "/v1/admin/client-tokens", {"identity": args.identity, "scopes": {"capabilities": args.capability, "hosts": args.host}, "ttl_seconds": args.ttl_seconds})
    elif args.command == "create-enrollment-token": value = call(args.url, args.token, "POST", "/v1/admin/enrollment-tokens", {"ttl_seconds": args.ttl_seconds})
    elif args.command == "task": value = call(args.url, args.token, "POST", "/v1/tasks", {"host": args.host, "capability": args.capability, "arguments": json.loads(args.arguments)})
    else: value = call(args.url, args.token, "GET", "/v1/tasks/" + args.task_id)
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__": main()
