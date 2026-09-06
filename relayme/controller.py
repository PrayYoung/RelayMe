"""Single-process HTTP/JSON Controller for RelayMe R0 and bounded R1 tasks."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import ssl
import stat
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

R1_CAPABILITY = "run_registered_task"
EXECUTOR_CAPABILITY = "start_registered_executor_task"
PERSISTENT_CAPABILITIES = frozenset({R1_CAPABILITY, EXECUTOR_CAPABILITY})
CAPABILITIES = frozenset({"list_hosts", "list_host_resources", "host_status", "process_list", "read_logs", "read_file", "git_diff", R1_CAPABILITY, EXECUTOR_CAPABILITY})
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "AGENT_INTERRUPTED", "AGENT_LOST"})
ALLOWED_ARTIFACT_NAMES = frozenset({"patch.diff", "result.json", "test.log", "analysis.md", "executor_report.md"})
MAX_RESULT_BYTES = 1_000_000
MAX_ARTIFACT_READ_BYTES = 100_000
R1_RESULT_RETENTION_SECONDS = 3600


def now() -> int:
    return int(time.time())


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, database: str):
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        # Deliberately non-durable: clients receive an observation once, while SQLite retains audit metadata only.
        self.results: dict[str, Any] = {}
        with self.lock:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS enrollment_tokens (hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS client_tokens (hash TEXT PRIMARY KEY, identity TEXT NOT NULL, scopes TEXT NOT NULL, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS client_token_audit (event_id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
              identity TEXT NOT NULL, token_hash TEXT, scopes TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS agents (host_id TEXT PRIMARY KEY, hostname TEXT NOT NULL, credential_hash TEXT NOT NULL,
              registered_at INTEGER NOT NULL, last_heartbeat INTEGER, status TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, actor TEXT NOT NULL, host_id TEXT NOT NULL, capability TEXT NOT NULL,
              arguments TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER,
              result_bytes INTEGER, result_hash TEXT, error TEXT, idempotency_key TEXT);
            CREATE TABLE IF NOT EXISTS execution_audit (execution_id TEXT PRIMARY KEY, actor TEXT NOT NULL, host_id TEXT NOT NULL,
              task_id TEXT NOT NULL, task_identity TEXT, idempotency_key_hash TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
              finished_at INTEGER, duration_ms INTEGER, state TEXT, exit_code INTEGER, terminating_signal INTEGER,
              timeout_seconds INTEGER, stdout_bytes INTEGER, stderr_bytes INTEGER, stdout_truncated INTEGER, stderr_truncated INTEGER,
              stdout_hash TEXT, stderr_hash TEXT, rejection_code TEXT);
            CREATE TABLE IF NOT EXISTS r1_idempotency (actor TEXT NOT NULL, host_id TEXT NOT NULL, registered_task_id TEXT NOT NULL,
              key_hash TEXT NOT NULL, execution_id TEXT NOT NULL, created_at INTEGER NOT NULL,
              PRIMARY KEY (actor, host_id, registered_task_id, key_hash));
            CREATE TABLE IF NOT EXISTS r1_results (execution_id TEXT PRIMARY KEY, result TEXT, expires_at INTEGER NOT NULL,
              expired_at INTEGER);
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
            if "idempotency_key" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            self.db.execute("UPDATE r1_results SET result=NULL, expired_at=? WHERE result IS NOT NULL AND expires_at < ?", (now(), now()))
            self.db.commit()

    def _expire_r1_results(self) -> None:
        self.db.execute("UPDATE r1_results SET result=NULL, expired_at=? WHERE result IS NOT NULL AND expires_at < ?", (now(), now()))

    def create_enrollment(self, ttl: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute("INSERT INTO enrollment_tokens VALUES (?, ?)", (token_hash(token), now() + ttl))
            self.db.commit()
        return token

    def create_client(self, identity: str, scopes: dict[str, Any], ttl: int | None) -> str:
        if not isinstance(identity, str) or not identity:
            raise ValueError("client identity is required")
        capabilities = scopes.get("capabilities", [])
        hosts = scopes.get("hosts", [])
        valid_r1_scope = lambda scope: isinstance(scope, str) and ((scope.startswith(R1_CAPABILITY + ":") and len(scope) > len(R1_CAPABILITY) + 1) or (scope.startswith("run_executor_task:") and len(scope) > len("run_executor_task:")))
        if not isinstance(capabilities, list) or not capabilities or not all(capability in CAPABILITIES - PERSISTENT_CAPABILITIES or valid_r1_scope(capability) for capability in capabilities):
            raise ValueError("invalid client capabilities")
        if not isinstance(hosts, list) or not all(isinstance(host, str) and host for host in hosts):
            raise ValueError("invalid client host scope")
        token = secrets.token_urlsafe(32)
        hashed = token_hash(token)
        with self.lock:
            self.db.execute("INSERT INTO client_tokens VALUES (?, ?, ?, ?)",
                            (hashed, identity, json.dumps(scopes), now() + ttl if ttl else None))
            self.db.execute("INSERT INTO client_token_audit (event,identity,token_hash,scopes,created_at) VALUES (?,?,?,?,?)",
                            ("CREATED", identity, hashed, json.dumps(scopes), now()))
            self.db.commit()
        return token

    def revoke_clients(self, identity: str) -> int:
        if not isinstance(identity, str) or not identity:
            raise ValueError("client identity is required")
        with self.lock:
            rows = self.db.execute("SELECT hash, scopes FROM client_tokens WHERE identity=?", (identity,)).fetchall()
            for row in rows:
                self.db.execute("INSERT INTO client_token_audit (event,identity,token_hash,scopes,created_at) VALUES (?,?,?,?,?)",
                                ("REVOKED", identity, row["hash"], row["scopes"], now()))
            self.db.execute("DELETE FROM client_tokens WHERE identity=?", (identity,))
            self.db.commit()
        return len(rows)

    def authenticate_client(self, token: str) -> tuple[str, dict[str, Any]] | None:
        with self.lock:
            row = self.db.execute("SELECT identity, scopes, expires_at FROM client_tokens WHERE hash=?", (token_hash(token),)).fetchone()
        if not row or (row["expires_at"] is not None and row["expires_at"] < now()):
            return None
        return row["identity"], json.loads(row["scopes"])

    def enroll(self, enrollment_token: str, hostname: str, metadata: dict[str, Any]) -> tuple[str, str] | None:
        with self.lock:
            row = self.db.execute("SELECT expires_at FROM enrollment_tokens WHERE hash=?", (token_hash(enrollment_token),)).fetchone()
            if not row or row["expires_at"] < now():
                return None
            self.db.execute("DELETE FROM enrollment_tokens WHERE hash=?", (token_hash(enrollment_token),))
            host_id, credential = str(uuid.uuid4()), secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO agents VALUES (?, ?, ?, ?, NULL, 'OFFLINE', ?)",
                            (host_id, hostname, token_hash(credential), now(), json.dumps(metadata)))
            self.db.commit()
            return host_id, credential

    def agent(self, host_id: str, credential: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT credential_hash FROM agents WHERE host_id=?", (host_id,)).fetchone()
        return bool(row and secrets.compare_digest(row["credential_hash"], token_hash(credential)))

    def heartbeat(self, host_id: str, metadata: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET last_heartbeat=?, status='ONLINE', metadata=? WHERE host_id=?",
                            (now(), json.dumps(metadata), host_id))
            self.db.commit()

    def hosts(self, permitted_hosts: list[str] | None = None) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT host_id, hostname, registered_at, last_heartbeat, status, metadata FROM agents ORDER BY hostname").fetchall()
        cutoff = now() - 90
        return [{**dict(r), "status": "ONLINE" if r["last_heartbeat"] and r["last_heartbeat"] >= cutoff else "OFFLINE",
                 "metadata": {key: value for key, value in json.loads(r["metadata"]).items() if key != "resources"}}
                for r in rows if permitted_hosts is None or "*" in permitted_hosts or r["host_id"] in permitted_hosts]

    def resolve_host(self, identifier: str) -> tuple[dict[str, Any] | None, str | None]:
        with self.lock:
            row = self.db.execute("SELECT host_id, hostname, metadata FROM agents WHERE host_id=?", (identifier,)).fetchone()
            if row: return dict(row), None
            rows = self.db.execute("SELECT host_id, hostname, metadata FROM agents WHERE hostname=?", (identifier,)).fetchall()
        if not rows: return None, "unknown_host"
        if len(rows) > 1: return None, "ambiguous_hostname"
        return dict(rows[0]), None

    def resources(self, host: dict[str, Any]) -> dict[str, Any]:
        resources = json.loads(host["metadata"]).get("resources", {})
        return {"host_id": host["host_id"], "hostname": host["hostname"],
                "services": list(resources.get("services", [])), "repositories": list(resources.get("repositories", [])),
                "allowed_file_roots": list(resources.get("allowed_file_roots", [])),
                "registered_tasks": list(resources.get("registered_tasks", [])),
                "executor_profiles": list(resources.get("executor_profiles", []))}

    @staticmethod
    def _idempotency_target(capability: str, arguments: dict[str, Any]) -> str:
        if capability == R1_CAPABILITY:
            return arguments["task_id"]
        if capability == EXECUTOR_CAPABILITY:
            return arguments["executor_profile_id"] + ":" + arguments["task_spec_id"]
        raise ValueError("capability is not idempotent")

    def create_task(self, actor: str, host: str, capability: str, arguments: dict[str, Any]) -> str:
        arguments = dict(arguments)
        idempotency_key = arguments.pop("idempotency_key", None) if capability in PERSISTENT_CAPABILITIES else None
        task_id = str(uuid.uuid4())
        with self.changed:
            self._expire_r1_results()
            if idempotency_key:
                key_hash = token_hash(idempotency_key)
                inserted = self.db.execute("INSERT OR IGNORE INTO r1_idempotency VALUES (?,?,?,?,?,?)",
                                           (actor, host, self._idempotency_target(capability, arguments), key_hash, task_id, now())).rowcount
                if not inserted:
                    existing = self.db.execute("SELECT execution_id FROM r1_idempotency WHERE actor=? AND host_id=? AND registered_task_id=? AND key_hash=?",
                                               (actor, host, self._idempotency_target(capability, arguments), key_hash)).fetchone()
                    self.db.commit()
                    assert existing
                    return existing["execution_id"]
            self.db.execute("INSERT INTO tasks (task_id,actor,host_id,capability,arguments,status,created_at,idempotency_key) VALUES (?,?,?,?,?,?,?,?)",
                            (task_id, actor, host, capability, json.dumps(arguments, sort_keys=True), "QUEUED", now(), token_hash(idempotency_key) if idempotency_key else None))
            self.db.commit()
            self.changed.notify_all()
        return task_id

    def next_task(self, host_id: str, wait_seconds: int) -> dict[str, Any] | None:
        deadline = time.monotonic() + min(max(wait_seconds, 0), 25)
        with self.changed:
            while True:
                row = self.db.execute("SELECT * FROM tasks WHERE host_id=? AND status='QUEUED' ORDER BY created_at LIMIT 1", (host_id,)).fetchone()
                if row:
                    # State transition is atomic under this process-wide lock, preventing duplicate execution.
                    started = now()
                    changed = self.db.execute("UPDATE tasks SET status='RUNNING', started_at=? WHERE task_id=? AND status='QUEUED'",
                                              (started, row["task_id"])).rowcount
                    self.db.commit()
                    if changed:
                        return {"task_id": row["task_id"], "capability": row["capability"], "arguments": json.loads(row["arguments"])}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.changed.wait(remaining)

    def finish_task(self, host_id: str, task_id: str, status: str, result: Any | None, error: Any | None) -> bool:
        if status not in {"SUCCEEDED", "FAILED"}:
            return False
        raw = json.dumps(result, separators=(",", ":")).encode() if result is not None else b""
        if len(raw) > MAX_RESULT_BYTES:
            if isinstance(result, dict) and result.get("execution_id") == task_id:
                result = {"execution_id": task_id, "task_id": result.get("task_id"), "state": "result_rejected_oversize",
                          "stdout": "", "stderr": "", "stdout_bytes": result.get("stdout_bytes", 0), "stderr_bytes": result.get("stderr_bytes", 0),
                          "stdout_truncated": True, "stderr_truncated": True, "duration_ms": result.get("duration_ms"),
                          "timeout_seconds": result.get("timeout_seconds"), "task_identity": result.get("task_identity")}
                raw, status, error = json.dumps(result, separators=(",", ":")).encode(), "FAILED", {"code": "result_too_large", "message": "result exceeded Controller forwarding limit"}
            else:
                status, result, raw, error = "FAILED", None, b"", "result exceeds Controller forwarding limit"
        error_value = json.dumps(error, separators=(",", ":")) if isinstance(error, dict) else error
        with self.lock:
            self._expire_r1_results()
            existing = self.db.execute("SELECT capability, status FROM tasks WHERE task_id=? AND host_id=?", (task_id, host_id)).fetchone()
            if existing and existing["capability"] in PERSISTENT_CAPABILITIES and existing["status"] in TERMINAL:
                return True
            changed = self.db.execute("UPDATE tasks SET status=?, finished_at=?, result_bytes=?, result_hash=?, error=? "
                                     "WHERE task_id=? AND host_id=? AND status='RUNNING'",
                                     (status, now(), len(raw), hashlib.sha256(raw).hexdigest() if raw else None, error_value, task_id, host_id)).rowcount
            self.db.commit()
            if changed and result is not None:
                if existing and existing["capability"] in PERSISTENT_CAPABILITIES:
                    self.db.execute("INSERT OR REPLACE INTO r1_results VALUES (?,?,?,NULL)",
                                    (task_id, json.dumps(result, separators=(",", ":")), now() + R1_RESULT_RETENTION_SECONDS))
                else:
                    self.results[task_id] = result
            if changed and isinstance(result, dict) and result.get("execution_id") == task_id:
                task = self.db.execute("SELECT actor, host_id, arguments, created_at, started_at, idempotency_key FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                arguments = json.loads(task["arguments"])
                self.db.execute("INSERT OR REPLACE INTO execution_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, task["actor"], task["host_id"], str(arguments.get("task_id", arguments.get("executor_profile_id", ""))), result.get("task_identity"),
                     task["idempotency_key"], task["created_at"], task["started_at"], now(), result.get("duration_ms"),
                     result.get("state"), result.get("exit_code"), result.get("terminating_signal"), result.get("timeout_seconds"),
                     result.get("stdout_bytes"), result.get("stderr_bytes"), int(bool(result.get("stdout_truncated"))), int(bool(result.get("stderr_truncated"))),
                     result.get("stdout_hash"), result.get("stderr_hash"), (error or {}).get("code") if isinstance(error, dict) else None))
                self.db.commit()
            elif changed:
                self.db.commit()
        return bool(changed)

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            self._expire_r1_results()
            self.db.commit()
            row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            if result["status"] == "RUNNING" and result["started_at"] and result["started_at"] + 300 < now():
                if result["capability"] == EXECUTOR_CAPABILITY:
                    agent = self.db.execute("SELECT last_heartbeat FROM agents WHERE host_id=?", (result["host_id"],)).fetchone()
                    terminal_state = "AGENT_LOST" if not agent or not agent["last_heartbeat"] or agent["last_heartbeat"] < now() - 90 else "AGENT_INTERRUPTED"
                else:
                    terminal_state = "TIMED_OUT"
                self.db.execute("UPDATE tasks SET status=?, finished_at=? WHERE task_id=?", (terminal_state, now(), task_id)); self.db.commit()
                result["status"], result["finished_at"] = terminal_state, now()
            result["arguments"] = json.loads(result["arguments"])
            if result["error"]:
                try:
                    structured = json.loads(result["error"])
                    if isinstance(structured, dict): result["error"] = structured
                except json.JSONDecodeError: pass
            if result["capability"] in PERSISTENT_CAPABILITIES and result["status"] in TERMINAL:
                retained = self.db.execute("SELECT result, expires_at, expired_at FROM r1_results WHERE execution_id=?", (task_id,)).fetchone()
                if retained and retained["result"] is not None and retained["expires_at"] >= now():
                    result["result"] = json.loads(retained["result"])
                else:
                    if retained and retained["result"] is not None:
                        self.db.execute("UPDATE r1_results SET result=NULL, expired_at=? WHERE execution_id=?", (now(), task_id))
                    result["result"] = {"execution_id": task_id, "task_id": result["arguments"].get("task_id", task_id), "state": "result_expired"}
                    result["result_expired"] = True
            # R0 observations remain a short-lived forwarding channel.
            elif result["status"] in TERMINAL and task_id in self.results:
                result["result"] = self.results.pop(task_id)
            return result

    def reviewable_tasks(self, actor: str, administrator: bool) -> list[dict[str, Any]]:
        with self.lock:
            self._expire_r1_results(); self.db.commit()
            query = "SELECT task_id,host_id,status,created_at,finished_at,arguments FROM tasks WHERE capability=? AND status IN ({})".format(",".join("?" for _ in TERMINAL))
            params: list[Any] = [EXECUTOR_CAPABILITY, *TERMINAL]
            if not administrator:
                query += " AND actor=?"; params.append(actor)
            rows = self.db.execute(query + " ORDER BY created_at DESC LIMIT 100", params).fetchall()
            values = []
            for row in rows:
                args = json.loads(row["arguments"])
                retained = self.db.execute("SELECT result FROM r1_results WHERE execution_id=?", (row["task_id"],)).fetchone()
                result = json.loads(retained["result"]) if retained and retained["result"] else None
                if result and result.get("reviewable"):
                    values.append({"task_id": row["task_id"], "host_id": row["host_id"], "state": row["status"], "executor_profile_id": args.get("executor_profile_id"), "task_spec_id": args.get("task_spec_id"), "created_at": row["created_at"], "finished_at": row["finished_at"]})
            return values

    def task_artifact(self, task_id: str, artifact_name: str, actor: str, administrator: bool, max_bytes: int = MAX_ARTIFACT_READ_BYTES) -> dict[str, Any]:
        task = self.task(task_id)
        if not task:
            raise KeyError("task not found")
        if task["actor"] != actor and not administrator:
            raise PermissionError("task not visible")
        if task.get("result_expired"):
            raise FileNotFoundError("task result expired")
        if task["status"] not in TERMINAL or task["capability"] != EXECUTOR_CAPABILITY:
            raise FileNotFoundError("task artifact not available")
        result = task.get("result")
        if not isinstance(result, dict):
            raise FileNotFoundError("task result missing")
        if not isinstance(artifact_name, str) or not artifact_name or "/" in artifact_name or "\\" in artifact_name or ".." in artifact_name or "\0" in artifact_name or artifact_name.startswith(("/", "\\")):
            raise ValueError("invalid artifact name")
        if artifact_name not in ALLOWED_ARTIFACT_NAMES:
            raise FileNotFoundError("artifact not declared in task manifest")
        manifest_items = [item for item in result.get("artifacts", []) if isinstance(item, dict) and item.get("name") == artifact_name]
        if not manifest_items:
            raise FileNotFoundError("artifact not declared in task manifest")
        manifest_item = manifest_items[0]
        output_dir = result.get("output_directory") or result.get("task_output_root")
        if not output_dir or not isinstance(output_dir, str):
            raise FileNotFoundError("task output directory not recorded")
        try:
            base = Path(output_dir).resolve(strict=True)
        except (OSError, RuntimeError):
            raise FileNotFoundError("task output directory does not exist or cannot be resolved")
        try:
            target = (base / artifact_name).resolve(strict=True)
        except (OSError, RuntimeError):
            raise FileNotFoundError("artifact file does not exist")
        if not target.is_relative_to(base):
            raise ValueError("artifact path escapes output root")
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("artifact is not a regular file")
        try:
            with open(target, "rb") as f:
                raw = f.read(max_bytes + 1)
        except OSError as exc:
            raise FileNotFoundError(f"cannot read artifact: {exc}")
        actual_size = target.stat().st_size
        truncated = actual_size > max_bytes
        bounded_bytes = raw[:max_bytes]
        content = bounded_bytes.decode("utf-8", "replace")
        size = manifest_item.get("size", actual_size)
        sha256 = manifest_item.get("sha256")
        if not sha256:
            sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        return {
            "task_id": task_id,
            "artifact_name": artifact_name,
            "type": manifest_item.get("type", "artifact"),
            "mime_type": manifest_item.get("mime_type", "application/json" if artifact_name.endswith(".json") else "text/plain"),
            "size": size,
            "sha256": sha256,
            "content": content,
            "truncated": truncated,
            "bytes_returned": len(bounded_bytes),
        }


class Api(BaseHTTPRequestHandler):
    server: "ControllerServer"
    def log_message(self, format: str, *args: Any) -> None: pass
    def body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1_000_000: raise ValueError
            value = json.loads(self.rfile.read(size))
            return value if isinstance(value, dict) else {}
        except (ValueError, json.JSONDecodeError): self.fail(HTTPStatus.BAD_REQUEST, "invalid JSON")
    def reply(self, status: int, value: Any) -> None:
        data = json.dumps(value, separators=(",", ":")).encode(); self.send_response(status)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def fail(self, status: int, message: str, code: str | None = None) -> None:
        self.reply(status, {"error": {"code": code, "message": message} if code else message}); raise RuntimeError(message)
    def bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "): self.fail(HTTPStatus.UNAUTHORIZED, "missing bearer token")
        return value[7:]
    def client(self) -> tuple[str, dict[str, Any]]:
        token = self.bearer()
        if secrets.compare_digest(token, self.server.bootstrap_token): return "administrator", {"admin": True, "capabilities": list(CAPABILITIES), "hosts": ["*"]}
        identity = self.server.store.authenticate_client(token)
        if not identity: self.fail(HTTPStatus.UNAUTHORIZED, "invalid or expired client token")
        return identity
    def agent(self) -> str:
        host_id = self.headers.get("X-RelayMe-Host", "")
        if not host_id or not self.server.store.agent(host_id, self.bearer()): self.fail(HTTPStatus.UNAUTHORIZED, "invalid agent credential")
        return host_id
    @staticmethod
    def allowed(scopes: dict[str, Any], host: str, capability: str, arguments: dict[str, Any] | None = None) -> bool:
        if scopes.get("admin"): return True
        if "*" not in scopes.get("hosts", []) and host not in scopes.get("hosts", []): return False
        if capability == R1_CAPABILITY:
            task_id = (arguments or {}).get("task_id")
            return isinstance(task_id, str) and (R1_CAPABILITY + ":" + task_id) in scopes.get("capabilities", [])
        if capability == EXECUTOR_CAPABILITY:
            profile = (arguments or {}).get("executor_profile_id")
            return isinstance(profile, str) and ("run_executor_task:" + profile) in scopes.get("capabilities", [])
        return capability in scopes.get("capabilities", [])
    def do_POST(self) -> None:
        try:
            path, body = urlparse(self.path).path, self.body()
            if path == "/v1/admin/enrollment-tokens":
                _, scope = self.client()
                if not scope.get("admin"): self.fail(403, "admin token required")
                self.reply(201, {"token": self.server.store.create_enrollment(int(body.get("ttl_seconds", 600))) }); return
            if path == "/v1/agents/enroll":
                enrolled = self.server.store.enroll(str(body.get("enrollment_token", "")), str(body.get("hostname", "")), body.get("metadata", {}))
                if not enrolled: self.fail(401, "invalid or expired enrollment token")
                self.reply(201, {"host_id": enrolled[0], "credential": enrolled[1]}); return
            if path == "/v1/agent/heartbeat":
                host = self.agent(); self.server.store.heartbeat(host, body.get("metadata", {})); self.reply(200, {"ok": True}); return
            if path == "/v1/agent/tasks/result":
                host = self.agent(); ok = self.server.store.finish_task(host, str(body.get("task_id", "")), str(body.get("status", "")), body.get("result"), body.get("error"))
                if not ok: self.fail(409, "task is not running for this host")
                self.reply(200, {"ok": True}); return
            if path == "/v1/tasks":
                actor, scopes = self.client(); host, cap, args = body.get("host"), body.get("capability"), body.get("arguments", {})
                if not isinstance(host, str) or cap not in CAPABILITIES - {"list_hosts", "list_host_resources"} or not isinstance(args, dict): self.fail(400, "invalid task")
                if cap == R1_CAPABILITY:
                    if set(args) - {"task_id", "idempotency_key"} or not isinstance(args.get("task_id"), str) or not args["task_id"] or ("idempotency_key" in args and (not isinstance(args["idempotency_key"], str) or not args["idempotency_key"] or len(args["idempotency_key"]) > 256)):
                        self.fail(400, "invalid registered-task request", "invalid_registered_task_request")
                if cap == EXECUTOR_CAPABILITY:
                    allowed = {"executor_profile_id", "task_spec_id", "brief", "idempotency_key"}
                    if set(args) - allowed or not isinstance(args.get("executor_profile_id"), str) or not args["executor_profile_id"] or args.get("task_spec_id") != "patch-and-test" or not isinstance(args.get("brief"), str) or not 1 <= len(args["brief"]) <= 8192 or ("idempotency_key" in args and (not isinstance(args["idempotency_key"], str) or not 1 <= len(args["idempotency_key"]) <= 256)):
                        self.fail(400, "invalid executor-task request", "invalid_executor_task_request")
                resolved, error = self.server.store.resolve_host(host)
                if error == "unknown_host": self.fail(404, "host identifier is unknown", error)
                if error == "ambiguous_hostname": self.fail(409, "hostname is ambiguous; use host_id", error)
                assert resolved
                host_id = resolved["host_id"]
                if not self.allowed(scopes, host_id, cap, args): self.fail(403, "scope does not permit task")
                self.reply(201, {"task_id": self.server.store.create_task(actor, host_id, cap, args), "status": "QUEUED"}); return
            self.fail(404, "not found")
        except RuntimeError: pass
    def do_GET(self) -> None:
        try:
            parsed, path = urlparse(self.path), urlparse(self.path).path
            if path == "/v1/agent/tasks/next":
                host = self.agent(); seconds = int(parse_qs(parsed.query).get("wait_seconds", ["20"])[0])
                task = self.server.store.next_task(host, seconds); self.reply(200, {"task": task}); return
            actor, scopes = self.client()
            if path == "/v1/hosts":
                if not scopes.get("admin") and "list_hosts" not in scopes.get("capabilities", []): self.fail(403, "scope does not permit list_hosts")
                self.reply(200, {"hosts": self.server.store.hosts(None if scopes.get("admin") else scopes.get("hosts", []))}); return
            if path.startswith("/v1/hosts/") and path.endswith("/resources"):
                if not scopes.get("admin") and "list_host_resources" not in scopes.get("capabilities", []): self.fail(403, "scope does not permit list_host_resources")
                identifier = path.removeprefix("/v1/hosts/").removesuffix("/resources").strip("/")
                resolved, error = self.server.store.resolve_host(identifier)
                if error == "unknown_host": self.fail(404, "host identifier is unknown", error)
                if error == "ambiguous_hostname": self.fail(409, "hostname is ambiguous; use host_id", error)
                assert resolved
                if not self.allowed(scopes, resolved["host_id"], "list_host_resources"): self.fail(403, "scope does not permit host resources")
                self.reply(200, {"resources": self.server.store.resources(resolved)}); return
            if path.startswith("/v1/tasks/"):
                rest = path.removeprefix("/v1/tasks/").strip("/")
                if "/artifacts/" in rest or rest.endswith("/artifacts") or rest.startswith("artifacts/"):
                    if "/artifacts/" in rest:
                        task_id, artifact_name = rest.split("/artifacts/", 1)
                        artifact_name = unquote(artifact_name)
                    else:
                        self.fail(400, "invalid artifact request", "invalid_artifact_request")
                    try:
                        artifact = self.server.store.task_artifact(task_id, artifact_name, actor, bool(scopes.get("admin")))
                        self.reply(200, artifact); return
                    except KeyError as exc: self.fail(404, str(exc), "task_not_found")
                    except PermissionError as exc: self.fail(403, str(exc), "task_not_visible")
                    except FileNotFoundError as exc: self.fail(404, str(exc), "artifact_not_found")
                    except ValueError as exc: self.fail(400, str(exc), "invalid_artifact_request")
                if rest.endswith("/result"):
                    task_id = rest.removesuffix("/result").strip("/")
                    task = self.server.store.task(task_id)
                    if not task: self.fail(404, "task not found")
                    if task["actor"] != actor and not scopes.get("admin"): self.fail(403, "task not visible")
                    self.reply(200, {"task_id": task["task_id"], "result": task.get("result"), "result_expired": task.get("result_expired", False)}); return
                task = self.server.store.task(rest)
                if not task: self.fail(404, "task not found")
                if task["actor"] != actor and not scopes.get("admin"): self.fail(403, "task not visible")
                self.reply(200, {"task": task}); return
            if path == "/v1/reviewable-tasks":
                self.reply(200, {"tasks": self.server.store.reviewable_tasks(actor, bool(scopes.get("admin")))}); return
            self.fail(404, "not found")
        except (RuntimeError, ValueError): pass


class ControllerServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: Store, bootstrap_token: str):
        super().__init__(address, Api); self.store, self.bootstrap_token = store, bootstrap_token


def load_bootstrap_token(token: str | None, token_file: str | None) -> str:
    if bool(token) == bool(token_file):
        raise ValueError("supply exactly one bootstrap token source")
    value = Path(token_file).read_text().strip() if token_file else token
    if not value:
        raise ValueError("bootstrap token must not be empty")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--db", default="relayme.db"); parser.add_argument("--bind", default="127.0.0.1:8765"); parser.add_argument("--bootstrap-token"); parser.add_argument("--bootstrap-token-file"); parser.add_argument("--tls-cert"); parser.add_argument("--tls-key")
    args = parser.parse_args(); host, port = args.bind.rsplit(":", 1)
    if bool(args.tls_cert) != bool(args.tls_key): parser.error("--tls-cert and --tls-key must be supplied together")
    try: bootstrap_token = load_bootstrap_token(args.bootstrap_token, args.bootstrap_token_file)
    except (OSError, ValueError) as error: parser.error(str(error))
    server = ControllerServer((host, int(port)), Store(args.db), bootstrap_token)
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()

if __name__ == "__main__": main()
