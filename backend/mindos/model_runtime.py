"""模型运行时管理 API 编排（P1 §6.1 / §6.2 / §6.4）。

- GET/PUT/test 两个通道（material-runtime / chat-provider）；
- 校验与保存复用 `runtime_config_provider`（同一套格式校验器 + secret saga）；
- 状态投影脱敏：不返回 API Key、secret_ref、完整 .env、供应商错误正文；
- 测试端点：候选配置不持久化、不发布、不入审计（§6.4），使用固定脱敏短文本。

本模块不持有 FastAPI Request；路由由 `configure_guards(guard)` 注册。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import config
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import (
    http_exception_handler as _fastapi_http_handler,
    request_validation_exception_handler as _fastapi_validation_handler,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import llm_transport, ollama_client, runtime_config_provider as rcp
from .stores.runtime_settings_store import ActiveProviderError, RevisionConflictError

# 固定脱敏测试文本（绝不携带真实材料/证据）。
_TEST_PROMPT = "ping"
_TEST_MESSAGES = [{"role": "user", "content": _TEST_PROMPT}]

router = APIRouter(prefix="/api/system/models", tags=["system-models"])
_PROVIDER = None


def get_runtime_provider():
    global _PROVIDER
    return _PROVIDER or rcp.get_provider()


def set_runtime_provider(provider) -> None:
    global _PROVIDER
    _PROVIDER = provider


# =====================================================================
# 请求模型
# =====================================================================


class MaterialRuntimePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseUrl: str
    model: str
    timeoutSeconds: int
    revision: int | None = None


class MaterialRuntimeTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseUrl: str | None = None
    model: str | None = None
    timeoutSeconds: int | None = None


class ChatProviderPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "ollama"
    externalEnabled: bool = False
    baseUrl: str | None = None
    model: str | None = None
    timeoutSeconds: int = 60
    totalBudgetSeconds: int = 90
    fallbackOllama: bool = True
    apiKey: str | None = None
    clearApiKey: bool = False
    revision: int | None = None


class ChatProviderTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    externalEnabled: bool | None = None
    baseUrl: str | None = None
    model: str | None = None
    timeoutSeconds: int | None = None
    totalBudgetSeconds: int | None = None
    fallbackOllama: bool | None = None
    apiKey: str | None = None


class ModelActionBody(BaseModel):
    """模型拉取/预热/卸载请求（P2 §7），仅携带目标模型名，无 URL/凭据。"""

    model_config = ConfigDict(extra="forbid")

    model: str


class ExternalProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    baseUrl: str = Field(min_length=1, max_length=2048)
    apiKey: str = Field(min_length=1, max_length=8192)
    model: str | None = Field(default=None, max_length=128)


class ExternalProviderUpdate(ExternalProviderCreate):
    revision: StrictInt = Field(ge=1)
    apiKey: str | None = Field(default=None, max_length=8192)


class ExternalProviderRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)


class ExternalProviderActivate(ExternalProviderRevision):
    model: str = Field(min_length=1, max_length=128)
    chatRevision: StrictInt = Field(ge=0)


# =====================================================================
# 错误映射
# =====================================================================


def _reject(exc: Exception) -> HTTPException:
    """把保存/测试校验异常映射为统一错误体 {code, message, details?} 的 HTTPException。"""
    from .external_model_discovery import DiscoveryError
    if isinstance(exc, KeyError):
        return HTTPException(404, {"code": "not_found", "message": "供应商不存在"})
    if isinstance(exc, ActiveProviderError):
        return HTTPException(409, {"code": "active_provider", "message": str(exc)})
    if isinstance(exc, DiscoveryError):
        return HTTPException(502, {"code": exc.code, "message": str(exc)})
    if isinstance(exc, RevisionConflictError):
        latest = exc.latest
        return HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": "配置已被其他会话更新，请刷新后重试",
                "details": [
                    f"revision={latest.get('revision')}",
                    f"source={latest.get('source', 'defaults')}",
                ],
            },
        )
    if isinstance(exc, (rcp.ValidationError, rcp.RuntimeConfigError)):
        return HTTPException(
            status_code=400,
            detail={"code": "invalid_config", "message": str(exc)},
        )
    return HTTPException(
        status_code=500,
        detail={"code": "internal", "message": "保存配置失败"},
    )


def _classify_test_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return f"http_{code}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "connection"
    return "unknown"


# 统一错误的稳定 code（§6.2.1 契约 {code, message, details?}）
_SYS_MODELS_PREFIX = "/api/system/models"


def _is_sys_route(path: str) -> bool:
    return path.startswith(_SYS_MODELS_PREFIX)


def install_error_handlers(app: FastAPI) -> None:
    """为 system-models 管理路由注册统一错误响应 {code, message, details?}。

    - HTTPException：path 命中 /api/system/models 时返回统一错误体，否则沿用
      FastAPI 默认 {detail: ...}（不影响其余 WEB/材料接口的错误格式）；
    - RequestValidationError：该前缀下 422 也归一为统一错误体。
    """

    @app.exception_handler(HTTPException)
    async def _sys_models_http_handler(request: Request, exc: HTTPException):
        if _is_sys_route(request.url.path):
            detail = exc.detail
            if isinstance(detail, dict) and "code" in detail and "message" in detail:
                return JSONResponse(status_code=exc.status_code, content=detail)
            return JSONResponse(
                status_code=exc.status_code,
                content={"code": _code_for_status(exc.status_code), "message": str(detail)},
            )
        return await _fastapi_http_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _sys_models_validation_handler(request: Request, exc: RequestValidationError):
        if _is_sys_route(request.url.path):
            details = []
            for e in exc.errors():
                loc = ".".join(str(p) for p in e.get("loc", []))
                details.append(f"{loc}: {e.get('msg', '')}" if loc else e.get("msg", ""))
            return JSONResponse(
                status_code=422,
                content={"code": "validation_error", "message": "请求参数校验失败", "details": details},
            )
        return await _fastapi_validation_handler(request, exc)


def _code_for_status(status: int) -> str:
    return {
        400: "invalid_config",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
    }.get(status, "http_error")


# =====================================================================
# 材料处理（本地 Ollama）
# =====================================================================


def get_material_runtime() -> dict:
    provider = get_runtime_provider()
    status = provider.section_status(rcp.SECTION_MATERIAL)
    local = provider.get_local_snapshot()
    if status.get("source") == "defaults":
        resp = {
            "revision": 0,
            "baseUrl": local.base_url,
            "model": local.model,
            "timeoutSeconds": local.timeout_seconds,
            "source": "defaults",
        }
    else:
        payload = status["payload"]
        resp = {
            "revision": status["revision"],
            "baseUrl": payload["baseUrl"],
            "model": payload["model"],
            "timeoutSeconds": payload["timeoutSeconds"],
            "source": "runtime_settings",
        }
    resp["appliesTo"] = ["summary", "entities", "relations", "tags", "contentDrafts", "wiki"]
    try:
        resp["health"] = ollama_client.health(local, local.model, store=provider.store)
    except Exception:
        resp["health"] = {
            "reachable": False,
            "version": None,
            "modelInstalled": False,
            "modelRunning": False,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }
    return resp


def put_material_runtime(body: MaterialRuntimePut) -> dict:
    provider = get_runtime_provider()
    try:
        row = provider.save_material_runtime(
            base_url=body.baseUrl,
            model=body.model,
            timeout_seconds=body.timeoutSeconds,
            expected_revision=body.revision,
        )
    except Exception as exc:
        raise _reject(exc) from exc
    return {
        "revision": row["revision"],
        "baseUrl": row["payload"]["baseUrl"],
        "model": row["payload"]["model"],
        "timeoutSeconds": row["payload"]["timeoutSeconds"],
        "source": "runtime_settings",
    }


def test_material_runtime(body: MaterialRuntimeTest) -> dict:
    """快速验证 Ollama 服务和指定模型是否已安装，不触发模型加载或推理。"""
    provider = get_runtime_provider()
    try:
        cand = provider.candidate_local_snapshot(
            base_url=body.baseUrl,
            model=body.model,
            timeout_seconds=body.timeoutSeconds,
        )
    except Exception as exc:
        raise _reject(exc) from exc
    started = time.time()
    try:
        listing = ollama_client.tags(cand, store=provider.store, timeout=5.0)
        installed = {
            str(item.get("name", "")).strip()
            for item in listing.get("models", [])
            if isinstance(item, dict)
        }
        # 配置模型名必须与 Ollama tags 返回的 name 完全相同；不将省略 tag 的
        # 名称自动折叠为 :latest，避免页面验证通过而实际调用了另一条模型引用。
        model_installed = cand.model.strip() in installed
        return {
            "ok": model_installed,
            "model": cand.model,
            "modelInstalled": model_installed,
            "models": sorted(name for name in installed if name),
            "testType": "connectivity",
            "errorCode": None if model_installed else "model_not_installed",
            "latencyMs": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": cand.model,
            "modelInstalled": False,
            "models": [],
            "testType": "connectivity",
            "errorCode": _classify_test_error(exc),
            "latencyMs": int((time.time() - started) * 1000),
        }


def test_material_runtime_inference(body: MaterialRuntimeTest) -> dict:
    """显式试运行材料模型；与快速连通性检查分离，允许触发模型加载。"""
    provider = get_runtime_provider()
    try:
        cand = provider.candidate_local_snapshot(
            base_url=body.baseUrl,
            model=body.model,
            timeout_seconds=body.timeoutSeconds,
        )
    except Exception as exc:
        raise _reject(exc) from exc
    data = json.dumps({
        "model": cand.model,
        "messages": _TEST_MESSAGES,
        "stream": False,
        "think": False,
    }).encode("utf-8")
    started = time.time()
    try:
        resp = llm_transport.allowed_urlopen(
            cand.base_url.rstrip("/") + "/api/chat",
            channel="material",
            store=provider.store,
            timeout=cand.timeout_seconds,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(resp.read().decode("utf-8"))
        message = payload.get("message")
        if not isinstance(message, dict) or not str(message.get("content") or "").strip():
            raise ValueError("empty_model_response")
        return {
            "ok": True,
            "model": cand.model,
            "testType": "inference",
            "latencyMs": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": cand.model,
            "testType": "inference",
            "errorCode": _classify_test_error(exc),
            "latencyMs": int((time.time() - started) * 1000),
        }


# =====================================================================
# 对话问答（外部 LLM）
# =====================================================================


def get_chat_provider() -> dict:
    provider = get_runtime_provider()
    status = provider.section_status(rcp.SECTION_CHAT)
    snap = provider.get_chat_snapshot()
    key = provider.resolve_api_key(snap)
    hint = ("••••" + key[-4:]) if key else None
    effective = (
        "openai"
        if (snap.provider == "openai" and snap.external_enabled and snap.base_url
            and snap.model and snap.api_key_configured)
        else "ollama"
    )
    return {
        "revision": status.get("revision", 0),
        "provider": snap.provider,
        "externalEnabled": snap.external_enabled,
        "baseUrl": snap.base_url,
        "model": snap.model,
        "apiKeyConfigured": snap.api_key_configured,
        "apiKeyHint": hint,
        "timeoutSeconds": snap.timeout_seconds,
        "totalBudgetSeconds": snap.total_budget_seconds,
        "fallbackOllama": snap.fallback_ollama,
        "source": status.get("source", "defaults"),
        "effectiveProvider": effective,
        "externalProviderId": snap.external_provider_id,
    }


def get_external_providers():
    return get_runtime_provider().list_external_providers()


def create_external_provider(body: ExternalProviderCreate):
    try:
        return get_runtime_provider().save_external_provider(name=body.name, base_url=body.baseUrl,
            api_key=body.apiKey, model=body.model)
    except Exception as exc:
        raise _reject(exc) from None


def update_external_provider(provider_id: str, body: ExternalProviderUpdate):
    try:
        return get_runtime_provider().save_external_provider(ident=provider_id, expected_revision=body.revision,
            name=body.name, base_url=body.baseUrl, api_key=body.apiKey, model=body.model)
    except Exception as exc:
        raise _reject(exc) from None


def discover_external_provider_models(provider_id: str, body: ExternalProviderRevision):
    try:
        return get_runtime_provider().discover_external_models(provider_id, body.revision)
    except Exception as exc:
        raise _reject(exc) from None


def activate_external_provider(provider_id: str, body: ExternalProviderActivate):
    try:
        profile = get_runtime_provider().activate_external_provider(provider_id, expected_revision=body.revision,
            model=body.model, chat_revision=body.chatRevision)
        return {"provider": profile, "chat": get_chat_provider()}
    except Exception as exc:
        raise _reject(exc) from None


def delete_external_provider(provider_id: str, revision: int = Query(ge=1)):
    try:
        return get_runtime_provider().delete_external_provider(provider_id, revision)
    except Exception as exc:
        raise _reject(exc) from None


def put_chat_provider(body: ChatProviderPut) -> dict:
    provider = get_runtime_provider()
    try:
        row = provider.save_chat_provider(
            provider=body.provider,
            external_enabled=body.externalEnabled,
            base_url=body.baseUrl,
            model=body.model,
            timeout_seconds=body.timeoutSeconds,
            total_budget_seconds=body.totalBudgetSeconds,
            fallback_ollama=body.fallbackOllama,
            api_key=body.apiKey,
            clear_api_key=body.clearApiKey,
            expected_revision=body.revision,
        )
    except Exception as exc:
        raise _reject(exc) from exc
    snap = provider.get_chat_snapshot()
    return {
        "revision": row["revision"],
        "provider": snap.provider,
        "externalEnabled": snap.external_enabled,
        "baseUrl": snap.base_url,
        "model": snap.model,
        "apiKeyConfigured": snap.api_key_configured,
        "timeoutSeconds": snap.timeout_seconds,
        "totalBudgetSeconds": snap.total_budget_seconds,
        "fallbackOllama": snap.fallback_ollama,
        "source": "runtime_settings",
    }


def test_chat_provider(body: ChatProviderTest) -> dict:
    provider = get_runtime_provider()
    try:
        cand = provider.candidate_chat_snapshot(
            provider=body.provider,
            external_enabled=body.externalEnabled,
            base_url=body.baseUrl,
            model=body.model,
            timeout_seconds=body.timeoutSeconds,
            total_budget_seconds=body.totalBudgetSeconds,
            fallback_ollama=body.fallbackOllama,
            api_key=body.apiKey,
        )
    except Exception as exc:
        raise _reject(exc) from exc

    headers = {"Content-Type": "application/json"}
    if cand.provider == "openai":
        key = provider.resolve_candidate_api_key(cand, body.apiKey)
        if not key:
            raise HTTPException(status_code=400, detail="缺少 API Key")
        headers["Authorization"] = f"Bearer {key}"
        url = (cand.base_url or "").rstrip("/") + "/chat/completions"
        model = cand.model
        channel = "diagnostic"
    else:
        url = cand.local.base_url.rstrip("/") + "/api/chat"
        model = cand.local.model
        channel = "material"
    data = json.dumps({"model": model, "messages": _TEST_MESSAGES, "stream": False}).encode("utf-8")
    started = time.time()
    try:
        resp = llm_transport.allowed_urlopen(
            url,
            channel=channel,
            store=provider.store,
            timeout=cand.timeout_seconds,
            data=data,
            method="POST",
            headers=headers,
        )
        resp.read()
        latency_ms = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "provider": cand.provider,
            "model": model,
            "latencyMs": latency_ms,
        }
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "provider": cand.provider,
            "model": model,
            "errorCode": _classify_test_error(exc),
            "latencyMs": latency_ms,
        }


# =====================================================================
# 运行监控 / 模型任务（P2 §7 / §8）
# =====================================================================

_MODEL_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:+-"
)


def _require_model_name(body: ModelActionBody) -> str:
    name = (body.model or "").strip()
    if not name or len(name) > 200:
        raise HTTPException(status_code=400, detail={"code": "invalid_model", "message": "模型名不能为空或超长"})
    if "\\" in name or any(c not in _MODEL_NAME_CHARS for c in name):
        raise HTTPException(status_code=400, detail={"code": "invalid_model", "message": "模型名包含非法字符"})
    return name


def _safe_model(m: dict) -> dict:
    det = m.get("details") or {}
    return {
        "name": m.get("name"),
        "sizeBytes": m.get("size"),
        "modifiedAt": m.get("modified_at"),
        "family": det.get("family"),
        "parameterSize": det.get("parameter_size"),
        "quantization": det.get("quantization_level"),
    }


def get_material_models() -> dict:
    """本地 Ollama 已安装模型列表（脱敏投影，含每模型 running 标记）。不可达返回 200 + available=false。"""
    provider = get_runtime_provider()
    local = provider.get_local_snapshot()
    try:
        listing = ollama_client.tags(local, store=provider.store)
    except Exception as exc:  # noqa: BLE001
        return {
            "reachable": False,
            "errorCode": _classify_test_error(exc),
            "models": [],
        }
    # 结合 /api/ps 标记每个模型是否正在运行（§8：模型列表需展示运行状态，不能只看全局）。
    # running_models 返回规范化名称（:latest 折叠），故对每个条目同样规范化后再比对。
    run_set: set[str] = set()
    try:
        run_set = ollama_client.running_models(local, store=provider.store)
    except Exception:  # noqa: BLE001
        run_set = set()
    models = []
    for m in listing.get("models", []):
        proj = _safe_model(m)
        proj["running"] = ollama_client._norm_model_name(proj.get("name")) in run_set
        models.append(proj)
    return {
        "reachable": True,
        "models": models,
    }


def run_material_health_check() -> dict:
    """合并健康探测：一次调用返回可达性、版本、安装与运行态。"""
    provider = get_runtime_provider()
    snap = provider.get_chat_snapshot()
    local = provider.get_local_snapshot()
    health = ollama_client.health(local, local.model, store=provider.store)
    return {
        "health": health,
        "localModel": snap.local.model,
    }


def get_monitor() -> dict:
    """聚合运行监控：资源（CPU/内存/GPU/Ollama）+ 索引队列 + 模型任务 + worker 状态。"""
    from . import resource_monitor
    from .stores import model_job_store as mjs
    from .stores.job_store import JobStore

    provider = get_runtime_provider()
    local = provider.get_local_snapshot()
    snapshot = resource_monitor.get_snapshot(local)
    store = mjs.ModelJobStore.instance()
    try:
        index_queue = JobStore.instance().queue_summary()
    except Exception:  # noqa: BLE001
        index_queue = {"active": None, "total": None}
    try:
        from .model_job_worker import ModelJobWorker

        worker = {"running": ModelJobWorker.instance().running}
    except Exception:  # noqa: BLE001
        worker = {"running": False}
    return {
        "sampledAt": snapshot["sampledAt"],
        "resource": snapshot,
        "indexQueue": index_queue,
        "modelJobs": [_job_projection(j) for j in store.list_jobs(limit=20)],
        "worker": worker,
    }


def _job_projection(job: dict) -> dict:
    return {
        "jobId": job.get("id"),
        "type": job.get("type"),
        "state": job.get("state"),
        "targetModel": job.get("target_model"),
        "progressCurrent": job.get("progress_current"),
        "progressTotal": job.get("progress_total"),
        "attempts": job.get("attempts"),
        "errorCode": job.get("error_code"),
        "errorMessageSafe": job.get("error_message_safe"),
        "createdAt": job.get("created_at"),
        "startedAt": job.get("started_at"),
        "finishedAt": job.get("finished_at"),
    }


def _create_model_job(body: ModelActionBody, type_: str) -> dict:
    name = _require_model_name(body)
    provider = get_runtime_provider()
    revision = provider.section_status(rcp.SECTION_MATERIAL).get("revision", 0)
    # §7.0.1 第 3 条：任务创建时把当前材料快照字段持久化，worker 领取/恢复时据此还原
    # config_revision 对应的不可变快照，配置变更不迁移已排队/恢复任务。
    local = provider.get_local_snapshot()
    from .stores.model_job_store import ModelJobStore

    job = ModelJobStore.instance().create_job(
        type_=type_,
        target_model=name,
        config_revision=revision,
        local_base_url=local.base_url,
        local_timeout_seconds=local.timeout_seconds,
        local_keep_alive=local.keep_alive,
        local_context_window=local.context_window,
    )
    return {
        "jobId": job["id"],
        "state": job["state"],
        "deduplicated": job.get("duplicate", False),
    }


def pull_model(body: ModelActionBody) -> dict:
    return _create_model_job(body, "pull")


def load_model(body: ModelActionBody) -> dict:
    return _create_model_job(body, "load")


def unload_model(body: ModelActionBody) -> dict:
    return _create_model_job(body, "unload")


def list_model_jobs(
    state: str | None = None,
    type_: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict:
    from .stores.model_job_store import ModelJobStore

    state = state or None
    type_ = type_ or None
    cursor = cursor or None
    try:
        items, next_cursor = ModelJobStore.instance().list_jobs_paged(
            state=state, type_=type_, limit=min(max(limit, 1), 100), cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_query", "message": str(exc)}) from exc
    return {
        "items": [_job_projection(j) for j in items],
        "nextCursor": next_cursor,
    }


def get_model_job(job_id: str) -> dict:
    from .stores.model_job_store import ModelJobNotFoundError, ModelJobStore

    try:
        job = ModelJobStore.instance().get(job_id)
        if job is None:
            raise ModelJobNotFoundError(job_id)
    except ModelJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "任务不存在"}) from exc
    return _job_projection(job)


def cancel_model_job(job_id: str) -> dict:
    from .stores.model_job_store import (
        ModelJobNotFoundError,
        ModelJobStore,
        ModelJobTerminalError,
    )

    try:
        job = ModelJobStore.instance().request_cancel(job_id)
    except ModelJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "任务不存在"}) from exc
    except ModelJobTerminalError as exc:
        raise HTTPException(status_code=409, detail={"code": "terminal", "message": "任务已进入终态，无法取消"}) from exc
    return _job_projection(job)


def configure_guards(guard) -> None:
    """注册管理路由；guard 统一为 server.require_local（loopback + CSRF，GET 不放松）。"""
    global router
    router = APIRouter(prefix="/api/system/models", tags=["system-models"])
    for path, function, methods in (
        ("/external-providers", get_external_providers, ["GET"]),
        ("/external-providers", create_external_provider, ["POST"]),
        ("/external-providers/{provider_id}", update_external_provider, ["PUT"]),
        ("/external-providers/{provider_id}", delete_external_provider, ["DELETE"]),
        ("/external-providers/{provider_id}/models", discover_external_provider_models, ["POST"]),
        ("/external-providers/{provider_id}/activate", activate_external_provider, ["POST"]),
    ):
        router.add_api_route(path, function, methods=methods, dependencies=[Depends(guard)])
    router.add_api_route(
        "/material-runtime", get_material_runtime, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/material-runtime", put_material_runtime, methods=["PUT"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/material-runtime/test", test_material_runtime, methods=["POST"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/material-runtime/test-inference", test_material_runtime_inference, methods=["POST"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/chat-provider", get_chat_provider, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/chat-provider", put_chat_provider, methods=["PUT"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/chat-provider/test", test_chat_provider, methods=["POST"],
        dependencies=[Depends(guard)],
    )
    # ---- P2：运行监控 / 模型任务（§7 / §8）----
    router.add_api_route(
        "/material-runtime/models", get_material_models, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/health", run_material_health_check, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/monitor", get_monitor, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/jobs", list_model_jobs, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/jobs/{job_id}", get_model_job, methods=["GET"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/jobs/{job_id}/cancel", cancel_model_job, methods=["POST"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/material-runtime/pull", pull_model, methods=["POST"], status_code=202,
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/material-runtime/load", load_model, methods=["POST"], status_code=202,
        dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/material-runtime/unload", unload_model, methods=["POST"], status_code=202,
        dependencies=[Depends(guard)],
    )
