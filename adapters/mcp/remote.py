"""Remote MCP transport for RelayMe R0 observation and bounded R1 tasks.

This module provides a production-grade remote Model Context Protocol (MCP) server
suitable for web-based LLM clients (such as Claude Web, Gemini Web/Spark, and future
ChatGPT custom MCP support). It acts as a thin protocol adapter over the RelayMe
Controller HTTP/JSON API, preserving existing authorization boundaries, caller identity,
and bounded execution semantics.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from adapters.mcp.server import (
    AdapterConfig,
    ConfigurationError,
    ControllerClient,
    ControllerError,
    RelayMeMcpAdapter,
    create_mcp_server,
    current_token,
    TOOL_NAMES,
    TOOL_SCHEMAS,
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class RemoteConfig:
    controller_url: str
    host: str = "127.0.0.1"
    port: int = 8000
    default_token: str | None = None
    ca_cert: str | None = None
    wait_seconds: int = 35
    transport: Literal["streamable-http", "sse", "both"] = "streamable-http"
    tls_cert: str | None = None
    tls_key: str | None = None
    allow_http_insecure: bool = False
    allowed_origins: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RemoteConfig":
        values = os.environ if env is None else env
        url = values.get("RELAYME_URL")
        if not url:
            raise ConfigurationError("RELAYME_URL is required")

        host = values.get("RELAYME_MCP_HOST", "127.0.0.1")
        try:
            port = int(values.get("RELAYME_MCP_PORT", "8000"))
        except ValueError as exc:
            raise ConfigurationError("RELAYME_MCP_PORT must be an integer") from exc
        if port < 1 or port > 65535:
            raise ConfigurationError("RELAYME_MCP_PORT must be between 1 and 65535")

        try:
            wait = int(values.get("RELAYME_WAIT_SECONDS", "35"))
        except ValueError as exc:
            raise ConfigurationError("RELAYME_WAIT_SECONDS must be an integer") from exc
        if wait < 1 or wait > 300:
            raise ConfigurationError("RELAYME_WAIT_SECONDS must be between 1 and 300")

        transport = values.get("RELAYME_MCP_TRANSPORT", "streamable-http").lower()
        if transport not in {"streamable-http", "sse", "both"}:
            raise ConfigurationError("RELAYME_MCP_TRANSPORT must be 'streamable-http', 'sse', or 'both'")

        allow_insecure = values.get("RELAYME_MCP_ALLOW_HTTP_INSECURE", "").strip().lower() in {"true", "1", "yes"}

        raw_origins = values.get("RELAYME_MCP_ALLOWED_ORIGINS", "")
        allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

        raw_hosts = values.get("RELAYME_MCP_ALLOWED_HOSTS", "")
        allowed_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]

        return cls(
            controller_url=url.rstrip("/"),
            host=host,
            port=port,
            default_token=values.get("RELAYME_TOKEN") or None,
            ca_cert=values.get("RELAYME_CA_CERT") or None,
            wait_seconds=wait,
            transport=transport,  # type: ignore[arg-type]
            tls_cert=values.get("RELAYME_MCP_TLS_CERT") or None,
            tls_key=values.get("RELAYME_MCP_TLS_KEY") or None,
            allow_http_insecure=allow_insecure,
            allowed_origins=allowed_origins,
            allowed_hosts=allowed_hosts,
        )


class RemoteAuthMiddleware:
    """Pure ASGI middleware extracting HTTP Bearer token and enforcing authentication.

    Unauthenticated requests to /health and /ping are allowed through.
    All other endpoints require either a caller-provided Bearer token or a server-side default token.
    The active token is bound to the `current_token` ContextVar for the duration of the request.
    """

    def __init__(self, app: ASGIApp, default_token: str | None = None):
        self.app = app
        self.default_token = default_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in ("/health", "/ping"):
            await self.app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        raw_auth = headers.get(b"authorization", b"").decode("latin-1").strip()
        bearer_token: str | None = None
        if raw_auth.startswith("Bearer ") or raw_auth.startswith("bearer "):
            bearer_token = raw_auth.split(" ", 1)[1].strip()

        effective_token = bearer_token or self.default_token
        if not effective_token:
            response = JSONResponse(
                {"error": "Unauthorized: missing Bearer token in Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        token_handle = current_token.set(effective_token)
        try:
            await self.app(scope, receive, send)
        finally:
            current_token.reset(token_handle)


def validate_remote_config(config: RemoteConfig) -> None:
    """Validate server configuration and security invariants."""
    if (config.tls_cert and not config.tls_key) or (config.tls_key and not config.tls_cert):
        raise ConfigurationError("Both --tls-cert and --tls-key must be provided for TLS")

    is_loopback = config.host in LOOPBACK_HOSTS
    has_tls = bool(config.tls_cert and config.tls_key)
    if not is_loopback and not has_tls and not config.allow_http_insecure:
        raise ConfigurationError(
            f"Non-loopback binding ('{config.host}') requires TLS (--tls-cert and --tls-key) "
            f"or --allow-http-insecure when deployed behind a TLS-terminating reverse proxy"
        )


def create_remote_app(
    config: RemoteConfig,
    adapter: RelayMeMcpAdapter | None = None,
) -> Starlette:
    """Construct the Starlette ASGI application hosting the remote MCP endpoints."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ConfigurationError("MCP SDK is required; install RelayMe with its MCP dependency") from exc

    if adapter is None:
        controller_client = ControllerClient(
            AdapterConfig(
                controller_url=config.controller_url,
                client_token=config.default_token,
                ca_cert=config.ca_cert,
                wait_seconds=config.wait_seconds,
            )
        )
        adapter = RelayMeMcpAdapter(controller_client)

    fastmcp = FastMCP(
        "RelayMe",
        instructions=(
            "RelayMe exposes seven read-only R0 observation/discovery tools and bounded R1 registered tasks, "
            "including one registered patch-and-test executor task. R1 uses only locally registered fixed policy; "
            "it provides no arbitrary shell, provider-specific authority, or mutation access."
        ),
        json_response=True,
    )

    # Configure transport security (DNS rebinding protection & origin filtering)
    default_allowed_hosts = [
        "127.0.0.1:*", "localhost:*", "[::1]:*",
        "127.0.0.1", "localhost", "[::1]",
        "testserver", "testserver:*",
    ]
    configured_hosts = list(default_allowed_hosts)
    if config.host not in LOOPBACK_HOSTS:
        configured_hosts.extend([config.host, f"{config.host}:*"])
    if f"{config.host}:{config.port}" not in configured_hosts:
        configured_hosts.append(f"{config.host}:{config.port}")
    configured_hosts.extend(config.allowed_hosts)

    default_allowed_origins = [
        "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        "https://127.0.0.1:*", "https://localhost:*", "https://[::1]:*",
        "http://testserver", "http://testserver:*",
    ]
    configured_origins = list(default_allowed_origins)
    configured_origins.extend(config.allowed_origins)

    fastmcp.settings.transport_security.allowed_hosts = configured_hosts
    fastmcp.settings.transport_security.allowed_origins = configured_origins

    # Register all 13 RelayMe MCP tools with shared schemas onto FastMCP instance
    create_mcp_server(adapter, server=fastmcp)

    async def health_endpoint(request: Any) -> Response:
        return JSONResponse({"status": "ok", "service": "relayme-mcp-remote"})

    async def ping_endpoint(request: Any) -> Response:
        return JSONResponse({"status": "ok"})

    routes: list[Route | Mount] = [
        Route("/health", endpoint=health_endpoint, methods=["GET"]),
        Route("/ping", endpoint=ping_endpoint, methods=["GET"]),
    ]

    lifespan = None
    if config.transport == "streamable-http":
        streamable_app = fastmcp.streamable_http_app()
        routes.extend(streamable_app.routes)
        lifespan = streamable_app.router.lifespan_context
    elif config.transport == "sse":
        sse_app = fastmcp.sse_app()
        routes.extend(sse_app.routes)
        lifespan = None
    elif config.transport == "both":
        streamable_app = fastmcp.streamable_http_app()
        sse_app = fastmcp.sse_app()
        routes.extend(streamable_app.routes)
        routes.extend(sse_app.routes)
        lifespan = streamable_app.router.lifespan_context

    base_app = Starlette(debug=False, routes=routes, lifespan=lifespan)

    # Wrap with CORS for browser-based LLM clients
    cors_origins = config.allowed_origins if config.allowed_origins else ["*"]
    cors_app = CORSMiddleware(
        base_app,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Wrap with pure ASGI authentication middleware
    return RemoteAuthMiddleware(cors_app, default_token=config.default_token)  # type: ignore[return-value]


def run_remote_server(config: RemoteConfig) -> None:
    """Run the remote MCP server using Uvicorn."""
    import uvicorn

    validate_remote_config(config)
    app = create_remote_app(config)

    uvicorn_kwargs: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "log_level": "info",
    }
    if config.tls_cert and config.tls_key:
        uvicorn_kwargs["ssl_certfile"] = config.tls_cert
        uvicorn_kwargs["ssl_keyfile"] = config.tls_key

    uvicorn_config = uvicorn.Config(app, **uvicorn_kwargs)
    server = uvicorn.Server(uvicorn_config)
    server.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RelayMe Remote MCP server for web-based LLM clients (Claude Web, Gemini, ChatGPT)",
    )
    parser.add_argument("--controller-url", default=os.environ.get("RELAYME_URL"), help="RelayMe Controller base URL (or RELAYME_URL)")
    parser.add_argument("--host", default=os.environ.get("RELAYME_MCP_HOST", "127.0.0.1"), help="Host address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RELAYME_MCP_PORT", "8000")), help="Port to bind (default: 8000)")
    parser.add_argument("--token", default=os.environ.get("RELAYME_TOKEN"), help="Default RelayMe client token for unauthenticated clients (optional)")
    parser.add_argument("--transport", choices=("streamable-http", "sse", "both"), default=os.environ.get("RELAYME_MCP_TRANSPORT", "streamable-http"), help="Remote transport (default: streamable-http)")
    parser.add_argument("--ca-cert", default=os.environ.get("RELAYME_CA_CERT"), help="CA certificate for Controller HTTPS connection")
    parser.add_argument("--wait-seconds", type=int, default=int(os.environ.get("RELAYME_WAIT_SECONDS", "35")), help="Task wait timeout in seconds (default: 35)")
    parser.add_argument("--tls-cert", default=os.environ.get("RELAYME_MCP_TLS_CERT"), help="Path to TLS certificate for HTTPS")
    parser.add_argument("--tls-key", default=os.environ.get("RELAYME_MCP_TLS_KEY"), help="Path to TLS private key for HTTPS")
    parser.add_argument("--allow-http-insecure", action="store_true", default=os.environ.get("RELAYME_MCP_ALLOW_HTTP_INSECURE", "").lower() in {"true", "1", "yes"}, help="Allow non-loopback HTTP without TLS (for reverse-proxy setups)")
    parser.add_argument("--allowed-origin", action="append", default=[], help="Allowed CORS / DNS-rebinding origin (can be repeated)")
    parser.add_argument("--allowed-host", action="append", default=[], help="Allowed Host header value for DNS-rebinding protection (can be repeated)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.controller_url:
        sys.stderr.write("error: --controller-url or RELAYME_URL environment variable is required\n")
        sys.exit(2)

    config = RemoteConfig(
        controller_url=args.controller_url.rstrip("/"),
        host=args.host,
        port=args.port,
        default_token=args.token or None,
        ca_cert=args.ca_cert or None,
        wait_seconds=args.wait_seconds,
        transport=args.transport,
        tls_cert=args.tls_cert or None,
        tls_key=args.tls_key or None,
        allow_http_insecure=args.allow_http_insecure,
        allowed_origins=args.allowed_origin,
        allowed_hosts=args.allowed_host,
    )

    try:
        run_remote_server(config)
    except ConfigurationError as exc:
        sys.stderr.write(f"configuration error: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
