"""Agent 记忆 API — FastAPI 路由

对标 CCSwitch 的 workspace 记忆管理：
  - OpenClaw 标准身份文件 CRUD（SOUL / AGENTS / IDENTITY / USER）
  - 旧长期记忆与日记兼容 API
  - 语义搜索
  - Agent 上下文注入
"""

import logging
import ipaddress
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

import memory_store
import tokenmanager_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local(request: Request, x_requested_by: Optional[str] = Header(default=None)):
    if x_requested_by != "centaur-vdb":
        raise HTTPException(403, "缺少 X-Requested-By 头（跨站请求防护）")
    if not _is_loopback_request(request):
        raise HTTPException(403, "本机管理接口仅允许 loopback 访问")


# ==================== 请求模型 ====================


class WriteFileRequest(BaseModel):
    content: str
    source_agent: str = "manual"


class WriteJournalRequest(BaseModel):
    content: str
    source_agent: str = "manual"


class SearchRequest(BaseModel):
    query: str
    n_results: int = 10
    memory_type: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    identity_only: bool = False


class TokenManagerSyncConfigRequest(BaseModel):
    enabled: bool
    url: str = tokenmanager_sync.DEFAULT_URL
    token: Optional[str] = None
    interval_seconds: int = 60


# ==================== 记忆文件 ====================


@router.get("/files")
def list_files():
    """列出所有记忆文件"""
    return {"files": memory_store.list_memory_files()}


@router.get("/files/{path:path}")
def read_file(path: str):
    """读取指定记忆文件"""
    from urllib.parse import unquote
    path = unquote(path)
    result = memory_store.read_memory_file(path)
    if result is None:
        raise HTTPException(404, f"文件不存在: {path}")
    return result


@router.put("/files/{path:path}", dependencies=[Depends(require_local)])
def write_file(path: str, req: WriteFileRequest):
    """写入/更新记忆文件（自动向量化）"""
    from urllib.parse import unquote
    path = unquote(path)
    try:
        result = memory_store.write_memory_file(
            path, req.content, req.source_agent
        )
        response = {"success": True, **result}
        if path in {"SOUL.md", "AGENTS.md", "IDENTITY.md", "USER.md"}:
            response["identitySync"] = tokenmanager_sync.publish_identity()
        return response
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/files/{path:path}", dependencies=[Depends(require_local)])
def delete_file(path: str):
    """删除记忆文件"""
    from urllib.parse import unquote
    path = unquote(path)
    ok = memory_store.delete_memory_file(path)
    if not ok:
        raise HTTPException(404, f"文件不存在或删除失败: {path}")
    return {"success": True}


# ==================== 日记 ====================


@router.get("/journal")
def list_journals(
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
):
    """列出日记"""
    return {"journals": memory_store.list_journals(from_date, to_date)}


@router.get("/journal/{date}")
def read_journal(date: str):
    """读取指定日期日记"""
    result = memory_store.read_journal(date)
    if result is None:
        return {"date": date, "content": "", "exists": False}
    return {**result, "exists": True}


@router.put("/journal/{date}", dependencies=[Depends(require_local)])
def write_journal(date: str, req: WriteJournalRequest):
    """写入日记"""
    try:
        result = memory_store.write_journal(date, req.content, req.source_agent)
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/journal/{date}", dependencies=[Depends(require_local)])
def delete_journal(date: str):
    """删除日记"""
    path = f"{memory_store.JOURNAL_DIR_NAME}/{date}.md"
    ok = memory_store.delete_memory_file(path)
    return {"success": ok}


# ==================== 语义搜索 ====================


@router.post("/search")
def search(req: SearchRequest):
    """语义搜索记忆内容"""
    if not req.query.strip():
        raise HTTPException(400, "查询内容为空")
    results = memory_store.search_memory(
        query=req.query,
        n_results=req.n_results,
        memory_type=req.memory_type,
        date_from=req.date_from,
        date_to=req.date_to,
        identity_only=req.identity_only,
    )
    return {
        "query": req.query,
        "results": results,
        "total": len(results),
    }


# ==================== Agent 上下文注入 ====================


@router.get("/context")
def get_context(
    agent: str = Query(default="default"),
    limit_chars: Optional[int] = Query(default=None),
):
    """生成 Agent 启动注入上下文"""
    return memory_store.get_context(agent=agent, limit_chars=limit_chars)


# ==================== 重建索引 ====================


@router.post("/reindex", dependencies=[Depends(require_local)])
def reindex():
    """增量重建全部记忆的向量索引（内容未变的文件跳过）"""
    result = memory_store.reindex_all_memory()
    return {"success": True, "files_indexed": result.get("indexed", 0), **result}


# ==================== TokenManager Agent 对话与记忆 ====================


@router.get("/tokenmanager")
def tokenmanager_status():
    return tokenmanager_sync.public_status()


@router.post("/tokenmanager/config", dependencies=[Depends(require_local)])
def configure_tokenmanager(req: TokenManagerSyncConfigRequest):
    try:
        return tokenmanager_sync.save_config(
            enabled=req.enabled,
            url=req.url,
            token=req.token,
            interval_seconds=req.interval_seconds,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/tokenmanager/test", dependencies=[Depends(require_local)])
def test_tokenmanager():
    try:
        return tokenmanager_sync.test_connection()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/tokenmanager/sync", dependencies=[Depends(require_local)])
def sync_tokenmanager():
    result = tokenmanager_sync.sync_now(max_pages=100)
    if not result.get("success") and not result.get("busy"):
        raise HTTPException(502, result.get("error") or "TokenManager 同步失败")
    return result


@router.get("/identity/status")
def identity_status():
    status = tokenmanager_sync.public_status()
    return {
        "pending": status.get("identity_pending", False),
        "pending_revision": status.get("identity_pending_revision"),
        "last_revision": status.get("identity_last_revision"),
        "last_completed_at": status.get("identity_last_completed_at"),
        "last_error": status.get("identity_last_error"),
        "last_result": status.get("identity_last_result"),
        "capabilities": status.get("capabilities", []),
        "token_configured": status.get("token_configured", False),
    }


@router.post("/identity/sync", dependencies=[Depends(require_local)])
def sync_identity():
    return tokenmanager_sync.publish_identity()
