"""MindOS 上传接口（P2 / P13 开放音频）。

- POST /api/mindos/uploads                        真实上传 + 进入处理链路
- GET  /api/mindos/uploads/{material_id}          处理状态轮询
- POST /api/mindos/uploads/{material_id}/retry    失败重试（重新进入处理）

校验失败（不支持 / 超限）一律不落盘、不创建处理任务。
文档/图片 ≤50MB，音频（MP3/WAV/M4A）≤200MB。
"""
import logging
import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Query, Form, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import WATCH_FOLDER
from .validation import (
    validate_import,
    OK,
    UNSUPPORTED,
    OVERSIZE,
    AUDIO_EXTENSIONS,
    DOC_EXTENSIONS,
    IMAGE_EXTENSIONS,
    DOC_IMAGE_MAX_BYTES,
    AUDIO_MAX_BYTES,
    CATEGORY_AUDIO,
)
from .services import ingestion
from .stores import derived_store
from .stores.job_store import FolderError, FolderNotFoundError, SCOPE_RAW, SCOPE_KNOWLEDGE
from . import derived as derived_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mindos", tags=["mindos"])


def _device_scope_of(request: Request = None) -> str:
    """票据模式下按真实 device_id 生成业务数据作用域；调试模式为 global。"""
    from .device_context import scope_for_device

    context = getattr(getattr(request, "state", None), "mindos_device_context", None)
    return scope_for_device(getattr(context, "device_id", None))


class TagRequest(BaseModel):
    tags: list[str]
    action: str  # "add" or "remove"


class MaterialMoveRequest(BaseModel):
    """材料移动：folderId 为目录树 ID（null = 未分类）。

    P14-06 起不再接受自由文本 folder（旧字符串接口已废弃）。
    """

    folderId: int | None = None


class RegenerateRequest(BaseModel):
    """重生成指定派生项（不得重传文件）。"""

    item: str  # "summary" | "analysis"/"parse" | "draft"


class DraftCardSaveRequest(BaseModel):
    expectedRevision: str
    title: str = ""
    content: str


class DraftCardConfirmRequest(BaseModel):
    expectedRevision: str


class FolderCreateRequest(BaseModel):
    name: str
    parentId: int | None = None
    scope: str = "RAW"


class FolderRenameRequest(BaseModel):
    name: str


class FolderMoveRequest(BaseModel):
    parentId: int | None = None


def configure_write_guard(guard) -> None:
    """由 server 在定义 require_local 后注入写操作防护。"""
    global router
    router = APIRouter(prefix="/api/mindos", tags=["mindos"])
    router.add_api_route("/uploads", mindos_upload, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route(
        "/uploads/{material_id}/retry",
        mindos_upload_retry,
        methods=["POST"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route("/uploads/{material_id}", mindos_upload_status, methods=["GET"])
    router.add_api_route(
        "/uploads/{material_id}/resume",
        mindos_upload_resume,
        methods=["POST"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route("/materials/{material_id}/processing", mindos_material_processing, methods=["GET"])
    router.add_api_route("/materials/{material_id}/draft-card", mindos_material_draft_card, methods=["GET"])
    router.add_api_route(
        "/materials/{material_id}/draft-card", mindos_material_draft_card_save,
        methods=["PUT"], dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/materials/{material_id}/draft-card/confirm", mindos_material_draft_card_confirm,
        methods=["POST"], dependencies=[Depends(guard)],
    )
    router.add_api_route(
        "/materials/{material_id}/regenerate",
        mindos_material_regenerate,
        methods=["POST"],
        dependencies=[Depends(guard)],
    )
    router.add_api_route("/materials", mindos_materials, methods=["GET"])
    router.add_api_route("/materials/{material_id}", mindos_material_detail, methods=["GET"])
    router.add_api_route("/materials/{material_id}/file", mindos_material_file, methods=["GET"])
    router.add_api_route("/materials/{material_id}/parts/{part_id}/file", mindos_material_part_file, methods=["GET"])
    router.add_api_route("/materials/{material_id}/summary", mindos_material_summary, methods=["GET"])
    router.add_api_route("/materials/{material_id}/summary/retry", mindos_material_summary_retry, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/analysis", mindos_material_analysis, methods=["GET"])
    router.add_api_route("/materials/{material_id}/tags", mindos_material_tags, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/impact", mindos_material_impact, methods=["GET"])
    router.add_api_route("/materials/{material_id}/versions", mindos_material_versions, methods=["GET"])
    router.add_api_route("/materials/{material_id}/versions", mindos_material_version_upload, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/version-impact", mindos_material_version_impact, methods=["GET"])
    router.add_api_route("/materials/{material_id}/move", mindos_material_move, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/tag-suggestions", mindos_material_tag_suggestions, methods=["GET"])
    router.add_api_route("/materials/{material_id}/tag-suggestions/{suggestion_id}/confirm", mindos_material_tag_suggestion_confirm, methods=["POST"], dependencies=[Depends(guard)])
    # P14-06：目录树 ID 化 API（旧字符串 folders API 已废弃移除）
    router.add_api_route("/folders", mindos_folder_list, methods=["GET"])
    router.add_api_route("/folders", mindos_folder_create, methods=["POST"], dependencies=[Depends(guard)], status_code=201)
    router.add_api_route("/folders/{folder_id}", mindos_folder_rename, methods=["PATCH"], dependencies=[Depends(guard)])
    router.add_api_route("/folders/{folder_id}/move", mindos_folder_move, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/folders/{folder_id}", mindos_folder_delete, methods=["DELETE"], dependencies=[Depends(guard)])
    router.add_api_route("/uploads/{material_id}/cancel", mindos_upload_cancel, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/materials/{material_id}/queue", mindos_material_queue_remove, methods=["DELETE"], dependencies=[Depends(guard)])


def _error_for_status(status: str) -> HTTPException:
    if status == UNSUPPORTED:
        return HTTPException(400, "不支持的文件类型")
    if status == OVERSIZE:
        return HTTPException(413, "文件超过大小限制（文档/图片 50MB，音频 200MB）")
    return HTTPException(400, "文件校验未通过")


def _require_folder(folder_id: int) -> None:
    """校验目录 ID 存在（scope=RAW）；不存在时抛 404（与 material/folder 写操作一致）。"""
    nodes = ingestion.list_folder_nodes(SCOPE_RAW)
    if not any(node["id"] == folder_id for node in nodes):
        raise HTTPException(404, "目标目录不存在")


async def _receive_upload(file: UploadFile, folder_id: int | None) -> tuple[str, str, Path]:
    """校验、暂存并原子发布上传文件，供首次上传与新版本上传共用。"""
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    safe_name = Path(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(400, "非法文件名")
    if folder_id is not None:
        _require_folder(folder_id)
    initial = validate_import(safe_name, 0)
    if initial["status"] != OK:
        raise _error_for_status(initial["status"])
    max_bytes = AUDIO_MAX_BYTES if initial["category"] == CATEGORY_AUDIO else DOC_IMAGE_MAX_BYTES
    watch_root = Path(WATCH_FOLDER).resolve()
    destination = Path(ingestion.destination_path(safe_name)).resolve()
    if not destination.is_relative_to(watch_root):
        raise HTTPException(400, "非法文件名")
    staging_dir = watch_root.parent / ".mindos_upload_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"{uuid.uuid4().hex}.uploading"
    written = 0
    try:
        with open(staging, "xb") as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(413, "文件超过大小限制（文档/图片 50MB，音频 200MB）")
                output.write(chunk)
    except HTTPException:
        staging.unlink(missing_ok=True)
        raise
    except Exception as exc:
        staging.unlink(missing_ok=True)
        logger.error("MindOS 上传落盘失败: %s", exc)
        raise HTTPException(500, "上传失败")
    if written == 0:
        staging.unlink(missing_ok=True)
        raise HTTPException(400, "文件为空")
    checked = validate_import(safe_name, written)
    if checked["status"] != OK:
        staging.unlink(missing_ok=True)
        raise _error_for_status(checked["status"])
    try:
        os.replace(staging, destination)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        logger.error("MindOS 上传发布失败: %s", exc)
        raise HTTPException(500, "上传失败")
    file_type = {"document": "document", "image": "image", "audio": "audio"}[checked["category"]]
    return safe_name, file_type, destination


@router.post("/uploads")
async def mindos_upload(
    request: Request,
    file: UploadFile = File(...),
    folderId: int | None = Form(default=None),
):
    safe_name, file_type, destination = await _receive_upload(file, folderId)
    material_id = ingestion.new_material_id()
    try:
        record = ingestion.start_ingestion(
            material_id, safe_name, file_type, str(destination), folder_id=folderId,
            device_scope=_device_scope_of(request),
        )
    except Exception:
        # 与 start_ingestion 的 SQLite 记录补偿配套：上传只有在“登记并入队”
        # 都成功时才对用户可见，失败时不遗留隐藏源文件。
        destination.unlink(missing_ok=True)
        raise
    return record


@router.get("/uploads/{material_id}")
def mindos_upload_status(material_id: str, request: Request):
    scope = _device_scope_of(request)
    record = ingestion.status_of(material_id, device_scope=scope)
    if record is None:
        raise HTTPException(404, "资料不存在")
    return record


@router.get("/materials")
def mindos_materials(
    request: Request,
    file_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    folder: str | None = Query(default=None),
    folderId: int | None = Query(default=None),
    tag: str | None = Query(default=None),
    recycled: bool = False,
):
    """返回原材料公开列表；仅返回业务字段，不返回宿主机路径。

    recycled=true 仅返回已回收材料（P15-05 回收站入口）；默认隐藏已回收材料。
    P14-06：folderId 按「选中目录 + 全部后代」子树筛选（folder 字符串参数仅兼容旧调用）。
    阶段 2：只返回当前设备作用域下的资料（请求票据身份决定作用域）。
    """
    if file_type and file_type not in {"document", "image", "audio"}:
        raise HTTPException(400, "不支持的资料类型筛选")
    allowed_statuses = {"uploaded", "queued", "processing", "available", "failed"}
    if status and status not in allowed_statuses:
        raise HTTPException(400, "不支持的处理状态筛选")
    if folder is not None and folderId is not None:
        raise HTTPException(400, "folder 与 folderId 不能同时使用")
    items = ingestion.list_materials(
        file_type, status, keyword, folder, tag, folderId,
        device_scope=_device_scope_of(request),
    )
    recycled_ids = ingestion.recycled_material_ids(device_scope=_device_scope_of(request))
    if recycled:
        # 回收站：只看已回收资料。
        items = [item for item in items if item["materialId"] in recycled_ids]
    else:
        items = [item for item in items if item["materialId"] not in recycled_ids]
    for item in items:
        item["recycled"] = item["materialId"] in recycled_ids
    _attach_knowledge_card_states(items, device_scope=_device_scope_of(request))
    return {"items": items, "total": len(items), "folders": ingestion.list_folders()}


def _attach_knowledge_card_states(items: list[dict], *, device_scope: str) -> None:
    """Attach a persisted material-to-card projection without list-time writes.

    The material pipeline, generated draft and card ledger remain their own
    SQLite facts.  This function only gives the materials table one compact,
    durable view of those facts; it must never create a draft or enqueue work.
    """
    material_ids = {str(item.get("materialId") or "") for item in items}
    drafts = derived_store.DerivedStore.instance().derived_records_for_owners(
        "material", material_ids, derived_svc.KIND_GENERATED_DRAFT,
    )
    draft_by_material = {str(record["owner_id"]): record for record in drafts}
    knowledge_ids = {
        str((record.get("content") or {}).get("knowledgeId") or "")
        for record in drafts
        if bool((record.get("content") or {}).get("confirmed"))
    }
    from .stores import card_ledger_store
    ledgers = card_ledger_store.get_many(knowledge_ids, device_scope=device_scope)

    for item in items:
        material_id = str(item.get("materialId") or "")
        record = draft_by_material.get(material_id)
        content = (record or {}).get("content") or {}
        if not record:
            item["knowledgeCard"] = {
                "state": "waiting" if item.get("status") != ingestion.ST_AVAILABLE else "generating",
                "knowledgeId": None,
                "indexState": None,
                "errorCode": None,
            }
            continue
        if not bool(content.get("confirmed")):
            item["knowledgeCard"] = {
                "state": "confirming" if record.get("status") == "confirming" else "draft",
                "knowledgeId": None,
                "indexState": None,
                "errorCode": content.get("errorCode"),
            }
            continue

        knowledge_id = str(content.get("knowledgeId") or "")
        ledger = ledgers.get(knowledge_id)
        if not ledger:
            # Never claim this card is searchable when the ledger cannot prove
            # it. The detail endpoint can repair historical missing-card data.
            item["knowledgeCard"] = {
                "state": "unknown", "knowledgeId": knowledge_id or None,
                "indexState": None, "errorCode": "card_ledger_missing",
            }
            continue
        visibility = str(ledger.get("visibility") or "")
        index_state = str(ledger.get("index_state") or "none")
        if visibility == "recycled":
            state = "recycled"
        elif visibility != "active":
            state = "unknown"
        elif index_state == "indexed" and card_ledger_store.is_rag_eligible(ledger):
            state = "available"
        elif index_state in {"index_failed", "index_corrupted"}:
            state = "failed"
        else:
            state = "indexing"
        item["knowledgeCard"] = {
            "state": state, "knowledgeId": knowledge_id or None,
            "indexState": index_state, "errorCode": ledger.get("index_error_code"),
        }


def _material_record(material_id: str, device_scope: str = "global") -> dict:
    record = ingestion.JobStore.instance().get(material_id)
    if record is None:
        raise HTTPException(404, "资料不存在")
    if (record.get("device_scope") or "global") != device_scope:
        # 阶段 2：资料不属于当前设备作用域，一律视为不存在，杜绝跨设备/账号读取。
        raise HTTPException(404, "资料不存在")
    return record


def _available_material_record(material_id: str, device_scope: str = "global") -> dict:
    record = _material_record(material_id, device_scope=device_scope)
    # material_jobs 是处理状态唯一事实来源；JobStore 的资料记录本身不保存
    # ``available``，直接读取它会让详情页已完成但草稿接口仍错误返回 409。
    public = ingestion.status_of(material_id)
    if public is None or public.get("status") != ingestion.ST_AVAILABLE:
        raise HTTPException(409, "资料处理完成后才能编辑或确认知识卡片")
    if record.get("recycled") or ingestion.is_recycled(material_id):
        raise HTTPException(409, "已回收的资料不能编辑或确认知识卡片，请先恢复后再操作")
    return record


def _repair_missing_confirmed_card(material_id: str) -> dict:
    """Self-heal historical drafts whose confirmed card was permanently deleted."""
    from .material_drafts import ensure_minimal_draft, reopen_after_card_purged

    draft = ensure_minimal_draft(material_id)
    knowledge_id = str(draft.get("knowledgeId") or "")
    if not draft.get("confirmed") or not knowledge_id:
        return draft
    from . import knowledge
    try:
        knowledge._find(knowledge_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        repaired = reopen_after_card_purged(material_id, knowledge_id)
        return repaired or ensure_minimal_draft(material_id)
    return draft


def mindos_material_detail(material_id: str, request: Request):
    scope = _device_scope_of(request)
    detail = ingestion.detail_of(material_id, device_scope=scope)
    if detail is None:
        raise HTTPException(404, "资料不存在")
    detail["recycled"] = ingestion.is_recycled(material_id, device_scope=scope)
    if detail.get("status") == "available":
        detail["draftCard"] = _repair_missing_confirmed_card(material_id)
    else:
        detail["draftCard"] = None
    return detail


def mindos_material_draft_card(material_id: str, request: Request = None):
    scope = _device_scope_of(request)
    _available_material_record(material_id, device_scope=scope)
    return {"materialId": material_id, **_repair_missing_confirmed_card(material_id)}


def mindos_material_draft_card_save(material_id: str, req: DraftCardSaveRequest, request: Request):
    scope = _device_scope_of(request)
    _available_material_record(material_id, device_scope=scope)
    _repair_missing_confirmed_card(material_id)
    if not (req.content or "").strip():
        raise HTTPException(400, "草稿正文不能为空")
    from .material_drafts import DraftConfirmationLocked, save_draft
    from .stores.derived_store import DraftRevisionConflict
    try:
        draft = save_draft(material_id, req.expectedRevision, req.title or "", req.content)
    except DraftRevisionConflict as exc:
        current = (exc.current.get("content") or {})
        raise HTTPException(409, {
            "detail": "草稿已在他处更新，请重新加载后合并修改",
            "currentRevision": current.get("revision"),
            "currentTitle": current.get("title", ""),
            "currentContent": current.get("content", ""),
        })
    except DraftConfirmationLocked as exc:
        raise HTTPException(409, "草稿正在确认或已确认，不能继续修改") from exc
    return {"materialId": material_id, **draft}


def mindos_material_draft_card_confirm(
    material_id: str,
    req: DraftCardConfirmRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    """确认当前材料草稿，创建卡片并在台账事务中写入索引 outbox。"""
    scope = _device_scope_of(request)
    _available_material_record(material_id, device_scope=scope)
    _repair_missing_confirmed_card(material_id)
    if not idempotency_key:
        raise HTTPException(400, "缺少 Idempotency-Key")
    from . import knowledge
    from .derived import KIND_GENERATED_DRAFT
    from .stores import card_ledger_store

    record = derived_store.DerivedStore.instance().get_derived_record(
        "material", material_id, KIND_GENERATED_DRAFT
    )
    content = (record or {}).get("content") or {}
    revision = str(content.get("revision") or "")
    if not revision or revision != req.expectedRevision:
        raise HTTPException(409, {"detail": "草稿已在他处更新，请重新加载后确认", "currentRevision": revision or None})
    if not str(content.get("content") or "").strip():
        raise HTTPException(409, "草稿正文不可确认")
    try:
        session = card_ledger_store.begin_material_confirmation(
            material_id, revision, idempotency_key, {"material_id": material_id}
        )
    except card_ledger_store.ConfirmationConflict as exc:
        raise HTTPException(409, str(exc))
    if session.get("state") == "ledger_committed":
        # 正式卡片已提交时，幂等重放也应修复材料详情中的附属草稿状态。
        # 这覆盖服务在台账提交后、草稿标记前中断的恢复窗口。
        knowledge_id = str(session.get("knowledge_id") or "")
        if knowledge_id:
            from .material_drafts import mark_confirmed
            try:
                mark_confirmed(material_id, revision, knowledge_id)
            except derived_store.DraftRevisionConflict:
                logger.warning("幂等确认未能修复材料草稿状态: %s", material_id)
        return {"materialId": material_id, "knowledgeId": session.get("knowledge_id"),
                "sessionId": session["session_id"], "idempotent": True}
    if session.get("state") == "preparing":
        from .material_drafts import DraftConfirmationLocked, lock_for_confirmation
        try:
            lock_for_confirmation(material_id, revision, session["session_id"])
        except (derived_store.DraftRevisionConflict, DraftConfirmationLocked) as exc:
            card_ledger_store.roll_back_material_confirmation(session["session_id"], "draft_confirmation_lock_conflict")
            raise HTTPException(409, "草稿已在他处更新或正在确认，请重新加载后操作") from exc
        card = knowledge.create_card_with_sources(
            title=str(content.get("title") or "待确认知识卡片"), content=str(content["content"]),
            tags=ingestion.material_tags(material_id),
            source_refs=[{"sourceType": "material", "id": material_id}],
            confirmation_session_id=session["session_id"],
            device_scope=scope,
        )
        knowledge_id = card["knowledgeId"]
        page = knowledge._find(knowledge_id)
        payload = {
            "title": str(card.get("title") or content.get("title") or "待确认知识卡片"),
            "body": knowledge._card_body(page), "tags": card.get("tags") or [],
            "content_revision": knowledge._content_revision(str(page.get("content") or "")),
            "rel_path": str(page.get("path") or ""),
            "folder_id": card.get("folderId"),
        }
        card_ledger_store.mark_confirmation_file_committed(session["session_id"], knowledge_id, payload)
        session = {**session, "knowledge_id": knowledge_id, "state": "file_committed", "payload_json": json.dumps(payload)}
    knowledge_id = str(session.get("knowledge_id") or "")
    if not knowledge_id:
        raise HTTPException(500, "确认会话缺少目标卡片")
    page = knowledge._find(knowledge_id)
    payload = json.loads(session.get("payload_json") or "{}")
    actual_revision = knowledge._content_revision(str(page.get("content") or ""))
    if str(payload.get("content_revision") or "") != actual_revision:
        card_ledger_store.roll_back_material_confirmation(session["session_id"], "confirmed_file_revision_conflict")
        raise HTTPException(409, "确认文件已变动，请重新确认草稿")
    payload.setdefault("title", str(content.get("title") or "待确认知识卡片"))
    payload.setdefault("body", knowledge._card_body(page))
    payload.setdefault("content_revision", knowledge._content_revision(str(page.get("content") or "")))
    result = card_ledger_store.finalize_material_confirmation(
        session["session_id"], knowledge_id, str(page["path"]), revision, payload
    )
    from .material_drafts import mark_confirmed
    try:
        mark_confirmed(material_id, revision, knowledge_id)
    except derived_store.DraftRevisionConflict:
        # 卡片与索引任务已经原子确认；草稿状态仅用于材料详情展示，不能让
        # 附属标记失败中断已完成的确认结果。
        logger.warning("确认后更新材料草稿状态冲突: %s", material_id)
    knowledge._schedule_vector_repairs()
    return {"materialId": material_id, "knowledgeId": knowledge_id,
            "sessionId": result["session"]["session_id"], "vectorJobId": (result["job"] or {}).get("job_id"),
            "idempotent": bool(result.get("idempotent"))}


def recover_material_confirmations() -> dict:
    """受控恢复确认会话；仅按会话标识定位新写文件，不扫描或迁移历史卡片。"""
    from . import knowledge
    from .stores import card_ledger_store

    completed = rolled_back = 0
    for session in card_ledger_store.pending_confirmations():
        if session["state"] == "preparing":
            page = knowledge.find_card_by_confirmation_session(session["session_id"])
            if page is None:
                card_ledger_store.roll_back_material_confirmation(session["session_id"], "service_interrupted")
                material_id = str(session.get("material_id") or "")
                if material_id:
                    from .material_drafts import unlock_confirmation
                    unlock_confirmation(material_id, str(session["target_revision"]), session["session_id"])
                rolled_back += 1
                continue
            knowledge_id = knowledge._knowledge_id(str(page.get("path") or ""))
            payload = {
                "title": str(page.get("title") or "未命名知识卡片"),
                "body": knowledge._card_body(page), "tags": knowledge._tags(page),
                "content_revision": knowledge._content_revision(str(page.get("content") or "")),
                "rel_path": str(page.get("path") or ""), "folder_id": knowledge._card_folder_id(page),
            }
            card_ledger_store.mark_confirmation_file_committed(session["session_id"], knowledge_id, payload)
            session = {**session, "knowledge_id": knowledge_id, "state": "file_committed",
                       "payload_json": json.dumps(payload, ensure_ascii=False)}
        knowledge_id = str(session.get("knowledge_id") or "")
        try:
            page = knowledge._find(knowledge_id)
            payload = json.loads(session.get("payload_json") or "{}")
            card_revision = knowledge._content_revision(str(page.get("content") or ""))
            expected_revision = str(payload.get("content_revision") or "")
            if not expected_revision or expected_revision != card_revision:
                card_ledger_store.roll_back_material_confirmation(
                    session["session_id"], "confirmed_file_revision_conflict",
                )
                rolled_back += 1
                continue
            payload.setdefault("title", str(page.get("title") or "未命名知识卡片"))
            payload.setdefault("body", knowledge._card_body(page))
            card_ledger_store.finalize_material_confirmation(
                session["session_id"], knowledge_id, str(page["path"]), str(session["target_revision"]), payload
            )
            material_id = str(session.get("material_id") or "")
            if material_id:
                from .material_drafts import mark_confirmed
                try:
                    mark_confirmed(material_id, str(session["target_revision"]), knowledge_id)
                except derived_store.DraftRevisionConflict:
                    logger.warning("恢复确认后更新材料草稿状态冲突: %s", material_id)
            completed += 1
        except Exception as exc:
            logger.warning("确认会话恢复失败 %s: %s", session["session_id"], type(exc).__name__)
    return {"completed": completed, "rolledBack": rolled_back}


def _material_impact(material_id: str, device_scope: str = "global") -> dict:
    """汇总资料被卡片、纠错本和待审草稿引用的影响对象。

    阶段 2：只统计当前设备作用域内的卡片/草稿，跨设备引用不对外呈现。
    """
    from . import knowledge

    cards = knowledge.cards_referencing_material(material_id, device_scope=device_scope)
    active_cards = [card for card in cards if not card["archived"] and not card["recycled"]]
    archived_cards = [card for card in cards if card["archived"] and not card["recycled"]]
    recycled_cards = [card for card in cards if card["recycled"]]
    store = derived_store.DerivedStore.instance()
    corrections = [
        {
            "correctionId": item["id"], "title": item["title"], "status": item["status"],
        }
        for item in store.list_corrections()
        if material_id in (item.get("sourceIds") or [])
    ]
    drafts = []
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
            "draftId": item["owner_id"], "type": content.get("type") or "",
            "status": item["status"],
        })
    return {
        "materialId": material_id,
        "activeKnowledgeCards": active_cards,
        "archivedKnowledgeCards": archived_cards,
        "recycledKnowledgeCards": recycled_cards,
        "activeKnowledgeCardCount": len(active_cards),
        "archivedKnowledgeCardCount": len(archived_cards),
        "recycledKnowledgeCardCount": len(recycled_cards),
        "corrections": corrections,
        "drafts": drafts,
    }


def _version_view(record: dict, device_scope: str = "global") -> dict:
    """版本记录转公开结构，同时让已完成的新版本补齐前代反向指针。"""
    public = ingestion.status_of(record["material_id"], device_scope=device_scope)
    if public is None:
        raise HTTPException(404, "资料不存在")
    public["recycled"] = ingestion.is_recycled(record["material_id"], device_scope=device_scope)
    return public


def mindos_material_impact(material_id: str, request: Request):
    """归档前影响预览：归档不删除卡片，只改变来源展示状态。"""
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    return _material_impact(material_id, device_scope=scope)


def mindos_material_versions(material_id: str, request: Request):
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    records = ingestion.JobStore.instance().list_versions(material_id)
    if records is None:
        raise HTTPException(404, "资料不存在")
    return {"materialId": material_id, "items": [_version_view(record, device_scope=scope) for record in records]}


async def mindos_material_version_upload(
    material_id: str,
    request: Request,
    file: UploadFile = File(...),
    versionNote: str | None = Form(default=None),
    targetFolderId: int | None = Form(default=None),
):
    """上传已有原材料的新版本；不自动改写任何正式卡片来源。

    P15-03：禁止历史版本分叉——只能基于「家族最新版本」上传新版本。若目标资料已被
    更新版本替代（superseded_by 已回填），继续上传会造成版本链分叉（V2 上再挂 V3，
    而 V1 分支也挂出 V3'），返回 409。已回收资料不能作为新版本基线。
    """
    scope = _device_scope_of(request)
    previous = _material_record(material_id, device_scope=scope)
    if previous.get("superseded_by_material_id"):
        raise HTTPException(
            409,
            "该资料已被更新版本替代，请基于最新版本上传新版本（禁止历史版本分叉）",
        )
    if ingestion.is_recycled(material_id, device_scope=scope):
        raise HTTPException(409, "已回收的资料不能上传新版本，请先恢复后再操作")
    folder_id = previous.get("folder_id") if targetFolderId is None else targetFolderId
    safe_name, file_type, destination = await _receive_upload(file, folder_id)
    new_material_id = ingestion.new_material_id()
    from .stores.chat_import_store import ChatImportStore
    chat_privacy = ChatImportStore()
    if material_id in chat_privacy.protected_ids():
        # A new version inherits the privacy boundary, not its predecessor's grant.
        chat_privacy.protect(new_material_id, scope)
    record = ingestion.start_ingestion(
        new_material_id, safe_name, file_type, str(destination), folder_id=folder_id,
        material_family_id=previous["material_family_id"],
        supersedes_material_id=material_id,
        version_note=versionNote,
        device_scope=scope,
    )
    return {
        "oldMaterialId": material_id,
        "newMaterialId": new_material_id,
        "materialFamilyId": record["materialFamilyId"],
        "versionNumber": record["versionNumber"],
        "status": record["status"],
    }


def mindos_material_version_impact(material_id: str, request: Request):
    """新版本可用后，返回其前代资料的关联对象，供用户显式处理来源。"""
    scope = _device_scope_of(request)
    current = _material_record(material_id, device_scope=scope)
    if not current.get("supersedes_material_id"):
        raise HTTPException(400, "该资料不是某个原材料的新版本")
    status = ingestion.status_of(material_id, device_scope=scope)
    if status is None:
        raise HTTPException(404, "资料不存在")
    if status["status"] != ingestion.ST_AVAILABLE:
        return {
            "materialId": material_id,
            "oldMaterialId": current["supersedes_material_id"],
            "ready": False,
            "status": status["status"],
            "activeKnowledgeCards": [], "archivedKnowledgeCards": [], "recycledKnowledgeCards": [],
            "activeKnowledgeCardCount": 0, "archivedKnowledgeCardCount": 0, "recycledKnowledgeCardCount": 0,
            "corrections": [], "drafts": [],
        }
    return {
        **_material_impact(current["supersedes_material_id"], device_scope=scope),
        "materialId": material_id,
        "oldMaterialId": current["supersedes_material_id"],
        "ready": True,
        "status": status["status"],
    }


def mindos_material_file(material_id: str, request: Request):
    scope = _device_scope_of(request)
    record = _material_record(material_id, device_scope=scope)
    target = Path(record["source_path"]).resolve()
    watch_root = Path(WATCH_FOLDER).resolve()
    if not target.is_relative_to(watch_root) or not target.is_file():
        raise HTTPException(404, "原始文件不存在")
    if target.suffix.lower() not in (DOC_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS):
        raise HTTPException(404, "暂不支持预览此文件")
    # FileResponse 内建 Range 支持（Accept-Ranges/206），音频播放器可 seek 到指定时刻。
    return FileResponse(str(target), filename=record["file_name"], content_disposition_type="inline")


def mindos_material_part_file(material_id: str, part_id: str, request: Request):
    """受控读取派生图片：只允许该资料的 image part，且必须落在受控派生目录内。

    不支持任意路径传入；part 归属 / artifact_key 越界一律 404。
    """
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    store = derived_store.DerivedStore.instance()
    part = store.get_part(material_id, part_id)
    if part is None or part["part_type"] != "image" or not part.get("artifact_key"):
        raise HTTPException(404, "图片不存在")
    file_path = store.image_file_path(material_id, part["artifact_key"])
    if file_path is None or not file_path.is_file():
        raise HTTPException(404, "图片文件不存在")
    mime = (part.get("image_meta") or {}).get("mime") or "application/octet-stream"
    return FileResponse(
        str(file_path),
        media_type=mime,
        filename=Path(file_path).name,
        content_disposition_type="inline",
    )


def mindos_material_summary(material_id: str, request: Request):
    """返回资料当前自动摘要的状态与文本（供前端轮询 / 重试后刷新）。"""
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    return {"materialId": material_id, **derived_svc.summary_of(material_id)}


def mindos_material_summary_retry(material_id: str, request: Request = None):
    """重新生成摘要（force=True，忽略输入 hash 幂等）；模型不可用时不阻塞返回。"""
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    source_path = ingestion.source_path_of(material_id, device_scope=scope)
    if not source_path:
        raise HTTPException(404, "资料不存在")
    derived_svc.submit_summary(material_id, source_path, force=True)
    return {"materialId": material_id, **derived_svc.summary_of(material_id)}


@router.post("/uploads/{material_id}/retry")
def mindos_upload_retry(
    material_id: str,
    request: Request,
    expectedSnapshotVersion: Annotated[int | None, Form()] = None,
):
    """失败后重试（阶段A-A5）：重新入队 material_job。

    ``expectedSnapshotVersion`` 为正文快照乐观锁；与当前可见版本不一致返回 409 并
    附带 ``currentSnapshotVersion`` 供前端提示重新加载（前端未传则跳过校验，保持兼容）。
    """
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    try:
        record = ingestion.retry_ingestion(
            material_id, expected_snapshot_version=expectedSnapshotVersion
        )
    except ingestion.SnapshotVersionConflict as e:
        raise HTTPException(
            409,
            {"detail": "内容已在他处更新，请重新加载后重试", "currentSnapshotVersion": e.current_version},
        )
    except ingestion.RetryNotAllowed as e:
        raise HTTPException(409, str(e))
    if record is None:
        raise HTTPException(404, "资料不存在")
    return record


@router.post("/uploads/{material_id}/resume")
def mindos_upload_resume(material_id: str, request: Request):
    """继续处理被暂停（paused）的任务：重新入队并生成一次性 resume_token（§9.1）。"""
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    try:
        record = ingestion.resume_ingestion(material_id)
    except ingestion.RetryNotAllowed as e:
        raise HTTPException(409, str(e))
    if record is None:
        raise HTTPException(404, "资料不存在")
    return record


def mindos_material_processing(material_id: str, request: Request):
    """返回资料的处理任务视图（§9.1 GET /processing）：任务状态、阶段、失败码与可执行动作。

    区别于 GET /materials/{id}：只专注处理链路（material_job + 快照版本），
    供前端在详情页按状态渲染「继续/重试/重新生成/取消」按钮。
    """
    scope = _device_scope_of(request)
    view = ingestion.processing_view(material_id, device_scope=scope)
    if view is None:
        raise HTTPException(404, "资料不存在")
    return view


def mindos_material_regenerate(material_id: str, req: RegenerateRequest, request: Request = None):
    """重生成指定派生项；不得重传文件。

    item=summary 重生成摘要；item=analysis/parse 同时重生成摘要、标签、实体、关系；
    item=draft 重生成草稿。
    模型不可用时不阻塞返回。
    """
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    source_path = ingestion.source_path_of(material_id, device_scope=scope)
    if not source_path:
        raise HTTPException(404, "资料不存在")
    item = (req.item or "").strip().lower()
    if item == "summary":
        derived_svc.submit_summary(material_id, source_path, force=True)
        return {"materialId": material_id, "item": "summary", **derived_svc.summary_of(material_id)}
    if item in {"analysis", "parse"}:
        return {"materialId": material_id, "item": "parse", **derived_svc.reparse_all(material_id, source_path)}
    if item == "draft":
        from .material_drafts import draft_of, ensure_minimal_draft, submit_generation
        draft = ensure_minimal_draft(material_id)
        if draft.get("userEdited"):
            raise HTTPException(409, {
                "detail": "草稿已有用户编辑，请先保留当前版本或显式丢弃编辑后再重新生成",
                "currentRevision": draft.get("revision"),
            })
        accepted = submit_generation(material_id, source_path, force=True)
        if not accepted:
            raise HTTPException(503, "草稿生成队列暂不可用，请稍后重试")
        return {"materialId": material_id, "item": "draft", **draft_of(material_id)}
    raise HTTPException(400, "仅支持重生成 summary、analysis/parse 或 draft")


def mindos_material_tags(material_id: str, req: TagRequest, request: Request):
    """Add or remove tags on a material (metadata only, no file content changes)."""
    scope = _device_scope_of(request)
    if not ingestion.source_path_of(material_id, device_scope=scope):
        raise HTTPException(404, "资料不存在")
    if req.action not in ("add", "remove"):
        raise HTTPException(400, "action 必须是 add 或 remove")
    tags = [t.strip()[:64] for t in req.tags if t.strip()]
    if not tags:
        raise HTTPException(400, "标签不能为空")
    updated = ingestion.set_material_tags(material_id, tags, req.action)
    return {"tags": updated}


def mindos_material_analysis(material_id: str, request: Request = None):
    """聚合返回摘要 / 标签候选 / 实体及其状态；缺失或失败时触发后台补算。

    派生生成在后台池执行（不阻塞本请求），前端据各 status 决定轮询或直接展示。
    """
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    source_path = ingestion.source_path_of(material_id, device_scope=scope)
    if not source_path:
        raise HTTPException(404, "资料不存在")
    derived_svc.refresh_analysis(material_id, source_path)
    return {"materialId": material_id, **derived_svc.analysis_of(material_id)}


def mindos_material_tag_suggestions(material_id: str, request: Request = None):
    """读取缓存候选标签（异步生成）；缺失/失败时触发后台重算并返回当前状态。

    候选仅为建议，用户逐条确认后才写入正式标签（见 confirm 接口）；同一输入
    hash 只生成一次，重试不会产生重复候选。
    """
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    source_path = ingestion.source_path_of(material_id, device_scope=scope)
    if not source_path:
        raise HTTPException(404, "资料不存在")
    derived_svc.refresh_analysis(material_id, source_path)
    return {"materialId": material_id, **derived_svc.tag_suggestions_of(material_id)}


def mindos_material_tag_suggestion_confirm(material_id: str, suggestion_id: str, request: Request = None):
    """确认某候选标签为正式标签（可审计、幂等）。

    suggestionId 必须存在于当前候选列表；确认后写入正式标签并把该候选标记为
    confirmed（候选保留在列表中供 UI 展示已确认态），同时写入审计日志。
    """
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    view = derived_svc.tag_suggestions_of(material_id)
    if view.get("status") != "ok":
        raise HTTPException(409, "候选标签尚未生成，请稍后重试")
    candidate = next(
        (it for it in view.get("items", []) if it.get("suggestionId") == suggestion_id),
        None,
    )
    if candidate is None:
        raise HTTPException(404, "候选标签不存在或已过期")
    name = candidate.get("name") or ""
    if not name:
        raise HTTPException(400, "候选标签内容为空")
    # 幂等：已确认直接返回当前正式标签，不重复写库/审计
    if candidate.get("confirmed"):
        existing = ingestion.material_tags(material_id, device_scope=scope)
        return {"suggestionId": suggestion_id, "name": name, "confirmed": True, "tags": existing}
    updated = ingestion.set_material_tags(material_id, [name], "add")
    derived_svc.confirm_tag_suggestion(material_id, suggestion_id)
    try:
        from annotations import add_audit
        sp = ingestion.source_path_of(material_id, device_scope=scope)
        add_audit(
            "material.tag_confirm",
            targets=[sp] if sp else (),
            payload={"materialId": material_id, "suggestionId": suggestion_id, "tag": name},
        )
    except Exception as exc:
        logger.warning("候选标签确认审计失败 %s: %s", material_id, exc)
    return {"suggestionId": suggestion_id, "name": name, "confirmed": True, "tags": updated}


def mindos_material_move(material_id: str, req: MaterialMoveRequest, request: Request):
    """移动资料到目录（folderId 为目录树 ID；null 移回未分类，不移动物理文件）。"""
    scope = _device_scope_of(request)
    _material_record(material_id, device_scope=scope)
    if req.folderId is not None:
        _require_folder(req.folderId)
    try:
        record = ingestion.JobStore.instance().update_material_folder_id(material_id, req.folderId)
    except FolderError as e:
        raise HTTPException(getattr(e, "status_code", 400), str(e))
    if record is None:
        raise HTTPException(404, "资料不存在")
    return {"materialId": material_id, "folderId": record["folder_id"], "folder": record["folder"]}


def mindos_folder_list(scope: str | None = Query(default=None)):
    """返回 RAW 目录树节点（扁平数组，前端按其组装树或直接展开）。"""
    return {"items": ingestion.list_folder_nodes(scope or "RAW")}


def mindos_folder_create(req: FolderCreateRequest):
    """创建目录节点（parentId 为空创建根目录；同 scope+parent 下名称唯一）。"""
    try:
        node = ingestion.JobStore.instance().create_folder_node(req.scope, req.name, req.parentId)
    except FolderError as e:
        raise HTTPException(getattr(e, "status_code", 400), str(e))
    return node


def mindos_folder_rename(folder_id: int, req: FolderRenameRequest):
    """重命名目录（仅改名称，不改变层级结构）。"""
    try:
        node = ingestion.JobStore.instance().rename_folder_node(folder_id, req.name)
    except FolderError as e:
        raise HTTPException(getattr(e, "status_code", 400), str(e))
    return node


def mindos_folder_move(folder_id: int, req: FolderMoveRequest):
    """移动目录到新父级（parentId 为空移回根）；禁止移动到自身或后代。"""
    try:
        node = ingestion.JobStore.instance().move_folder_node(folder_id, req.parentId)
    except FolderError as e:
        raise HTTPException(getattr(e, "status_code", 400), str(e))
    return node


def mindos_folder_delete(
    folder_id: int,
    target_folder_id: Annotated[int | None, Query(alias="targetFolderId")] = None,
    move_to_root: Annotated[bool, Query(alias="moveToRoot")] = False,
):
    """删除目录：必须明确迁移策略（targetFolderId 或 moveToRoot=true）。

    目录自身归类的材料迁往目标；子目录整体提升到目标下；不接触任何原材料文件。
    KNOWLEDGE 目录被删时，归入该目录的知识卡片同步迁往目标（或知识根目录）。

    跨存储原子性：知识卡片迁移（Wiki）与目录删除（SQLite）为两阶段流程——
    先迁移卡片、后删除目录；任一步失败即补偿回滚，Wiki 写入失败不会留下悬空卡片
    （目录不提交删除），目录删除失败会将卡片归属恢复原状（可安全重试）。
    RAW 目录删除固定返回 movedCards: 0，保证响应结构稳定。
    """
    try:
        node = ingestion.JobStore.instance().folder_node(folder_id)
        if node is None:
            raise FolderNotFoundError("目录不存在")
        if node["scope"] == SCOPE_KNOWLEDGE:
            result = _delete_knowledge_folder(folder_id, target_folder_id, move_to_root, node)
        else:
            result = ingestion.JobStore.instance().delete_folder_node(
                folder_id, target_folder_id, move_to_root
            )
            result["movedCards"] = 0
    except FolderError as e:
        raise HTTPException(getattr(e, "status_code", 400), str(e))
    return {"folderId": folder_id, **result}


def _delete_knowledge_folder(
    folder_id: int,
    target_folder_id: int | None,
    move_to_root: bool,
    node: dict,
) -> dict:
    """KNOWLEDGE 目录删除：先迁移知识卡片、后删目录的两阶段流程（跨存储补偿）。

    - 预读待迁移卡片原始内容（含 frontmatter），供失败补偿回滚；
    - 目标为「Resources」根且被删目录本身就是 Resources 根时，先改名腾位 → 新建替代根
      → 再迁移卡片，保证卡片绝不指向即将删除的悬空 ID；
    - 卡片迁移任一张失败 → 已迁移卡片写回原内容、目录不删，返回 503（可重试）；
    - 目录删除失败 → 全部卡片写回原内容（资源根场景同时回滚新建节点与改名），
      目录保留，再次发起删除可正常完成。
    """
    store = ingestion.JobStore.instance()
    # 延迟导入避免模块级循环依赖：knowledge 依赖 ingestion，不依赖 uploads。
    from . import knowledge as knowledge_svc

    # 与 store 层一致的入参校验：未指定迁移目标时直接拒绝，避免无谓迁移。
    if target_folder_id is None and not move_to_root:
        raise FolderError("删除目录必须指定迁移目标（targetFolderId 或 moveToRoot）")
    if target_folder_id == folder_id:
        raise FolderError("迁移目标不能是目录自身")

    records = knowledge_svc.collect_cards_in_folder(folder_id)
    if not records:
        # 无卡片需迁移：不触碰 Wiki，直接删目录。
        result = store.delete_folder_node(folder_id, target_folder_id, move_to_root)
        result["movedCards"] = 0
        return result

    # ---- 目标解析（含 Resources 根被删的特殊处理）----
    renamed_root = None  # 替换方案：旧 Resources 根改名后的节点
    new_root_id = None   # 替换方案：新建的替代 Resources 根
    is_resources_root = node["parentId"] is None and node["name"] == "Resources"
    if is_resources_root and target_folder_id is None:
        # 替代 Resources 根必须先可用，再迁移卡片：把旧根临时改名腾位 → 新建 Resources。
        renamed_root = store.rename_folder_node(folder_id, f"删除中_{int(time.time())}")
        new_root_id = store.create_folder_node(SCOPE_KNOWLEDGE, "Resources")["id"]
        target_id = new_root_id
    elif target_folder_id is not None:
        target_id = target_folder_id
    else:
        target_id = knowledge_svc.ensure_resources_root_id()

    # ---- 阶段一：迁移知识卡片（Wiki）----
    migrated: list[dict] = []
    try:
        for record in records:
            knowledge_svc.write_card_folder(record["path"], record["content"], target_id)
            migrated.append(record)
    except Exception as exc:
        # Wiki 写入失败：目录树不提交删除；已迁移卡片补偿回原内容；资源根场景回滚新节点与改名。
        if migrated:
            knowledge_svc.restore_card_contents(migrated)
        _rollback_resources_swap(store, renamed_root, new_root_id)
        raise HTTPException(503, f"知识卡片迁移失败，目录未删除，请稍后重试：{exc}")

    # ---- 阶段二：删除目录树（SQLite）----
    try:
        result = store.delete_folder_node(folder_id, target_folder_id, move_to_root)
    except FolderError:
        # 目录删除失败（目录仍在）：卡片写回原内容归属；资源根场景回滚新节点与改名。
        knowledge_svc.restore_card_contents(records)
        _rollback_resources_swap(store, renamed_root, new_root_id)
        raise
    result["movedCards"] = len(migrated)
    return result


def _rollback_resources_swap(store, renamed_root: dict | None, new_root_id: int | None):
    """回滚 Resources 根替换方案：删除新建的替代根、把旧根改回「Resources」。

    任一步失败仅记录日志（尽量恢复，不掩盖主流程已上报的错误）。
    """
    if new_root_id is not None:
        try:
            store.delete_folder_node(new_root_id, move_to_root=True)
        except FolderError:
            logger.warning("回滚失败：清理替代 Resources 根 %s", new_root_id, exc_info=True)
    if renamed_root is not None:
        try:
            store.rename_folder_node(renamed_root["id"], "Resources")
        except FolderError:
            logger.warning("回滚失败：Resources 根改名恢复 %s", renamed_root["id"], exc_info=True)


def mindos_upload_cancel(material_id: str):
    """取消上传/处理任务：标记失败（原始文件保留），前端停止轮询。

    任务可能仍由后台 worker 处理，此处先标记 MindOS 对外可见状态为失败，
    并尽力取消仍处于排队/暂停的 ``material_job``（processing 中的任务由
    worker 在写快照前二次比对放弃结果），避免与正在执行的解析竞争。
    """
    record = ingestion.JobStore.instance().get(material_id)
    if record is None:
        raise HTTPException(404, "资料不存在")
    ingestion.JobStore.instance().mark_canceled(material_id)
    ingestion.cancel_material_job(material_id)
    # 标记失败状态（errorMessage 可读），前端据此停止轮询并展示重试入口
    return ingestion.public_record(record, ingestion.ST_FAILED, "用户已取消上传/处理")


def mindos_material_queue_remove(material_id: str):
    """移出尚未完成的导入队列项，不影响正在处理或已经完成的资料。"""
    try:
        removed = ingestion.remove_from_queue(material_id)
    except ingestion.RetryNotAllowed as exc:
        raise HTTPException(409, str(exc))
    if not removed:
        raise HTTPException(404, "资料不存在")
    return {"materialId": material_id, "removed": True}
