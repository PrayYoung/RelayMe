"""Local-only Controller administration for RelayMe v0.2."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .controller import CAPABILITIES, R1_CAPABILITY, Store


def write_token(path: str, token: str) -> None:
    destination = Path(path)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local RelayMe Controller administration")
    parser.add_argument("--db", required=True, help="Protected local Controller SQLite database")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-client", help="Create a scoped client token from local Controller state")
    create.add_argument("--name", required=True)
    create.add_argument("--capability", action="append", required=True,
                        help=f"R0 capability or exact {R1_CAPABILITY}:TASK_ID scope")
    create.add_argument("--host", action="append", default=["*"])
    create.add_argument("--ttl-seconds", type=int)
    create.add_argument("--output-file", help="New protected file to receive the token; otherwise emit it once")
    revoke = commands.add_parser("revoke-client", help="Revoke all client tokens for one identity")
    revoke.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    if args.command == "create-client" and not all(capability in CAPABILITIES - {R1_CAPABILITY} or capability.startswith(R1_CAPABILITY + ":") and len(capability) > len(R1_CAPABILITY) + 1 for capability in args.capability):
        parser.error("--capability must be an R0 capability or an exact run_registered_task:TASK_ID scope")
    store = Store(args.db)
    try:
        if args.command == "create-client":
            token = store.create_client(args.name, {"hosts": args.host, "capabilities": args.capability}, args.ttl_seconds)
            if args.output_file:
                write_token(args.output_file, token)
            else:
                print(token)
        else:
            print(store.revoke_clients(args.name))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
