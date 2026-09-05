"""知君运行状态：将要使用的模型通道、抽取开关、后台 worker 是否在跑。只读，不发网络请求。"""
from __future__ import annotations

from fastapi import APIRouter

from .zhijun import jobs
from .zhijun.provider import provider_status

_PREFIX = "/api/mindos/zhijun"
_TAGS = ["zhijun-status"]


def status():
    info = provider_status()
    if not jobs.extraction_enabled():
        extraction = "disabled"
    elif info.get("provider") == "ollama":
        extraction = "beta"
    else:
        extraction = "enabled"
    try:
        from .stores.ontology_store import OntologyStore

        pending = OntologyStore.instance().pending_jobs()
    except Exception:  # noqa: BLE001
        pending = None
    return {
        "provider": info.get("provider"),
        "model": info.get("model"),
        "external": bool(info.get("external")),
        "configured": bool(info.get("configured")),
        "error": info.get("error"),
        "extraction": extraction,
        "workerRunning": jobs.worker_running(),
        "pendingJobs": pending,
    }


router = APIRouter(prefix=_PREFIX, tags=_TAGS)
router.add_api_route("/status", status, methods=["GET"])
