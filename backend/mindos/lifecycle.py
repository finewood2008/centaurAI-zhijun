"""MindOS P15-04/05：删除影响预览与受控回收 / 永久清除。

面向 MindOS 对象的删除闭环，与 P15-01 来源管理、P15-03 版本链配套：

- 删除影响预览（deletion-impact）：删除前展示全部关联影响（活跃/已归档/已回收
  知识卡片、其它卡片引用、纠错本、待审草稿、治理待办、派生内容），并返回一次性
  confirmToken；预览绝不返回物理路径 / 绝对路径 / 内部 artifact key。
- 受控回收（recycle / unrecycle）：把无阻塞依赖的对象移出活跃索引（材料原文件进入
  回收目录），可恢复；执行必须携带 deletion-impact 返回的 confirmToken 与依赖决策。
- 永久清除（purge）：先清理向量索引、派生数据、治理待办，再处理物理文件/卡片文件；
  任一清理失败进入可重试状态，禁止「原文件已删除但索引仍能命中」。

执行规则（P15-05 §8.3）：
1. 缺少、过期或不匹配的 confirmToken 返回 409；
2. 存在未处理依赖时拒绝执行；
3. 只有唯一来源的活跃卡片，不能仅移除来源后继续活跃（必须替换或归档）；
4. 永久清除先处理派生与索引，再处理物理文件/卡片文件；
5. 任一清理失败可重试，不产生索引孤儿。

历史关系策略：永久清除会移除已归档/已回收知识卡片中的直接来源，确保可编辑的
sourceRefs 不留下悬空 ID；已归档纠错记录和已丢弃生成草稿保留 sourceIds 仅作不可
解析的审计快照，不参与列表、检索、问答或后续来源校验，因而不作为活跃关系处理。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import WATCH_FOLDER
from runtime_paths import TRASH_DIR

from . import derived as derived_svc
from . import knowledge
from .services import ingestion
from .stores import derived_store, governance_store, card_ledger_store
from .stores.job_store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mindos", tags=["mindos-lifecycle"])

# ---- 一次性 confirmToken（内存 + TTL，跨重启自然过期 → 需重新获取预览） ----

_CONFIRM_TTL_SECONDS = 600
_CONFIRM_TOKENS: dict[str, dict] = {}
_CONFIRM_LOCK = threading.Lock()

# 材料原文件回收目录（受控回收站；材料记录保留可恢复）
_MINDOS_TRASH_SUBDIR = "mindos"


def _issue_token(target_type: str, target_id: str, fingerprint: str) -> str:
    token = uuid.uuid4().hex[:24]
    with _CONFIRM_LOCK:
        _CONFIRM_TOKENS[token] = {
            "targetType": target_type,
            "targetId": target_id,
            "fingerprint": fingerprint,
            "expiresAt": time.time() + _CONFIRM_TTL_SECONDS,
        }
    return token


def _consume_token(token: str, target_type: str, target_id: str, fingerprint: str) -> bool:
    """校验并一次性消费 confirmToken；无效/过期/状态已变化一律返回 False。"""
    if not token:
        return False
    with _CONFIRM_LOCK:
        entry = _CONFIRM_TOKENS.pop(token, None)
    if entry is None:
        return False
    return (
        entry["targetType"] == target_type
        and entry["targetId"] == target_id
        and entry["fingerprint"] == fingerprint
        and entry["expiresAt"] >= time.time()
    )


def _deps_fingerprint(blocking: list[dict]) -> str:
    """按阻塞依赖集合计算指纹：预览后依赖状态变化（新增/解决）即令 token 失效。"""
    payload = json.dumps(
        [(d["type"], d["id"]) for d in blocking],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---- 请求模型 ----

class DependencyAction(BaseModel):
    """单条依赖处理决策（对应 deletion-impact 返回的 blockingDependencies）。"""

    type: str  # knowledge | correction | draft
    id: str
    action: str  # knowledge: recycle|replaceSource|detachSource；correction: archive；draft: discard
    # replacementSource 是 P15-04 的通用替代来源；保留 replacementMaterialId
    # 兼容首版前端请求，服务端将其视为 {sourceType: material, id: ...}。
    replacementSource: dict | None = None
    replacementMaterialId: str | None = None


class DeletionExecuteRequest(BaseModel):
    confirmToken: str
    dependencyActions: list[DependencyAction] = []
    expectedRevision: str | None = None


# ---- 派生 / 清理统计 ----

def _material_cleanup_summary(material_id: str, source_path: str) -> dict:
    from vector_store import list_all_documents

    vectors = 0
    try:
        for doc in list_all_documents():
            if str(doc.get("id") or "") == source_path:
                vectors = int(doc.get("chunk_count") or 0)
                break
    except Exception as exc:
        logger.debug("材料向量计数失败 %s: %s", material_id, type(exc).__name__)
    store = derived_store.DerivedStore.instance()
    derived_records = store.count_derived_records_for_material(material_id)
    parts = store.count_parts_for_material(material_id)
    embedded_images = store.count_image_parts_for_material(material_id)
    return {
        "vectors": vectors,
        "derivedRecords": derived_records + parts,
        "embeddedImages": embedded_images,
    }


def _knowledge_cleanup_summary(knowledge_id: str) -> dict:
    from . import knowledge_index

    return {
        "vectors": knowledge_index.count_card_chunks(knowledge_id),
        "derivedRecords": 0,
        "embeddedImages": 0,
    }


# ---- 阻塞依赖 ----

def _material_referencing_drafts(material_id: str) -> list[dict]:
    drafts: list[dict] = []
    store = derived_store.DerivedStore.instance()
    for item in store.list_derived_records("generation", derived_svc.KIND_GENERATED_DRAFT):
        if item.get("status") != "ok":
            continue
        content = item.get("content") or {}
        refs = content.get("sourceRefs") or []
        source_ids = content.get("sourceIds") or []
        if material_id not in source_ids and not any(
            ref.get("sourceType") == "material" and ref.get("id") == material_id
            for ref in refs if isinstance(ref, dict)
        ):
            continue
        drafts.append({
            "draftId": item["owner_id"],
            "type": content.get("type") or "",
            "status": item["status"],
        })
    return drafts


def _knowledge_referencing_drafts(knowledge_id: str) -> list[dict]:
    drafts: list[dict] = []
    store = derived_store.DerivedStore.instance()
    for item in store.list_derived_records("generation", derived_svc.KIND_GENERATED_DRAFT):
        if item.get("status") != "ok":
            continue
        content = item.get("content") or {}
        refs = content.get("sourceRefs") or []
        source_ids = content.get("sourceIds") or []
        if knowledge_id not in source_ids and not any(
            ref.get("sourceType") == "knowledge" and ref.get("id") == knowledge_id
            for ref in refs if isinstance(ref, dict)
        ):
            continue
        drafts.append({
            "draftId": item["owner_id"],
            "type": content.get("type") or "",
            "status": item["status"],
        })
    return drafts


def _material_blocking_deps(material_id: str) -> list[dict]:
    """材料删除/回收的阻塞依赖：活跃卡片 + 活跃纠错记录 + 待审草稿。"""
    deps: list[dict] = []
    for card in knowledge.cards_referencing_material(material_id):
        if card["archived"] or card["recycled"]:
            continue  # 非活跃卡片不阻塞，仅影响预览列出
        deps.append({
            "type": "knowledge", "id": card["knowledgeId"], "title": card["title"],
            "status": "active", "allowedActions": ["recycle", "replaceSource", "detachSource"],
        })
    store = derived_store.DerivedStore.instance()
    for corr in store.list_corrections(status="active"):
        if material_id in (corr.get("sourceIds") or []):
            deps.append({
                "type": "correction", "id": corr["id"], "title": corr["title"],
                "status": "active", "allowedActions": ["archive"],
            })
    for draft in _material_referencing_drafts(material_id):
        deps.append({
            "type": "draft", "id": draft["draftId"],
            "title": draft["type"] or "内容草稿",
            "status": "active", "allowedActions": ["discard"],
        })
    return deps


def _knowledge_blocking_deps(knowledge_id: str) -> list[dict]:
    """卡片删除/回收的阻塞依赖：引用该卡片的活跃卡片 + 活跃纠错记录 + 待审草稿。"""
    deps: list[dict] = []
    for card in knowledge.cards_referencing_knowledge(knowledge_id):
        if card["archived"] or card["recycled"]:
            continue
        deps.append({
            "type": "knowledge", "id": card["knowledgeId"], "title": card["title"],
            "status": "active", "allowedActions": ["recycle", "replaceSource", "detachSource"],
        })
    store = derived_store.DerivedStore.instance()
    for corr in store.list_corrections(status="active"):
        if knowledge_id in (corr.get("sourceIds") or []):
            deps.append({
                "type": "correction", "id": corr["id"], "title": corr["title"],
                "status": "active", "allowedActions": ["archive"],
            })
    for draft in _knowledge_referencing_drafts(knowledge_id):
        deps.append({
            "type": "draft", "id": draft["draftId"],
            "title": draft["type"] or "内容草稿",
            "status": "active", "allowedActions": ["discard"],
        })
    edit_draft = card_ledger_store.get_edit_draft(knowledge_id)
    if edit_draft is not None:
        deps.append({
            "type": "editDraft", "id": knowledge_id, "title": "未发布的卡片修改草稿",
            "status": "active", "allowedActions": ["discard"],
        })
    pending = card_ledger_store.get_pending_update(knowledge_id)
    if pending is not None and pending.get("state") in {"indexing", "recovering"}:
        deps.append({
            "type": "pendingUpdate", "id": knowledge_id, "title": "正在建立索引的卡片新版本",
            "status": str(pending.get("state") or "indexing"), "allowedActions": [],
        })
    return deps


# ---- 删除影响预览 ----

def _governance_refs_material(material_id: str) -> list[dict]:
    return [
        {"id": g["id"], "kind": g["kind"], "title": g["title"], "status": g["status"]}
        for g in governance_store.instance().list(limit=1000)
        if g.get("materialId") == material_id
    ]


def _governance_refs_knowledge(knowledge_id: str) -> list[dict]:
    return [
        {"id": g["id"], "kind": g["kind"], "title": g["title"], "status": g["status"]}
        for g in governance_store.instance().list(limit=1000)
        if g.get("sourceKnowledgeId") == knowledge_id or g.get("targetKnowledgeId") == knowledge_id
    ]


def deletion_impact_material(material_id: str) -> dict:
    """GET：删除/回收材料前的影响预览（不返回任何物理路径）。"""
    record = JobStore.instance().get(material_id)
    if record is None:
        raise HTTPException(404, "资料不存在")
    cards = knowledge.cards_referencing_material(material_id)
    active_cards = [c for c in cards if not c["archived"] and not c["recycled"]]
    archived_cards = [c for c in cards if c["archived"] and not c["recycled"]]
    recycled_cards = [c for c in cards if c["recycled"]]
    store = derived_store.DerivedStore.instance()
    corrections = [
        {"id": c["id"], "title": c["title"], "status": c["status"]}
        for c in store.list_corrections()
        if material_id in (c.get("sourceIds") or [])
    ]
    drafts = _material_referencing_drafts(material_id)
    governance = _governance_refs_material(material_id)
    blocking = _material_blocking_deps(material_id)
    cleanup = _material_cleanup_summary(material_id, record["source_path"])
    can = not blocking
    token = _issue_token("material", material_id, _deps_fingerprint(blocking))
    return {
        "target": {"type": "material", "id": material_id, "title": record["file_name"]},
        "recycled": bool(record.get("recycled")),
        "canRecycle": can,
        "canPurge": can,
        "confirmToken": token,
        "blockingDependencies": blocking,
        "knowledgeCards": {
            "active": active_cards,
            "archived": archived_cards,
            "recycled": recycled_cards,
            "activeCount": len(active_cards),
            "archivedCount": len(archived_cards),
            "recycledCount": len(recycled_cards),
        },
        "corrections": corrections,
        "drafts": drafts,
        "governanceItems": governance,
        "cleanupSummary": cleanup,
    }


def deletion_impact_knowledge(knowledge_id: str) -> dict:
    """GET：删除/回收知识卡片前的影响预览（不返回任何物理路径）。"""
    page = knowledge._find(knowledge_id)
    cards = knowledge.cards_referencing_knowledge(knowledge_id)
    active_cards = [c for c in cards if not c["archived"] and not c["recycled"]]
    archived_cards = [c for c in cards if c["archived"] and not c["recycled"]]
    recycled_cards = [c for c in cards if c["recycled"]]
    store = derived_store.DerivedStore.instance()
    corrections = [
        {"id": c["id"], "title": c["title"], "status": c["status"]}
        for c in store.list_corrections()
        if knowledge_id in (c.get("sourceIds") or [])
    ]
    drafts = _knowledge_referencing_drafts(knowledge_id)
    governance = _governance_refs_knowledge(knowledge_id)
    blocking = _knowledge_blocking_deps(knowledge_id)
    cleanup = _knowledge_cleanup_summary(knowledge_id)
    edit_draft = card_ledger_store.get_edit_draft(knowledge_id)
    pending_update = card_ledger_store.get_pending_update(knowledge_id)
    can = not blocking
    token = _issue_token("knowledge", knowledge_id, _deps_fingerprint(blocking))
    return {
        "target": {"type": "knowledge", "id": knowledge_id, "title": page.get("title") or knowledge_id},
        "recycled": knowledge._is_recycled(page),
        "archived": knowledge._is_archived(page),
        "canRecycle": can,
        "canPurge": can,
        "confirmToken": token,
        "expectedRevision": knowledge._content_revision(str(page.get("content") or "")),
        "blockingDependencies": blocking,
        "workingEditDraft": ({
            "exists": True, "revision": edit_draft["draft_revision"], "updatedAt": edit_draft["updated_at"],
        } if edit_draft else {"exists": False}),
        "pendingCardUpdate": ({
            "exists": True, "state": pending_update["state"],
            "revision": pending_update["target_revision"], "updatedAt": pending_update["updated_at"],
        } if pending_update else {"exists": False}),
        "requiredDecisions": (["discard_edit_draft"] if edit_draft else []) +
                             (["pending_update_must_finish"] if pending_update and pending_update.get("state") in {"indexing", "recovering"} else []),
        "referencingKnowledgeCards": {
            "active": active_cards,
            "archived": archived_cards,
            "recycled": recycled_cards,
            "activeCount": len(active_cards),
            "archivedCount": len(archived_cards),
            "recycledCount": len(recycled_cards),
        },
        "corrections": corrections,
        "drafts": drafts,
        "governanceItems": governance,
        "cleanupSummary": cleanup,
    }


# ---- 依赖处理 ----

def _replacement_source(action: DependencyAction, target_type: str, target_id: str) -> dict:
    """读取并校验通用替代来源，兼容旧 replacementMaterialId 请求字段。"""
    raw = action.replacementSource
    if raw is None and action.replacementMaterialId:
        raw = {"sourceType": "material", "id": action.replacementMaterialId}
    if not isinstance(raw, dict):
        raise HTTPException(400, f"替换来源需指定 replacementSource：{action.id}")
    st = str(raw.get("sourceType") or "").strip()
    sid = str(raw.get("id") or "").strip()
    if st not in ("material", "knowledge") or not sid:
        raise HTTPException(400, "replacementSource 仅支持有效的 material / knowledge 来源")
    if st == target_type and sid == target_id:
        raise HTTPException(400, "替换来源不能是待删除目标自身")
    return {"sourceType": st, "id": sid}


def _plan_dependency_actions(
    target_type: str, target_id: str, blocking: list[dict], actions: list[DependencyAction]
) -> list[dict]:
    """先完整校验依赖决策并生成计划，绝不在本阶段写入任何对象。"""
    blockers = {(str(item["type"]), str(item["id"])): item for item in blocking}
    chosen: dict[tuple[str, str], DependencyAction] = {}
    for act in actions:
        key = (act.type.strip(), act.id.strip())
        if not all(key) or not act.action.strip():
            raise HTTPException(400, "依赖处理决策缺少 type / id / action")
        if key not in blockers:
            raise HTTPException(400, "依赖处理决策不属于当前删除影响，拒绝执行")
        if key in chosen:
            raise HTTPException(400, "同一依赖只能提交一次处理决策")
        if act.action.strip() not in blockers[key].get("allowedActions", []):
            raise HTTPException(400, "依赖处理动作不被当前影响预览允许")
        chosen[key] = act
    missing = [item for key, item in blockers.items() if key not in chosen]
    if missing:
        raise HTTPException(409, "存在未处理的活跃依赖，请提交每项依赖的处理决策")

    store = derived_store.DerivedStore.instance()
    plans: list[dict] = []
    for (typ, aid), act in chosen.items():
        action = act.action.strip()
        if typ == "knowledge":
            page = knowledge._find(aid)
            old_refs = knowledge._source_refs(page)
            if action == "recycle":
                plans.append({"kind": "recycle_knowledge", "id": aid})
                continue
            remaining = [
                ref for ref in old_refs
                if not (ref["sourceType"] == target_type and ref["id"] == target_id)
            ]
            if action == "replaceSource":
                remaining.append(_replacement_source(act, target_type, target_id))
            if not remaining and not knowledge._is_archived(page) and not knowledge._is_recycled(page):
                raise HTTPException(409, f"唯一来源的活跃卡片不能仅移除来源，请选择替换来源或移至回收站：{aid}")
            # 复用 P15-01 的存在性、归档/回收、自引用和循环校验；此处只校验，不写入。
            validated = knowledge._validate_sources(
                aid,
                [knowledge.KnowledgeSourceRef(**ref) for ref in remaining],
                old_refs,
            )
            plans.append({"kind": "update_sources", "id": aid, "old": old_refs, "new": validated})
        elif typ == "correction":
            corr = store.get_correction(aid)
            if corr is None or corr.get("status") != "active" or target_id not in (corr.get("sourceIds") or []):
                raise HTTPException(409, "纠错记录依赖状态已变化，请重新获取删除影响预览")
            plans.append({"kind": "archive_correction", "id": aid})
        elif typ == "draft":
            draft = store.get_derived_record("generation", aid, derived_svc.KIND_GENERATED_DRAFT)
            content = (draft or {}).get("content") or {}
            refs = content.get("sourceRefs") or []
            linked = target_id in (content.get("sourceIds") or []) or any(
                isinstance(ref, dict) and ref.get("sourceType") == target_type and ref.get("id") == target_id
                for ref in refs
            )
            if draft is None or draft.get("status") != "ok" or not linked:
                raise HTTPException(409, "草稿依赖状态已变化，请重新获取删除影响预览")
            plans.append({"kind": "discard_draft", "id": aid})
        elif typ == "editDraft":
            edit_draft = card_ledger_store.get_edit_draft(aid)
            if edit_draft is None or aid != target_id:
                raise HTTPException(409, "卡片修改草稿状态已变化，请重新获取删除影响预览")
            plans.append({"kind": "discard_edit_draft", "id": aid, "row": edit_draft})
        elif typ == "pendingUpdate":
            raise HTTPException(409, "卡片新版本正在建立索引，请等待完成或失败后再删除")
        else:
            raise HTTPException(400, f"不支持的依赖类型：{typ!r}")
    return plans


def _apply_dependency_plans(plans: list[dict]) -> None:
    """执行预验证计划；失败时按逆序补偿，避免半成功污染其它生命周期对象。"""
    store = derived_store.DerivedStore.instance()
    undo: list[Callable[[], object]] = []
    try:
        for plan in plans:
            kind, aid = plan["kind"], plan["id"]
            if kind == "recycle_knowledge":
                knowledge.knowledge_recycle(aid)
                undo.append(lambda item_id=aid: knowledge.knowledge_recycle_restore(item_id))
            elif kind == "update_sources":
                knowledge.knowledge_update_sources_for_lifecycle(aid, plan["new"])
                undo.append(lambda item_id=aid, old=plan["old"]: knowledge.knowledge_update_sources_for_lifecycle(item_id, old))
            elif kind == "archive_correction":
                if store.archive_correction(aid) is None:
                    raise HTTPException(409, "纠错记录依赖状态已变化，请重新获取删除影响预览")
                undo.append(lambda item_id=aid: store.set_correction_status(item_id, "active"))
            elif kind == "discard_draft":
                if not store.discard_draft(aid):
                    raise HTTPException(409, "草稿依赖状态已变化，请重新获取删除影响预览")
                undo.append(lambda item_id=aid: store.set_derived_status("generation", item_id, derived_svc.KIND_GENERATED_DRAFT, "ok"))
            elif kind == "discard_edit_draft":
                if not card_ledger_store.discard_edit_draft(aid):
                    raise HTTPException(409, "卡片修改草稿状态已变化，请重新获取删除影响预览")
                undo.append(lambda row=plan["row"]: card_ledger_store.restore_edit_draft(row))
        return
    except Exception as exc:
        for restore in reversed(undo):
            try:
                restore()
            except Exception:
                logger.exception("生命周期依赖补偿失败")
        if isinstance(exc, HTTPException):
            raise
        logger.exception("生命周期依赖处理失败，已尝试补偿")
        raise HTTPException(500, "依赖处理失败，已尝试回滚，请重新获取删除影响预览")


def _ensure_executable(
    req: DeletionExecuteRequest, target_type: str, target_id: str, blocking: list[dict]
) -> None:
    """执行前置校验：confirmToken 一次性有效 + 依赖全部处理。"""
    # 先完成无副作用的全量校验；否则后续某项失败会让前面的依赖处理被部分写入。
    plans = _plan_dependency_actions(target_type, target_id, blocking, req.dependencyActions)
    fingerprint = _deps_fingerprint(blocking)
    if not _consume_token(req.confirmToken, target_type, target_id, fingerprint):
        raise HTTPException(409, "confirmToken 缺失、过期或状态已变化，请重新获取删除影响预览")
    _apply_dependency_plans(plans)
    remaining = (
        _material_blocking_deps(target_id)
        if target_type == "material" else _knowledge_blocking_deps(target_id)
    )
    if remaining:
        titles = "、".join(f"{d['type']}:{d['title']}" for d in remaining[:5])
        raise HTTPException(409, f"存在未处理的活跃依赖，请先处理后再执行：{titles}")


def _detach_inactive_card_sources(target_type: str, target_id: str) -> None:
    """永久清除前自动解除已归档/已回收卡片的历史来源，避免留下悬空 ID。

    非活跃卡片不会参与检索或问答，因而不要求用户逐项决策；但其 frontmatter 仍是
    结构化数据，必须在目标物理删除前解除直接引用。活跃卡片始终走 blockingDependencies。
    """
    cards = (
        knowledge.cards_referencing_material(target_id)
        if target_type == "material" else knowledge.cards_referencing_knowledge(target_id)
    )
    plans: list[dict] = []
    for card in cards:
        if not (card["archived"] or card["recycled"]):
            continue
        page = knowledge._find(card["knowledgeId"])
        old = knowledge._source_refs(page)
        new = [ref for ref in old if not (ref["sourceType"] == target_type and ref["id"] == target_id)]
        if len(new) != len(old):
            plans.append({"kind": "update_sources", "id": card["knowledgeId"], "old": old, "new": new})
    if plans:
        _apply_dependency_plans(plans)


# ---- 材料：回收 / 恢复 / 永久清除 ----

def _trash_dir() -> Path:
    path = Path(TRASH_DIR) / _MINDOS_TRASH_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _trash_path(material_id: str, file_name: str) -> Path:
    safe = "".join(ch for ch in (file_name or "file") if ch not in '\\/:*?"<>|') or "file"
    return _trash_dir() / f"{material_id}__{safe}"


def _source_file(source_path: str) -> Path | None:
    """返回受控监控目录内的原文件 Path；越界返回 None（不触碰）。"""
    try:
        target = Path(source_path).resolve()
        if target.is_relative_to(Path(WATCH_FOLDER).resolve()):
            return target
    except Exception:
        pass
    return None


def _audit(action: str, source_path: str, payload: dict) -> None:
    try:
        from annotations import add_audit
        add_audit(action, [source_path] if source_path else (), payload=payload)
    except Exception as exc:
        logger.warning("MindOS 生命周期审计失败 %s: %s", action, exc)


def recycle_material(material_id: str, req: DeletionExecuteRequest) -> dict:
    """回收材料：原文件进入受控回收目录、移出活跃索引；记录保留可恢复。"""
    record = JobStore.instance().get(material_id)
    if record is None:
        raise HTTPException(404, "资料不存在")
    if record.get("recycled"):
        raise HTTPException(409, "该资料已在回收站中")
    blocking = _material_blocking_deps(material_id)
    _ensure_executable(req, "material", material_id, blocking)
    # 先使持久化 worker 失去提交资格，再移动源文件/清理索引。worker 在各提交
    # 边界复核该状态，避免处理结果在回收后重新出现。
    from .stores.material_pipeline_store import MaterialPipelineStore
    MaterialPipelineStore.instance().cancel_for_lifecycle(
        material_id, int(record.get("version_number") or 1)
    )
    source_path = record["source_path"]
    source = _source_file(source_path)
    if source is None or not source.is_file():
        raise HTTPException(409, "原文件不存在或不在受控目录中，无法回收，请先修复资料状态")
    destination = _trash_path(material_id, record["file_name"])
    # 先移动文件、再删索引；索引清理失败时将文件移回，避免活跃资料突然失去检索能力。
    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        logger.error("材料回收失败（文件移动）%s: %s", material_id, exc)
        raise HTTPException(500, "回收失败：原文件移动异常，请稍后重试")
    from vector_store import delete_document
    try:
        delete_document(source_path)
    except Exception as exc:
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
        except OSError:
            logger.exception("材料回收补偿失败 %s", material_id)
        logger.error("材料回收失败（索引清理）%s: %s", material_id, exc)
        raise HTTPException(500, "回收失败：索引清理异常，请稍后重试")
    if not JobStore.instance().set_recycled(material_id, True):
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
            from watcher import submit_index
            submit_index(source_path, force=True, submit_wiki=False)
        except Exception:
            logger.exception("材料回收记录写入失败后的补偿失败 %s", material_id)
        raise HTTPException(500, "回收失败：状态写入异常，请稍后重试")
    _audit("material.recycle", source_path, {"materialId": material_id})
    public = ingestion.status_of(material_id)
    return {"materialId": material_id, "recycled": True, "status": (public or {}).get("status")}


def unrecycle_material(material_id: str) -> dict:
    """从回收站恢复材料：原文件移回原路径、重新进入活跃索引。"""
    record = JobStore.instance().get(material_id)
    if record is None:
        raise HTTPException(404, "资料不存在")
    if not record.get("recycled"):
        raise HTTPException(409, "该资料不在回收站中")
    source_path = record["source_path"]
    source = _source_file(source_path)
    if source is None:
        raise HTTPException(409, "资料原路径不在受控目录中，无法恢复")
    if not source.exists():
        trash_file = _trash_path(material_id, record["file_name"])
        if not trash_file.is_file():
            raise HTTPException(409, "回收站中的原文件不存在，无法恢复，请先处理异常记录")
        source.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(trash_file), str(source))
        except OSError as exc:
            raise HTTPException(500, f"恢复失败：原文件还原异常，请稍后重试：{exc}")
    if not JobStore.instance().set_recycled(material_id, False):
        raise HTTPException(500, "恢复失败：状态写入异常，请稍后重试")
    # 恢复后重建检索索引（异步提交，不阻塞恢复结果）。
    if source is not None and source.is_file():
        from watcher import submit_index
        submit_index(source_path, force=True, submit_wiki=False)
    _audit("material.unrecycle", source_path, {"materialId": material_id})
    public = ingestion.status_of(material_id)
    return {"materialId": material_id, "recycled": False, "status": (public or {}).get("status")}


def purge_material(material_id: str, req: DeletionExecuteRequest) -> dict:
    """永久清除材料：先清理索引/派生/治理，再处理物理文件；失败可重试不产生索引孤儿。"""
    record = JobStore.instance().get(material_id)
    if record is None:
        raise HTTPException(404, "资料不存在")
    blocking = _material_blocking_deps(material_id)
    _ensure_executable(req, "material", material_id, blocking)
    from .stores.material_pipeline_store import MaterialPipelineStore
    MaterialPipelineStore.instance().cancel_for_lifecycle(
        material_id, int(record.get("version_number") or 1)
    )
    _detach_inactive_card_sources("material", material_id)
    source_path = record["source_path"]

    def _restore_active_index() -> None:
        source = _source_file(source_path)
        if source is not None and source.is_file():
            try:
                from watcher import submit_index
                submit_index(source_path, force=True, submit_wiki=False)
            except Exception:
                logger.exception("材料永久清除失败后的索引补偿失败 %s", material_id)

    from vector_store import delete_document
    # 顺序：向量 → 派生（parts/图片/派生记录）→ 治理 → 标注 → 物理文件 → 记录。
    try:
        delete_document(source_path)
    except Exception as exc:
        logger.error("材料永久清除失败（索引清理）%s: %s", material_id, exc)
        raise HTTPException(500, "永久清除失败：索引清理异常，请重试（原文件未删除）")
    store = derived_store.DerivedStore.instance()
    try:
        store.delete_for_material(material_id)
        store.delete_derived_records_for_material(material_id)
    except Exception as exc:
        _restore_active_index()
        logger.error("材料永久清除失败（派生清理）%s: %s", material_id, exc)
        raise HTTPException(500, "永久清除失败：派生数据清理异常，请重试")
    try:
        governance_store.instance().purge_material_items(material_id)
        from annotations import delete as annotations_delete
        annotations_delete(source_path)
    except Exception as exc:
        _restore_active_index()
        logger.error("材料永久清除失败（治理/标注清理）%s: %s", material_id, exc)
        raise HTTPException(500, "永久清除失败：治理或标注清理异常，请重试")
    # 物理文件：已回收则位于回收目录；否则位于原路径。删除后不再可命中（索引已清）。
    for candidate in (
        _trash_path(material_id, record["file_name"]) if record.get("recycled") else None,
        _source_file(source_path),
    ):
        if candidate is None:
            continue
        try:
            if candidate.is_file():
                candidate.unlink()
        except OSError as exc:
            logger.error("材料永久清除失败（文件删除）%s: %s", material_id, exc)
            raise HTTPException(500, "永久清除失败：物理文件删除异常，请重试")

    JobStore.instance().delete(material_id)
    _audit("material.purge", source_path, {"materialId": material_id})
    return {"materialId": material_id, "purged": True}


# ---- 知识卡片：回收 / 恢复 / 永久清除 ----

def recycle_knowledge(knowledge_id: str, req: DeletionExecuteRequest) -> dict:
    """回收知识卡片：标记 mindos_recycled 并移出向量索引；文件保留可恢复。"""
    page = knowledge._find(knowledge_id)
    knowledge._check_revision(page, req.expectedRevision)
    if knowledge._is_recycled(page):
        raise HTTPException(409, "该卡片已在回收站中")
    blocking = _knowledge_blocking_deps(knowledge_id)
    _ensure_executable(req, "knowledge", knowledge_id, blocking)
    knowledge.knowledge_recycle(knowledge_id)
    _audit("knowledge.recycle", str(page.get("path") or ""), {"knowledgeId": knowledge_id})
    return {"knowledgeId": knowledge_id, "recycled": True}


def unrecycle_knowledge(knowledge_id: str, req: DeletionExecuteRequest | None = None) -> dict:
    """从回收站恢复知识卡片（重新进入列表/搜索/图谱与向量索引）。"""
    page = knowledge._find(knowledge_id)
    knowledge._check_revision(page, req.expectedRevision if req else None)
    if not knowledge._is_recycled(page):
        raise HTTPException(409, "该卡片不在回收站中")
    updated = knowledge.knowledge_recycle_restore(knowledge_id)
    _audit("knowledge.unrecycle", str(page.get("path") or ""), {"knowledgeId": knowledge_id})
    return {"knowledgeId": knowledge_id, "recycled": False, "item": updated["item"]}


def purge_knowledge(knowledge_id: str, req: DeletionExecuteRequest) -> dict:
    """永久清除知识卡片：状态可恢复，文件先进入隔离区。"""
    page = knowledge._find(knowledge_id)
    knowledge._check_revision(page, req.expectedRevision)
    blocking = _knowledge_blocking_deps(knowledge_id)
    _ensure_executable(req, "knowledge", knowledge_id, blocking)
    rel_path = str(page.get("path") or "")
    purge_id = uuid.uuid4().hex
    card_ledger_store.create_purge_job(purge_id, knowledge_id, rel_path, json.dumps({"blocking": blocking}, ensure_ascii=False))
    card_ledger_store.mark_visibility(knowledge_id, "purging", "purge")
    try:
        _detach_inactive_card_sources("knowledge", knowledge_id)
        card_ledger_store.update_purge_job(purge_id, "dependencies_detached")
        governance_store.instance().purge_knowledge_items(knowledge_id)
        card_ledger_store.update_purge_job(purge_id, "governance_cleaned")
    except Exception as exc:
        logger.error("卡片永久清除失败（治理清理）%s: %s", knowledge_id, exc)
        card_ledger_store.update_purge_job(purge_id, "failed", "governance_cleanup", str(exc))
        raise HTTPException(500, "永久清除失败：治理待办清理异常，请重试")
    try:
        knowledge.knowledge_purge(knowledge_id, purge_id=purge_id)
        card_ledger_store.update_purge_job(purge_id, "file_deleted")
        # Base generation chunks are logically hidden now; physical reclaim is
        # deferred until a retired generation can be safely removed.
        card_ledger_store.update_purge_job(purge_id, "completed_with_vector_cleanup_pending")
    except HTTPException as exc:
        card_ledger_store.update_purge_job(purge_id, "failed", "file_isolation", str(exc.detail))
        raise
    _audit("knowledge.purge", str(page.get("path") or ""), {"knowledgeId": knowledge_id})
    return {"knowledgeId": knowledge_id, "purged": True}


def recover_pending_purges() -> dict:
    """Continue durable purge jobs after a process crash without replaying finished steps."""
    recovered = failed = 0
    for job in card_ledger_store.list_purge_jobs(non_terminal_only=True):
        purge_id, knowledge_id, state = job["purge_id"], job["knowledge_id"], job["state"]
        try:
            if state == "prepared":
                card_ledger_store.mark_visibility(knowledge_id, "purging", "purge")
                # No dependency mutation was persisted for prepared jobs.
                card_ledger_store.update_purge_job(purge_id, "dependencies_detached")
                state = "dependencies_detached"
            if state == "dependencies_detached":
                governance_store.instance().purge_knowledge_items(knowledge_id)
                card_ledger_store.update_purge_job(purge_id, "governance_cleaned")
                state = "governance_cleaned"
            if state == "governance_cleaned":
                try:
                    knowledge.knowledge_purge(knowledge_id, purge_id=purge_id)
                except HTTPException as exc:
                    # The file may already be in the isolation directory from
                    # the prior process; then only the derived page index is left.
                    isolated = TRASH_DIR / "purging" / purge_id
                    if not isolated.is_dir():
                        raise exc
                    knowledge.wiki_store._delete_page_index(str(job["rel_path"]))
                    card_ledger_store.mark_visibility(knowledge_id, "purged", "purge")
                card_ledger_store.update_purge_job(purge_id, "file_deleted")
                state = "file_deleted"
            if state == "file_deleted":
                card_ledger_store.mark_visibility(knowledge_id, "purged", "purge")
                card_ledger_store.update_purge_job(purge_id, "completed_with_vector_cleanup_pending")
            recovered += 1
        except Exception as exc:
            failed += 1
            card_ledger_store.update_purge_job(purge_id, "failed", "recovery_failed", f"{type(exc).__name__}: {exc}")
    return {"recovered": recovered, "failed": failed}


def cleanup_purge_isolation(retention_days: int = 7) -> int:
    cutoff = time.time() - max(retention_days, 1) * 86400
    root = TRASH_DIR / "purging"
    removed = 0
    if not root.is_dir():
        return removed
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                removed += 1
        except OSError:
            logger.warning("清理卡片 purge 隔离目录失败: %s", child.name)
    return removed


# ---- 路由 ----

def configure_write_guard(guard) -> None:
    """由 server 注入写操作防护（loopback + CSRF）。"""
    global router
    router = APIRouter(prefix="/api/mindos", tags=["mindos-lifecycle"])
    router.add_api_route("/materials/{material_id}/deletion-impact", deletion_impact_material, methods=["GET"])
    router.add_api_route("/knowledge/{knowledge_id}/deletion-impact", deletion_impact_knowledge, methods=["GET"])
    router.add_api_route("/materials/{material_id}/recycle", recycle_material, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/unrecycle", unrecycle_material, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/purge", purge_material, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/knowledge/{knowledge_id}/recycle", recycle_knowledge, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/knowledge/{knowledge_id}/unrecycle", unrecycle_knowledge, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/knowledge/{knowledge_id}/purge", purge_knowledge, methods=["POST"], dependencies=[Depends(guard)])
