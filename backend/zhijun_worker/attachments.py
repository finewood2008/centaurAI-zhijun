"""Chat attachment operations through the canonical, scoped DE material lifecycle."""
import base64
import hashlib
import os

from .capabilities import require, CapabilityError


def upload_import(conversation_id, batch_id, file_id, request):
    from mindos import chat_imports as svc
    svc.require_import_enabled()
    from mindos.chat_import_routes import batch_for
    from mindos.domain_scope import _device_scope_of
    scope = _device_scope_of(request)
    store, batch = batch_for(conversation_id, batch_id, request)
    item = next((entry for entry in batch["files"] if entry["id"] == file_id), None)
    if item is None:
        raise svc.error("IMPORT_FILE_NOT_FOUND", "文件记录不存在", 404)
    if item["material_id"]:
        return svc.file_view(item, scope)
    if batch["state"] == "replying":
        raise svc.error("IMPORT_BUSY", "这一批文件正在生成反馈")
    descriptor = getattr(request.state, "zhijun_uploads", None)
    if not descriptor:
        raise svc.error("FILE_REQUIRED", "请选择文件", 422)
    upload_id = descriptor["files"][0]["uploadId"]
    metadata = require().call("uploads.describe", {"id": upload_id})
    if metadata.get("fileName") != item["name"] or metadata.get("size") != item["size"]:
        raise svc.error("FILE_MISMATCH", "文件名称或大小已变化，请作为新文件发送", 400)
    store.file_update(file_id, "uploading")
    try:
        # The workspace capability transports only the already-attached upload
        # bytes. The material operation itself is the real RAG V2 application
        # endpoint; no materials.ingest fallback is permitted.
        content = bytearray()
        offset = 0
        while offset < item["size"]:
            part = require().call("uploads.read", {"id": upload_id, "offset": offset,
                                                   "limit": min(524288, item["size"] - offset)})
            if (part.get("id") != upload_id or part.get("offset") != offset or part.get("size") != item["size"]):
                raise CapabilityError("CAPABILITY_UPLOAD_CONTRACT", 502)
            try:
                chunk = base64.b64decode(part.get("data", ""), validate=True)
            except (ValueError, TypeError):
                raise CapabilityError("CAPABILITY_UPLOAD_CONTRACT", 502) from None
            if not chunk or len(content) + len(chunk) > item["size"]:
                raise CapabilityError("CAPABILITY_UPLOAD_CONTRACT", 502)
            content.extend(chunk)
            offset += len(chunk)
            if bool(part.get("hasMore")) != (offset < item["size"]):
                raise CapabilityError("CAPABILITY_UPLOAD_CONTRACT", 502)
        from .data_agent_rag_v2 import configured_client
        idempotency_key = "zj-upload-" + hashlib.sha256(
            batch_id.encode() + b"\0" + file_id.encode() + b"\0" + bytes(content)
        ).hexdigest()[:48]
        accepted = configured_client().upload_bytes(
            item["name"], bytes(content), idempotency_key=idempotency_key, title=item["name"]
        )
        if (not accepted.get("materialId") or type(accepted.get("materialVersion")) is not int
                or not accepted.get("jobId") or not accepted.get("statusUrl")):
            raise CapabilityError("RAG_V2_UPLOAD_CONTRACT", 502)
        store.protect(accepted["materialId"], scope)
        store.file_update(file_id, "reading", material_id=accepted["materialId"],
                          version=accepted["materialVersion"], job_id=accepted["jobId"],
                          status_url=accepted["statusUrl"])
        if batch["state"] == "complete":
            store.update(batch_id, "uploading")
    except Exception as exc:
        store.file_update(file_id, "failed", error="上传未完成，请重新选择这个文件重试")
        from .data_agent_rag_v2 import DataAgentRagV2Error
        if isinstance(exc, DataAgentRagV2Error):
            from fastapi import HTTPException
            detail = {"code": exc.code, "detail": "Data Agent 暂时无法接收文件",
                      "retryable": exc.retryable}
            if exc.trace_id:
                detail["traceId"] = exc.trace_id
            raise HTTPException(exc.status or 503, detail) from None
        raise
    return svc.file_view(next(entry for entry in store.get(batch_id)["files"] if entry["id"] == file_id), scope)


def resume_or_retry(material_id, action, request):
    if action not in {"resume", "retry"}:
        raise ValueError("Unknown material action")
    if os.environ.get("ZHIJUN_WORKSPACE_ID"):
        from mindos.chat_imports import require_import_enabled
        require_import_enabled()
    from mindos.uploads import mindos_upload_resume, mindos_upload_retry
    return (mindos_upload_resume if action == "resume" else mindos_upload_retry)(material_id, request=request)
