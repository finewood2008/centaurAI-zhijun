"""Vendor-neutral remote MCP server (Streamable HTTP + OAuth 2.1)."""

from __future__ import annotations

import html
import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_access import (
    BASIC_SCOPE,
    SCOPES,
    CentaurOAuthProvider,
    ResourceTokenVerifier,
    admin_password_is_set,
    ca_certificate_path,
    ca_fingerprint,
    get_runtime_config,
    get_store,
    public_urls,
    remote_mode_active,
    request_source_var,
    verify_admin_password,
)
from mcp_tools import create_mcp_server


class ResourceBearerAuthBackend(AuthenticationBackend):
    """Authenticate Bearer tokens and enforce their RFC 8707 resource."""

    async def authenticate(self, conn: HTTPConnection):
        header = conn.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        access = get_store().load_access_token(header[7:].strip())
        if not access:
            return None
        urls = public_urls()
        expected = ""
        if conn.url.path.rstrip("/") == "/mcp/basic":
            expected = urls["basic"]
        elif conn.url.path.rstrip("/") == "/mcp/kb":
            expected = urls["kb"]
        elif conn.url.path.rstrip("/") == "/mcp/full":
            expected = urls["full"]
        if expected and (access.resource or "").rstrip("/") != expected.rstrip("/"):
            return None
        return AuthCredentials(access.scopes), AuthenticatedUser(access)


class ClientSourceMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        source = scope.get("client")
        source_ip = str(source[0]) if source else ""
        token = request_source_var.set(source_ip)
        try:
            await self.app(scope, receive, send)
        finally:
            request_source_var.reset(token)


def _consent_page(request_id: str, *, error: str = "") -> HTMLResponse:
    if not remote_mode_active("advanced"):
        return HTMLResponse(
            "<h1>Advanced MCP access is disabled</h1><p>Switch to advanced mode in the desktop application.</p>",
            status_code=403,
        )
    pending = get_store().get_authorization_request(request_id)
    if not pending:
        return HTMLResponse(
            "<h1>Authorization request expired</h1><p>Return to the Agent and connect again.</p>",
            status_code=400,
        )
    row, params = pending
    configured = admin_password_is_set()
    scopes = ", ".join(params.scopes or [])
    resource = str(params.resource or "")
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    disabled = "" if configured else "disabled"
    setup_hint = "" if configured else "<p class='error'>请先在桌面程序的 MCP 设置中配置管理员密码。</p>"
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>MCP 授权</title><style>
body{{font-family:system-ui,sans-serif;background:#f4f6f8;color:#18202a;margin:0;padding:32px}}
.card{{max-width:560px;margin:5vh auto;background:white;border:1px solid #dce2e8;border-radius:16px;padding:28px;box-shadow:0 12px 40px #16202a18}}
h1{{font-size:22px;margin:0 0 8px}} .muted{{color:#627080;font-size:14px}} dl{{background:#f7f9fb;padding:16px;border-radius:10px}}
dt{{font-size:12px;color:#718096;margin-top:8px}} dd{{margin:2px 0;word-break:break-all}} input{{box-sizing:border-box;width:100%;padding:12px;border:1px solid #b9c3cf;border-radius:8px;margin:10px 0}}
.actions{{display:flex;gap:10px}} button{{flex:1;padding:11px;border-radius:8px;border:1px solid #b9c3cf;background:white;font-weight:650}} button.primary{{background:#276ef1;color:white;border-color:#276ef1}} .error{{color:#b42318}}
</style></head><body><main class="card"><h1>授权 Agent 读取私人记忆库</h1>
<p class="muted">请核对客户端与读取范围。授权只允许读取，不允许修改数据库。</p>{error_html}{setup_hint}
<dl><dt>客户端</dt><dd>{html.escape(str(row['label']))}</dd><dt>权限</dt><dd>{html.escape(scopes)}</dd><dt>MCP 资源</dt><dd>{html.escape(resource)}</dd></dl>
<form method="post" action="/oauth/consent"><input type="hidden" name="request" value="{html.escape(request_id)}">
<label>管理员密码</label><input type="password" name="password" autocomplete="current-password" {disabled}>
<div class="actions"><button type="submit" name="action" value="deny">拒绝</button><button class="primary" type="submit" name="action" value="approve" {disabled}>允许读取</button></div>
</form></main></body></html>"""
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def consent_get(request: Request) -> Response:
    return _consent_page(request.query_params.get("request", ""))


async def consent_post(request: Request) -> Response:
    form = await request.form()
    request_id = str(form.get("request") or "")
    action = str(form.get("action") or "")
    try:
        if action == "deny":
            return RedirectResponse(get_store().deny_authorization_request(request_id), status_code=302)
        if not verify_admin_password(str(form.get("password") or "")):
            return _consent_page(request_id, error="管理员密码错误。")
        _, redirect = get_store().approve_authorization_request(request_id)
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return _consent_page(request_id, error=str(exc))


async def health(request: Request) -> Response:
    config = get_runtime_config()
    backend_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.get("http://127.0.0.1:8618/api/health")
            backend_ok = response.status_code == 200
    except httpx.HTTPError:
        pass
    return JSONResponse(
        {
            "status": "ok" if config.get("enabled") and backend_ok else "degraded",
            "enabled": bool(config.get("enabled")),
            "mode": config.get("mode", "basic"),
            "backend": backend_ok,
            "transport": "streamable-http",
            "protocol": "2025-11-25",
            "oauth": config.get("mode") == "advanced",
        },
        headers={"Cache-Control": "no-store"},
    )


async def ca_download(request: Request) -> Response:
    path = ca_certificate_path()
    if not path.is_file():
        return JSONResponse({"error": "CA certificate is not installed"}, status_code=404)
    return FileResponse(path, media_type="application/x-x509-ca-cert", filename="centaurai-memory-ca.crt")


async def service_document(request: Request) -> Response:
    urls = public_urls()
    config = get_runtime_config()
    advanced = config.get("mode") == "advanced"
    return JSONResponse(
        {
            "name": "CentaurAI Personal Memory MCP",
            "protocol": "MCP 2025-11-25",
            "transport": "streamable-http",
            "mode": config.get("mode", "basic"),
            "enabled": bool(config.get("enabled")),
            "authorization": "OAuth 2.1 or pre-issued Bearer token" if advanced else "pre-issued Bearer connection key",
            "resources": (
                {"knowledge_base": urls["kb"], "full_memory": urls["full"]}
                if advanced
                else {"basic_memory": urls["basic"]}
            ),
            "ca_certificate": urls["ca"],
            "ca_sha256": ca_fingerprint(),
        }
    )


def build_app() -> Starlette:
    urls = public_urls()
    external = urlparse(urls["base"])
    host_header = external.netloc
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host_header, "127.0.0.1:8620", "localhost:8620", "testserver"],
        allowed_origins=[urls["base"], "http://127.0.0.1:*", "http://localhost:*"],
    )
    provider = CentaurOAuthProvider()

    def make_server(profile: str, path: str, scope: str):
        auth = AuthSettings(
            issuer_url=AnyHttpUrl(urls["issuer"]),
            service_documentation_url=AnyHttpUrl(urls["base"]),
            required_scopes=[scope],
            resource_server_url=AnyHttpUrl(urls[profile]),
        )
        server = create_mcp_server(
            profile=profile,
            host="127.0.0.1",
            port=8620,
            streamable_http_path=path,
            stateless_http=True,
            json_response=True,
            auth=auth,
            token_verifier=ResourceTokenVerifier(urls[profile]),
            transport_security=transport_security,
        )
        return server, server.streamable_http_app()

    basic_server, basic_app = make_server("basic", "/mcp/basic", BASIC_SCOPE)
    kb_server, kb_app = make_server("kb", "/mcp/kb", "kb:read")
    full_server, full_app = make_server("full", "/mcp/full", "memory:read")

    registration_options = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=SCOPES,
        # Consent still restricts each resource to one approved tier.
        default_scopes=SCOPES,
    )
    revocation_options = RevocationOptions(enabled=True)
    metadata = build_metadata(
        AnyHttpUrl(urls["issuer"]),
        AnyHttpUrl(urls["base"]),
        registration_options,
        revocation_options,
    )
    # The SDK accepts public DCR clients, so advertise "none" alongside the
    # confidential-client methods. Some generic clients use this field to
    # decide whether they may register a PKCE-only loopback client.
    metadata.token_endpoint_auth_methods_supported = [
        "none",
        "client_secret_post",
        "client_secret_basic",
    ]

    async def authorization_metadata(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": "MCP-Protocol-Version",
                },
            )
        if not remote_mode_active("advanced"):
            return JSONResponse(
                {"error": "advanced_mode_disabled"},
                status_code=404,
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            )
        return JSONResponse(
            metadata.model_dump(mode="json", exclude_none=True),
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
        )

    routes = [
        Route("/", service_document, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/ca.crt", ca_download, methods=["GET"]),
        Route("/oauth/consent", consent_get, methods=["GET"]),
        Route("/oauth/consent", consent_post, methods=["POST"]),
        Route(
            "/.well-known/oauth-authorization-server",
            authorization_metadata,
            methods=["GET", "OPTIONS"],
        ),
    ]
    auth_routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(urls["issuer"]),
        service_documentation_url=AnyHttpUrl(urls["base"]),
        client_registration_options=registration_options,
        revocation_options=revocation_options,
    )
    routes.extend(route for route in auth_routes if route.path != "/.well-known/oauth-authorization-server")
    # FastMCP already wrapped these MCP routes with scope requirements and
    # generated the three RFC 9728 protected-resource metadata routes.
    routes.extend(basic_app.routes)
    routes.extend(kb_app.routes)
    routes.extend(full_app.routes)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(basic_server.session_manager.run())
            await stack.enter_async_context(kb_server.session_manager.run())
            await stack.enter_async_context(full_server.session_manager.run())
            yield

    return Starlette(
        debug=False,
        routes=routes,
        middleware=[
            Middleware(AuthenticationMiddleware, backend=ResourceBearerAuthBackend()),
            Middleware(AuthContextMiddleware),
            Middleware(ClientSourceMiddleware),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    config = get_runtime_config()
    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=int(config.get("mcp_port", 8620)),
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
