"""Ollama HTTP 适配器（P1 version/tags；P2 扩展 ps/pull/load/unload）。

所有出站统一走 `llm_transport.allowed_urlopen`，保持模型请求参数与错误处理一致。
任务型操作（pull/load/unload）的进度/终态由 `model_job` worker 驱动，本模块只负责
单次 Ollama HTTP 调用，不感知 `model_jobs` 表。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from . import llm_transport
from .runtime_config_provider import LocalOllamaSnapshot

_CHANNEL = "material"
_JSON_HEADERS = {"Content-Type": "application/json"}


def _get_json(snapshot: LocalOllamaSnapshot, endpoint: str, store=None, timeout: float = 5.0) -> dict:
    url = snapshot.base_url.rstrip("/") + endpoint
    resp = llm_transport.allowed_urlopen(
        url, channel=_CHANNEL, store=store, timeout=timeout
    )
    return json.loads(resp.read().decode("utf-8"))


def _post_json(
    snapshot: LocalOllamaSnapshot,
    endpoint: str,
    payload: dict,
    store=None,
    timeout: float | None = None,
) -> dict:
    url = snapshot.base_url.rstrip("/") + endpoint
    data = json.dumps(payload).encode("utf-8")
    resp = llm_transport.allowed_urlopen(
        url, channel=_CHANNEL, store=store, timeout=timeout,
        headers=_JSON_HEADERS, data=data, method="POST",
    )
    return json.loads(resp.read().decode("utf-8"))


def version(snapshot: LocalOllamaSnapshot, store=None, timeout: float = 5.0) -> dict:
    return _get_json(snapshot, "/api/version", store=store, timeout=timeout)


def tags(snapshot: LocalOllamaSnapshot, store=None, timeout: float = 5.0) -> dict:
    return _get_json(snapshot, "/api/tags", store=store, timeout=timeout)


def ps(snapshot: LocalOllamaSnapshot, store=None, timeout: float = 5.0) -> dict:
    """查询运行中的模型（/api/ps）。"""
    return _get_json(snapshot, "/api/ps", store=store, timeout=timeout)


def running_models(snapshot: LocalOllamaSnapshot, store=None, timeout: float = 5.0) -> set[str]:
    """返回当前已加载模型的规范化名称集合。"""
    return {_norm_model_name(m.get("name", "")) for m in ps(snapshot, store=store, timeout=timeout).get("models", [])}


def pull(
    snapshot: LocalOllamaSnapshot,
    model: str,
    store=None,
    timeout: float | None = None,
    on_progress=None,
    should_abort=None,
) -> tuple[dict, list[dict]]:
    """流式拉取模型（/api/pull）。逐 NDJSON 行解析，`on_progress(current, total)` 可选。

    返回 (最终状态消息, 本次解析的所有进度消息列表)；网络/解析错误向上抛出由
    worker 分类为失败。`timeout` 覆盖时采用总超时，逐行读取靠底层 socket 超时兜底。
    `should_abort()` 在每次流读取边界求值，truthy 时中止并返回 `status="cancelled"`，
    用于协作式取消（§7 取消规则 4 的「流读取边界停止」）。
    """
    url = snapshot.base_url.rstrip("/") + "/api/pull"
    data = json.dumps({"name": model, "stream": True}).encode("utf-8")
    resp = llm_transport.allowed_urlopen(
        url, channel=_CHANNEL, store=store, timeout=timeout,
        headers=_JSON_HEADERS, data=data, method="POST",
    )
    seen: list[dict] = []
    final: dict = {"status": "unknown", "model": model}
    for raw in resp:
        if should_abort is not None and should_abort():
            final = {"status": "cancelled", "model": model}
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        seen.append(msg)
        if on_progress is not None:
            progress = msg.get("progress")
            if isinstance(progress, (int, float)):
                on_progress(int(progress), msg.get("total"))
        if msg.get("status") in ("success", "error"):
            final = msg
            break
    return final, seen


def load(snapshot: LocalOllamaSnapshot, model: str, store=None, timeout: float | None = None) -> dict:
    """预热加载模型（/api/generate）。幂等；失败由 worker 分类为可重试。"""
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": snapshot.keep_alive}
    return _post_json(snapshot, "/api/generate", payload, store=store, timeout=timeout)


def unload(snapshot: LocalOllamaSnapshot, model: str, store=None, timeout: float | None = None) -> dict:
    """卸载模型（/api/generate keep_alive=0）。仅影响内存，不删除模型文件。幂等。"""
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    return _post_json(snapshot, "/api/generate", payload, store=store, timeout=timeout)


def _norm_model_name(name: str) -> str:
    """规范化模型名：`qwen3` 与 `qwen3:latest` 视为同一模型（默认 tag）。"""
    name = (name or "").strip()
    if not name:
        return name
    base, _, tag = name.partition(":")
    if not tag or tag == "latest":
        return base
    return name


def model_installed(snapshot: LocalOllamaSnapshot, model: str, store=None, timeout: float = 5.0) -> bool:
    names = {_norm_model_name(m.get("name")) for m in tags(snapshot, store=store, timeout=timeout).get("models", [])}
    return _norm_model_name(model) in names


def health(snapshot: LocalOllamaSnapshot, model: str, store=None) -> dict:
    """安全健康探测：可达性、版本、模型是否安装、当前是否加载。失败不抛异常。"""
    out = {
        "reachable": False,
        "version": None,
        "modelInstalled": False,
        "modelRunning": False,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        v = version(snapshot, store=store)
        out["reachable"] = True
        out["version"] = v.get("version")
        out["modelInstalled"] = model_installed(snapshot, model, store=store)
    except Exception:
        return out
    try:
        if _norm_model_name(model) in running_models(snapshot, store=store):
            out["modelRunning"] = True
    except Exception:
        pass
    return out
