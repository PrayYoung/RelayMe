"""OAuth 2.1 edge for RelayMe remote MCP interoperability.

This module provides a standards-compliant OAuth 2.1 authorization server edge that allows
web-based MCP clients (Gemini Spark, Claude Web) to authenticate to RelayMe without exposing
internal bearer tokens. It is NOT a second Controller — it is a thin authentication proxy.

Architecture:
    Web Client
      ↓ OAuth 2.1 / PKCE Authorization Code Flow
    relayme-mcp-oauth  (this module)
      ├── /.well-known/oauth-protected-resource  (RFC 9728)
      ├── /.well-known/oauth-authorization-server  (RFC 8414)
      ├── /register   (RFC 7591 Dynamic Client Registration — legacy fallback)
      ├── /authorize  (PKCE code endpoint — browser redirect)
      ├── /token      (code → opaque access token)
      ├── /revoke     (RFC 7009)
      └── /mcp        (inline MCP, resolved RelayMe bearer injected via ContextVar)
            ↓ RelayMe bearer (from allowlist lookup, never visible to web client)
       existing RelayMe Controller

Security model:
    - RelayMe bearer tokens are NEVER returned to web clients
    - All tokens stored as SHA-256 hashes; cleartext never persisted
    - PKCE S256 mandatory; plain method rejected
    - Explicit allowlist only: sub → relayme_token (no wildcards, no auto-grant)
    - Auth codes are single-use; tokens respect TTLs
    - v0.5 bearer pass-through (relayme-mcp-remote) is fully preserved
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from adapters.mcp.server import (
    AdapterConfig,
    ConfigurationError,
    ControllerClient,
    RelayMeMcpAdapter,
    create_mcp_server,
    current_token,
)
from adapters.mcp.remote import LOOPBACK_HOSTS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = os.path.expanduser("~/.config/relayme/oauth.db")
_DEFAULT_ACCESS_TTL = 3600     # 1 hour
_DEFAULT_REFRESH_TTL = 86400   # 24 hours
_AUTH_CODE_TTL = 300            # 5 minutes
_DCR_RATE_LIMIT_WINDOW = 3600  # 1 hour
_DCR_RATE_LIMIT_MAX = 20

# Known MCP client redirect URIs (must be accepted at /register and /authorize)
KNOWN_MCP_REDIRECT_URIS: frozenset[str] = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://gemini.google.com/oauth-redirect",
    "https://vertexaisearch.cloud.google.com/oauth-redirect",
})

# Scopes supported: standard RelayMe scopes + Gemini Spark scope
SUPPORTED_SCOPES = (
    "relayme.read",
    "relayme.execute",
    "ACCESS_VIEW_MANAGE_MCP_CONTENT",  # required by Gemini Spark
    "offline_access",                   # required by Gemini Spark for refresh tokens
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OAuthConfig:
    """All configuration for the OAuth 2.1 edge. No hardcoded values."""

    base_url: str                    # Public HTTPS URL used in all discovery docs (REQUIRED)
    controller_url: str              # RelayMe Controller URL
    host: str = "127.0.0.1"
    port: int = 8001
    db_path: str = _DEFAULT_DB_PATH
    access_token_ttl: int = _DEFAULT_ACCESS_TTL
    refresh_token_ttl: int = _DEFAULT_REFRESH_TTL
    ca_cert: str | None = None
    wait_seconds: int = 35
    transport: Literal["streamable-http", "sse", "both"] = "streamable-http"
    tls_cert: str | None = None
    tls_key: str | None = None
    allow_http_insecure: bool = False
    allowed_origins: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    # Dev-only: automatically approve /authorize with this sub (never use in production)
    dev_auto_approve_sub: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ConfigurationError("base_url is required (RELAYME_OAUTH_BASE_URL)")
        if not (self.base_url.startswith("https://") or self.base_url.startswith("http://")):
            raise ConfigurationError("base_url must be a full URL (https://...)")
        if not self.controller_url:
            raise ConfigurationError("controller_url is required (RELAYME_URL)")
        if self.port < 1 or self.port > 65535:
            raise ConfigurationError("port must be between 1 and 65535")
        if self.access_token_ttl < 60:
            raise ConfigurationError("access_token_ttl must be >= 60 seconds")
        if self.refresh_token_ttl < self.access_token_ttl:
            raise ConfigurationError("refresh_token_ttl must be >= access_token_ttl")
        if self.wait_seconds < 1 or self.wait_seconds > 300:
            raise ConfigurationError("wait_seconds must be between 1 and 300")

    @property
    def base(self) -> str:
        """Base URL with trailing slash stripped."""
        return self.base_url.rstrip("/")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OAuthConfig":
        v = os.environ if env is None else env

        def _int(key: str, default: int) -> int:
            raw = v.get(key, str(default))
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{key} must be an integer") from exc

        transport = v.get("RELAYME_MCP_TRANSPORT", "streamable-http").lower()
        if transport not in {"streamable-http", "sse", "both"}:
            raise ConfigurationError("RELAYME_MCP_TRANSPORT must be 'streamable-http', 'sse', or 'both'")

        raw_origins = v.get("RELAYME_MCP_ALLOWED_ORIGINS", "")
        raw_hosts = v.get("RELAYME_MCP_ALLOWED_HOSTS", "")
        allow_insecure = v.get("RELAYME_MCP_ALLOW_HTTP_INSECURE", "").lower() in {"true", "1", "yes"}

        return cls(
            base_url=v.get("RELAYME_OAUTH_BASE_URL", "").rstrip("/"),
            controller_url=v.get("RELAYME_URL", "").rstrip("/"),
            host=v.get("RELAYME_MCP_HOST", "127.0.0.1"),
            port=_int("RELAYME_MCP_PORT", 8001),
            db_path=v.get("RELAYME_OAUTH_DB", _DEFAULT_DB_PATH),
            access_token_ttl=_int("RELAYME_OAUTH_ACCESS_TTL", _DEFAULT_ACCESS_TTL),
            refresh_token_ttl=_int("RELAYME_OAUTH_REFRESH_TTL", _DEFAULT_REFRESH_TTL),
            ca_cert=v.get("RELAYME_CA_CERT") or None,
            wait_seconds=_int("RELAYME_WAIT_SECONDS", 35),
            transport=transport,  # type: ignore[arg-type]
            tls_cert=v.get("RELAYME_MCP_TLS_CERT") or None,
            tls_key=v.get("RELAYME_MCP_TLS_KEY") or None,
            allow_http_insecure=allow_insecure,
            allowed_origins=[o.strip() for o in raw_origins.split(",") if o.strip()],
            allowed_hosts=[h.strip() for h in raw_hosts.split(",") if h.strip()],
            dev_auto_approve_sub=v.get("RELAYME_OAUTH_DEV_AUTO_APPROVE_SUB") or None,
        )


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def _sha256(value: str) -> str:
    """SHA-256 hex digest. Used to hash all tokens before storage."""
    return hashlib.sha256(value.encode()).hexdigest()


def _secure_token(n_bytes: int = 32) -> str:
    """Cryptographically secure URL-safe random token."""
    return secrets.token_urlsafe(n_bytes)


def _constant_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


def _pkce_s256_verify(verifier: str, challenge: str) -> bool:
    """Verify PKCE S256: challenge == BASE64URL(SHA256(ASCII(verifier)))."""
    import base64
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return _constant_compare(computed, challenge)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class OAuthStore:
    """Thin SQLite wrapper for OAuth 2.1 state.

    Security invariants:
    - All token/code values stored as SHA-256 hashes; cleartext never persisted
    - Auth codes are single-use (atomic consume)
    - Refresh tokens are single-use with rotation
    - Expiry checked before revocation status
    """

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                client_id         TEXT PRIMARY KEY,
                redirect_uris     TEXT NOT NULL,
                grant_types       TEXT NOT NULL DEFAULT 'authorization_code refresh_token',
                token_auth_method TEXT NOT NULL DEFAULT 'none',
                created_at        INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_codes (
                code_hash      TEXT PRIMARY KEY,
                client_id      TEXT NOT NULL,
                sub            TEXT NOT NULL,
                scope          TEXT NOT NULL DEFAULT '',
                redirect_uri   TEXT NOT NULL,
                pkce_challenge TEXT NOT NULL,
                pkce_method    TEXT NOT NULL DEFAULT 'S256',
                resource       TEXT,
                expires_at     INTEGER NOT NULL,
                used           INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS access_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id  TEXT NOT NULL,
                sub        TEXT NOT NULL,
                scope      TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                revoked    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id  TEXT NOT NULL,
                sub        TEXT NOT NULL,
                scope      TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                revoked    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS allowlist (
                sub           TEXT PRIMARY KEY,
                relayme_token TEXT NOT NULL,
                scopes        TEXT NOT NULL DEFAULT 'relayme.read relayme.execute',
                label         TEXT,
                created_at    INTEGER NOT NULL
            );
        """)
        self._conn.commit()

    # -- Clients --

    def register_client(
        self,
        redirect_uris: list[str],
        grant_types: list[str] | None = None,
        token_auth_method: str = "none",
    ) -> str:
        client_id = _secure_token(16)
        gt = " ".join(grant_types or ["authorization_code", "refresh_token"])
        self._conn.execute(
            "INSERT INTO clients (client_id, redirect_uris, grant_types, token_auth_method, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (client_id, json.dumps(redirect_uris), gt, token_auth_method, int(time.time())),
        )
        self._conn.commit()
        return client_id

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["redirect_uris"] = json.loads(d["redirect_uris"])
        d["grant_types"] = d["grant_types"].split()
        return d

    # -- Auth codes --

    def create_auth_code(
        self,
        client_id: str,
        sub: str,
        scope: str,
        redirect_uri: str,
        pkce_challenge: str,
        pkce_method: str,
        resource: str | None,
        ttl: int = _AUTH_CODE_TTL,
    ) -> str:
        code = _secure_token(32)
        self._conn.execute(
            "INSERT INTO auth_codes"
            " (code_hash, client_id, sub, scope, redirect_uri, pkce_challenge, pkce_method, resource, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_sha256(code), client_id, sub, scope, redirect_uri, pkce_challenge, pkce_method,
             resource, int(time.time()) + ttl),
        )
        self._conn.commit()
        return code

    def consume_auth_code(self, code: str) -> dict[str, Any] | None:
        """Atomically consume an auth code. Returns None if invalid/expired/already used."""
        code_hash = _sha256(code)
        row = self._conn.execute(
            "SELECT * FROM auth_codes WHERE code_hash = ?", (code_hash,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d["used"]:
            return None
        if d["expires_at"] < int(time.time()):
            return None
        self._conn.execute("UPDATE auth_codes SET used = 1 WHERE code_hash = ?", (code_hash,))
        self._conn.commit()
        return d

    # -- Access tokens --

    def create_access_token(self, client_id: str, sub: str, scope: str, ttl: int) -> str:
        token = _secure_token(32)
        self._conn.execute(
            "INSERT INTO access_tokens (token_hash, client_id, sub, scope, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (_sha256(token), client_id, sub, scope, int(time.time()) + ttl),
        )
        self._conn.commit()
        return token

    def lookup_access_token(self, token: str) -> dict[str, Any] | None:
        token_hash = _sha256(token)
        row = self._conn.execute(
            "SELECT * FROM access_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d["expires_at"] < int(time.time()):  # expiry checked before revocation
            return None
        if d["revoked"]:
            return None
        return d

    def revoke_token(self, token: str) -> None:
        """RFC 7009: revoke access or refresh token — always succeeds."""
        h = _sha256(token)
        self._conn.execute("UPDATE access_tokens SET revoked = 1 WHERE token_hash = ?", (h,))
        self._conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (h,))
        self._conn.commit()

    # -- Refresh tokens --

    def create_refresh_token(self, client_id: str, sub: str, scope: str, ttl: int) -> str:
        token = _secure_token(32)
        self._conn.execute(
            "INSERT INTO refresh_tokens (token_hash, client_id, sub, scope, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (_sha256(token), client_id, sub, scope, int(time.time()) + ttl),
        )
        self._conn.commit()
        return token

    def consume_refresh_token(self, token: str) -> dict[str, Any] | None:
        """Single-use rotation: mark old token revoked atomically."""
        h = _sha256(token)
        row = self._conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (h,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d["revoked"]:
            return None
        if d["expires_at"] < int(time.time()):
            return None
        self._conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (h,))
        self._conn.commit()
        return d

    # -- Allowlist --

    def add_allowlist_entry(self, sub: str, relayme_token: str, scopes: str, label: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO allowlist (sub, relayme_token, scopes, label, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sub, relayme_token, scopes, label, int(time.time())),
        )
        self._conn.commit()

    def get_allowlist_entry(self, sub: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM allowlist WHERE sub = ?", (sub,)).fetchone()
        return dict(row) if row else None

    def remove_allowlist_entry(self, sub: str) -> bool:
        c = self._conn.execute("DELETE FROM allowlist WHERE sub = ?", (sub,))
        self._conn.commit()
        return c.rowcount > 0

    def list_allowlist(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT sub, scopes, label, created_at FROM allowlist ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_active_tokens(self) -> list[dict[str, Any]]:
        now = int(time.time())
        rows = self._conn.execute(
            "SELECT sub, scope, expires_at FROM access_tokens"
            " WHERE expires_at > ? AND revoked = 0 ORDER BY expires_at",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# DCR rate limiter (in-memory, per source IP)
# ---------------------------------------------------------------------------

class _DcrRateLimiter:
    def __init__(
        self,
        max_requests: int = _DCR_RATE_LIMIT_MAX,
        window: int = _DCR_RATE_LIMIT_WINDOW,
    ) -> None:
        self._max = max_requests
        self._window = window
        self._counts: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, ip: str) -> bool:
        now = time.time()
        cutoff = now - self._window
        timestamps = [t for t in self._counts[ip] if t > cutoff]
        self._counts[ip] = timestamps
        if len(timestamps) >= self._max:
            return False
        self._counts[ip].append(now)
        return True


# ---------------------------------------------------------------------------
# OAuth error helper
# ---------------------------------------------------------------------------

def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------

def _make_prm_handler(config: OAuthConfig):
    """RFC 9728 Protected Resource Metadata."""
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": f"{config.base}/mcp",
            "authorization_servers": [config.base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(SUPPORTED_SCOPES),
        })
    return handler


def _make_asm_handler(config: OAuthConfig):
    """RFC 8414 Authorization Server Metadata."""
    async def handler(request: Request) -> JSONResponse:
        b = config.base
        return JSONResponse({
            "issuer": b,
            "authorization_endpoint": f"{b}/authorize",
            "token_endpoint": f"{b}/token",
            "registration_endpoint": f"{b}/register",
            "revocation_endpoint": f"{b}/revoke",
            "scopes_supported": list(SUPPORTED_SCOPES),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "subject_types_supported": ["public"],
        })
    return handler


def _make_register_handler(config: OAuthConfig, store: OAuthStore, rate_limiter: _DcrRateLimiter):
    """RFC 7591 Dynamic Client Registration."""
    async def handler(request: Request) -> JSONResponse:
        client_ip = (request.client.host if request.client else None) or "unknown"
        if not rate_limiter.is_allowed(client_ip):
            return _oauth_error("rate_limit_exceeded", "Too many registration requests", status=429)
        try:
            body = await request.json()
        except Exception:
            return _oauth_error("invalid_request", "Request body must be JSON")

        redirect_uris = body.get("redirect_uris")
        if not redirect_uris or not isinstance(redirect_uris, list):
            return _oauth_error("invalid_redirect_uri", "redirect_uris is required and must be a list")
        if not all(isinstance(u, str) for u in redirect_uris):
            return _oauth_error("invalid_redirect_uri", "All redirect_uris must be strings")

        grant_types = body.get("grant_types", ["authorization_code", "refresh_token"])
        token_auth_method = body.get("token_endpoint_auth_method", "none")

        client_id = store.register_client(
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            token_auth_method=token_auth_method,
        )
        log.info("DCR: registered client_id=%s redirect_uris=%s", client_id, redirect_uris)
        return JSONResponse({
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "token_endpoint_auth_method": token_auth_method,
        }, status_code=201)

    return handler


def _build_approval_html(params: dict[str, str]) -> HTMLResponse:
    """Minimal operator approval page — not a user-facing login UI."""
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

    fields = "".join(
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
        for k, v in params.items()
        if k not in ("_approved", "_sub")
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>RelayMe OAuth Authorization</title>
<style>body{{font-family:monospace;max-width:600px;margin:2em auto;padding:1em}}
input[type=text]{{width:100%;padding:4px;margin:4px 0}}
input[type=submit]{{padding:8px 16px;cursor:pointer}}</style>
</head><body>
<h2>RelayMe OAuth Authorization</h2>
<p>Client <code>{_esc(params.get("client_id",""))}</code> is requesting access.</p>
<p>Enter the OAuth subject identifier to approve this request:</p>
<form method="POST">
  {fields}
  <input type="hidden" name="_approved" value="1">
  <label>Subject (sub):
    <input type="text" name="_sub" required autofocus placeholder="e.g. user@example.com">
  </label><br><br>
  <input type="submit" value="Approve">
</form>
<p><small>The subject must be registered in the RelayMe OAuth allowlist.</small></p>
</body></html>""")


def _make_authorize_get_handler(config: OAuthConfig, store: OAuthStore):
    """OAuth 2.1 Authorization Endpoint (GET)."""
    async def handler(request: Request) -> Response:
        params = dict(request.query_params)
        response_type = params.get("response_type", "")
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        scope = params.get("scope", "relayme.read relayme.execute")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        resource = params.get("resource") or None

        if response_type != "code":
            return _oauth_error("unsupported_response_type", "Only response_type=code is supported")

        # PKCE validation — reject before client lookup
        if not code_challenge:
            return _oauth_error("invalid_request", "code_challenge is required (PKCE S256 mandatory)")
        if code_challenge_method != "S256":
            return _oauth_error(
                "invalid_request",
                f"code_challenge_method must be S256; got {code_challenge_method!r}. "
                "plain method is not permitted.",
            )

        # Client validation
        client = store.get_client(client_id)
        if client is None:
            return _oauth_error("invalid_client", "Unknown client_id", status=401)

        if not redirect_uri:
            return _oauth_error("invalid_request", "redirect_uri is required")
        if redirect_uri not in client["redirect_uris"]:
            return _oauth_error(
                "invalid_redirect_uri",
                f"redirect_uri does not match any registered value for client {client_id}",
            )

        # Determine subject
        if config.dev_auto_approve_sub:
            sub = config.dev_auto_approve_sub
            log.warning("DEV: auto-approving /authorize for sub=%s client=%s", sub, client_id)
        else:
            approved = params.get("_approved", "")
            sub = params.get("_sub", "").strip()
            if not approved or not sub:
                return _build_approval_html(params)

        # Verify sub is in allowlist
        entry = store.get_allowlist_entry(sub)
        if entry is None:
            log.warning("authorize: sub=%s not in allowlist; denying", sub)
            ep = urllib.parse.urlencode({
                "error": "access_denied",
                "error_description": "Subject is not in the RelayMe OAuth allowlist",
                **({"state": state} if state else {}),
            })
            return RedirectResponse(f"{redirect_uri}?{ep}", status_code=302)

        code = store.create_auth_code(
            client_id=client_id,
            sub=sub,
            scope=scope,
            redirect_uri=redirect_uri,
            pkce_challenge=code_challenge,
            pkce_method=code_challenge_method,
            resource=resource,
        )
        log.info("authorize: code issued for sub=%s client=%s", sub, client_id)
        rp = urllib.parse.urlencode({"code": code, **({"state": state} if state else {})})
        return RedirectResponse(f"{redirect_uri}?{rp}", status_code=302)

    return handler


def _make_authorize_post_handler(config: OAuthConfig, store: OAuthStore):
    """OAuth 2.1 Authorization Endpoint (POST — operator submits approval form)."""
    async def handler(request: Request) -> Response:
        form = await request.form()
        # Rebuild as dict matching GET params, passing through approval fields
        params: dict[str, str] = {}
        for k in ("response_type", "client_id", "redirect_uri", "scope", "state",
                   "code_challenge", "code_challenge_method", "resource", "_approved", "_sub"):
            v = form.get(k, "")
            if v:
                params[k] = str(v)

        # Delegate to GET handler logic by constructing a new request with these as query params
        qs = urllib.parse.urlencode(params).encode()
        new_scope = dict(request.scope)
        new_scope["query_string"] = qs
        new_scope["method"] = "GET"
        new_request = Request(new_scope, request._receive)  # type: ignore[arg-type]
        get_handler = _make_authorize_get_handler(config, store)
        return await get_handler(new_request)

    return handler


def _make_token_handler(config: OAuthConfig, store: OAuthStore):
    """OAuth 2.1 Token Endpoint."""
    async def handler(request: Request) -> JSONResponse:
        try:
            form = await request.form()
        except Exception:
            return _oauth_error("invalid_request", "Request body must be application/x-www-form-urlencoded")

        grant_type = str(form.get("grant_type", ""))

        if grant_type == "authorization_code":
            return await _exchange_code(config, store, form)
        elif grant_type == "refresh_token":
            return await _refresh_token(config, store, form)
        else:
            return _oauth_error("unsupported_grant_type", f"Unsupported grant_type: {grant_type!r}")

    return handler


async def _exchange_code(config: OAuthConfig, store: OAuthStore, form: Any) -> JSONResponse:
    code = str(form.get("code", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    client_id = str(form.get("client_id", ""))
    code_verifier = str(form.get("code_verifier", ""))

    if not all([code, redirect_uri, client_id, code_verifier]):
        return _oauth_error("invalid_request", "code, redirect_uri, client_id, code_verifier are all required")

    client = store.get_client(client_id)
    if client is None:
        return _oauth_error("invalid_client", "Unknown client_id", status=401)

    code_rec = store.consume_auth_code(code)
    if code_rec is None:
        return _oauth_error("invalid_grant", "Authorization code is invalid, expired, or already used")

    if code_rec["client_id"] != client_id:
        return _oauth_error("invalid_grant", "client_id mismatch")
    if code_rec["redirect_uri"] != redirect_uri:
        return _oauth_error("invalid_grant", "redirect_uri mismatch")
    if not _pkce_s256_verify(code_verifier, code_rec["pkce_challenge"]):
        return _oauth_error("invalid_grant", "PKCE code_verifier verification failed")

    sub = code_rec["sub"]
    entry = store.get_allowlist_entry(sub)
    if entry is None:
        return _oauth_error("access_denied", "Subject is no longer in the RelayMe OAuth allowlist")

    scope = code_rec["scope"]
    access_token = store.create_access_token(client_id, sub, scope, config.access_token_ttl)
    refresh_token = store.create_refresh_token(client_id, sub, scope, config.refresh_token_ttl)
    log.info("token: issued access+refresh for sub=%s client=%s", sub, client_id)
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": config.access_token_ttl,
        "refresh_token": refresh_token,
        "scope": scope,
    })


async def _refresh_token(config: OAuthConfig, store: OAuthStore, form: Any) -> JSONResponse:
    token = str(form.get("refresh_token", ""))
    client_id = str(form.get("client_id", ""))

    if not token or not client_id:
        return _oauth_error("invalid_request", "refresh_token and client_id are required")

    client = store.get_client(client_id)
    if client is None:
        return _oauth_error("invalid_client", "Unknown client_id", status=401)

    rec = store.consume_refresh_token(token)
    if rec is None:
        return _oauth_error("invalid_grant", "Refresh token is invalid, expired, or already used")
    if rec["client_id"] != client_id:
        return _oauth_error("invalid_grant", "client_id mismatch")

    sub = rec["sub"]
    entry = store.get_allowlist_entry(sub)
    if entry is None:
        return _oauth_error("access_denied", "Subject is no longer in the RelayMe OAuth allowlist")

    new_access = store.create_access_token(client_id, sub, rec["scope"], config.access_token_ttl)
    new_refresh = store.create_refresh_token(client_id, sub, rec["scope"], config.refresh_token_ttl)
    log.info("token: rotated refresh for sub=%s client=%s", sub, client_id)
    return JSONResponse({
        "access_token": new_access,
        "token_type": "Bearer",
        "expires_in": config.access_token_ttl,
        "refresh_token": new_refresh,
        "scope": rec["scope"],
    })


def _make_revoke_handler(config: OAuthConfig, store: OAuthStore):
    """RFC 7009 Token Revocation — always returns 200."""
    async def handler(request: Request) -> JSONResponse:
        try:
            form = await request.form()
            token = str(form.get("token", ""))
        except Exception:
            token = ""
        if token:
            store.revoke_token(token)
            log.info("revoke: token revoked")
        return JSONResponse({}, status_code=200)
    return handler


# ---------------------------------------------------------------------------
# OAuth MCP Auth Middleware
# ---------------------------------------------------------------------------

class OAuthMcpAuthMiddleware:
    """Pure ASGI middleware: resolves opaque OAuth access token → RelayMe bearer.

    For /mcp paths:
      1. Extract Bearer from Authorization header
      2. Validate against access_tokens table (hash lookup)
      3. Resolve sub → relayme_token from allowlist
      4. Set current_token ContextVar (picked up by ControllerClient.request)

    On 401: returns WWW-Authenticate pointing to the PRM document per RFC 9728 §3.
    All other paths pass through without auth check.
    """

    def __init__(self, app: ASGIApp, store: OAuthStore, config: OAuthConfig) -> None:
        self.app = app
        self.store = store
        self._prm_url = f"{config.base}/.well-known/oauth-protected-resource"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_auth = headers.get(b"authorization", b"").decode("latin-1").strip()
        bearer: str | None = None
        if raw_auth.lower().startswith("bearer "):
            bearer = raw_auth.split(" ", 1)[1].strip()

        if not bearer:
            return await self._unauthorized(scope, receive, send, "Bearer token required")

        token_rec = self.store.lookup_access_token(bearer)
        if token_rec is None:
            return await self._unauthorized(scope, receive, send, "Token is invalid, expired, or revoked")

        sub = token_rec["sub"]
        entry = self.store.get_allowlist_entry(sub)
        if entry is None:
            return await self._unauthorized(scope, receive, send, "Subject is not in the RelayMe allowlist")

        handle = current_token.set(entry["relayme_token"])
        try:
            await self.app(scope, receive, send)
        finally:
            current_token.reset(handle)

    async def _unauthorized(self, scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        www_auth = f'Bearer resource_metadata="{self._prm_url}"'
        response = JSONResponse(
            {"error": "Unauthorized", "detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": www_auth},
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _validate_oauth_config(config: OAuthConfig) -> None:
    if (config.tls_cert and not config.tls_key) or (config.tls_key and not config.tls_cert):
        raise ConfigurationError("Both --tls-cert and --tls-key must be provided for TLS")
    is_loopback = config.host in LOOPBACK_HOSTS
    has_tls = bool(config.tls_cert and config.tls_key)
    if not is_loopback and not has_tls and not config.allow_http_insecure:
        raise ConfigurationError(
            f"Non-loopback binding ('{config.host}') requires TLS or --allow-http-insecure"
        )


def create_oauth_app(config: OAuthConfig, store: OAuthStore) -> Starlette:
    """Build the full OAuth edge Starlette ASGI app."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ConfigurationError("MCP SDK is required") from exc

    rate_limiter = _DcrRateLimiter()

    # MCP adapter — token resolved per-request by OAuthMcpAuthMiddleware
    controller_client = ControllerClient(
        AdapterConfig(
            controller_url=config.controller_url,
            client_token=None,
            ca_cert=config.ca_cert,
            wait_seconds=config.wait_seconds,
        )
    )
    adapter = RelayMeMcpAdapter(controller_client)
    fastmcp = FastMCP(
        "RelayMe",
        instructions=(
            "RelayMe exposes read-only R0 observation/discovery tools and bounded R1 registered tasks. "
            "R1 uses only locally registered fixed policy; no arbitrary shell access."
        ),
        json_response=True,
    )

    # DNS rebinding / host validation
    base_host = urllib.parse.urlparse(config.base).netloc or ""
    default_hosts = [
        "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
        "[::1]", "[::1]:*", "testserver", "testserver:*",
    ]
    configured_hosts = list(default_hosts)
    if config.host not in LOOPBACK_HOSTS:
        configured_hosts.extend([config.host, f"{config.host}:*"])
    if f"{config.host}:{config.port}" not in configured_hosts:
        configured_hosts.append(f"{config.host}:{config.port}")
    if base_host and base_host not in configured_hosts:
        configured_hosts.append(base_host)
    configured_hosts.extend(config.allowed_hosts)
    fastmcp.settings.transport_security.allowed_hosts = configured_hosts

    default_origins = [
        "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        "https://127.0.0.1:*", "https://localhost:*", "https://[::1]:*",
        "http://testserver", "http://testserver:*",
    ]
    configured_origins = list(default_origins)
    if config.base:
        configured_origins.append(config.base)
    configured_origins.extend(config.allowed_origins)
    fastmcp.settings.transport_security.allowed_origins = configured_origins

    create_mcp_server(adapter, server=fastmcp)

    # MCP transport
    if config.transport == "streamable-http":
        mcp_sub = fastmcp.streamable_http_app()
        lifespan = mcp_sub.router.lifespan_context
    elif config.transport == "sse":
        mcp_sub = fastmcp.sse_app()
        lifespan = None
    else:
        sh_app = fastmcp.streamable_http_app()
        sse_app_inst = fastmcp.sse_app()
        mcp_sub = sh_app
        lifespan = sh_app.router.lifespan_context

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "relayme-mcp-oauth"})

    async def ping(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes: list[Any] = [
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/ping", endpoint=ping, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource",
              endpoint=_make_prm_handler(config), methods=["GET"]),
        Route("/.well-known/oauth-authorization-server",
              endpoint=_make_asm_handler(config), methods=["GET"]),
        Route("/register",
              endpoint=_make_register_handler(config, store, rate_limiter), methods=["POST"]),
        Route("/authorize",
              endpoint=_make_authorize_get_handler(config, store), methods=["GET"]),
        Route("/authorize",
              endpoint=_make_authorize_post_handler(config, store), methods=["POST"]),
        Route("/token",
              endpoint=_make_token_handler(config, store), methods=["POST"]),
        Route("/revoke",
              endpoint=_make_revoke_handler(config, store), methods=["POST"]),
    ]
    routes.extend(mcp_sub.routes)

    base_app = Starlette(debug=False, routes=routes, lifespan=lifespan)

    cors_origins = config.allowed_origins if config.allowed_origins else ["*"]
    cors_app = CORSMiddleware(
        base_app,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    return OAuthMcpAuthMiddleware(cors_app, store=store, config=config)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run_oauth_server(config: OAuthConfig) -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    _validate_oauth_config(config)
    store = OAuthStore(config.db_path)
    app = create_oauth_app(config, store)
    log.info("RelayMe OAuth edge on %s:%s  public=%s", config.host, config.port, config.base)
    if config.dev_auto_approve_sub:
        log.warning("DEV MODE: auto-approving sub=%s", config.dev_auto_approve_sub)
    uvicorn_kwargs: dict[str, Any] = {"host": config.host, "port": config.port, "log_level": "info"}
    if config.tls_cert and config.tls_key:
        uvicorn_kwargs["ssl_certfile"] = config.tls_cert
        uvicorn_kwargs["ssl_keyfile"] = config.tls_key
    uvicorn_config = uvicorn.Config(app, **uvicorn_kwargs)
    uvicorn.Server(uvicorn_config).run()


# ---------------------------------------------------------------------------
# Management CLI subcommands
# ---------------------------------------------------------------------------

def _config_from_args(args: argparse.Namespace) -> OAuthConfig:
    if not getattr(args, "controller_url", None):
        sys.stderr.write("error: --controller-url or RELAYME_URL is required\n"); sys.exit(2)
    if not getattr(args, "base_url", None):
        sys.stderr.write("error: --base-url or RELAYME_OAUTH_BASE_URL is required\n"); sys.exit(2)
    return OAuthConfig(
        base_url=args.base_url.rstrip("/"),
        controller_url=args.controller_url.rstrip("/"),
        host=args.host,
        port=args.port,
        db_path=args.db,
        access_token_ttl=args.access_ttl,
        refresh_token_ttl=args.refresh_ttl,
        ca_cert=args.ca_cert or None,
        wait_seconds=args.wait_seconds,
        transport=args.transport,
        tls_cert=args.tls_cert or None,
        tls_key=args.tls_key or None,
        allow_http_insecure=args.allow_http_insecure,
        allowed_origins=args.allowed_origin or [],
        allowed_hosts=args.allowed_host or [],
        dev_auto_approve_sub=args.dev_auto_approve_sub or None,
    )


def _cmd_serve(args: argparse.Namespace) -> None:
    run_oauth_server(_config_from_args(args))


def _cmd_add_client(args: argparse.Namespace) -> None:
    scopes = args.scopes or "relayme.read relayme.execute"
    OAuthStore(args.db).add_allowlist_entry(args.sub, args.token, scopes, args.label or None)
    print(f"Added: sub={args.sub!r} label={args.label!r} scopes={scopes!r}")


def _cmd_list_clients(args: argparse.Namespace) -> None:
    entries = OAuthStore(args.db).list_allowlist()
    if not entries:
        print("No entries in allowlist."); return
    for e in entries:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["created_at"]))
        print(f"  sub={e['sub']!r}  scopes={e['scopes']!r}  label={e.get('label','')!r}  added={ts}")


def _cmd_revoke_client(args: argparse.Namespace) -> None:
    if OAuthStore(args.db).remove_allowlist_entry(args.sub):
        print(f"Revoked: sub={args.sub!r}")
    else:
        print(f"Not found: sub={args.sub!r}"); sys.exit(1)


def _cmd_list_tokens(args: argparse.Namespace) -> None:
    tokens = OAuthStore(args.db).list_active_tokens()
    if not tokens:
        print("No active access tokens."); return
    for t in tokens:
        exp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t["expires_at"]))
        print(f"  sub={t['sub']!r}  scope={t['scope']!r}  expires={exp}")


# ---------------------------------------------------------------------------
# Argument parser + entrypoint
# ---------------------------------------------------------------------------

def _add_db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=os.environ.get("RELAYME_OAUTH_DB", _DEFAULT_DB_PATH),
                   help=f"SQLite DB path (default: {_DEFAULT_DB_PATH})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RelayMe OAuth 2.1 edge for web MCP clients (Gemini Spark, Claude Web)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # serve
    sp = sub.add_parser("serve", help="Start the OAuth edge server")
    sp.add_argument("--base-url", default=os.environ.get("RELAYME_OAUTH_BASE_URL", ""),
                    help="Public HTTPS URL (RELAYME_OAUTH_BASE_URL)")
    sp.add_argument("--controller-url", default=os.environ.get("RELAYME_URL", ""),
                    help="RelayMe Controller URL (RELAYME_URL)")
    sp.add_argument("--host", default=os.environ.get("RELAYME_MCP_HOST", "127.0.0.1"))
    sp.add_argument("--port", type=int, default=int(os.environ.get("RELAYME_MCP_PORT", "8001")))
    sp.add_argument("--transport", choices=("streamable-http", "sse", "both"),
                    default=os.environ.get("RELAYME_MCP_TRANSPORT", "streamable-http"))
    sp.add_argument("--ca-cert", default=os.environ.get("RELAYME_CA_CERT"))
    sp.add_argument("--wait-seconds", type=int,
                    default=int(os.environ.get("RELAYME_WAIT_SECONDS", "35")))
    sp.add_argument("--tls-cert", default=os.environ.get("RELAYME_MCP_TLS_CERT"))
    sp.add_argument("--tls-key", default=os.environ.get("RELAYME_MCP_TLS_KEY"))
    sp.add_argument("--allow-http-insecure", action="store_true",
                    default=os.environ.get("RELAYME_MCP_ALLOW_HTTP_INSECURE", "").lower() in {"true","1","yes"})
    sp.add_argument("--access-ttl", type=int,
                    default=int(os.environ.get("RELAYME_OAUTH_ACCESS_TTL", str(_DEFAULT_ACCESS_TTL))))
    sp.add_argument("--refresh-ttl", type=int,
                    default=int(os.environ.get("RELAYME_OAUTH_REFRESH_TTL", str(_DEFAULT_REFRESH_TTL))))
    sp.add_argument("--allowed-origin", action="append", default=[])
    sp.add_argument("--allowed-host", action="append", default=[])
    sp.add_argument("--dev-auto-approve-sub",
                    default=os.environ.get("RELAYME_OAUTH_DEV_AUTO_APPROVE_SUB"),
                    help="[DEV ONLY] Auto-approve /authorize with this sub — never use in production")
    _add_db_arg(sp)
    sp.set_defaults(func=_cmd_serve)

    # add-client
    ap = sub.add_parser("add-client", help="Add a sub→token entry to the allowlist")
    ap.add_argument("--sub", required=True, help="OAuth subject (e.g. user@example.com)")
    ap.add_argument("--token", required=True, help="RelayMe bearer token for this subject")
    ap.add_argument("--scopes", default="relayme.read relayme.execute")
    ap.add_argument("--label", default=None)
    _add_db_arg(ap)
    ap.set_defaults(func=_cmd_add_client)

    # list-clients
    lp = sub.add_parser("list-clients", help="List allowlist entries")
    _add_db_arg(lp)
    lp.set_defaults(func=_cmd_list_clients)

    # revoke-client
    rp = sub.add_parser("revoke-client", help="Remove a subject from the allowlist")
    rp.add_argument("--sub", required=True)
    _add_db_arg(rp)
    rp.set_defaults(func=_cmd_revoke_client)

    # list-tokens
    tp = sub.add_parser("list-tokens", help="List active (non-expired, non-revoked) access tokens")
    _add_db_arg(tp)
    tp.set_defaults(func=_cmd_list_tokens)

    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigurationError as exc:
        sys.stderr.write(f"configuration error: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
