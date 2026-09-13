"""UDS-only authenticated dispatch to real domain FastAPI routes, including SSE."""
from __future__ import annotations

import json
import os
import re
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, quote, unquote

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse

from . import capabilities as ports
from .auth import HEADER, MAX_BODY, NonceStore, ProofError, strict_json, verify
from .workspace import WorkspaceLock


def _catalog():
    configured = os.environ.get("ZHIJUN_PRODUCT_CATALOG")
    source = Path(configured) if configured else Path(__file__).resolve().parents[2] / "frontend/shared/product-operations.json"
    if (not source.is_absolute() or ".." in source.parts
            or any(path.is_symlink() for path in (source, *source.parents))):
        raise ValueError("WORKER_CATALOG_PATH_INVALID")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > MAX_BODY:
            raise ValueError("WORKER_CATALOG_FILE_INVALID")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("WORKER_CATALOG_FILE_INVALID")
        entries = strict_json(raw)["operations"]
        if not isinstance(entries, list) or not entries or any(type(item) is not dict for item in entries):
            raise ValueError("WORKER_CATALOG_INVALID")
        names = [item["id"] for item in entries]
        if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise ValueError("WORKER_CATALOG_INVALID")
        return {item["id"]: item for item in entries if item["capability"] == "domain"}
    finally:
        os.close(fd)


def _check_environment(workspace):
    workspace.validate()
    if os.environ.get("ZHIJUN_WORKSPACE_ID") != workspace.workspace_id or os.environ.get("MINDOS_RUNTIME_ENV") != "production" or os.environ.get("MINDOS_LOCAL_WEB_DEBUG_ACCESS") != "0":
        raise ValueError("WORKER_ENVIRONMENT_INVALID")
    for name in ("CENTAURAI_DATABASE_DATA_ROOT", "CENTAUR_METADATA_DB", "CENTAUR_GBRAIN_HOME", "CENTAUR_MCP_DATA_DIR", "CENTAUR_MCP_CONFIG_DIR"):
        value = os.environ.get(name)
        if not value:
            raise ValueError("WORKER_PATH_NOT_ISOLATED")
        path = Path(value)
        if not path.is_absolute() or not path.is_relative_to(workspace.data_root) or any(p.is_symlink() for p in (path, *path.parents)) or ".." in path.parts:
            raise ValueError("WORKER_PATH_NOT_ISOLATED")
    secret_value = os.environ.get("CENTAUR_SECRET_STORE_DIR", "")
    secret_root = Path(secret_value)
    if (not secret_value or not secret_root.is_absolute()
            or any(path.is_symlink() for path in (secret_root, *secret_root.parents))
            or secret_root == workspace.data_root
            or secret_root.is_relative_to(workspace.data_root)
            or workspace.data_root.is_relative_to(secret_root)):
        raise ValueError("WORKER_SECRET_PATH_NOT_ISOLATED")
    try:
        secret_info = secret_root.stat()
    except OSError:
        raise ValueError("WORKER_SECRET_PATH_NOT_ISOLATED") from None
    if (not stat.S_ISDIR(secret_info.st_mode) or secret_info.st_uid != os.geteuid()
            or secret_info.st_mode & 0o077):
        raise ValueError("WORKER_SECRET_PATH_NOT_ISOLATED")
    import runtime_paths
    if (runtime_paths.DATA_ROOT != workspace.data_root
            or runtime_paths.SECRET_STORE_DIR != secret_root):
        raise ValueError("WORKER_PATH_ALREADY_IMPORTED")


def create_app(workspace, capabilities=None):
    _check_environment(workspace)
    capabilities = capabilities or ports.UnconfiguredCapabilities()
    lock = WorkspaceLock(workspace)

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        threads_stopped = True
        try:
            ports.bind(workspace, capabilities)
            from mindos.stores.ontology_store import OntologyStore
            from mindos.stores.conversation_store import ConversationStore
            from mindos.stores.growth_store import GrowthStore
            OntologyStore.instance()
            ConversationStore.instance()
            GrowthStore.instance()
            from mindos.zhijun.jobs import start_worker, stop_worker
            from mindos.chat_imports import start_worker as start_imports, stop_worker as stop_imports
            try:
                start_worker()
                start_imports()
                app.state.active = True
                yield
            finally:
                app.state.active = False
                stop_imports()
                stop_worker()
                from mindos.zhijun.jobs import worker_running
                from mindos import chat_imports
                threads_stopped = not worker_running() and not (chat_imports._thread and chat_imports._thread.is_alive())
        finally:
            ports.unbind()
            if threads_stopped:
                lock.close()
            else:
                # Keep ownership until process exit; a timed-out task must never overlap a replacement worker.
                raise RuntimeError("WORKER_SHUTDOWN_INCOMPLETE")

    domain = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    domain.state.active = False

    def require_workspace(request: Request):
        if not domain.state.active or getattr(request.state, "zhijun_workspace", None) is not workspace:
            raise HTTPException(401, {"code": "WORKER_PROOF_REQUIRED"})

    from fastapi import Depends
    registered = []
    from mindos import conversations, ontology, growth, nudges, zhijun_onboarding, zhijun_status, zhijun_home
    for module in (conversations, ontology, growth, nudges, zhijun_onboarding):
        module.configure_write_guard(require_workspace)
    for module in (conversations, ontology, growth, nudges, zhijun_onboarding, zhijun_status, zhijun_home):
        registered.extend(module.router.routes)
        domain.include_router(module.router, dependencies=[Depends(require_workspace)])
    from mindos import memory_routes, matters_routes, chat_import_routes, sensitive_rule_routes
    from mindos.zhijun import charter
    for module in (memory_routes, matters_routes, chat_import_routes, sensitive_rule_routes, charter):
        router = module.build_router(require_workspace)
        registered.extend(router.routes)
        domain.include_router(router, dependencies=[Depends(require_workspace)])

    @domain.exception_handler(ports.CapabilityError)
    async def capability_error(request, exc):
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)

    catalog = _catalog()
    normalize = lambda path: re.sub(r"\{[^}]+\}", "{}", path)
    actual = {(method, normalize(route.path)) for route in registered for method in route.methods}
    missing = [item["id"] for item in catalog.values() if (item["method"], normalize(item["path"])) not in actual]
    if missing:
        raise ValueError("WORKER_CATALOG_ROUTES_MISSING: " + ",".join(missing))
    return DispatchApp(domain, workspace, catalog)


class DispatchApp:
    def __init__(self, domain, workspace, catalog):
        self.domain, self.workspace, self.catalog = domain, workspace, catalog
        self.nonces = NonceStore()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.domain(scope, receive, send)
        if scope["type"] != "http":
            return
        try:
            raw_target = scope.get("raw_path", scope["path"].encode()) + (b"?" + scope["query_string"] if scope.get("query_string") else b"")
            if scope["method"] != "POST" or raw_target not in {b"/v1/dispatch", b"/v1/events"}:
                raise HTTPException(404, {"code": "WORKER_ROUTE_NOT_ALLOWED"})
            values = [v for k, v in scope["headers"] if k.lower() == HEADER.lower().encode()]
            if len(values) != 1:
                raise ProofError("WORKER_PROOF_REQUIRED")
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > MAX_BODY:
                    raise HTTPException(413, {"code": "WORKER_REQUEST_TOO_LARGE"})
                if not event.get("more_body"):
                    break
            verify(values[0].decode("ascii"), self.workspace.key, self.workspace.workspace_id,
                   self.workspace.ownership_epoch, bytes(body), self.nonces,
                   expected_relative_path=raw_target.decode("ascii"))
            if raw_target == b"/v1/events":
                if not self.domain.state.active:
                    raise HTTPException(503, {"code": "WORKER_NOT_RUNNING"})
                from .events import parse, handle
                from starlette.concurrency import run_in_threadpool
                try:
                    result = await run_in_threadpool(handle, parse(bytes(body)))
                except ports.CapabilityError as exc:
                    raise HTTPException(exc.status, {"code": exc.code}) from None
                return await JSONResponse(result)(scope, receive, send)
            operation = strict_json(body)
            if type(operation) is not dict or set(operation) != {"version", "requestId", "operationId", "params", "query", "body"} or type(operation["version"]) is not int or operation["version"] != 1:
                raise HTTPException(400, {"code": "WORKER_OPERATION_INVALID"})
            item = self.catalog.get(operation["operationId"]) if isinstance(operation["operationId"], str) else None
            if not item or not isinstance(operation["requestId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", operation["requestId"]):
                raise HTTPException(400, {"code": "WORKER_OPERATION_INVALID"})
            params, query = operation["params"], operation["query"]
            if type(params) is not dict or set(params) != set(item["pathParams"]) or type(query) is not dict or not set(query).issubset(item["query"]):
                raise HTTPException(400, {"code": "WORKER_PARAMETERS_INVALID"})
            path = item["path"]
            for name, value in params.items():
                if not isinstance(value, str) or not value or len(value) > 256 or re.search(r"[/\\\x00-\x1f\x7f]", value) or value in {".", ".."}:
                    raise HTTPException(400, {"code": "WORKER_PARAMETERS_INVALID"})
                path = path.replace("{" + name + "}", quote(value, safe=""))
            if any(type(v) not in (str, int, bool) or len(str(v)) > 1000 for v in query.values()):
                raise HTTPException(400, {"code": "WORKER_PARAMETERS_INVALID"})
            if item["body"] == "none" and operation["body"] is not None:
                raise HTTPException(400, {"code": "WORKER_BODY_INVALID"})
            uploads = None
            if item["body"] == "multipart":
                uploads = operation["body"]
                if (type(uploads) is not dict or set(uploads) != {"kind", "fields", "files"}
                        or uploads["kind"] != "multipart" or uploads["fields"] != {}
                        or type(uploads["files"]) is not list or len(uploads["files"]) != 1
                        or type(uploads["files"][0]) is not dict
                        or set(uploads["files"][0]) != {"field", "uploadId"}
                        or uploads["files"][0]["field"] != "file"
                        or not isinstance(uploads["files"][0]["uploadId"], str)
                        or not re.fullmatch(r"[0-9a-f]{32}", uploads["files"][0]["uploadId"])):
                    raise HTTPException(400, {"code": "WORKER_UPLOAD_DESCRIPTOR_INVALID"})
            raw_body = b"" if item["body"] in {"none", "multipart"} else json.dumps(operation["body"], ensure_ascii=False, allow_nan=False).encode()
            if len(raw_body) > item["maxRequestBytes"]:
                raise HTTPException(413, {"code": "WORKER_REQUEST_TOO_LARGE"})
            child = dict(scope, method=item["method"], path=unquote(path), raw_path=path.encode(), query_string=urlencode(query).encode(),
                         headers=[(b"content-type", b"application/json"), (b"content-length", str(len(raw_body)).encode())],
                         state={"zhijun_workspace": self.workspace, "zhijun_uploads": uploads})
            delivered = False
            async def child_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw_body, "more_body": False}
                return await receive()
            token = ports.execution.set({"requestId": operation["requestId"], "operationId": operation["operationId"]})
            try:
                await self.domain(child, child_receive, send)
            finally:
                ports.execution.reset(token)
        except (ProofError, UnicodeError) as exc:
            await JSONResponse({"detail": {"code": str(exc) if isinstance(exc, ProofError) else "WORKER_PROOF_INVALID"}}, status_code=401)(scope, receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
