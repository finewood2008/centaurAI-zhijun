"""MindOS 知识成品 API，复用 Wiki 存储但隔离 MindOS 数据契约。"""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import wiki_store
from runtime_paths import TRASH_DIR
from .services import ingestion
from .stores import governance_store, card_ledger_store
from .stores.job_store import FolderError, FolderNameConflictError, SCOPE_KNOWLEDGE
from .tag_suggest import suggest_tags
from . import knowledge_index

router = APIRouter(prefix="/api/mindos/knowledge", tags=["mindos-knowledge"])

_CARD_LOCKS_GUARD = threading.Lock()
_CARD_LOCKS: dict[str, threading.RLock] = {}
_REPAIR_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="card-vector-repair")
_REPAIR_LOCK = threading.Lock()
_REPAIR_RUNNING = False
_REPAIR_ACCEPTING = True
_REPAIR_IDLE = threading.Event()
_REPAIR_IDLE.set()


class KnowledgeCreate(BaseModel):
    title: str
    content: str = ""
    tags: list[str] = []
    # P14-07：目标目录 ID（scope=KNOWLEDGE）；缺省时进入「Resources」知识根目录。
    folderId: int | None = None


class KnowledgeUpdate(BaseModel):
    title: str = ""
    content: str
    # Omitted tags preserve the existing values; an explicit [] clears them.
    tags: list[str] | None = None
    # 目录归属：缺省保留当前目录；传入时校验为 KNOWLEDGE 目录。
    folderId: int | None = None
    expectedRevision: str | None = None


class KnowledgeTagRequest(BaseModel):
    tags: list[str]
    action: str  # "add" or "remove"
    expectedRevision: str | None = None


class KnowledgeMoveRequest(BaseModel):
    """卡片移动到目录：folderId 为 KNOWLEDGE 目录 ID；null = 移回知识根目录。"""

    folderId: int | None = None
    expectedRevision: str | None = None


class KnowledgeRevisionRequest(BaseModel):
    expectedRevision: str | None = None


class KnowledgeEditDraftSave(BaseModel):
    expectedDraftRevision: str
    title: str = ""
    content: str = ""
    tags: list[str] = []
    folderId: int | None = None
    sourceRefs: list[dict] = []


class KnowledgeEditDraftConfirm(BaseModel):
    expectedDraftRevision: str


class KnowledgeSourceRef(BaseModel):
    """单条来源引用。sourceType 仅允许 material / knowledge（P15-01）。"""

    sourceType: str
    id: str


class KnowledgeSourcesUpdate(BaseModel):
    """PUT /sources 请求体：提交去重前的完整来源列表（顺序即用户意图）。"""

    sourceRefs: list[KnowledgeSourceRef]
    expectedRevision: str | None = None


class KnowledgeFromMaterialCreate(BaseModel):
    """从原材料创建卡片时的显式草稿选择。

    默认保持原来的空白引用卡片，避免在用户未选择时把 AI 派生内容伪装成正式知识；
    仅 ``prefillFromSummary=true`` 时才把已经生成的资料摘要写入可编辑草稿。
    """

    prefillFromSummary: bool = False


def _knowledge_id(path: str) -> str:
    """根据规范化相对路径生成知识卡片 ID。

    统一使用正斜杠相对路径作为 key，避免同一文件因反斜杠/正斜杠混用
    被当成两个不同页面（导致列表、搜索、治理中出现重复卡片）。
    """
    norm = str(path or "").replace("\\", "/").lstrip("./")
    return "knowledge_" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _content_revision(content: str) -> str:
    return "rev_" + hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:12]


def _card_lock(knowledge_id: str) -> threading.RLock:
    with _CARD_LOCKS_GUARD:
        return _CARD_LOCKS.setdefault(knowledge_id, threading.RLock())


def _strict_meta(content: str) -> tuple[dict, str]:
    """Parse a MindOS card without silently discarding malformed metadata."""
    if not content.startswith("---\n"):
        raise HTTPException(422, "invalid_card_frontmatter: 缺少 frontmatter")
    end = content.find("\n---", 4)
    if end < 0:
        raise HTTPException(422, "invalid_card_frontmatter: frontmatter 未闭合")
    for line in content[4:end].splitlines():
        if line.strip() and ":" not in line:
            raise HTTPException(422, "invalid_card_frontmatter: 存在非法字段行")
    meta, body = wiki_store._parse_frontmatter(content)
    if meta.get("mindos_card") is not True:
        raise HTTPException(422, "invalid_card_frontmatter: mindos_card 必须为 true")
    if not isinstance(meta.get("tags", []), list) or not all(isinstance(item, str) for item in meta.get("tags", [])):
        raise HTTPException(422, "invalid_card_frontmatter: tags 必须为字符串数组")
    refs = meta.get("mindos_source_refs", [])
    if refs and (not isinstance(refs, list) or not all(isinstance(item, dict) and item.get("sourceType") in ("material", "knowledge") and item.get("id") for item in refs)):
        raise HTTPException(422, "invalid_card_frontmatter: mindos_source_refs 非法")
    for key in ("created_at", "updated_at"):
        value = meta.get(key)
        # Pre-ledger cards legitimately omitted these fields.  Preserve and
        # normalize them on their next successful mutation; reject malformed
        # values rather than making legacy cards permanently uneditable.
        if value is None:
            continue
        if not isinstance(value, str):
            raise HTTPException(422, f"invalid_card_frontmatter: {key} 必须为 ISO-8601 时间")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, f"invalid_card_frontmatter: {key} 非法") from exc
    return meta, body


def _check_revision(page: dict, expected: str | None) -> None:
    actual = _content_revision(str(page.get("content") or ""))
    if expected is not None and expected != actual:
        raise HTTPException(409, detail={"code": "revision_conflict",
                                         "detail": "卡片已在其他位置更新，请刷新后重试",
                                         "revision": actual, "updatedAt": _updated_at(page)})


def _source_ids(page: dict) -> list[str]:
    """Read MindOS-only material source IDs from a card's frontmatter, never source paths."""
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return []
    values = meta.get("mindos_source_material_ids") or []
    return [str(value) for value in values if str(value).startswith("mindos_")]


def _source_refs(page: dict) -> list[dict]:
    """读取带类型的来源引用 [{sourceType, id}]（P14-10）。

    优先读取新字段 mindos_source_refs（可混合 material / knowledge）；同时兼容旧字段
    mindos_source_material_ids（仅 material，保证旧卡片仍可追溯）；按 (sourceType, id)
    去重并保留首次出现顺序。
    """
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return []
    refs: list[dict] = []
    raw_refs = meta.get("mindos_source_refs") or []
    if not isinstance(raw_refs, list):
        raw_refs = []
    for r in raw_refs:
        if not isinstance(r, dict):
            continue
        st = str(r.get("sourceType") or "").strip()
        sid = str(r.get("id") or "").strip()
        if st in ("material", "knowledge") and sid:
            refs.append({"sourceType": st, "id": sid})
    old = meta.get("mindos_source_material_ids") or []
    if isinstance(old, str):
        old = [old]
    for sid in old:
        sid = str(sid).strip()
        if sid.startswith("mindos_"):
            refs.append({"sourceType": "material", "id": sid})
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for ref in refs:
        key = (ref["sourceType"], ref["id"])
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _is_mindos_card(page: dict) -> bool:
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
        return bool(meta.get("mindos_card"))
    except Exception:
        return False


def _tags(page: dict) -> list[str]:
    """Read tags from a card's frontmatter."""
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return []
    raw = meta.get("tags") or []
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if str(t).strip()]


def _sources(page: dict) -> list[dict]:
    recycled_ids: set[str] = set()
    try:
        recycled_ids = ingestion.recycled_material_ids()
    except Exception:
        pass
    sources = []
    for ref in _source_refs(page):
        st, sid = ref["sourceType"], ref["id"]
        if st == "material":
            record = ingestion.status_of(sid)
            if record:
                sources.append({
                    "sourceType": "material",
                    "id": sid,
                    "materialId": sid,
                    "title": record["fileName"],
                    "fileName": record["fileName"],
                    "archived": False,
                    "recycled": sid in recycled_ids,
                })
            else:
                sources.append({
                    "sourceType": "material", "id": sid, "materialId": sid,
                    "title": "来源资料已不可用", "fileName": "来源资料已不可用",
                    "archived": False, "recycled": False,
                })
        else:
            try:
                card = _find(sid)
                sources.append({
                    "sourceType": "knowledge",
                    "id": sid,
                    "knowledgeId": sid,
                    "title": card.get("title") or sid,
                    "fileName": card.get("title") or sid,
                    "archived": _is_archived(card),
                    "recycled": _is_recycled(card),
                })
            except HTTPException:
                sources.append({
                    "sourceType": "knowledge", "id": sid, "knowledgeId": sid,
                    "title": "来源卡片已不可用", "fileName": "来源卡片已不可用",
                    "archived": False, "recycled": False,
                })
    return sources


def _card_folder_id(page: dict) -> int | None:
    """读取卡片 frontmatter 中的 mindos_folder_id（KNOWLEDGE 目录节点 ID）。"""
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return None
    value = meta.get("mindos_folder_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _effective_card_folder_id(page: dict) -> int | None:
    knowledge_id = _knowledge_id(str(page.get("path") or ""))
    ledger = card_ledger_store.get(knowledge_id) or {}
    if ledger.get("folder_id") is not None:
        return int(ledger["folder_id"])
    return _card_folder_id(page)


def _knowledge_folder_node(folder_id: int) -> dict | None:
    """按 ID 校验目录节点：必须是存在的 KNOWLEDGE 目录；否则返回 None。

    目录服务不可用（如隔离的单元测试环境）时同样返回 None，
    详情/列表读取不因目录解析失败而整体报错。
    """
    try:
        node = ingestion.JobStore.instance().folder_node(folder_id)
    except Exception:
        return None
    if node is None or node["scope"] != SCOPE_KNOWLEDGE:
        return None
    return node


def _resources_root_node() -> dict | None:
    """查找 KNOWLEDGE scope 的「Resources」根节点（只查不建）。"""
    try:
        nodes = ingestion.JobStore.instance().list_folder_nodes(SCOPE_KNOWLEDGE)
    except Exception:
        return None
    for node in nodes:
        if node["parentId"] is None and node["name"] == "Resources":
            return node
    return None


def _ensure_resources_root() -> dict | None:
    """确保 KNOWLEDGE 「Resources」根目录存在（并发安全：冲突时重新查找）。

    目录服务不可用时返回 None，写路径据此省略 mindos_folder_id（保持兼容）。
    """
    existing = _resources_root_node()
    if existing is not None:
        return existing
    try:
        return ingestion.JobStore.instance().create_folder_node(SCOPE_KNOWLEDGE, "Resources")
    except FolderNameConflictError:
        rerun = _resources_root_node()
        if rerun is not None:
            return rerun
        raise
    except Exception:
        return None


def _resolve_target_folder(folder_id: int | None) -> dict | None:
    """解析写路径目标目录。

    - folderId 缺省 → 知识根目录（目录服务不可用时返回 None，写路径省略目录写入）；
    - 指定 folderId → 必须存在且 scope=KNOWLEDGE，否则 404/400；服务不可用 → 503。
    """
    if folder_id is None:
        return _ensure_resources_root()
    try:
        node = ingestion.JobStore.instance().folder_node(folder_id)
    except Exception:
        raise HTTPException(503, "知识目录服务暂不可用")
    if node is None:
        raise HTTPException(404, "目标目录不存在")
    if node["scope"] != SCOPE_KNOWLEDGE:
        raise HTTPException(400, "知识卡片只能归入知识目录（scope=KNOWLEDGE），不能选择原材料目录")
    return node


def _effective_folder_node(page: dict) -> dict | None:
    """卡片的有效目录：frontmatter 指向的 KNOWLEDGE 目录；缺失/悬空/被删时归回 Resources。

    目录服务不可用时返回 None。
    """
    folder_id = _effective_card_folder_id(page)
    if folder_id is not None:
        node = _knowledge_folder_node(folder_id)
        if node is not None:
            return node
    return _ensure_resources_root()


def _public_folder(page: dict) -> tuple[int | None, str]:
    """返回 (folderId, folderPath)；正常时 folderId 有值（缺省为 Resources 根）。"""
    node = _effective_folder_node(page)
    if node is None:
        return None, ""
    try:
        path = ingestion.JobStore.instance().folder_path(node["id"])
    except Exception:
        path = ""
    return node["id"], path


def _render_frontmatter(meta: dict) -> str:
    """将 frontmatter dict 渲染为「---」包裹的文本（列表/布尔/数字原样序列化）。"""
    fm_lines = []
    for key, value in meta.items():
        if isinstance(value, list):
            fm_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif isinstance(value, bool):
            fm_lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            fm_lines.append(f"{key}: {value}")
        else:
            fm_lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    return "---\n" + "\n".join(fm_lines) + "\n---"


def _write_meta(page: dict, patch: dict) -> dict:
    """就地改写 frontmatter 指定键（保留其余全部字段与正文）。"""
    content = str(page.get("content") or "")
    meta, body = _strict_meta(content)
    meta.update(patch)
    return _write_card(page, f"{_render_frontmatter(meta)}\n{body}")


def _write_card(page: dict, content: str) -> dict:
    """Serialize writes for one card inside this backend process."""
    path = str(page["path"])
    with _card_lock(_knowledge_id(path)):
        return wiki_store.write_page(path, content, source_agent="mindos")


def _public(page: dict) -> dict:
    updated = page.get("updated_at") or page.get("updatedAt")
    if isinstance(updated, (int, float)):
        updated = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat()
    sources = _sources(page)
    is_archived = _is_archived(page)
    is_merged = _is_merged(page)
    archived_count = sum(1 for s in sources if s.get("archived"))
    if sources and archived_count:
        source_label = f"{len(sources)} 项来源（{archived_count} 项已归档）"
    elif sources:
        source_label = f"{len(sources)} 项来源"
    else:
        source_label = "无来源"
    folder_id, folder_path = _public_folder(page)
    knowledge_id = _knowledge_id(str(page["path"]))
    ledger = card_ledger_store.get(knowledge_id) or {}
    edit_draft = card_ledger_store.get_edit_draft(knowledge_id)
    pending_update = card_ledger_store.get_pending_update(knowledge_id)
    try:
        _strict_meta(str(page.get("content") or ""))
        metadata_status = "valid"
    except Exception:
        metadata_status = "invalid"
    return {
        "knowledgeId": knowledge_id,
        "title": page.get("title") or "未命名知识卡片",
        "content": page.get("content") or "",
        # P14-07：知识卡片归入 KNOWLEDGE 目录树；folder 兼容字段 = 目录末段名。
        "folderId": folder_id,
        "folderPath": folder_path,
        "folder": folder_path.rsplit("/", 1)[-1] if folder_path else "Resources",
        "updatedAt": updated or "",
        "sourceLabel": source_label,
        "sources": sources,
        "tags": _tags(page),
        "readOnly": False,
        "isArchived": is_archived,
        "isRecycled": _is_recycled(page),
        "isMerged": is_merged,
        "revision": _content_revision(str(page.get("content") or "")),
        "vectorSyncState": ledger.get("vector_sync_state", "pending"),
        "approvalState": ledger.get("approval_state", "draft"),
        "indexState": ledger.get("index_state", "none"),
        "ragEligible": card_ledger_store.is_rag_eligible(
            ledger, _content_revision(str(page.get("content") or "")),
        ),
        "metadataStatus": metadata_status,
        "editDraft": ({"exists": True, "baseRevision": edit_draft["base_revision"],
                       "draftRevision": edit_draft["draft_revision"], "updatedAt": edit_draft["updated_at"]}
                      if edit_draft else {"exists": False}),
        "metadataRevision": int(ledger.get("metadata_revision") or 0),
        "pendingUpdate": ({"state": pending_update["state"], "phase": pending_update.get("phase"),
                           "targetRevision": pending_update.get("target_revision"),
                           "errorCode": pending_update["error_code"]}
                          if pending_update else None),
    }


def _is_rag_eligible_page(page: dict, *, device_scope: str = "global") -> bool:
    """Whether this exact Wiki revision may be exposed to RAG consumers."""
    knowledge_id = _knowledge_id(str(page.get("path") or ""))
    return card_ledger_store.is_rag_eligible(
        card_ledger_store.get(knowledge_id, device_scope=device_scope),
        _content_revision(str(page.get("content") or "")),
    )


def _require_draft_for_mutation(knowledge_id: str) -> None:
    """Reject normal frontmatter/content writes to a confirmed card revision."""
    state = card_ledger_store.get(knowledge_id)
    if state and state.get("approval_state") == "confirmed":
        raise HTTPException(409, "已确认卡片请先创建修改草稿，再修改卡片")


def _iter_wiki_pages():
    """遍历全部 Wiki 页面；关系校验与影响分析不能使用列表展示上限。"""
    offset = 0
    page_size = 500
    while True:
        result = wiki_store.list_pages(limit=page_size, offset=offset)
        pages = result.get("items", [])
        yield from pages
        offset += len(pages)
        if not pages or offset >= int(result.get("total", 0)):
            break


def _find(knowledge_id: str) -> dict:
    """Find a MindOS card by its stable ID without imposing a list-page cap."""
    for page in _iter_wiki_pages():
        if _knowledge_id(str(page["path"])) == knowledge_id:
            detail = wiki_store.read_page(str(page["path"]))
            if detail and _is_mindos_card(detail):
                return detail
    # write_page() publishes the Markdown file atomically, while a filesystem
    # event may race the SQLite page-list refresh. On a cache miss, inspect
    # only existing Wiki files and let read_page refresh the matched one.
    # This is a lookup repair, not a startup scan or a card-state migration.
    try:
        root = wiki_store._wiki_root()
        for path in root.rglob("*.md"):
            if path.name.startswith("."):
                continue
            rel_path = wiki_store._rel_from_path(path)
            if _knowledge_id(rel_path) != knowledge_id:
                continue
            detail = wiki_store.read_page(rel_path)
            if detail and _is_mindos_card(detail):
                return detail
            break
    except OSError as exc:
        logger.warning("知识卡片文件回读失败: %s", type(exc).__name__)
    raise HTTPException(404, detail={"code": "knowledge_not_found", "detail": "知识成品不存在"})


def find_card_by_confirmation_session(session_id: str) -> dict | None:
    """Locate only a card explicitly stamped for an unfinished material confirmation."""
    if not session_id:
        return None
    candidates = list(_iter_wiki_pages())
    # The card may have been published immediately before a process crash, before
    # the Wiki page-list cache observed its filesystem event.
    try:
        root = wiki_store._wiki_root()
        known = {str(item.get("path") or "") for item in candidates}
        for path in root.rglob("*.md"):
            rel_path = wiki_store._rel_from_path(path)
            if rel_path not in known:
                candidates.append({"path": rel_path})
    except OSError as exc:
        logger.warning("确认卡片恢复扫描失败: %s", type(exc).__name__)
    for item in candidates:
        page = wiki_store.read_page(str(item.get("path") or "")) or item
        if not _is_mindos_card(page):
            continue
        try:
            meta, _ = _strict_meta(str(page.get("content") or ""))
        except HTTPException:
            continue
        if str(meta.get("mindos_confirmation_session") or "") == session_id:
            return page
    return None


def cards_referencing_material(material_id: str, *, device_scope: str | None = None) -> list[dict]:
    """显式 scope 只返回已知归属的依赖；内部删除检查不传 scope 时保留全量依赖。

    依赖包括草稿/归档/回收，不能以 RAG 准入判断是否存在。旧草稿可能尚无
    归属账本：前台不曝光，内部也不能因此断言资料可安全删除。
    """
    results: list[dict] = []
    for item in _iter_wiki_pages():
        knowledge_id = _knowledge_id(str(item.get("path") or ""))
        if device_scope is not None and not card_ledger_store.get(knowledge_id, device_scope=device_scope):
            continue
        page = wiki_store.read_page(str(item["path"])) or item
        if not _is_mindos_card(page):
            continue
        if not any(
            ref["sourceType"] == "material" and ref["id"] == material_id
            for ref in _source_refs(page)
        ):
            continue
        results.append({
            "knowledgeId": _knowledge_id(str(page["path"])),
            "title": page.get("title") or "未命名知识卡片",
            "archived": _is_archived(page),
            "recycled": _is_recycled(page),
        })
    return results


def cards_referencing_knowledge(knowledge_id: str) -> list[dict]:
    """返回引用某知识卡片作为来源的其它卡片（P15-04 卡片删除影响预览）。

    与归档影响一致：已归档 / 已回收卡片同样记录在案（供用户在处理依赖时一并决策），
    但只有活跃卡片作为阻塞依赖。
    """
    results: list[dict] = []
    for item in _iter_wiki_pages():
        page = wiki_store.read_page(str(item["path"])) or item
        if not _is_mindos_card(page):
            continue
        if _knowledge_id(str(page["path"])) == knowledge_id:
            continue
        if not any(
            ref["sourceType"] == "knowledge" and ref["id"] == knowledge_id
            for ref in _source_refs(page)
        ):
            continue
        results.append({
            "knowledgeId": _knowledge_id(str(page["path"])),
            "title": page.get("title") or "未命名知识卡片",
            "archived": _is_archived(page),
            "recycled": _is_recycled(page),
        })
    return results


def configure_write_guard(guard) -> None:
    global router
    router = APIRouter(prefix="/api/mindos/knowledge", tags=["mindos-knowledge"])
    router.add_api_route("", knowledge_create, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}", knowledge_update, methods=["PUT"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/move", knowledge_move, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/tags", knowledge_tags, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/tag-suggestions", knowledge_tag_suggestions, methods=["GET"])
    router.add_api_route("/{knowledge_id}/sources", knowledge_sources, methods=["GET"])
    router.add_api_route("/{knowledge_id}/sources", knowledge_update_sources, methods=["PUT"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/retry-index", knowledge_retry_index, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/confirm", knowledge_confirm, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/edit-draft", knowledge_begin_edit_draft, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/edit-draft", knowledge_get_edit_draft, methods=["GET"])
    router.add_api_route("/{knowledge_id}/edit-draft", knowledge_save_edit_draft, methods=["PUT"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/edit-draft/confirm", knowledge_confirm_edit_draft, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/{knowledge_id}/edit-draft/retry", knowledge_retry_edit_draft, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("", knowledge_list, methods=["GET"])
    router.add_api_route("/{knowledge_id}", knowledge_detail, methods=["GET"])


def _is_archived(page: dict) -> bool:
    """兼容内部调用：只有已合并卡片属于不可用的历史记录。

    独立归档状态已废弃，旧的 ``mindos_archived`` 标记不再参与状态机。
    """
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return False
    return bool(meta.get("mindos_merged_into"))


def _is_merged(page: dict) -> bool:
    """卡片是否因合并而归档（mindos_merged_into），此类卡片不可单独恢复。"""
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return False
    return bool(meta.get("mindos_merged_into"))


def _is_recycled(page: dict) -> bool:
    """卡片是否已回收（P15-05：frontmatter mindos_recycled，普通列表/搜索/图谱隐藏）。"""
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        return False
    return bool(meta.get("mindos_recycled"))


def _is_active_mindos_card(page: dict) -> bool:
    """MindOS 卡片且未归档、未回收（列表 / 搜索 / 图谱可见）。"""
    return _is_mindos_card(page) and not _is_archived(page) and not _is_recycled(page)


# 问答词不应成为卡片检索的唯一条件。例如「MindOS 是什么」中的有效检索词是
# ``MindOS``，而不是完整问句；GBrain 不可用时尤其依赖这一降级路径。
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*|[\u4e00-\u9fff]+")
_QUESTION_SUFFIX_RE = re.compile(
    r"(?:是什么意思|是什么|是啥|是多少|有哪些|怎么样|怎么做|如何|多少|吗|呢|的)$"
)


def _card_body(page: dict) -> str:
    """返回可供检索与问答使用的卡片正文，不把 frontmatter/重复标题当成知识。"""
    raw = str(page.get("content") or "")
    try:
        _, body = wiki_store._parse_frontmatter(raw)
    except Exception:
        body = raw

    lines = body.strip().splitlines()
    # create/from-material 写入的空白模板只有 ``# 标题``。标题已作为独立字段返回，
    # 移除这个重复 heading 后空字符串才能如实表达「尚未填写知识内容」。
    if lines and re.fullmatch(r"#{1,6}\s+.*", lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _is_substantive_card_body(body: str) -> bool:
    """判定正文是否足以作为问答证据，而非空模板/合并占位文本。"""
    useful: list[str] = []
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line or line == "---" or re.fullmatch(r"#{1,6}\\s+.*", line):
            continue
        # 历史合并卡片可能只剩「合并自：标题」记录；它是操作痕迹，不是知识正文。
        if line.startswith("合并自："):
            continue
        useful.append(line)
    return len("".join(useful)) >= 16


def _sync_card_index(page: dict) -> None:
    """把变更路径的卡片同步到派生向量索引（绝不从读路径调用）。"""
    # Wiki 写入适配层在极简测试替身或旧版本中可能仅表示成功而返回 None；
    # 索引属于派生数据，绝不能反过来让主写入/治理事务失败。
    if not isinstance(page, dict):
        return
    knowledge_id = _knowledge_id(str(page.get("path") or ""))
    # 阶段 C：普通保存、Wiki 监听和批量修复不得让 draft/confirming 卡片入队。
    # 确认流程是唯一允许调用 confirm_and_enqueue 的生产端。
    ledger_before = card_ledger_store.get(knowledge_id)
    if not ledger_before or ledger_before.get("approval_state") != "confirmed":
        return
    page_revision = _content_revision(str(page.get("content") or ""))
    if str(ledger_before.get("current_revision") or "") != page_revision:
        # The controlled working-copy publisher writes the file immediately
        # before it flips the ledger.  Ignore that short, durable pending state;
        # it is not an external mutation and the outbox worker will activate it.
        pending = card_ledger_store.get_pending_update(knowledge_id)
        if pending and str(pending.get("target_revision") or "") == page_revision:
            return
        # A watcher or another writer changed a confirmed file. Do not silently
        # adopt it as the confirmed revision or enqueue it for indexing.
        card_ledger_store.mark_needs_reconfirmation(knowledge_id)
        return
    body = _card_body(page)
    if not _is_active_mindos_card(page) or not _is_substantive_card_body(body):
        visibility = "archived" if _is_archived(page) else "recycled" if _is_recycled(page) else "active"
        if visibility == "active":
            card_ledger_store.ensure(knowledge_id, str(page.get("path") or ""),
                                     _content_revision(str(page.get("content") or "")), visibility)
            # Empty/template-only content must immediately hide the previous
            # active semantic version while keeping the card itself active.
            card_ledger_store.mark_visibility(knowledge_id, "active", "empty")
        else:
            card_ledger_store.mark_visibility(knowledge_id, visibility, visibility)
        return
    state = card_ledger_store.ensure(knowledge_id, str(page.get("path") or ""),
                                     _content_revision(str(page.get("content") or "")), "active")
    if not card_ledger_store.can_index(state, _content_revision(str(page.get("content") or ""))):
        return
    card_ledger_store.enqueue_vector_repair(
        knowledge_id, int(state["desired_vector_version"]), json.dumps({
            "title": str(page.get("title") or "未命名知识卡片"), "body": body,
            "tags": _tags(page), "content_revision": page_revision,
            "rel_path": str(page.get("path") or ""), "folder_id": _effective_card_folder_id(page),
        }, ensure_ascii=False),
    )
    _schedule_vector_repairs()


def _schedule_vector_repairs() -> None:
    global _REPAIR_RUNNING
    with _REPAIR_LOCK:
        if _REPAIR_RUNNING or not _REPAIR_ACCEPTING:
            return
        _REPAIR_RUNNING = True
        _REPAIR_IDLE.clear()
    _REPAIR_POOL.submit(_run_vector_repairs)


def start_vector_worker() -> None:
    global _REPAIR_ACCEPTING
    with _REPAIR_LOCK:
        _REPAIR_ACCEPTING = True


def stop_vector_worker(wait: bool = True) -> None:
    """Stop new claims and optionally drain the current card-index attempt."""
    global _REPAIR_ACCEPTING
    with _REPAIR_LOCK:
        _REPAIR_ACCEPTING = False
    if wait:
        _REPAIR_IDLE.wait()


def _run_vector_repairs() -> None:
    global _REPAIR_RUNNING
    try:
        while True:
            with _REPAIR_LOCK:
                if not _REPAIR_ACCEPTING:
                    return
            job = card_ledger_store.claim_vector_job()
            if job is None:
                return
            try:
                payload = json.loads(job["payload_json"])
                state = card_ledger_store.get(str(job["knowledge_id"]))
                is_pending_edit = bool(payload.get("pending_edit_update"))
                # A newer edit or visibility change makes this job obsolete.
                if (
                    not state
                    or (not is_pending_edit and not card_ledger_store.can_index(state, str(payload.get("target_revision") or "")))
                    or (is_pending_edit and not card_ledger_store.pending_update_can_index(
                        str(job["knowledge_id"]), str(payload.get("target_revision") or ""), int(job["vector_version"])))
                    or int(state.get("desired_vector_version") or 0) != int(job["vector_version"])
                ):
                    card_ledger_store.finish_vector_job(job["job_id"], True)
                    continue
                ok = knowledge_index.index_card(str(job["knowledge_id"]), payload["title"], payload["body"], payload.get("tags") or [],
                                                  content_revision=payload["content_revision"], rel_path=payload["rel_path"],
                                                  folder_id=payload.get("folder_id"), raise_transient=True,
                                                  activate=not is_pending_edit,
                                                  vector_version=int(job["vector_version"]) if is_pending_edit else None)
                if ok and is_pending_edit:
                    manifest = card_ledger_store.get_vector_manifest(
                        str(job["knowledge_id"]), int(job["vector_version"]),
                    )
                    if not manifest or not card_ledger_store.mark_pending_vector_written(
                        str(job["knowledge_id"]), str(payload["target_revision"]), int(job["vector_version"]),
                        int(manifest["expected_chunk_count"]),
                    ):
                        raise card_ledger_store.ConfirmationConflict("pending vector checkpoint conflict")
                    page = _find(str(job["knowledge_id"]))
                    page_revision = _content_revision(str(page.get("content") or ""))
                    base_revision = str(payload.get("base_revision") or "")
                    target_revision = str(payload.get("target_revision") or "")
                    if page_revision == base_revision:
                        _write_card(page, str(payload["file_content"]))
                    elif page_revision != target_revision:
                        raise card_ledger_store.ConfirmationConflict("confirmed card changed during update")
                    if not card_ledger_store.mark_pending_file_committed(
                        str(job["knowledge_id"]), target_revision, int(job["vector_version"]),
                        hashlib.sha256(str(payload["file_content"]).encode("utf-8")).hexdigest(),
                    ):
                        raise card_ledger_store.ConfirmationConflict("pending file checkpoint conflict")
                    ok = card_ledger_store.activate_pending_update(
                        str(job["knowledge_id"]), target_revision, int(job["vector_version"]),
                    )
                card_ledger_store.finish_vector_job(job["job_id"], ok, "index_failed" if not ok else "")
            except knowledge_index.CardIndexError as exc:
                terminal = card_ledger_store.finish_vector_job(
                    job["job_id"], False, exc.code, transient=exc.transient,
                )
                if terminal == "failed":
                    if bool(payload.get("pending_edit_update")):
                        card_ledger_store.fail_pending_update(str(job["knowledge_id"]), exc.code)
                    else:
                        card_ledger_store.mark_vector_failed(str(job["knowledge_id"]), exc.code)
                else:
                    time.sleep(0.2 * int(job.get("attempts") or 1))
            except Exception as exc:
                if "payload" in locals() and bool(payload.get("pending_edit_update")):
                    card_ledger_store.fail_pending_update(str(job["knowledge_id"]), type(exc).__name__)
                else:
                    card_ledger_store.mark_vector_failed(str(job["knowledge_id"]))
                card_ledger_store.finish_vector_job(job["job_id"], False, f"{type(exc).__name__}: {exc}")
    finally:
        with _REPAIR_LOCK:
            _REPAIR_RUNNING = False
            _REPAIR_IDLE.set()


def recover_interrupted_vector_repairs() -> dict:
    """Requeue only durable user-confirmed outbox work after a healthy restart."""
    result = card_ledger_store.recover_interrupted_vector_jobs()
    if result.get("recovered"):
        _schedule_vector_repairs()
    return result


def audit_active_card_vectors(*, repair: bool = True) -> dict:
    """Reconcile indexed ledger rows with the currently routed vector union."""
    checked = healthy = repaired = corrupted = 0
    states = card_ledger_store.list_indexed_cards()
    verification = knowledge_index.verify_card_vectors([
        (str(state["knowledge_id"]), int(state["indexed_vector_version"]),
         str(state.get("indexed_revision") or ""))
        for state in states
    ])
    for state in states:
        checked += 1
        knowledge_id = str(state["knowledge_id"])
        version = int(state["indexed_vector_version"])
        revision = str(state.get("indexed_revision") or "")
        if verification.get(knowledge_id, False):
            healthy += 1
            continue
        corrupted += 1
        card_ledger_store.mark_vector_manifest_corrupted(knowledge_id, version)
        card_ledger_store.mark_vector_corrupted(knowledge_id)
        if not repair:
            continue
        try:
            page = _find(knowledge_id)
            body = _card_body(page)
            if not _is_substantive_card_body(body):
                continue
            card_ledger_store.retry_index(knowledge_id, revision, {
                "title": str(page.get("title") or "未命名知识卡片"),
                "body": body,
                "tags": _tags(page),
                "content_revision": revision,
                "folder_id": _effective_card_folder_id(page),
            })
            repaired += 1
        except Exception as exc:
            logger.warning("知识卡片索引对账修复入队失败 %s: %s", knowledge_id, type(exc).__name__)
    if repaired:
        _schedule_vector_repairs()
    return {"checked": checked, "healthy": healthy, "corrupted": corrupted, "repaired": repaired}


def recover_vector_repairs() -> dict:
    """Startup/admin recovery: queue active cards missing a current v2 vector."""
    reclaimed = card_ledger_store.reclaim_vector_jobs()
    queued = 0
    for item in _iter_wiki_pages():
        page = wiki_store.read_page(str(item.get("path") or "")) or item
        if not _is_active_mindos_card(page) or not _is_substantive_card_body(_card_body(page)):
            continue
        kid = _knowledge_id(str(page.get("path") or ""))
        state = card_ledger_store.get(kid)
        if state is None or state.get("vector_sync_state") != "clean" or knowledge_index.count_card_chunks(kid) == 0:
            _sync_card_index(page)
            queued += 1
    _schedule_vector_repairs()
    return {"reclaimed": reclaimed, "queued": queued}


def _backfill_vector_index_once() -> None:
    """Compatibility no-op. Versioned cards are repaired only by write/repair jobs."""
    return None


def _query_terms(query: str) -> list[str]:
    """提取本地卡片检索的有效词，兼容英文专有名词与中文问句。"""
    terms: list[str] = []
    seen: set[str] = set()
    for token in _QUERY_TOKEN_RE.findall(query.casefold()):
        # 中文连续片段可能包含问句尾缀，如「项目预算是多少」。去掉尾缀后仍保留
        # 「项目预算」作为可匹配词；纯问句（「是什么」）则自然被过滤。
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            token = _QUESTION_SUFFIX_RE.sub("", token)
        token = token.strip("._-")
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _lexical_card_score(title: str, body: str, terms: list[str]) -> float | None:
    """基于有效词的稳定本地兜底分；任一有效词命中才返回候选。"""
    if not terms:
        return 0.8
    title_folded = title.casefold()
    body_folded = body.casefold()
    # ASCII 名称使用词边界，避免 MindOS 被 MindOS-P14 这类版本/文件名子串
    # 伪命中，进而挤掉真正解释产品定义的知识卡片。
    def contains(text: str, term: str) -> bool:
        if term.isascii() and any(char.isalnum() for char in term):
            return re.search(
                rf"(?<![a-z0-9_.-]){re.escape(term)}(?![a-z0-9_.-])", text,
            ) is not None
        return term in text

    title_hits = sum(contains(title_folded, term) for term in terms)
    body_hits = sum(contains(body_folded, term) for term in terms)
    if not title_hits and not body_hits:
        return None
    coverage = (title_hits + body_hits) / (2 * len(terms))
    # 标题命中略强于正文命中；分数仅用于同一证据桶内排序，不跨桶改变优先级。
    return (0.9 if title_hits else 0.7) + 0.1 * coverage


def _subtree_folder_ids(folder_id: int) -> set[int]:
    """选定 KNOWLEDGE 目录及其全部后代的 ID 集合；目录服务不可用时退化单节点。"""
    try:
        return ingestion.JobStore.instance().folder_descendants(folder_id)
    except Exception:
        return {folder_id}


def knowledge_list(q: str = "", tag: str = "", limit: int = 100, folderId: int | None = None, recycled: bool = False, device_scope: str = "global"):
    """知识卡片列表。

    recycled=true 仅返回已回收卡片（P15-05 回收站入口）；默认隐藏已回收卡片。
    folderId 提供时仅返回归入该 KNOWLEDGE 目录（含全部后代子树）的卡片；
    未归类/悬空的旧卡片视作归入「Resources」知识根目录。
    按规范化路径去重：DB 中可能残留反斜杠/正斜杠两种 path 记录（旧数据），
    同一文件只保留一条，避免列表出现重复卡片。
    阶段 2：device_scope 非 world 时仅返回当前设备/账号作用域内已采购的卡片，
    跨设备卡片不回显。
    """
    def in_scope(knowledge_id: str) -> bool:
        if device_scope == "global":
            return True
        return bool(card_ledger_store.get(knowledge_id, device_scope=device_scope))

    items = wiki_store.list_pages(query=q, limit=500).get("items", [])
    details = [wiki_store.read_page(str(item["path"])) or item for item in items]
    details = [
        item for item in details
        if _is_mindos_card(item) and in_scope(_knowledge_id(str(item.get("path") or ""))) and (
            _is_recycled(item) if recycled else not _is_recycled(item)
        ) and (
            not _is_archived(item)
        ) and (
            # 草稿只在原材料详情中编辑；知识成品列表仅展示已确认卡片。索引
            # 排队/失败仍是已确认卡片的内部任务状态，故不要求 ragEligible。
            recycled or (card_ledger_store.get(_knowledge_id(str(item.get("path") or ""))) or {}).get("approval_state") == "confirmed"
        )
    ]
    # 已确认不等同于 RAG 准入；检索/问答仍由 _is_rag_eligible_page 严格限制
    # 为当前已确认且已索引的版本。
    tag_lower = tag.strip().lower() if tag else ""
    if tag_lower:
        details = [item for item in details if tag_lower in [t.lower() for t in _tags(item)]]
    # folderId 子树筛选：卡片的有效目录（缺省/悬空归 Resources 根）须落在子树内
    subtree: set[int] | None = None
    if folderId is not None:
        subtree = _subtree_folder_ids(folderId)
    # 去重：同一规范化路径只保留一条
    seen: set[str] = set()
    unique: list[dict] = []
    for item in details:
        key = str(item.get("path") or "").replace("\\", "/").lstrip("./")
        if key in seen:
            continue
        seen.add(key)
        if subtree is not None:
            node = _effective_folder_node(item)
            if node is None or node["id"] not in subtree:
                continue
        unique.append(item)
    return {"items": [_public(item) for item in unique[:limit]], "total": len(unique)}


def search_cards(query: str, limit: int = 20, for_qa: bool = False, device_scope: str = "global") -> list[dict]:
    """检索 MindOS 卡片。

    普通搜索保留标题命中的导航能力；问答只接受正文有效、正文相关的卡片，
    并优先使用独立知识向量索引，完全不依赖可选 GBrain CLI。
    阶段 2：只检索当前设备/账号作用域内的卡片（账本 device_scope 过滤，
    跨设备卡片不回显）。
    """
    terms = _query_terms(query)
    candidates: dict[str, tuple[float, dict]] = {}

    def in_scope(knowledge_id: str) -> bool:
        if device_scope == "global":
            return True
        return bool(card_ledger_store.get(knowledge_id, device_scope=device_scope))

    def norm_path(p: str) -> str:
        return str(p or "").replace("\\", "/").lstrip("./")

    for page in wiki_store.list_pages(limit=500).get("items", []):
        detail = wiki_store.read_page(str(page["path"])) or page
        if not _is_active_mindos_card(detail) or not _is_rag_eligible_page(detail, device_scope=device_scope):
            continue
        if not in_scope(_knowledge_id(str(detail.get("path") or ""))):
            continue
        body = _card_body(detail)
        if for_qa and not _is_substantive_card_body(body):
            continue
        # 问答不能因为「标题碰巧命中」就把无关正文放进证据；普通统一搜索仍可
        # 把这类结果作为导航建议展示。
        score = _lexical_card_score(
            "" if for_qa else str(detail.get("title") or ""), body, terms
        )
        if score is None:
            continue
        candidates[norm_path(str(page["path"]))] = (score, detail)

    # 独立向量库的结果优先合入。仍需回读 Wiki 页面校验活跃状态，避免归档卡片
    # 因异常中断的旧向量写入而“复活”。
    for hit in knowledge_index.search_cards(query, limit=limit, device_scope=device_scope):
        path = ""
        try:
            detail = _find(str(hit.get("knowledgeId") or ""))
            path = norm_path(str(detail.get("path") or ""))
        except HTTPException:
            continue
        body = _card_body(detail)
        if not in_scope(_knowledge_id(str(detail.get("path") or ""))):
            continue
        if (not _is_active_mindos_card(detail) or not _is_rag_eligible_page(detail, device_scope=device_scope)
                or not _is_substantive_card_body(body)):
            continue
        score = float(hit.get("score") or 0.0)
        if path not in candidates or score > candidates[path][0]:
            candidates[path] = (score, detail)
    try:
        for hit in wiki_store.search_wiki(query, n_results=limit):
            path = norm_path(str(hit.get("page_path") or ""))
            detail = wiki_store.read_page(path) if path else None
            if not detail:
                continue
            # 阶段 2：Wiki fallback 命中同样复核设备作用域，跨设备/账号卡片不得回显。
            if not in_scope(_knowledge_id(str(detail.get("path") or ""))):
                continue
            if (_is_active_mindos_card(detail) and _is_rag_eligible_page(detail, device_scope=device_scope)
                    and (not for_qa or _is_substantive_card_body(_card_body(detail)))):
                score = float(hit.get("score") or 0.0)
                if path not in candidates or score > candidates[path][0]:
                    candidates[path] = (score, detail)
    except Exception:
        # Keyword matching above remains available if an optional Wiki vector engine is unavailable.
        pass
    rows = []
    for score, page in candidates.values():
        public = _public(page)
        body = _card_body(page)
        if for_qa and not _is_substantive_card_body(body):
            continue
        rows.append({
            "knowledgeId": public["knowledgeId"],
            "title": public["title"],
            "snippet": body[:400],
            "score": score,
        })
    return sorted(rows, key=lambda item: item["score"], reverse=True)[:limit]


def search_cards_by_ids(ids, query: str, for_qa: bool = False, device_scope: str = "global") -> list[dict]:
    """按知识卡片 ID 精确检索（AG-02 sourceIds 范围限定用）。

    sourceIds 是服务端已授权的检索范围，不经过 top-k 截断。对每个指定 ID 做
    受范围约束的混合评分：

    - 词面评分（标题/正文命中）为基础；
    - 合入知识卡片正文向量命中——语义相关但无相同关键词的指定卡片也能被召回。

    返回 [{knowledgeId, title, snippet, score}]，顺序不保证（调用方自行按 score
    排序）。向量命中同样复核 active / 正文有效性，避免残留归档或空白卡片。

    阶段 2：卡片不属于当前设备作用域时视为不存在，跨设备/账号卡片不召回。
    """
    terms = _query_terms(query)
    target = {str(knowledge_id) for knowledge_id in ids}
    best: dict[str, dict] = {}

    def in_scope(knowledge_id: str) -> bool:
        if device_scope == "global":
            return True
        return bool(card_ledger_store.get(knowledge_id, device_scope=device_scope))

    def consider(card: dict) -> None:
        card_id = str(card.get("knowledgeId") or "")
        if card_id not in target:
            return
        score = float(card.get("score") or 0.0)
        if card_id not in best or score > best[card_id]["score"]:
            best[card_id] = card

    # 词面评分：按指定 ID 精确读取并复用 active / 正文有效性判断。
    for knowledge_id in ids:
        try:
            page = _find(str(knowledge_id))
        except HTTPException:
            continue
        if not in_scope(_knowledge_id(str(page.get("path") or ""))):
            continue
        if not _is_active_mindos_card(page) or not _is_rag_eligible_page(page, device_scope=device_scope):
            continue
        body = _card_body(page)
        if for_qa and not _is_substantive_card_body(body):
            continue
        score = _lexical_card_score(
            "" if for_qa else str(page.get("title") or ""), body, terms
        )
        if score is None:
            continue
        consider({
            "knowledgeId": _knowledge_id(str(page["path"])),
            "title": str(page.get("title") or "未命名知识卡片"),
            "snippet": body[:400],
            "score": score,
        })

    # 向量命中（仅目标卡片）：语义相关但无相同关键词的卡片由此召回。
    try:
        for hit in knowledge_index.search_cards(
            query, limit=max(len(ids) * 5, 20), device_scope=device_scope,
        ):
            card_id = str(hit.get("knowledgeId") or "")
            if card_id not in target:
                continue
            try:
                page = _find(card_id)
            except HTTPException:
                continue
            if not in_scope(_knowledge_id(str(page.get("path") or ""))):
                continue
            if not _is_active_mindos_card(page) or not _is_rag_eligible_page(page, device_scope=device_scope):
                continue
            body = _card_body(page)
            if for_qa and not _is_substantive_card_body(body):
                continue
            hit = dict(hit)
            hit["snippet"] = body[:400]
            consider(hit)
    except Exception:
        # 向量索引不可用时保持词面结果（不阻断范围限定检索）
        pass

    return [best[card_id] for card_id in best]


def knowledge_detail(knowledge_id: str):
    return _public(_find(knowledge_id))


def knowledge_view(knowledge_id: str) -> dict | None:
    """返回 active MindOS 卡片的公开详情视图（AG-02-04 Agent 详情投影用）。

    返回 `_public(page)` 之外额外附加：
    - body：清理后的卡片正文（不含 frontmatter/重复标题）；
    - evidenceEligible：正文是否达到可作证据的长度；
    - revision：稳定内容版本标识（由正文派生，AG-06 并发更新/审批提交预留）；
    - indexStatus：真实索引状态（ready / not_indexed / empty），不因详情读取成功
      就默认 ready。

    归档、合并、回收或不存在的卡片统一返回 None。active/正文有效性判断只在本
    模块维护，Agent 层复用本函数而不复制 `_is_active_mindos_card` /
    `_is_substantive_card_body` 的规则。
    """
    try:
        page = _find(knowledge_id)
    except HTTPException:
        return None
    if not _is_active_mindos_card(page) or not _is_rag_eligible_page(page):
        return None
    body = _card_body(page)
    # revision：稳定内容版本标识（同一正文恒相同，正文变化即变化），可用于
    # 检测并发更新冲突；不做成可预测的递增整数，避免伪装存在版本计数器。
    content = str(page.get("content") or "")
    revision = _content_revision(content)
    if body:
        index_status = "ready" if knowledge_index.count_card_chunks(knowledge_id) > 0 else "not_indexed"
    else:
        index_status = "empty"
    return {
        **_public(page),
        "body": body,
        "evidenceEligible": _is_substantive_card_body(body),
        "revision": revision,
        "indexStatus": index_status,
    }


def evidence_body(knowledge_id: str) -> str | None:
    """返回可作为证据的卡片正文（AG-02-03 证据展开用）。

    只返回 active MindOS 卡片且正文有效（≥16 字实质内容）的正文；归档、合并、
    回收或无实质正文的卡片统一返回 None。active/正文有效性的判断条件只在本
    模块维护，Agent 层复用本函数而不复制 `_is_active_mindos_card` /
    `_is_substantive_card_body` 的规则。
    """
    try:
        page = _find(knowledge_id)
    except HTTPException:
        return None
    if not _is_active_mindos_card(page) or not _is_rag_eligible_page(page):
        return None
    body = _card_body(page)
    if not _is_substantive_card_body(body):
        return None
    return body


# ---- P15-01：知识卡片来源管理（独立接口，避免干扰普通卡片更新） ----

_ALLOWED_SOURCE_TYPES = ("material", "knowledge")


def _updated_at(page: dict) -> str:
    """公开用途的 updated_at（兼容 int 时间戳与 ISO 字符串）。"""
    updated = page.get("updated_at") or page.get("updatedAt")
    if isinstance(updated, (int, float)):
        updated = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat()
    return str(updated or "")


def _source_exists(ref: dict, allow_existing_unavailable: bool = False) -> None:
    """校验单个来源存在；默认拒绝新增不可用来源。

    回收资料和归档知识卡片不接受作为新增来源；历史不可用来源仅在显式兼容读取时允许。
    """
    st, sid = ref["sourceType"], ref["id"]
    if st == "material":
        record = ingestion.status_of(sid)
        if record is None:
            raise HTTPException(404, f"来源资料不存在：{sid}")
        if sid in ingestion.recycled_material_ids() and not allow_existing_unavailable:
            raise HTTPException(400, f"来源资料已回收：{sid}，请先恢复后再关联")
        if record.get("status") != ingestion.ST_AVAILABLE and not allow_existing_unavailable:
            raise HTTPException(400, f"来源资料尚不可用：{sid}")
        return
    try:
        card = _find(sid)
    except HTTPException:
        raise HTTPException(404, f"来源知识卡片不存在：{sid}")
    if _is_archived(card) and not allow_existing_unavailable:
        raise HTTPException(400, f"来源知识卡片已归档：{sid}，请先恢复后再关联")
    if _is_recycled(card) and not allow_existing_unavailable:
        raise HTTPException(400, f"来源知识卡片已回收：{sid}，请先恢复后再关联")


def _transitive_knowledge_closure(start_id: str, seen: set[str] | None = None) -> set[str]:
    """从知识卡片 start_id 出发，经 sourceRefs 可到达的全部知识卡片 ID 闭包（含自身）。

    材料没有出边，不在闭包中展开；用于在新增来源前检测循环引用。
    """
    seen = set() if seen is None else seen
    if start_id in seen:
        return seen
    seen.add(start_id)
    try:
        card = _find(start_id)
    except HTTPException:
        return seen
    for ref in _source_refs(card):
        if ref["sourceType"] == "knowledge":
            _transitive_knowledge_closure(ref["id"], seen)
    return seen


def _validate_sources(
    knowledge_id: str,
    raw: list[KnowledgeSourceRef],
    existing_refs: list[dict] | None = None,
) -> list[dict]:
    """校验并把去重保序后的来源列表准备成 frontmatter 结构。

    - sourceType 白名单（400）；
    - 禁止引用自身（400）；
    - 每个来源存在且未归档（400/404）；
    - 禁止循环引用（409）：目标卡片不得出现在任一知识来源的传递闭包中；
    - 按 (sourceType, id) 去重，保留用户提交的首次顺序；
    - 普通手工卡片允许空来源（产品未强制「基于资料创建」必须保留来源）。
    """
    refs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        st = (item.sourceType or "").strip()
        sid = (item.id or "").strip()
        if st not in _ALLOWED_SOURCE_TYPES:
            raise HTTPException(400, f"sourceType 仅支持 material / knowledge：{st!r}")
        if not sid:
            raise HTTPException(400, "来源 id 不能为空")
        if st == "knowledge" and sid == knowledge_id:
            raise HTTPException(400, "知识卡片不能引用自身")
        key = (st, sid)
        if key in seen:
            continue  # 去重，保留首次顺序
        seen.add(key)
        refs.append({"sourceType": st, "id": sid})
    existing_keys = {
        (ref["sourceType"], ref["id"])
        for ref in (existing_refs or [])
    }
    for ref in refs:
        # 已归档来源不能新增，但历史卡片可保留其已有引用，以便用户移除或
        # 在 P15-03 中将它替换为新版本。
        _source_exists(ref, (ref["sourceType"], ref["id"]) in existing_keys)
    for ref in refs:
        if ref["sourceType"] != "knowledge":
            continue
        closure = _transitive_knowledge_closure(ref["id"])
        if knowledge_id in closure:
            raise HTTPException(
                409, "禁止形成卡片循环引用：该来源已直接或间接引用本卡片"
            )
    return refs


def knowledge_sources(knowledge_id: str):
    """GET：返回卡片当前的完整来源列表（含标题、归档状态），不做任何校验写入。"""
    page = _find(knowledge_id)
    return {
        "knowledgeId": knowledge_id,
        "sourceRefs": _sources(page),
        "updatedAt": _updated_at(page),
    }


def knowledge_update_sources(knowledge_id: str, req: KnowledgeSourcesUpdate):
    """PUT：整表替换卡片来源；成功后同步维护 refs / material_ids 兼容字段 / updated_at。

    正文、标签、目录不受影响；仅改写 frontmatter 的来源键。
    """
    page = _find(knowledge_id)
    _strict_meta(str(page.get("content") or ""))
    _check_revision(page, req.expectedRevision)
    _require_draft_for_mutation(knowledge_id)
    if _is_merged(page):
        raise HTTPException(400, "已合并的卡片不能修改来源")
    if _is_archived(page):
        raise HTTPException(400, "已归档的卡片不能修改来源，请先恢复后再编辑")
    if _is_recycled(page):
        raise HTTPException(400, "已回收的卡片不能修改来源，请先恢复后再编辑")
    refs = _validate_sources(knowledge_id, req.sourceRefs, _source_refs(page))
    material_ids = [r["id"] for r in refs if r["sourceType"] == "material"]
    now = datetime.now(timezone.utc).isoformat()
    updated = _write_meta(
        page,
        {
            "mindos_source_refs": refs,
            "mindos_source_material_ids": material_ids,
            "updated_at": now,
        },
    )
    card_ledger_store.touch_metadata(knowledge_id, _content_revision(str(updated.get("content") or "")))
    return {
        "knowledgeId": knowledge_id,
        "sourceRefs": _sources(updated),
        "updatedAt": now,
        "revision": _content_revision(str(updated.get("content") or "")),
    }


def knowledge_update_sources_for_lifecycle(knowledge_id: str, source_refs: list[dict]) -> dict:
    """生命周期内部更新来源。

    受控回收/永久清除必须消除已归档或已回收卡片中的失效来源；普通编辑接口仍
    严格禁止编辑这些卡片。本函数不暴露为 HTTP 路由，仅由 lifecycle 在确认删除
    后使用，并复用相同的存在性、循环与自引用校验。
    """
    page = _find(knowledge_id)
    old_refs = _source_refs(page)
    refs = _validate_sources(
        knowledge_id,
        [KnowledgeSourceRef(**ref) for ref in source_refs],
        old_refs,
    )
    now = datetime.now(timezone.utc).isoformat()
    updated = _write_meta(page, {
        "mindos_source_refs": refs,
        "mindos_source_material_ids": [r["id"] for r in refs if r["sourceType"] == "material"],
        "updated_at": now,
    })
    return {"knowledgeId": knowledge_id, "sourceRefs": _sources(updated), "updatedAt": now,
            "revision": _content_revision(str(updated.get("content") or ""))}


def _register_created_draft(page: dict, device_scope: str) -> None:
    """Only creation writes establish ownership; reads never infer old-card scope."""
    path = str(page["path"])
    knowledge_id = _knowledge_id(path)
    revision = _content_revision(str(page.get("content") or ""))
    card_ledger_store.ensure(knowledge_id, path, revision, device_scope=device_scope)
    card_ledger_store.update_draft_revision(knowledge_id, path, revision)


def knowledge_create(req: KnowledgeCreate, request: Request = None):
    from .device_context import scope_for_device

    context = getattr(getattr(request, "state", None), "mindos_device_context", None)
    scope = scope_for_device(getattr(context, "device_id", None))
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    # P14-07：目标 KNOWLEDGE 目录；缺省进入「Resources」知识根目录。
    target = _resolve_target_folder(req.folderId)
    try:
        now = datetime.now(timezone.utc).isoformat()
        tags = [t.strip()[:64] for t in req.tags if t.strip()]
        lines = [
            f"title: {json.dumps(title, ensure_ascii=False)}",
            "type: note",
            f"tags: {json.dumps(tags, ensure_ascii=False)}",
            "maturity: seedling",
            "mindos_card: true",
        ]
        if target is not None:
            lines.append(f"mindos_folder_id: {target['id']}")
        lines += [
            f"created_at: {json.dumps(now)}",
            f"updated_at: {json.dumps(now)}",
        ]
        body = "---\n" + "\n".join(lines) + f"\n---\n# {title}\n\n{req.content.rstrip()}\n"
        page = wiki_store.create_page(title, folder="Resources", page_type="note")
        page = wiki_store.write_page(str(page["path"]), body, source_agent="mindos")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _register_created_draft(page, scope)
    _sync_card_index(page)
    return {"item": _public(page)}


def knowledge_create_from_material(
    material_id: str, req: KnowledgeFromMaterialCreate | None = None, *, device_scope: str = "global",
):
    """创建引用原材料的可编辑卡片，可选以已生成摘要预填「待编辑草稿」。"""
    material = ingestion.detail_of(material_id, device_scope=device_scope)
    if material is None:
        raise HTTPException(404, "资料不存在")
    req = req or KnowledgeFromMaterialCreate()
    # P14-07：从资料生成的卡片默认归入「Resources」知识根目录。
    target = _ensure_resources_root()
    title = f"{material['fileName']} 的知识卡片"
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "type: note",
        f"tags: {json.dumps(material.get('tags', []), ensure_ascii=False)}",
        "maturity: seedling",
        "mindos_card: true",
    ]
    if target is not None:
        lines.append(f"mindos_folder_id: {target['id']}")
    lines += [
        f"created_at: {json.dumps(now)}",
        f"updated_at: {json.dumps(now)}",
        f"mindos_source_material_ids: {json.dumps([material_id], ensure_ascii=False)}",
        f"mindos_source_refs: {json.dumps([{'sourceType': 'material', 'id': material_id}], ensure_ascii=False)}",
    ]
    draft = ""
    if req.prefillFromSummary:
        summary = ingestion.summary_text_of(material).strip()
        if summary:
            # 摘要本身就是待编辑的卡片正文；不写入模板提示文字，以免它成为
            # 用户内容或影响后续检索词权重。
            draft = f"{summary}\n"
        else:
            # 不以正文摘录冒充 AI 摘要：摘要尚未就绪时仍创建空白引用模板，由调用方
            # 通过 prefilled=false 明确提示用户。
            draft = ""
    body = "---\n" + "\n".join(lines) + f"\n---\n# {title}\n\n{draft}"
    try:
        # First allocate a collision-safe Wiki path, then replace only that card's content.
        page = wiki_store.create_page(title, folder="Resources", page_type="note")
        page = wiki_store.write_page(str(page["path"]), body, source_agent="mindos")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _register_created_draft(page, device_scope)
    _sync_card_index(page)
    return {"item": _public(page), "prefilled": bool(draft)}


def create_card_with_sources(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_refs: list[dict] | None = None,
    folder_id: int | None = None,
    confirmation_session_id: str | None = None,
    *,
    device_scope: str = "global",
) -> dict:
    """创建正式知识卡片并写入带类型来源引用（P14-10 草稿「另存为知识卡片」复用）。

    与 knowledge_create_from_material 同一写入链路：归入 KNOWLEDGE 目录（缺省
    Resources 根），来源引用写 frontmatter：
    - mindos_source_refs: [{sourceType: material|knowledge, id}] 承载混合来源；
    - mindos_source_material_ids 仅写 material 来源（兼容旧读取逻辑）。
    返回 _public 公开结构；仅由用户显式调用触发。
    """
    title = title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    target = _resolve_target_folder(folder_id)
    now = datetime.now(timezone.utc).isoformat()
    tags = [t.strip()[:64] for t in (tags or []) if t.strip()]
    lines = [
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "type: note",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        "maturity: seedling",
        "mindos_card: true",
    ]
    if target is not None:
        lines.append(f"mindos_folder_id: {target['id']}")
    source_refs = [
        {"sourceType": str(r.get("sourceType")), "id": str(r.get("id"))}
        for r in (source_refs or [])
        if r.get("sourceType") in ("material", "knowledge") and r.get("id")
    ]
    if source_refs:
        lines.append(f"mindos_source_refs: {json.dumps(source_refs, ensure_ascii=False)}")
        material_ids = [r["id"] for r in source_refs if r["sourceType"] == "material"]
        if material_ids:
            lines.append(f"mindos_source_material_ids: {json.dumps(material_ids, ensure_ascii=False)}")
    if confirmation_session_id:
        lines.append(f"mindos_confirmation_session: {json.dumps(confirmation_session_id, ensure_ascii=False)}")
    lines += [
        f"created_at: {json.dumps(now)}",
        f"updated_at: {json.dumps(now)}",
    ]
    body = "---\n" + "\n".join(lines) + f"\n---\n# {title}\n\n{content.rstrip()}\n"
    try:
        # First allocate a collision-safe Wiki path, then replace only that card's content.
        page = wiki_store.create_page(title, folder="Resources", page_type="note")
        page = wiki_store.write_page(str(page["path"]), body, source_agent="mindos")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _register_created_draft(page, device_scope)
    _sync_card_index(page)
    return _public(page)


def knowledge_update(knowledge_id: str, req: KnowledgeUpdate):
    page = _find(knowledge_id)
    _strict_meta(str(page.get("content") or ""))
    _check_revision(page, req.expectedRevision)
    state = card_ledger_store.get(knowledge_id)
    if state and state.get("approval_state") == "confirmed":
        raise HTTPException(409, "已确认卡片请先创建修改草稿，再修改正文")
    if _is_merged(page):
        raise HTTPException(400, "已合并的卡片不能编辑")
    if _is_archived(page):
        raise HTTPException(400, "已归档的卡片不能编辑，请先恢复后再编辑")
    if _is_recycled(page):
        raise HTTPException(400, "已回收的卡片不能编辑，请先恢复后再编辑")
    title = req.title.strip() or page.get("title") or "未命名知识卡片"
    content = req.content
    # Keep the existing path stable while updating the page title in frontmatter.
    now = datetime.now(timezone.utc).isoformat()
    tags = [t.strip()[:64] for t in req.tags] if req.tags is not None else _tags(page)
    # P14-07：目录归属——未传 folderId 时保留当前有效目录；传入时校验为 KNOWLEDGE 目录。
    target = _effective_folder_node(page) if req.folderId is None else _resolve_target_folder(req.folderId)
    raw = str(page.get("content") or "")
    meta, _ = _strict_meta(raw)
    meta.update({
        "title": title,
        "type": "note",
        "tags": tags,
        "maturity": "seedling",
        "mindos_card": True,
        "mindos_source_material_ids": _source_ids(page),
        "mindos_source_refs": _source_refs(page),
        "created_at": meta.get("created_at") or now,
        "updated_at": now,
    })
    if target is not None:
        meta["mindos_folder_id"] = target["id"]
    new_content = _render_frontmatter(meta) + f"\n# {title}\n\n{content.rstrip()}\n"
    try:
        updated = _write_card(page, new_content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _sync_card_index(updated)
    card_ledger_store.update_draft_revision(
        knowledge_id, str(updated.get("path") or ""), _content_revision(str(updated.get("content") or ""))
    )
    return {"item": _public(updated)}


def knowledge_edit_as_draft(knowledge_id: str, req: KnowledgeRevisionRequest):
    page = _find(knowledge_id)
    _check_revision(page, req.expectedRevision)
    try:
        state = card_ledger_store.edit_as_draft(knowledge_id, req.expectedRevision)
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    return {"item": _public(page), "approvalState": state["approval_state"], "indexState": state["index_state"]}


def _edit_draft_payload(page: dict, *, title: str | None = None, content: str | None = None,
                        tags: list[str] | None = None, folder_id: int | None = None,
                        source_refs: list[dict] | None = None) -> dict:
    """Normalize the mutable part of a confirmed-card working copy."""
    return {
        "title": (title if title is not None else str(page.get("title") or "未命名知识卡片")).strip() or "未命名知识卡片",
        "content": content if content is not None else _card_body(page),
        "tags": [str(tag).strip()[:64] for tag in (tags if tags is not None else _tags(page)) if str(tag).strip()],
        "folderId": folder_id if folder_id is not None else _effective_card_folder_id(page),
        "sourceRefs": source_refs if source_refs is not None else [
            {"sourceType": ref["sourceType"], "id": ref["id"]} for ref in _source_refs(page)
        ],
    }


def _edit_draft_response(knowledge_id: str, row: dict) -> dict:
    payload = json.loads(row["payload_json"])
    return {"knowledgeId": knowledge_id, "baseRevision": row["base_revision"],
            "draftRevision": row["draft_revision"], "updatedAt": row["updated_at"], **payload}


def _render_edit_draft_content(page: dict, payload: dict) -> str:
    """Render the future confirmed file without modifying the current version."""
    meta, _ = _strict_meta(str(page.get("content") or ""))
    now = datetime.now(timezone.utc).isoformat()
    refs = []
    for ref in payload.get("sourceRefs") or []:
        source_type, source_id = str(ref.get("sourceType") or ""), str(ref.get("id") or "")
        if source_type in ("material", "knowledge") and source_id:
            refs.append({"sourceType": source_type, "id": source_id})
    meta.update({"title": payload["title"], "type": "note", "tags": payload.get("tags") or [],
                 "maturity": "seedling", "mindos_card": True, "mindos_source_refs": refs,
                 "mindos_source_material_ids": [r["id"] for r in refs if r["sourceType"] == "material"],
                 "updated_at": now})
    target = _resolve_target_folder(payload.get("folderId")) if payload.get("folderId") is not None else None
    if target is not None:
        meta["mindos_folder_id"] = target["id"]
    return _render_frontmatter(meta) + f"\n# {payload['title']}\n\n{str(payload.get('content') or '').rstrip()}\n"


def knowledge_begin_edit_draft(knowledge_id: str, req: KnowledgeRevisionRequest):
    page = _find(knowledge_id)
    _check_revision(page, req.expectedRevision)
    try:
        row = card_ledger_store.begin_edit_draft(
            knowledge_id, str(req.expectedRevision or _content_revision(str(page.get("content") or ""))), _edit_draft_payload(page),
        )
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    return _edit_draft_response(knowledge_id, row)


def knowledge_get_edit_draft(knowledge_id: str):
    _find(knowledge_id)
    row = card_ledger_store.get_edit_draft(knowledge_id)
    if row is None:
        raise HTTPException(404, "编辑草稿不存在")
    return _edit_draft_response(knowledge_id, row)


def knowledge_save_edit_draft(knowledge_id: str, req: KnowledgeEditDraftSave):
    page = _find(knowledge_id)
    if _is_recycled(page) or _is_merged(page):
        raise HTTPException(409, "当前卡片不可编辑")
    try:
        requested_refs = [KnowledgeSourceRef(**item) for item in req.sourceRefs]
    except Exception as exc:
        raise HTTPException(400, "来源格式无效") from exc
    refs = _validate_sources(knowledge_id, requested_refs, _source_refs(page))
    payload = _edit_draft_payload(page, title=req.title, content=req.content, tags=req.tags,
                                  folder_id=req.folderId, source_refs=refs)
    try:
        row = card_ledger_store.save_edit_draft(knowledge_id, req.expectedDraftRevision, payload)
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    return _edit_draft_response(knowledge_id, row)


def knowledge_confirm_edit_draft(
    knowledge_id: str, req: KnowledgeEditDraftConfirm,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    if not idempotency_key:
        raise HTTPException(400, "缺少 Idempotency-Key")
    page = _find(knowledge_id)
    draft = card_ledger_store.get_edit_draft(knowledge_id)
    if draft is None:
        raise HTTPException(404, "编辑草稿不存在")
    payload = json.loads(draft["payload_json"])
    if not _is_substantive_card_body(str(payload.get("content") or "")):
        raise HTTPException(409, "卡片正文不可确认")
    file_content = _render_edit_draft_content(page, payload)
    target_revision = _content_revision(file_content)
    job_payload = {**payload, "content_revision": target_revision,
                   "rel_path": str(page.get("path") or ""), "base_revision": draft["base_revision"],
                   "file_content": file_content, "folder_id": payload.get("folderId")}
    try:
        result = card_ledger_store.begin_pending_update(
            knowledge_id, req.expectedDraftRevision, target_revision, job_payload,
        )
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    _schedule_vector_repairs()
    return {"knowledgeId": knowledge_id, "vectorJobId": (result.get("job") or {}).get("job_id"),
            "idempotent": bool(result.get("idempotent")), "indexState": "updating"}


def knowledge_retry_edit_draft(knowledge_id: str):
    _find(knowledge_id)
    try:
        job = card_ledger_store.retry_pending_update(knowledge_id)
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    _schedule_vector_repairs()
    return {"knowledgeId": knowledge_id, "vectorJobId": job.get("job_id"), "indexState": "updating"}


def knowledge_retry_index(knowledge_id: str, req: KnowledgeRevisionRequest):
    page = _find(knowledge_id)
    _check_revision(page, req.expectedRevision)
    body = _card_body(page)
    if not _is_substantive_card_body(body):
        raise HTTPException(409, "卡片正文不可索引")
    try:
        job = card_ledger_store.retry_index(knowledge_id, req.expectedRevision, {
            "title": str(page.get("title") or "未命名知识卡片"), "body": body,
            "tags": _tags(page), "content_revision": req.expectedRevision,
            "folder_id": _effective_card_folder_id(page),
        })
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    _schedule_vector_repairs()
    return {"knowledgeId": knowledge_id, "vectorJobId": job.get("job_id"), "indexState": "indexing"}


def knowledge_confirm(
    knowledge_id: str,
    req: KnowledgeRevisionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    """Confirm a user-authored draft and atomically enqueue its card index job."""
    if not idempotency_key:
        raise HTTPException(400, "缺少 Idempotency-Key")
    page = _find(knowledge_id)
    _strict_meta(str(page.get("content") or ""))
    _check_revision(page, req.expectedRevision)
    if not _is_active_mindos_card(page):
        raise HTTPException(409, "非活跃卡片不可确认")
    body = _card_body(page)
    if not _is_substantive_card_body(body):
        raise HTTPException(409, "卡片正文不可确认")
    revision = _content_revision(str(page.get("content") or ""))
    payload = {
        "title": str(page.get("title") or "未命名知识卡片"), "body": body,
        "tags": _tags(page), "content_revision": revision,
        "rel_path": str(page.get("path") or ""), "folder_id": _effective_card_folder_id(page),
    }
    try:
        session = card_ledger_store.begin_card_confirmation(
            knowledge_id, revision, idempotency_key, payload,
        )
        if session["state"] == "preparing":
            # The current Wiki draft was revision-CAS checked above. Persisting
            # this transition makes `confirming` observable and recoverable.
            card_ledger_store.mark_confirmation_file_committed(
                session["session_id"], knowledge_id, payload,
            )
        result = card_ledger_store.finalize_material_confirmation(
            session["session_id"], knowledge_id, str(page.get("path") or ""), revision, payload,
        )
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    _schedule_vector_repairs()
    return {
        "knowledgeId": knowledge_id,
        "vectorJobId": (result.get("job") or {}).get("job_id"),
        "idempotent": bool(result.get("idempotent")),
        "approvalState": "confirmed",
        "indexState": "indexing",
    }


def knowledge_move(knowledge_id: str, req: KnowledgeMoveRequest):
    """移动知识卡片到 KNOWLEDGE 目录（folderId=null 时移回知识根目录）。

    仅改写 frontmatter 的 mindos_folder_id，不改变卡片 ID、路径、链接或来源关系。
    """
    page = _find(knowledge_id)
    _strict_meta(str(page.get("content") or ""))
    _check_revision(page, req.expectedRevision)
    if _is_merged(page):
        raise HTTPException(400, "已合并的卡片不能移动")
    if _is_archived(page):
        raise HTTPException(400, "已归档的卡片不能移动，请先恢复后再移动")
    if _is_recycled(page):
        raise HTTPException(400, "已回收的卡片不能移动，请先恢复后再移动")
    target = _resolve_target_folder(req.folderId)
    state = card_ledger_store.get(knowledge_id)
    try:
        if state and state.get("approval_state") == "confirmed":
            card_ledger_store.update_folder_metadata(
                knowledge_id, int(target["id"]), str(state.get("current_revision") or ""),
            )
            updated = page
        else:
            updated = _write_meta(page, {"mindos_folder_id": target["id"]})
            card_ledger_store.touch_metadata(knowledge_id)
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    return {"item": _public(updated)}


def collect_cards_in_folder(folder_id: int) -> list[dict]:
    """预读直接归入指定 KNOWLEDGE 目录的卡片原始内容（含 frontmatter）。

    删除目录的卡片迁移前置步骤：保存原始 content 供失败时补偿回滚。
    子目录中卡片的目录 ID 不会因本目录删除而失效（子目录整体提升保留），无需迁移。
    """
    records: list[dict] = []
    for page in wiki_store.list_pages(limit=500).get("items", []):
        detail = wiki_store.read_page(str(page["path"])) or page
        if not _is_mindos_card(detail):
            continue
        if _effective_card_folder_id(detail) != folder_id:
            continue
        records.append({"path": str(detail["path"]), "content": str(detail.get("content") or "")})
    return records


def write_card_folder(path: str, content: str, target_id: int) -> None:
    """改写单张知识卡片 frontmatter 的 mindos_folder_id（删除迁移用）。

    不改变卡片 ID、路径、链接或来源关系；仅就地改写目录归属。
    """
    _write_meta({"path": path, "content": content}, {"mindos_folder_id": target_id})


def restore_card_contents(records: list[dict]) -> None:
    """删除流程补偿：把已迁移卡片逐张写回删除前的原始内容。

    单张写入失败仅记录日志，不中断其余卡片回滚，尽量恢复归属原状。
    """
    for record in records:
        try:
            wiki_store.write_page(record["path"], record["content"], source_agent="mindos")
        except Exception:
            logging.getLogger(__name__).exception(
                "补偿回滚知识卡片 frontmatter 失败：%s", record["path"]
            )


def ensure_resources_root_id() -> int:
    """确保 KNOWLEDGE「Resources」知识根存在并返回其 ID；目录服务不可用时抛 503。"""
    node = _ensure_resources_root()
    if node is None:
        raise HTTPException(503, "知识目录服务暂不可用")
    return node["id"]


def _rewrite_tags(page: dict, tags: list[str]) -> dict:
    """Rewrite a card's frontmatter tags in-place, preserving all other fields."""
    content = str(page.get("content") or "")
    meta, body = _strict_meta(content)
    meta["tags"] = tags
    fm_lines = []
    for key, value in meta.items():
        if isinstance(value, list):
            fm_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif isinstance(value, bool):
            fm_lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, str) and key not in ("created_at", "updated_at"):
            fm_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            fm_lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    new_content = "---\n" + "\n".join(fm_lines) + f"\n---\n{body}"
    updated = _write_card(page, new_content)
    _sync_card_index(updated)
    return updated


# ---- P15-05：知识卡片回收 / 恢复 / 永久清除 ----

def _set_recycled(knowledge_id: str, recycled: bool) -> dict:
    """回收/恢复知识卡片，并对已确认卡片执行受控的索引状态迁移。"""
    page = _find(knowledge_id)
    content = str(page.get("content") or "")
    meta, body = _strict_meta(content)
    if recycled:
        meta["mindos_recycled"] = True
    else:
        meta.pop("mindos_recycled", None)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    updated = _write_card(page, f"{_render_frontmatter(meta)}\n{body}")
    revision = _content_revision(str(updated.get("content") or ""))
    state = card_ledger_store.get(knowledge_id)
    if state and state.get("approval_state") == "confirmed":
        state = card_ledger_store.transition_lifecycle_visibility(
            knowledge_id, "recycled" if recycled else "active", revision,
        )
        if not recycled and state:
            payload = {
                "title": str(updated.get("title") or "未命名知识卡片"),
                "body": _card_body(updated), "tags": _tags(updated),
                "content_revision": revision, "rel_path": str(updated.get("path") or ""),
                "folder_id": _effective_card_folder_id(updated),
            }
            card_ledger_store.enqueue_vector_repair(
                knowledge_id, int(state["desired_vector_version"]), json.dumps(payload, ensure_ascii=False),
            )
            _schedule_vector_repairs()
    else:
        _sync_card_index(updated)
    return _public(updated)


def knowledge_recycle(knowledge_id: str):
    """回收知识卡片：标记 mindos_recycled，列表/搜索/图谱隐藏，不删除文件。

    依赖校验（其它卡片引用、纠错本、草稿）由 lifecycle 在调用前完成。
    """
    page = _find(knowledge_id)
    if _is_recycled(page):
        raise HTTPException(409, "该卡片已在回收站中")
    return {"item": _set_recycled(knowledge_id, True)}


def knowledge_recycle_restore(knowledge_id: str):
    """从回收站恢复知识卡片（移除 mindos_recycled，重新进入列表/搜索/图谱）。"""
    page = _find(knowledge_id)
    content = str(page.get("content") or "")
    try:
        meta, _ = wiki_store._parse_frontmatter(content)
    except Exception:
        meta = {}
    if meta.get("mindos_merged_into"):
        raise HTTPException(400, "已合并的卡片不能单独恢复，请保持回收状态")
    if not _is_recycled(page):
        raise HTTPException(409, "该卡片不在回收站中")
    return {"item": _set_recycled(knowledge_id, False)}


def knowledge_purge(knowledge_id: str, purge_id: str | None = None) -> None:
    """永久清除知识卡片：隔离文件并清理派生页面索引。

    仅由 lifecycle 在依赖处理完成后调用（存在未处理依赖时必须先拒绝）。
    文件删除后 _find 将 404，来源引用需由调用方先行处理。
    """
    page = _find(knowledge_id)
    source_material_ids = [
        str(ref["id"]) for ref in _source_refs(page)
        if ref.get("sourceType") == "material" and ref.get("id")
    ]
    card_ledger_store.mark_visibility(knowledge_id, "purging", "purge")
    rel_path = str(page.get("path") or "")
    target = wiki_store._resolve_rel_path(rel_path)
    try:
        if target.is_file():
            purge_dir = TRASH_DIR / "purging" / (purge_id or knowledge_id)
            purge_dir.mkdir(parents=True, exist_ok=True)
            isolated = purge_dir / target.name
            try:
                os.replace(target, isolated)
            except OSError:
                shutil.move(str(target), str(isolated))
    except OSError:
        raise HTTPException(500, "卡片文件隔离失败，请稍后重试")
    try:
        wiki_store._delete_page_index(rel_path)
    except Exception as exc:
        # The file is safely isolated, but a durable purge job must remain
        # resumable until its derived page index cleanup succeeds.
        raise HTTPException(500, "卡片页面索引清理失败，请稍后重试") from exc
    card_ledger_store.mark_visibility(knowledge_id, "purged", "purge")
    # The material draft is the authoritative UI linkage to its confirmed
    # card.  A permanently deleted card must reopen that draft instead of
    # leaving a confirmed state pointing at a non-existent page.
    if source_material_ids:
        from .material_drafts import reopen_after_card_purged
        for material_id in source_material_ids:
            try:
                reopen_after_card_purged(material_id, knowledge_id)
            except Exception as exc:
                logger.warning("永久删除后解除材料草稿确认关联失败 %s: %s", material_id, type(exc).__name__)


def knowledge_tags(knowledge_id: str, req: KnowledgeTagRequest):
    """Add or remove tags on a knowledge card (frontmatter only)."""
    page = _find(knowledge_id)
    _strict_meta(str(page.get("content") or ""))
    _check_revision(page, req.expectedRevision)
    _require_draft_for_mutation(knowledge_id)
    if _is_archived(page):
        raise HTTPException(400, "已归档的卡片不能编辑标签，请先恢复后再编辑")
    if _is_recycled(page):
        raise HTTPException(400, "已回收的卡片不能编辑标签，请先恢复后再编辑")
    if req.action not in ("add", "remove"):
        raise HTTPException(400, "action 必须是 add 或 remove")
    incoming = [t.strip()[:64] for t in req.tags if t.strip()]
    if not incoming:
        raise HTTPException(400, "标签不能为空")
    current = _tags(page)
    if req.action == "add":
        merged = current + [t for t in incoming if t not in current]
    else:
        remove_set = set(incoming)
        merged = [t for t in current if t not in remove_set]
    updated = _rewrite_tags(page, merged)
    return {"tags": _tags(updated)}


def knowledge_tag_suggestions(knowledge_id: str):
    """返回基于卡片正文的 3~5 个候选标签（仅建议，用户确认后才写入）。"""
    page = _find(knowledge_id)
    content = str(page.get("content") or "")
    try:
        _, body = wiki_store._parse_frontmatter(content)
        text = body.strip()
    except Exception:
        text = content
    tags = suggest_tags(text, str(page.get("title") or ""))
    return {"knowledgeId": knowledge_id, "suggestions": tags}
