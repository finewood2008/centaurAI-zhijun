"""Versioned file references and durable chat imports (loopback/CSRF guarded)."""
from __future__ import annotations

import hashlib
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from . import chat_imports as svc
from .stores.chat_import_store import ChatImportStore
from .uploads import _device_scope_of
from .zhijun.reply_assistance import ReplyInput


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialRef(Strict):
    materialId: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)


class ImportFile(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(default=0, ge=0, le=200 * 1024 * 1024)
    materialId: str | None = Field(default=None, max_length=100)
    version: int | None = Field(default=None, ge=1)


class ImportCreate(Strict):
    requestId: str = Field(min_length=16, max_length=100)
    content: str = Field(default="", max_length=4000)
    files: list[ImportFile] = Field(min_length=1, max_length=5)
    localOnly: bool = False
    replyAssistance: ReplyInput | None = None


class ReferenceSelection(Strict):
    refs: list[MaterialRef] = Field(default_factory=list, max_length=5)
    localOnly: bool = False


class Consent(ReferenceSelection):
    serviceId: str | None = None


class FileFailure(Strict):
    detail: str = Field(default="上传中断，请重试", max_length=300)


def batch_for(conversation_id: str, batch_id: str, request: Request):
    store = svc.require_conversation(conversation_id, _device_scope_of(request))
    batch = store.get(batch_id)
    if not batch or batch["conversation_id"] != conversation_id:
        raise svc.error("IMPORT_NOT_FOUND", "导入记录不存在", 404)
    return store, batch


def create_import(conversation_id: str, req: ImportCreate, request: Request):
    scope = _device_scope_of(request)
    store = svc.require_conversation(conversation_id, scope)
    from .zhijun.reply_assistance import resolve_input
    from .zhijun.routing import Router
    from .stores.ontology_store import OntologyStore
    # Resolve before saving; attaching files must not erase an assisted text's ancestry.
    existing = next((b for b in store.batches(conversation_id) if b["request_key"] == req.requestId), None)
    expression, sources = resolve_input(Router(OntologyStore.instance(), store.conversations, conversation_id),
        req.replyAssistance, req.content.strip(), retry_user_id=existing["message_id"] if existing else None)
    if len({f.id for f in req.files}) != len(req.files):
        raise svc.error("BAD_ATTACHMENTS", "文件标识重复", 400)
    for item in req.files:
        if item.materialId:
            record = svc.require_material(item.materialId, scope)
            if item.version != record["versionNumber"]:
                raise svc.error("ATTACHMENT_VERSION_CHANGED", "文件版本已变化，请重新选择")
            item.name = record["fileName"]
            store.protect(item.materialId, scope)
        else:
            from .validation import validate_import
            if not item.size or validate_import(item.name, item.size)["status"] != "ok":
                raise svc.error("BAD_ATTACHMENTS", "文件为空、不支持或超过大小限制", 400)
    batch = store.create(conversation_id, req.requestId, req.content.strip(), [f.model_dump() for f in req.files], req.localOnly,
                         input_meta={"replyAssistance": expression, "routingSources": sources} if expression else None)
    return svc.batch_view(batch, store)


async def upload_import_file(conversation_id: str, batch_id: str, file_id: str, request: Request, file: UploadFile = File(...)):
    from .services import ingestion
    from .uploads import _receive_upload

    store, batch = batch_for(conversation_id, batch_id, request)
    item = next((f for f in batch["files"] if f["id"] == file_id), None)
    if item is None:
        raise svc.error("IMPORT_FILE_NOT_FOUND", "文件记录不存在", 404)
    if item["material_id"]:
        return svc.file_view(item, _device_scope_of(request))
    if batch["state"] == "replying":
        raise svc.error("IMPORT_BUSY", "这一批文件正在生成反馈")
    if file.filename != item["name"]:
        raise svc.error("FILE_MISMATCH", "请选择原来的文件；新文件请另行发送", 400)
    store.file_update(file_id, "uploading")
    destination = None
    try:
        name, kind, destination = await _receive_upload(file, None)
        if destination.stat().st_size != item["size"]:
            raise svc.error("FILE_MISMATCH", "文件大小已变化，请作为新文件发送", 400)
        digest = hashlib.sha256()
        with destination.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        scope = _device_scope_of(request)
        with svc._upload_lock:
            record = svc.find_duplicate(store, scope, digest.hexdigest(), item["size"])
            if record is None:
                material_id = ingestion.new_material_id()
                # Protect BEFORE a worker can read/index/extract the material.
                store.protect(material_id, scope, digest.hexdigest())
                record = ingestion.start_ingestion(material_id, name, kind, str(destination), device_scope=scope)
                destination = None  # now owned by the material lifecycle
            store.file_update(file_id, "saved", material_id=record["materialId"], version=record["versionNumber"])
            if batch["state"] == "complete":
                store.update(batch_id, "uploading")
    except Exception:
        store.file_update(file_id, "failed", error="上传未完成，请重新选择这个文件重试")
        raise
    finally:
        if destination is not None:
            destination.unlink(missing_ok=True)  # only this request's unused upload
    return svc.file_view(next(f for f in store.get(batch_id)["files"] if f["id"] == file_id), _device_scope_of(request))


def fail_file(conversation_id: str, batch_id: str, file_id: str, req: FileFailure, request: Request):
    store, batch = batch_for(conversation_id, batch_id, request)
    item = next((f for f in batch["files"] if f["id"] == file_id), None)
    if not item:
        raise svc.error("IMPORT_FILE_NOT_FOUND", "文件记录不存在", 404)
    if not item["material_id"]:
        store.file_update(file_id, "failed", error=req.detail)
    return {"ok": True}


def seal_import(conversation_id: str, batch_id: str, request: Request):
    store, batch = batch_for(conversation_id, batch_id, request)
    if batch["state"] not in {"complete", "replying"}:
        for item in batch["files"]:
            if not item["material_id"] and item["state"] in {"pending", "uploading"}:
                store.file_update(item["id"], "failed", error="上传未完成，请重新选择这个文件")
        refs = svc.unique_refs([{"materialId": f["material_id"], "version": f["version"]} for f in batch["files"] if f["material_id"]])
        store.select(conversation_id, refs, bool(batch["local_only"]))
        store.update(batch_id, "queued")
    return svc.batch_view(store.get(batch_id), store)


def retry_file(conversation_id: str, batch_id: str, file_id: str, request: Request):
    from .uploads import mindos_upload_resume, mindos_upload_retry
    store, batch = batch_for(conversation_id, batch_id, request)
    if batch["state"] == "replying":
        raise svc.error("IMPORT_BUSY", "反馈正在生成中")
    item = next((f for f in svc.batch_view(batch, store)["files"] if f["id"] == file_id), None)
    if not item or not item["materialId"]:
        raise svc.error("IMPORT_FILE_NOT_FOUND", "请重新选择并上传原文件", 404)
    if item["state"] == "paused":
        mindos_upload_resume(item["materialId"], request)
    elif item["state"] in {"failed", "empty"}:
        mindos_upload_retry(item["materialId"], request=request)
    else:
        raise svc.error("RETRY_NOT_ALLOWED", "这个文件不需要重试")
    store.update(batch_id, "queued")
    return svc.batch_view(store.get(batch_id), store)


def retry_import(conversation_id: str, batch_id: str, request: Request):
    from .uploads import mindos_upload_resume, mindos_upload_retry

    store, batch = batch_for(conversation_id, batch_id, request)
    if batch["state"] == "replying":
        raise svc.error("IMPORT_BUSY", "反馈正在生成中")
    if batch["state"] == "complete":
        return svc.batch_view(batch, store)
    for item in svc.batch_view(batch, store)["files"]:
        if item["materialId"] and item["state"] == "paused":
            mindos_upload_resume(item["materialId"], request)
        elif item["materialId"] and item["state"] in {"failed", "empty"}:
            mindos_upload_retry(item["materialId"], request=request)
    store.update(batch_id, "queued")
    return svc.batch_view(store.get(batch_id), store)


def get_imports(conversation_id: str, request: Request):
    store = svc.require_conversation(conversation_id, _device_scope_of(request))
    try:
        service = svc.service_info()
    except Exception:
        service = None
    return {"items": [svc.batch_view(b, store) for b in store.batches(conversation_id)],
            "selection": store.selection(conversation_id), "service": service}


def set_references(conversation_id: str, req: ReferenceSelection, request: Request):
    store = svc.require_conversation(conversation_id, _device_scope_of(request))
    refs = [r.model_dump() for r in req.refs]
    svc.validate_refs(refs, _device_scope_of(request))
    # References must be attached in this conversation, not arbitrary client IDs.
    known = {(r["materialId"], r["version"]) for r in store.refs(conversation_id)}
    if any((r["materialId"], r["version"]) not in known for r in refs):
        raise svc.error("ATTACHMENT_NOT_LINKED", "请先通过选择已有资料把文件加入对话", 400)
    store.select(conversation_id, refs, req.localOnly)
    return store.selection(conversation_id)


def grant_consent(conversation_id: str, req: Consent, request: Request):
    store = svc.require_conversation(conversation_id, _device_scope_of(request))
    refs = [r.model_dump() for r in req.refs]
    svc.validate_refs(refs, _device_scope_of(request))
    known = {(r["materialId"], r["version"]) for r in store.refs(conversation_id)}
    if not refs or any((r["materialId"], r["version"]) not in known for r in refs):
        raise svc.error("ATTACHMENT_NOT_LINKED", "文件不在当前对话中", 400)
    if not req.localOnly:
        for ref in refs:
            svc.read_ref(ref, _device_scope_of(request))
        service = svc.service_info()
        if not service["external"] or service["id"] != req.serviceId:
            raise svc.error("SERVICE_CHANGED", "模型服务已变化，请重新确认")
        store.grant(refs, service["id"])
    store.select(conversation_id, refs, req.localOnly)
    for batch in store.batches(conversation_id):
        if batch["state"] == "consent" and any(f["material_id"] in {r["materialId"] for r in refs} for f in batch["files"]):
            store.update(batch["id"], "queued", local_only=req.localOnly)
    return {"ok": True}


def preview(conversation_id: str, material_id: str, version: int, request: Request, offset: int = 0):
    store = svc.require_conversation(conversation_id, _device_scope_of(request))
    ref = {"materialId": material_id, "version": version}
    if ref not in store.refs(conversation_id):
        raise svc.error("ATTACHMENT_NOT_LINKED", "文件不在当前对话中", 404)
    record, _, text = svc.read_ref(ref, _device_scope_of(request))
    offset = max(0, min(offset, len(text)))
    return {"name": record["fileName"], "text": text[offset:offset + 12000], "offset": offset,
            "totalChars": len(text), "hasMore": offset + 12000 < len(text)}


def build_router(guard):
    router = APIRouter(prefix="/api/mindos/conversations", tags=["chat-imports"])
    writes = [Depends(guard)]
    router.add_api_route("/{conversation_id}/imports", create_import, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/imports", get_imports, methods=["GET"])
    router.add_api_route("/{conversation_id}/imports/{batch_id}/files/{file_id}", upload_import_file, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/imports/{batch_id}/files/{file_id}/failed", fail_file, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/imports/{batch_id}/files/{file_id}/retry", retry_file, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/imports/{batch_id}/seal", seal_import, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/imports/{batch_id}/retry", retry_import, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/references", set_references, methods=["PUT"], dependencies=writes)
    router.add_api_route("/{conversation_id}/file-consent", grant_consent, methods=["POST"], dependencies=writes)
    router.add_api_route("/{conversation_id}/files/{material_id}/preview", preview, methods=["GET"])
    return router
