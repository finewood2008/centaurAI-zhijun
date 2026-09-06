"""Chat attachment operations through the canonical, scoped DE material lifecycle."""
import os

from .capabilities import require, CapabilityError


def upload_import(conversation_id, batch_id, file_id, request):
    from mindos import chat_imports as svc
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
        # DE protects the material before parsing/enqueueing. No filesystem path crosses this port.
        record = require().call("materials.ingest", {"uploadId": upload_id, "attachmentPurpose": "chat"})
        if not record.get("materialId") or type(record.get("versionNumber")) is not int:
            raise CapabilityError("CAPABILITY_MATERIAL_CONTRACT", 502)
        store.protect(record["materialId"], scope)
        store.file_update(file_id, "saved", material_id=record["materialId"], version=record["versionNumber"])
        if batch["state"] == "complete":
            store.update(batch_id, "uploading")
    except Exception:
        store.file_update(file_id, "failed", error="上传未完成，请重新选择这个文件重试")
        raise
    return svc.file_view(next(entry for entry in store.get(batch_id)["files"] if entry["id"] == file_id), scope)


def resume_or_retry(material_id, action, request):
    if action not in {"resume", "retry"}:
        raise ValueError("Unknown material action")
    if os.environ.get("ZHIJUN_WORKSPACE_ID"):
        from mindos.domain_scope import _device_scope_of
        _device_scope_of(request)
        return require().call("materials." + action, {"materialId": material_id})
    from mindos.uploads import mindos_upload_resume, mindos_upload_retry
    return (mindos_upload_resume if action == "resume" else mindos_upload_retry)(material_id, request=request)
