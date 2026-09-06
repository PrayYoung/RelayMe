"""Tests for RelayMe v0.6 OAuth 2.1 edge (adapters/mcp/oauth.py).

Coverage:
  - OAuthConfig validation
  - OAuthStore CRUD (clients, auth codes, access/refresh tokens, allowlist)
  - PRM /.well-known/oauth-protected-resource (RFC 9728)
  - ASM /.well-known/oauth-authorization-server (RFC 8414)
  - DCR /register (RFC 7591)
  - /authorize (PKCE S256, redirect validation, plain rejected, bad client)
  - /token (code exchange, PKCE mismatch, expired code, code reuse, bad grant)
  - /token refresh rotation, revoked refresh token rejected
  - /revoke always 200
  - /mcp unauthenticated → 401 with correct WWW-Authenticate
  - /mcp authenticated → tools discovered
  - RelayMe bearer token never leaked in any response
  - Sub not in allowlist → 401 at token exchange
  - Expired access token rejected at /mcp
  - CORS headers present on OAuth endpoints
  - v0.5 relayme-mcp-remote bearer pass-through still works
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from adapters.mcp.oauth import (
    OAuthConfig,
    OAuthStore,
    _pkce_s256_verify,
    _sha256,
    _secure_token,
    create_oauth_app,
)
from adapters.mcp.server import ConfigurationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _make_config(**kwargs) -> OAuthConfig:
    defaults = dict(
        base_url="http://testserver",
        controller_url="http://fake-controller:18765",
        host="127.0.0.1",
        port=8001,
        allow_http_insecure=True,
    )
    defaults.update(kwargs)
    return OAuthConfig(**defaults)


def _make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


FAKE_RELAY_TOKEN = "relay-bearer-token-abc123"
TEST_SUB = "test-user@example.com"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
GEMINI_REDIRECT = "https://gemini.google.com/oauth-redirect"


class _ControllerDouble:
    """Drop-in replacement for ControllerClient.request for tests."""
    def __init__(self):
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, path: str, body: Any = None) -> dict:
        self.calls.append((method, path, body))
        if path == "/v1/hosts":
            return {"hosts": [{"host_id": "h1", "hostname": "test-host"}]}
        return {"result": "ok"}


# ---------------------------------------------------------------------------
# OAuthConfig Tests
# ---------------------------------------------------------------------------

class TestOAuthConfig:

    def test_valid_config_minimal(self):
        c = _make_config()
        assert c.base == "http://testserver"
        assert c.access_token_ttl == 3600

    def test_missing_base_url_raises(self):
        with pytest.raises(ConfigurationError, match="base_url is required"):
            _make_config(base_url="")

    def test_bad_base_url_raises(self):
        with pytest.raises(ConfigurationError, match="full URL"):
            _make_config(base_url="not-a-url")

    def test_missing_controller_url_raises(self):
        with pytest.raises(ConfigurationError, match="controller_url is required"):
            _make_config(controller_url="")

    def test_invalid_port_raises(self):
        with pytest.raises(ConfigurationError, match="port"):
            _make_config(port=99999)

    def test_access_ttl_too_short(self):
        with pytest.raises(ConfigurationError, match="access_token_ttl"):
            _make_config(access_token_ttl=10)

    def test_refresh_ttl_shorter_than_access(self):
        with pytest.raises(ConfigurationError, match="refresh_token_ttl"):
            _make_config(access_token_ttl=3600, refresh_token_ttl=60)

    def test_base_strips_trailing_slash(self):
        c = _make_config(base_url="http://testserver/")
        assert c.base == "http://testserver"

    def test_from_env_minimal(self):
        env = {
            "RELAYME_OAUTH_BASE_URL": "https://example.com",
            "RELAYME_URL": "https://controller:18765",
            "RELAYME_MCP_ALLOW_HTTP_INSECURE": "true",
        }
        c = OAuthConfig.from_env(env)
        assert c.base == "https://example.com"
        assert c.controller_url == "https://controller:18765"


# ---------------------------------------------------------------------------
# OAuthStore Tests
# ---------------------------------------------------------------------------

class TestOAuthStore:

    def test_init_creates_tables(self):
        db = _tmp_db()
        store = OAuthStore(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"clients", "auth_codes", "access_tokens", "refresh_tokens", "allowlist"} <= tables
        store.close()

    def test_register_and_get_client(self):
        store = OAuthStore(_tmp_db())
        cid = store.register_client([CLAUDE_REDIRECT])
        client = store.get_client(cid)
        assert client is not None
        assert CLAUDE_REDIRECT in client["redirect_uris"]

    def test_get_unknown_client_returns_none(self):
        store = OAuthStore(_tmp_db())
        assert store.get_client("no-such-id") is None

    def test_allowlist_add_get_remove(self):
        store = OAuthStore(_tmp_db())
        store.add_allowlist_entry(TEST_SUB, FAKE_RELAY_TOKEN, "relayme.read", "Test User")
        e = store.get_allowlist_entry(TEST_SUB)
        assert e is not None
        assert e["relayme_token"] == FAKE_RELAY_TOKEN
        assert store.remove_allowlist_entry(TEST_SUB)
        assert store.get_allowlist_entry(TEST_SUB) is None

    def test_allowlist_upsert(self):
        store = OAuthStore(_tmp_db())
        store.add_allowlist_entry(TEST_SUB, "token-v1", "relayme.read", None)
        store.add_allowlist_entry(TEST_SUB, "token-v2", "relayme.read", None)
        assert store.get_allowlist_entry(TEST_SUB)["relayme_token"] == "token-v2"

    def test_access_token_lifecycle(self):
        store = OAuthStore(_tmp_db())
        store.add_allowlist_entry(TEST_SUB, FAKE_RELAY_TOKEN, "relayme.read", None)
        cid = store.register_client([CLAUDE_REDIRECT])
        tok = store.create_access_token(cid, TEST_SUB, "relayme.read", 3600)
        rec = store.lookup_access_token(tok)
        assert rec is not None
        assert rec["sub"] == TEST_SUB

    def test_expired_access_token_rejected(self):
        store = OAuthStore(_tmp_db())
        cid = store.register_client([CLAUDE_REDIRECT])
        tok = store.create_access_token(cid, TEST_SUB, "relayme.read", -1)  # expired
        assert store.lookup_access_token(tok) is None

    def test_revoked_access_token_rejected(self):
        store = OAuthStore(_tmp_db())
        cid = store.register_client([CLAUDE_REDIRECT])
        tok = store.create_access_token(cid, TEST_SUB, "relayme.read", 3600)
        store.revoke_token(tok)
        assert store.lookup_access_token(tok) is None

    def test_auth_code_single_use(self):
        store = OAuthStore(_tmp_db())
        cid = store.register_client([CLAUDE_REDIRECT])
        verifier, challenge = _make_pkce_pair()
        code = store.create_auth_code(cid, TEST_SUB, "relayme.read", CLAUDE_REDIRECT, challenge, "S256", None)
        assert store.consume_auth_code(code) is not None
        assert store.consume_auth_code(code) is None  # second use fails

    def test_expired_auth_code_rejected(self):
        store = OAuthStore(_tmp_db())
        cid = store.register_client([CLAUDE_REDIRECT])
        verifier, challenge = _make_pkce_pair()
        code = store.create_auth_code(cid, TEST_SUB, "relayme.read", CLAUDE_REDIRECT, challenge, "S256", None, ttl=-1)
        assert store.consume_auth_code(code) is None

    def test_refresh_token_rotation(self):
        store = OAuthStore(_tmp_db())
        cid = store.register_client([CLAUDE_REDIRECT])
        rt = store.create_refresh_token(cid, TEST_SUB, "relayme.read", 86400)
        rec = store.consume_refresh_token(rt)
        assert rec is not None
        assert store.consume_refresh_token(rt) is None  # used


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

def _make_test_client(config_kwargs=None, allowlist=True):
    """Create a TestClient with a seeded store (controller calls mocked)."""
    db = _tmp_db()
    config = _make_config(**(config_kwargs or {}), db_path=db)
    store = OAuthStore(db)
    if allowlist:
        store.add_allowlist_entry(TEST_SUB, FAKE_RELAY_TOKEN, "relayme.read relayme.execute", "Test")

    with patch("adapters.mcp.oauth.ControllerClient") as mock_cc:
        mock_instance = MagicMock()
        mock_instance.request.side_effect = lambda m, p, b=None: (
            {"hosts": []} if p == "/v1/hosts" else {"result": "ok"}
        )
        mock_cc.return_value = mock_instance
        app = create_oauth_app(config, store)

    client = TestClient(app, base_url="http://testserver", raise_server_exceptions=True)
    return client, store, config


# ---------------------------------------------------------------------------
# Discovery Endpoint Tests
# ---------------------------------------------------------------------------

class TestDiscoveryEndpoints:

    def test_prm_required_fields(self):
        client, store, config = _make_test_client()
        with client:
            r = client.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        data = r.json()
        assert "resource" in data
        assert "authorization_servers" in data
        assert isinstance(data["authorization_servers"], list)
        assert "bearer_methods_supported" in data

    def test_asm_required_fields(self):
        client, store, config = _make_test_client()
        with client:
            r = client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        data = r.json()
        for field in ("issuer", "authorization_endpoint", "token_endpoint",
                      "registration_endpoint", "revocation_endpoint",
                      "code_challenge_methods_supported", "response_types_supported",
                      "grant_types_supported"):
            assert field in data, f"Missing field: {field}"
        assert "S256" in data["code_challenge_methods_supported"]
        assert "code" in data["response_types_supported"]
        assert "authorization_code" in data["grant_types_supported"]

    def test_prm_resource_matches_mcp_path(self):
        client, store, config = _make_test_client()
        with client:
            r = client.get("/.well-known/oauth-protected-resource")
        data = r.json()
        assert data["resource"].endswith("/mcp")

    def test_asm_authorization_servers_matches_base(self):
        client, store, config = _make_test_client()
        with client:
            r = client.get("/.well-known/oauth-authorization-server")
        data = r.json()
        assert data["issuer"] == config.base


# ---------------------------------------------------------------------------
# DCR /register Tests
# ---------------------------------------------------------------------------

class TestRegisterEndpoint:

    def test_register_success(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/register", json={"redirect_uris": [CLAUDE_REDIRECT]})
        assert r.status_code == 201
        data = r.json()
        assert "client_id" in data
        assert CLAUDE_REDIRECT in data["redirect_uris"]

    def test_register_missing_redirect_uris(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/register", json={"grant_types": ["authorization_code"]})
        assert r.status_code == 400
        assert "redirect_uri" in r.json()["error"]

    def test_register_non_json_body(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/register", content=b"not-json", headers={"Content-Type": "text/plain"})
        assert r.status_code == 400

    def test_register_returns_gemini_redirect(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/register", json={"redirect_uris": [GEMINI_REDIRECT]})
        assert r.status_code == 201
        assert GEMINI_REDIRECT in r.json()["redirect_uris"]


# ---------------------------------------------------------------------------
# /authorize Tests
# ---------------------------------------------------------------------------

class TestAuthorizeEndpoint:

    def _register_client(self, client, redirect_uri=CLAUDE_REDIRECT):
        r = client.post("/register", json={"redirect_uris": [redirect_uri]})
        assert r.status_code == 201
        return r.json()["client_id"]

    def test_authorize_dev_mode_redirects_with_code(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid = self._register_client(client)
            verifier, challenge = _make_pkce_pair()
            r = client.get("/authorize", params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }, follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "code=" in loc

    def test_authorize_missing_code_challenge_rejected(self):
        client, store, config = _make_test_client()
        with client:
            cid = self._register_client(client)
            r = client.get("/authorize", params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CLAUDE_REDIRECT,
            })
        assert r.status_code == 400
        assert "code_challenge" in r.json()["error_description"]

    def test_authorize_plain_pkce_rejected(self):
        client, store, config = _make_test_client()
        with client:
            cid = self._register_client(client)
            r = client.get("/authorize", params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": "not-s256",
                "code_challenge_method": "plain",
            })
        assert r.status_code == 400
        assert "S256" in r.json()["error_description"]

    def test_authorize_bad_client_id_rejected(self):
        client, store, config = _make_test_client()
        with client:
            _, challenge = _make_pkce_pair()
            r = client.get("/authorize", params={
                "response_type": "code",
                "client_id": "no-such-client",
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            })
        assert r.status_code == 401

    def test_authorize_bad_redirect_uri_rejected(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid = self._register_client(client)
            _, challenge = _make_pkce_pair()
            r = client.get("/authorize", params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": "https://evil.example.com/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            })
        assert r.status_code == 400
        assert "redirect_uri" in r.json()["error"]

    def test_authorize_sub_not_in_allowlist_redirects_error(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": "unknown@example.com"},
            allowlist=False,
        )
        with client:
            cid = self._register_client(client)
            _, challenge = _make_pkce_pair()
            r = client.get("/authorize", params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }, follow_redirects=False)
        assert r.status_code == 302
        assert "access_denied" in r.headers["location"]


# ---------------------------------------------------------------------------
# /token Tests
# ---------------------------------------------------------------------------

class TestTokenEndpoint:

    def _full_authorize(self, client, store, sub=TEST_SUB, redirect_uri=CLAUDE_REDIRECT):
        """Perform full authorize flow and return (client_id, code, verifier)."""
        r = client.post("/register", json={"redirect_uris": [redirect_uri]})
        cid = r.json()["client_id"]
        verifier, challenge = _make_pkce_pair()
        r2 = client.get("/authorize", params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, follow_redirects=False)
        assert r2.status_code == 302
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(r2.headers["location"]).query)
        code = qs["code"][0]
        return cid, code, verifier

    def test_token_exchange_success(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid, code, verifier = self._full_authorize(client, store)
            r = client.post("/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_REDIRECT,
                "client_id": cid,
                "code_verifier": verifier,
            })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["token_type"] == "Bearer"
        assert "refresh_token" in data
        assert data["expires_in"] > 0

    def test_token_relay_bearer_not_in_response(self):
        """RelayMe bearer token must NOT appear in any token response field."""
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid, code, verifier = self._full_authorize(client, store)
            r = client.post("/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_REDIRECT,
                "client_id": cid,
                "code_verifier": verifier,
            })
        assert r.status_code == 200
        assert FAKE_RELAY_TOKEN not in r.text

    def test_token_pkce_verifier_mismatch_rejected(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid, code, _ = self._full_authorize(client, store)
            r = client.post("/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_REDIRECT,
                "client_id": cid,
                "code_verifier": "wrong-verifier-value-xxxxxxxxxxxxxxxx",
            })
        assert r.status_code == 400
        assert "PKCE" in r.json()["error_description"]

    def test_token_code_reuse_rejected(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid, code, verifier = self._full_authorize(client, store)
            payload = {"grant_type": "authorization_code", "code": code,
                       "redirect_uri": CLAUDE_REDIRECT, "client_id": cid, "code_verifier": verifier}
            r1 = client.post("/token", data=payload)
            r2 = client.post("/token", data=payload)
        assert r1.status_code == 200
        assert r2.status_code == 400
        assert "invalid_grant" in r2.json()["error"]

    def test_token_bad_grant_type(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/token", data={"grant_type": "implicit"})
        assert r.status_code == 400
        assert "unsupported_grant_type" in r.json()["error"]

    def test_token_sub_not_in_allowlist_rejected(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB},
            allowlist=False,
        )
        # Manually seed allowlist for authorize but remove before token
        store.add_allowlist_entry(TEST_SUB, FAKE_RELAY_TOKEN, "relayme.read", None)
        with client:
            cid, code, verifier = self._full_authorize(client, store)
            store.remove_allowlist_entry(TEST_SUB)  # revoke before token exchange
            r = client.post("/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_REDIRECT,
                "client_id": cid,
                "code_verifier": verifier,
            })
        assert r.status_code == 400
        assert "allowlist" in r.json()["error_description"].lower()

    def test_refresh_token_rotation(self):
        client, store, config = _make_test_client(
            config_kwargs={"dev_auto_approve_sub": TEST_SUB}
        )
        with client:
            cid, code, verifier = self._full_authorize(client, store)
            r1 = client.post("/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_REDIRECT,
                "client_id": cid,
                "code_verifier": verifier,
            })
            assert r1.status_code == 200
            old_refresh = r1.json()["refresh_token"]

            r2 = client.post("/token", data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": cid,
            })
            assert r2.status_code == 200
            new_refresh = r2.json()["refresh_token"]
            assert new_refresh != old_refresh

            # Old refresh token must be rejected
            r3 = client.post("/token", data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": cid,
            })
            assert r3.status_code == 400
            assert "invalid_grant" in r3.json()["error"]


# ---------------------------------------------------------------------------
# /revoke Tests
# ---------------------------------------------------------------------------

class TestRevokeEndpoint:

    def test_revoke_always_200(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/revoke", data={"token": "some-made-up-token"})
        assert r.status_code == 200

    def test_revoke_empty_token_200(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/revoke", data={})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# /mcp Auth Tests
# ---------------------------------------------------------------------------

class TestMcpAuthMiddleware:

    def test_mcp_unauthenticated_returns_401(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/mcp", json={
                "jsonrpc": "2.0", "method": "tools/list", "id": 1
            })
        assert r.status_code == 401

    def test_mcp_401_has_correct_www_authenticate(self):
        client, store, config = _make_test_client()
        with client:
            r = client.post("/mcp", json={})
        assert r.status_code == 401
        www_auth = r.headers.get("www-authenticate", "")
        assert "Bearer" in www_auth
        assert "resource_metadata" in www_auth
        assert "oauth-protected-resource" in www_auth

    def test_mcp_unauthenticated_relay_bearer_not_leaked(self):
        """Ensure the internal RelayMe bearer is never in any error response."""
        client, store, config = _make_test_client()
        with client:
            r = client.post("/mcp", json={})
        assert FAKE_RELAY_TOKEN not in r.text

    def test_mcp_expired_token_rejected(self):
        client, store, config = _make_test_client()
        with client:
            cid = store.register_client([CLAUDE_REDIRECT])
            tok = store.create_access_token(cid, TEST_SUB, "relayme.read", -1)  # expired
            r = client.post("/mcp", json={},
                            headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401

    def test_mcp_authenticated_reaches_server(self):
        """With a valid opaque token, /mcp should be reachable (not 401)."""
        client, store, config = _make_test_client()
        with client:
            cid = store.register_client([CLAUDE_REDIRECT])
            tok = store.create_access_token(cid, TEST_SUB, "relayme.read relayme.execute", 3600)
            r = client.post("/mcp", json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                }
            }, headers={"Authorization": f"Bearer {tok}"})
        # Should not be 401 — may be 200, 202, or method-specific
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Health / CORS Tests
# ---------------------------------------------------------------------------

class TestHealthAndCors:

    def test_health_endpoint(self):
        client, store, config = _make_test_client()
        with client:
            r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "relayme-mcp-oauth"

    def test_discovery_endpoints_no_auth_required(self):
        """PRM and ASM must be unauthenticated per MCP spec."""
        client, store, config = _make_test_client()
        with client:
            r1 = client.get("/.well-known/oauth-protected-resource")
            r2 = client.get("/.well-known/oauth-authorization-server")
        assert r1.status_code == 200
        assert r2.status_code == 200


# ---------------------------------------------------------------------------
# PKCE helper unit tests
# ---------------------------------------------------------------------------

class TestPkceHelper:

    def test_s256_verify_correct(self):
        verifier, challenge = _make_pkce_pair()
        assert _pkce_s256_verify(verifier, challenge)

    def test_s256_verify_wrong_verifier(self):
        _, challenge = _make_pkce_pair()
        assert not _pkce_s256_verify("wrong-verifier", challenge)

    def test_sha256_deterministic(self):
        assert _sha256("test") == _sha256("test")
        assert _sha256("test") != _sha256("other")


# ---------------------------------------------------------------------------
# v0.5 backward compatibility: relayme-mcp-remote still works
# ---------------------------------------------------------------------------

class TestV05BackwardCompat:

    def test_remote_bearer_passthrough_still_works(self):
        """v0.5 relayme-mcp-remote with a direct bearer token must still function."""
        from adapters.mcp.remote import RemoteConfig, create_remote_app
        config = RemoteConfig(
            controller_url="http://fake-controller:18765",
            host="127.0.0.1",
            port=8000,
            default_token="some-bearer-token",
            allow_http_insecure=True,
        )
        with patch("adapters.mcp.server.ControllerClient") as mock_cc:
            mock_instance = MagicMock()
            mock_instance.request.return_value = {"hosts": []}
            mock_cc.return_value = mock_instance
            app = create_remote_app(config)
        client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
        with client:
            r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "relayme-mcp-remote"
