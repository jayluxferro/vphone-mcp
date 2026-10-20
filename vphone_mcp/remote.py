"""Remote transport for vphone-mcp: Streamable HTTP (or legacy SSE) + bearer auth.

stdio stays the default so existing local configs keep working untouched. When
a network transport is selected the whole ASGI app is wrapped in
:class:`AuthMiddleware`, which compares the ``Authorization: Bearer``
header against a static token taken from the environment
(``VPHONE_MCP_AUTH_TOKEN``, or the file named by ``VPHONE_MCP_AUTH_TOKEN_FILE``).
The token never appears in argv — process listings on a shared host would leak
it — so there is deliberately no ``--token`` flag.

The token is the only gate in front of tools that launch VMs and run
vphone-cli with sudo, so a network transport refuses to start without one
unless ``--allow-anonymous`` is passed explicitly.

A second endpoint, ``/files/<path under $VPHONE_ROOT/vphone-mcp>``, serves the
files tools produce (see :mod:`vphone_mcp.files`) to clients that cannot read
the server's disk, and accepts PUT uploads into ``uploads/`` so a remote client
can put an IPA on the host. It
authenticates with HTTP Basic rather than a bearer header — Basic credentials
ride inside the URL itself, so the emitted link works in curl, a browser or a
downloader with nothing else configured — but the credentials are not a second
secret: the password is this same MCP token, and the user is the hash of the
full path the link was minted for.
"""

import argparse
import base64
import binascii
import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import files

STDIO = "stdio"
HTTP = "streamable-http"
SSE = "sse"

TOKEN_ENV = "VPHONE_MCP_AUTH_TOKEN"
TOKEN_FILE_ENV = "VPHONE_MCP_AUTH_TOKEN_FILE"

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_TRUE = frozenset({"1", "true", "yes", "on"})

HEALTH_PATH = "/healthz"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"vphone-mcp: {name} must be an integer, got {raw!r}")


def _read_token() -> str:
    """Token from $VPHONE_MCP_AUTH_TOKEN, else from $VPHONE_MCP_AUTH_TOKEN_FILE."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    path = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"vphone-mcp: cannot read {TOKEN_FILE_ENV}={path}: {exc}")


@dataclass
class RemoteConfig:
    """Resolved transport configuration (env vars, overridden by CLI flags)."""

    transport: str = STDIO
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    token: str = ""
    allow_anonymous: bool = False
    json_response: bool = False
    stateless: bool = False
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    files_enabled: bool = True
    public_url: str = ""

    @property
    def is_network(self) -> bool:
        return self.transport != STDIO

    @property
    def is_loopback(self) -> bool:
        return self.host in _LOOPBACK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vphone-mcp",
        description=(
            "MCP server for vphone-cli iOS VMs. Defaults to stdio; pass "
            "--transport streamable-http to serve remote clients (requires "
            f"${TOKEN_ENV})."
        ),
    )
    p.add_argument(
        "--transport",
        choices=[STDIO, HTTP, SSE, "http"],
        default=os.environ.get("VPHONE_MCP_TRANSPORT", STDIO).strip() or STDIO,
        help="Transport to serve ('http' is an alias for streamable-http). "
        "Env: VPHONE_MCP_TRANSPORT (default: stdio)",
    )
    p.add_argument(
        "--host",
        default=os.environ.get("VPHONE_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        help="Bind address for network transports. Env: VPHONE_MCP_HOST "
        "(default: 127.0.0.1 — use 0.0.0.0 to accept remote clients)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=_env_int("VPHONE_MCP_PORT", 8765),
        help="Bind port for network transports. Env: VPHONE_MCP_PORT (default: 8765)",
    )
    p.add_argument(
        "--path",
        default=os.environ.get("VPHONE_MCP_PATH", "/mcp").strip() or "/mcp",
        help="HTTP path of the streamable-http endpoint. Env: VPHONE_MCP_PATH "
        "(default: /mcp)",
    )
    p.add_argument(
        "--allow-anonymous",
        action="store_true",
        default=_env_flag("VPHONE_MCP_ALLOW_ANONYMOUS"),
        help="Serve a network transport without a token (dangerous — every "
        "tool becomes world-callable). Env: VPHONE_MCP_ALLOW_ANONYMOUS",
    )
    p.add_argument(
        "--json-response",
        action="store_true",
        default=_env_flag("VPHONE_MCP_JSON_RESPONSE"),
        help="Reply with plain JSON instead of an SSE stream (some proxies "
        "buffer SSE). Env: VPHONE_MCP_JSON_RESPONSE",
    )
    p.add_argument(
        "--stateless",
        action="store_true",
        default=_env_flag("VPHONE_MCP_STATELESS"),
        help="New transport per request — no server-side session affinity. "
        "Env: VPHONE_MCP_STATELESS",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        default=None,
        help="Allowed Host header value (repeatable, 'host:*' wildcards the "
        "port). Enables DNS-rebinding protection. Env: VPHONE_MCP_ALLOWED_HOSTS "
        "(comma-separated)",
    )
    p.add_argument(
        "--allowed-origin",
        action="append",
        dest="allowed_origins",
        default=None,
        help="Allowed Origin header value (repeatable). Env: "
        "VPHONE_MCP_ALLOWED_ORIGINS (comma-separated)",
    )
    p.add_argument(
        "--public-url",
        default=os.environ.get("VPHONE_MCP_PUBLIC_URL", "").strip(),
        help="External base URL clients reach this server on, used to build "
        "file links (e.g. https://vphone.example.com). Env: "
        "VPHONE_MCP_PUBLIC_URL — set it whenever a proxy or a wildcard bind "
        "address means the bind host is not what clients dial",
    )
    p.add_argument(
        "--no-files",
        action="store_true",
        default=_env_flag("VPHONE_MCP_DISABLE_FILES"),
        help="Do not serve the /files/<path> endpoint. Env: "
        "VPHONE_MCP_DISABLE_FILES",
    )
    p.add_argument(
        "--ssl-certfile",
        default=os.environ.get("VPHONE_MCP_SSL_CERTFILE", "").strip(),
        help="TLS certificate for direct HTTPS serving. Env: "
        "VPHONE_MCP_SSL_CERTFILE",
    )
    p.add_argument(
        "--ssl-keyfile",
        default=os.environ.get("VPHONE_MCP_SSL_KEYFILE", "").strip(),
        help="TLS private key for direct HTTPS serving. Env: VPHONE_MCP_SSL_KEYFILE",
    )
    return p


def parse_config(argv: list[str] | None = None) -> RemoteConfig:
    """Resolve the transport config from env vars and CLI flags, or exit."""
    args = _build_parser().parse_args(argv)
    cfg = RemoteConfig(
        transport=HTTP if args.transport == "http" else args.transport,
        host=args.host,
        port=args.port,
        path=args.path if args.path.startswith("/") else "/" + args.path,
        token=_read_token(),
        allow_anonymous=args.allow_anonymous,
        json_response=args.json_response,
        stateless=args.stateless,
        allowed_hosts=args.allowed_hosts or _env_list("VPHONE_MCP_ALLOWED_HOSTS"),
        allowed_origins=args.allowed_origins or _env_list("VPHONE_MCP_ALLOWED_ORIGINS"),
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        files_enabled=not args.no_files,
        public_url=args.public_url,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: RemoteConfig) -> None:
    """Fail loudly on configurations that would silently expose the VMs."""
    if not cfg.is_network:
        return
    if not cfg.token and not cfg.allow_anonymous:
        raise SystemExit(
            f"vphone-mcp: {cfg.transport} transport requires an auth token. "
            f"Set ${TOKEN_ENV} (or ${TOKEN_FILE_ENV}), e.g.\n"
            "  export VPHONE_MCP_AUTH_TOKEN=\"$(openssl rand -hex 32)\"\n"
            "Pass --allow-anonymous only if something else (a tunnel, an "
            "authenticating proxy) already gates access."
        )
    if bool(cfg.ssl_certfile) != bool(cfg.ssl_keyfile):
        raise SystemExit(
            "vphone-mcp: --ssl-certfile and --ssl-keyfile must be given together"
        )
    if cfg.files_enabled and not cfg.public_url and cfg.host in ("0.0.0.0", "::"):
        _warn(
            "binding a wildcard address without --public-url: file links will "
            "be built as http://127.0.0.1:%d/... , which remote clients cannot "
            "fetch. Set VPHONE_MCP_PUBLIC_URL to the URL clients dial."
            % cfg.port
        )
    if not cfg.token:
        _warn(
            f"serving {cfg.transport} on {cfg.host}:{cfg.port} with NO "
            "authentication — every tool is callable by anyone who can reach "
            "this port."
        )
    elif not cfg.is_loopback and not cfg.ssl_certfile:
        _warn(
            f"serving {cfg.transport} on {cfg.host}:{cfg.port} over plain HTTP "
            "— the bearer token travels in cleartext. Terminate TLS in front "
            "of it (reverse proxy / tunnel) or pass --ssl-certfile/--ssl-keyfile."
        )


def _warn(message: str) -> None:
    print(f"vphone-mcp: WARNING: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Authentication middleware
# ---------------------------------------------------------------------------

def _authorization(scope) -> bytes:
    """The request's Authorization header, or b"" when absent."""
    for key, value in scope.get("headers") or ():
        if key.lower() == b"authorization":
            return value
    return b""


class AuthMiddleware:
    """ASGI middleware requiring ``Authorization: Bearer <token>``.

    It wraps the whole app rather than a single route, so the SSE transport's
    ``/messages/`` endpoint is covered too. Two paths are handled elsewhere:
    ``HEALTH_PATH``, so a supervisor or proxy can probe liveness without a
    credential, and ``/files/...``, which authenticates with Basic because its
    verification needs the requested path (see :func:`_serve_file`).
    """

    def __init__(self, app, token: str = "", health_path: str = HEALTH_PATH):
        self.app = app
        self._token = token.encode()
        self.health_path = health_path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":  # lifespan / websocket pass straight through
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == self.health_path:
            await _send_json(send, 200, {"status": "ok"})
            return
        if path.startswith(files.ROUTE_PREFIX + "/"):
            await self.app(scope, receive, send)  # _serve_file authenticates
            return
        if not self._bearer_ok(scope):
            await _send_json(
                send,
                401,
                {
                    "error": "invalid_token",
                    "error_description": "A valid bearer token is required",
                },
                headers=[
                    (
                        b"www-authenticate",
                        b'Bearer realm="vphone-mcp", error="invalid_token"',
                    )
                ],
            )
            return
        await self.app(scope, receive, send)

    def _bearer_ok(self, scope) -> bool:
        if not self._token:  # --allow-anonymous: nothing to check
            return True
        scheme, _, presented = _authorization(scope).partition(b" ")
        if scheme.lower() != b"bearer":
            return False
        return hmac.compare_digest(presented.strip(), self._token)


def basic_credentials(header: str) -> tuple[str, str] | None:
    """(user, password) from an ``Authorization: Basic`` header, or None."""
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(presented.strip(), validate=True).decode(
            "utf-8", "replace"
        )
    except (binascii.Error, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


async def _send_json(send, status: int, body: dict, headers=()) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------------------
# App construction / run
# ---------------------------------------------------------------------------

def _transport_security(cfg: RemoteConfig):
    """DNS-rebinding settings matching the bind address.

    FastMCP only auto-protects when it is constructed with a loopback host —
    ours is constructed at import time with the default, so binding elsewhere
    would otherwise inherit an allowlist that rejects every real Host header
    with 421.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if cfg.allowed_hosts or cfg.allowed_origins:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=cfg.allowed_hosts,
            allowed_origins=cfg.allowed_origins,
        )
    if cfg.is_loopback:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    # Reachable under an arbitrary hostname: the bearer token is the gate.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _file_handler(token: str):
    """Build the ``/files/{path}`` endpoint bound to the MCP token.

    Authentication is HTTP Basic: the password is the MCP token, and the user
    is the hash of the full path the link was minted for. Both halves are
    checked here rather than in :class:`AuthMiddleware` because verifying the
    hash needs the requested path.
    """

    async def serve_file(request):
        from starlette.responses import FileResponse, JSONResponse

        credentials = basic_credentials(request.headers.get("authorization", ""))
        if credentials is None or (
            token and not hmac.compare_digest(credentials[1], token)
        ):
            return JSONResponse(
                {
                    "error": "invalid_credentials",
                    "error_description": "Basic credentials are required: the "
                    "path hash as user, the MCP token as password",
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="vphone-mcp files"'},
            )
        if request.method == "PUT":
            return await _store_upload(request, credentials[0])
        # No path leaves this function unverified: resolve() only returns a
        # file that stayed inside the root and hashes to the presented user.
        target = files.resolve(request.path_params["path"], credentials[0])
        if target is None:
            return JSONResponse(
                {"error": "not_found", "error_description": "No such file"},
                status_code=404,
            )
        return FileResponse(
            target, filename=target.name, content_disposition_type="inline"
        )

    return serve_file


class _TooLarge(Exception):
    pass


async def _store_upload(request, presented_hash: str):
    """Write a PUT body to ``uploads/``, streamed and size-capped.

    The target is verified exactly like a download — canonical path, hash match
    — with the extra rule that only ``uploads/`` is writable. The body lands in
    a ``.part`` file first and is renamed into place, so a truncated transfer
    never looks like a complete IPA.
    """
    import anyio
    from starlette.responses import JSONResponse

    target = files.upload_target(request.path_params["path"], presented_hash)
    if target is None:
        return JSONResponse(
            {
                "error": "not_writable",
                "error_description": f"Uploads go to {files.upload_dir()}; mint "
                "the URL with the create_upload_url tool",
            },
            status_code=404,
        )

    limit = files.max_upload_bytes()
    declared = request.headers.get("content-length")
    if limit and declared and declared.isdigit() and int(declared) > limit:
        return _too_large(limit)

    files.prune_uploads()  # bound the directory before adding to it
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return JSONResponse(
            {"error": "write_failed", "error_description": str(exc)},
            status_code=500,
        )

    partial = target.with_name(target.name + files.PART_SUFFIX)
    written = 0
    try:
        with open(partial, "wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if limit and written > limit:
                    raise _TooLarge
                # Keep the event loop free during a multi-hundred-MB IPA.
                await anyio.to_thread.run_sync(handle.write, chunk)
        partial.replace(target)
    except _TooLarge:
        partial.unlink(missing_ok=True)
        return _too_large(limit)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return JSONResponse(
            {"error": "write_failed", "error_description": str(exc)},
            status_code=500,
        )
    return JSONResponse(
        {"path": str(target), "bytes": written}, status_code=201
    )


def _too_large(limit: int):
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "error": "too_large",
            "error_description": f"Upload exceeds {limit // (1024 * 1024)} MB "
            "(VPHONE_MCP_MAX_UPLOAD_MB)",
        },
        status_code=413,
    )


def public_base_url(cfg: RemoteConfig) -> str:
    """Base URL that emitted file links are built on."""
    if cfg.public_url:
        return cfg.public_url.rstrip("/")
    scheme = "https" if cfg.ssl_certfile else "http"
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::") else cfg.host
    return f"{scheme}://{host}:{cfg.port}"


def build_app(mcp, cfg: RemoteConfig):
    """Build the ASGI app for a network transport, wrapped in auth."""
    mcp.settings.host = cfg.host
    mcp.settings.port = cfg.port
    mcp.settings.streamable_http_path = cfg.path
    mcp.settings.json_response = cfg.json_response
    mcp.settings.stateless_http = cfg.stateless
    mcp.settings.transport_security = _transport_security(cfg)

    if cfg.files_enabled:
        mcp.custom_route(
            files.ROUTE_PREFIX + "/{path:path}", methods=["GET", "PUT"]
        )(_file_handler(cfg.token))
        files.configure(public_base_url(cfg), cfg.token)

    app = mcp.sse_app() if cfg.transport == SSE else mcp.streamable_http_app()
    return AuthMiddleware(app, token=cfg.token)


def run_server(mcp, cfg: RemoteConfig) -> None:
    """Run the server on the configured transport (blocking)."""
    if not cfg.is_network:
        mcp.run(transport=STDIO)
        return

    import uvicorn

    app = build_app(mcp, cfg)
    endpoint = "/sse" if cfg.transport == SSE else cfg.path
    scheme = "https" if cfg.ssl_certfile else "http"
    shown = "localhost" if cfg.host in ("0.0.0.0", "::") else cfg.host
    print(
        f"vphone-mcp: {cfg.transport} on {scheme}://{shown}:{cfg.port}{endpoint} "
        f"(auth: {'bearer token' if cfg.token else 'NONE'})",
        file=sys.stderr,
        flush=True,
    )
    if cfg.files_enabled:
        print(
            f"vphone-mcp: files on {files.ROUTE_PREFIX}/<path under "
            f"{files.root()}> (basic auth: path hash as user, MCP token as "
            "password) — GET links come from screenshot(), the launch tools "
            f"and get_file_url(); PUT into {files.upload_dir().name}/ via "
            "create_upload_url()",
            file=sys.stderr,
            flush=True,
        )
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level=mcp.settings.log_level.lower(),
        ssl_certfile=cfg.ssl_certfile or None,
        ssl_keyfile=cfg.ssl_keyfile or None,
    )
