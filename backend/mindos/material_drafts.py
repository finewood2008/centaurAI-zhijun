"""材料知识卡片草稿：最小草稿、异步扩写与 revision/CAS 编辑。"""
from __future__ import annotations

import hashlib
import json
import logging
import time

from .derived import KIND_GENERATED_DRAFT, _call_llm, _generator_name
from .ollama_material_scheduler import PRIORITY_MANUAL_REGENERATE, PRIORITY_SUMMARY_ENTITIES, _scheduler
from .runtime_config_provider import get_provider
from .material_snapshot_saga import MaterialSnapshotSaga
from .stores import derived_store
from .stores.material_pipeline_store import MaterialPipelineStore

logger = logging.getLogger(__name__)

# ``pending`` is persisted before the in-memory scheduler runs. A process restart
# drops the queued callable, so an old pending row is a lease rather than proof
# that work is still running. Keep the lease comfortably above the maximum
# normal model call/polling window, then renew it when a read resubmits the task.
PENDING_GENERATION_LEASE_SECONDS = 15 * 60


class DraftConfirmationLocked(RuntimeError):
    pass


def _revision(title: str, body: str) -> str:
    payload = json.dumps({"title": title.strip(), "content": body.strip()}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _draft_view(record: dict | None) -> dict:
    if record is None:
        return {
            "status": "pending", "cardState": "draft", "title": "", "content": "",
            "revision": None, "snapshotVersion": None,
        }
    content = record.get("content") or {}
    confirmed = bool(content.get("confirmed"))
    view = {
        # status 是草稿生成任务的内部状态；对用户可见的卡片状态只能是草稿或已确认。
        "cardState": "confirmed" if confirmed else "draft",
        "status": record.get("status", "pending"),
        "title": content.get("title", ""),
        "content": content.get("content", ""),
        "revision": content.get("revision"),
        "snapshotVersion": content.get("snapshotVersion"),
        "snapshotId": content.get("snapshotId"),
        "origin": content.get("origin", "minimal"),
        "userEdited": bool(content.get("userEdited")),
        "knowledgeId": content.get("knowledgeId"),
        "confirmed": confirmed,
        "errorCode": content.get("errorCode"),
    }
    # 确认后的索引是内部任务，但必须对用户可观测，才能解释「已确认但不可检索」。
    knowledge_id = str(content.get("knowledgeId") or "")
    if confirmed and knowledge_id:
        from .stores import card_ledger_store
        state = card_ledger_store.get(knowledge_id) or {}
        view["indexState"] = state.get("index_state", "none")
        view["indexErrorCode"] = state.get("index_error_code")
    return view


def draft_of(material_id: str) -> dict:
    record = derived_store.DerivedStore.instance().get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    return _draft_view(record)


def _default_title(material_id: str, supplied_title: str = "") -> str:
    """草稿标题默认使用原材料文件名，而不是技术占位文案。"""
    if supplied_title.strip():
        return supplied_title.strip()
    try:
        from .services import ingestion

        record = ingestion.JobStore.instance().get(material_id) or {}
        return str(record.get("file_name") or "").strip()
    except Exception:
        return ""


def _ensure_minimal_draft_record(material_id: str, *, title: str = "") -> tuple[dict | None, bool]:
    """Return the stored minimal draft record and whether this call created it."""
    store = derived_store.DerivedStore.instance()
    current = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    if current is not None:
        content = current.get("content") or {}
        # 早期版本的默认标题为“待确认知识卡片”。仅对未由用户编辑、未确认的
        # 占位草稿自修复为文件名，避免改写用户内容或已确认的正式卡片。
        default_title = _default_title(material_id, title)
        if (
            default_title
            and content.get("title") == "待确认知识卡片"
            and not content.get("userEdited")
            and not content.get("confirmed")
        ):
            updated = dict(content)
            updated["title"] = default_title
            updated["revision"] = _revision(default_title, str(updated.get("content") or ""))
            try:
                current = store.save_material_draft_cas(
                    material_id,
                    str(content.get("revision") or ""),
                    updated,
                    str(current.get("input_hash") or ""),
                    str(current.get("generator") or "minimal"),
                    status=current.get("status", "pending"),
                    require_unedited=True,
                )
            except derived_store.DraftRevisionConflict:
                current = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
        return current, False
    snapshot = MaterialPipelineStore.instance().current_snapshot(material_id)
    if snapshot is None:
        return None, False
    text = MaterialSnapshotSaga(MaterialPipelineStore.instance()).read_snapshot_text(snapshot).strip()
    body = text[:1200] if text else "未提取到可检索文本。请补充知识卡片内容。"
    draft_title = _default_title(material_id, title) or "未命名材料"
    content = {
        "title": draft_title, "content": body, "snapshotId": snapshot["snapshot_id"],
        "snapshotVersion": snapshot["version"], "sourceHash": snapshot.get("source_hash") or "",
        "inputHash": _input_hash(text), "generationParams": {"mode": "minimal", "version": 1},
        "origin": "minimal", "userEdited": False,
    }
    content["revision"] = _revision(content["title"], content["content"])
    try:
        record = store.save_material_draft_cas(
            material_id, "", content, snapshot.get("source_hash") or "", "minimal", status="pending"
        )
        created = True
    except derived_store.DraftRevisionConflict:
        record = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
        created = False
    return record, created


def ensure_minimal_draft(material_id: str, *, title: str = "") -> dict:
    """为当前快照创建可编辑兜底草稿；已存在草稿绝不覆盖。"""
    record, _created = _ensure_minimal_draft_record(material_id, title=title)
    return _draft_view(record)


def _generate(material_id: str, snapshot_id: str, source_hash: str, text: str, force: bool) -> None:
    store = derived_store.DerivedStore.instance()
    existing = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    if existing is None:
        ensure_minimal_draft(material_id)
        existing = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    if existing is None:
        return
    current = existing.get("content") or {}
    if current.get("userEdited"):
        return  # 重新生成不能覆盖用户草稿；前端应先显式丢弃编辑再发起生成。
    pipeline = MaterialPipelineStore.instance()
    current_snapshot = pipeline.current_snapshot(material_id)
    if (
        current_snapshot is None
        or current_snapshot["snapshot_id"] != snapshot_id
        or (current_snapshot.get("source_hash") or "") != source_hash
    ):
        return
    try:
        target_snapshot = pipeline.get_snapshot(snapshot_id)
    except Exception:
        return
    snap = get_provider().get_local_snapshot()
    params = {"temperature": 0.2, "maxTokens": 1000, "promptVersion": 1, "model": snap.model}
    try:
        answer = _call_llm(
            "你是 MindOS 知识卡片助手。仅基于输入材料写中文知识卡片草稿，不要编造事实。",
            "请输出可编辑的知识卡片正文，使用简洁标题和要点。材料：\n" + (text[:6000] or "无可检索文本"),
            temperature=0.2, max_tokens=1000, snap=snap,
        ).strip()
        if not answer:
            raise ValueError("empty_response")
    except Exception as exc:
        latest = pipeline.current_snapshot(material_id)
        if latest is None or latest["snapshot_id"] != snapshot_id or (latest.get("source_hash") or "") != source_hash:
            return
        failed = dict(current)
        failed["errorCode"] = type(exc).__name__.lower()
        try:
            failed.update({"snapshotId": snapshot_id, "snapshotVersion": target_snapshot["version"],
                           "sourceHash": source_hash, "inputHash": _input_hash(text), "generationParams": params})
            store.save_material_draft_cas(material_id, str(current.get("revision") or ""), failed,
                                          _input_hash(text), _generator_name(snap), status="failed",
                                          require_unedited=True)
        except derived_store.DraftRevisionConflict:
            pass
        return
    updated = dict(current)
    # 模型返回前材料可能已被重处理；旧快照的结果绝不能覆盖新草稿。
    latest = pipeline.current_snapshot(material_id)
    if latest is None or latest["snapshot_id"] != snapshot_id or (latest.get("source_hash") or "") != source_hash:
        return
    updated.update({"content": answer, "snapshotId": snapshot_id, "snapshotVersion": target_snapshot["version"],
                    "sourceHash": source_hash, "inputHash": _input_hash(text), "generationParams": params,
                    "origin": "model", "userEdited": False})
    updated.pop("errorCode", None)
    updated["revision"] = _revision(updated.get("title", ""), answer)
    try:
        store.save_material_draft_cas(material_id, str(current.get("revision") or ""), updated,
                                      _input_hash(text), _generator_name(snap), status="ok",
                                      require_unedited=True)
    except derived_store.DraftRevisionConflict:
        pass


def submit_generation(material_id: str, source_path: str, *, force: bool = False) -> bool:
    del source_path  # 草稿生成只使用提交时捕获的受控正文快照。
    pipeline = MaterialPipelineStore.instance()
    snapshot = pipeline.current_snapshot(material_id)
    if snapshot is None:
        return False
    try:
        text = MaterialSnapshotSaga(pipeline).read_snapshot_text(snapshot).strip()
    except Exception:
        return False
    # 首次草稿与摘要/实体同属当前材料的主链产物，不能被批量后台派生长期饿死。
    priority = PRIORITY_MANUAL_REGENERATE if force else PRIORITY_SUMMARY_ENTITIES
    return _scheduler.submit(priority, lambda: _generate(
        material_id, snapshot["snapshot_id"], snapshot.get("source_hash") or "", text, force
    ),
                             material_id=material_id, kind="generated-draft")


def recover_stale_generation(material_id: str, source_path: str, *, now: float | None = None) -> dict:
    """Requeue an abandoned generated-draft task without blocking the read.

    The queue is process-local while the draft row is durable. After a restart,
    historical rows can therefore remain ``pending`` forever. Renew the row's
    lease before submitting so detail-page polling cannot enqueue duplicates on
    every request. User-edited/confirmed drafts are never regenerated.
    """
    store = derived_store.DerivedStore.instance()
    record = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    if record is None:
        record, created = _ensure_minimal_draft_record(material_id)
        if record is None:
            return _draft_view(None)
        if created:
            if submit_generation(material_id, source_path):
                return _draft_view(record)
            failed = {
                **(record.get("content") or {}),
                "errorCode": "generation_queue_unavailable",
            }
            finished = store.finish_pending_derived_lease(
                "material",
                material_id,
                KIND_GENERATED_DRAFT,
                float(record.get("updated_at") or 0),
                status="failed",
                content=failed,
            )
            return _draft_view(finished or store.get_derived_record(
                "material", material_id, KIND_GENERATED_DRAFT
            ))
    content = record.get("content") or {}
    current_time = time.time() if now is None else now
    try:
        updated_at = float(record.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    if (
        record.get("status") != "pending"
        or content.get("userEdited")
        or content.get("confirmed")
        or current_time - updated_at < PENDING_GENERATION_LEASE_SECONDS
    ):
        return _draft_view(record)

    if _scheduler.has_task(material_id, "generated-draft"):
        return _draft_view(record)

    claimed = store.renew_material_draft_generation_lease(
        material_id,
        current_time - PENDING_GENERATION_LEASE_SECONDS,
        renewed_at=current_time,
    )
    if claimed is None:
        return draft_of(material_id)
    record = claimed
    content = record.get("content") or {}
    if submit_generation(material_id, source_path):
        return _draft_view(record)

    failed = dict(content)
    failed["errorCode"] = "generation_queue_unavailable"
    finished = store.finish_pending_derived_lease(
        "material",
        material_id,
        KIND_GENERATED_DRAFT,
        current_time,
        status="failed",
        content=failed,
    )
    return _draft_view(finished or store.get_derived_record(
        "material", material_id, KIND_GENERATED_DRAFT
    ))


def save_draft(material_id: str, expected_revision: str, title: str, body: str) -> dict:
    store = derived_store.DerivedStore.instance()
    existing = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    if existing is None:
        ensure_minimal_draft(material_id, title=title)
        existing = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
        # 允许兼容客户端在首次保存时提交空 revision；正常客户端先 GET 草稿并带回 revision。
        if existing is not None and expected_revision == "":
            expected_revision = str((existing.get("content") or {}).get("revision") or "")
    snapshot = MaterialPipelineStore.instance().current_snapshot(material_id)
    candidate = existing
    revision = expected_revision
    # A generated enhancement may finish between rendering and the user's PUT.
    # Explicit user input wins over an unedited AI/minimal revision, while a
    # revision written by another user/session must still produce a real 409.
    for _attempt in range(3):
        current = (candidate or {}).get("content") or {}
        if current.get("confirmed"):
            raise DraftConfirmationLocked("draft is already confirmed")
        if current.get("confirmationSessionId"):
            raise DraftConfirmationLocked("draft confirmation is in progress")
        updated = dict(current)
        updated.update({"title": title.strip(), "content": body.strip(), "origin": "user", "userEdited": True,
                        "snapshotId": snapshot["snapshot_id"] if snapshot else current.get("snapshotId"),
                        "snapshotVersion": snapshot["version"] if snapshot else current.get("snapshotVersion")})
        if snapshot is not None:
            updated.update({"sourceHash": snapshot.get("source_hash") or "", "inputHash": _input_hash(body),
                            "generationParams": {"mode": "user", "version": 1}})
        updated.pop("errorCode", None)
        updated["revision"] = _revision(updated["title"], updated["content"])
        try:
            record = store.save_material_draft_cas(
                material_id, revision, updated,
                (snapshot.get("source_hash") or "") if snapshot else "", "user", status="ok",
            )
            return _draft_view(record)
        except derived_store.DraftRevisionConflict as exc:
            latest = exc.current
            latest_content = (latest or {}).get("content") or {}
            if latest_content.get("confirmed") or latest_content.get("confirmationSessionId"):
                raise DraftConfirmationLocked("draft confirmation is in progress") from exc
            if latest_content.get("userEdited"):
                if (
                    str(latest_content.get("title") or "").strip() == title.strip()
                    and str(latest_content.get("content") or "").strip() == body.strip()
                ):
                    return _draft_view(latest)
                raise
            candidate = latest
            revision = str(latest_content.get("revision") or "")
    # Continuous non-user writers are unexpected, but preserving CAS safety is
    # preferable to silently overwriting after the bounded retry.
    raise derived_store.DraftRevisionConflict(candidate or {})


def lock_for_confirmation(material_id: str, expected_revision: str, session_id: str) -> dict:
    """CAS-lock a draft before publishing its formal knowledge-card file."""
    store = derived_store.DerivedStore.instance()
    record = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    content = (record or {}).get("content") or {}
    if not record or str(content.get("revision") or "") != expected_revision:
        raise derived_store.DraftRevisionConflict(record or {"content": content})
    locked_by = str(content.get("confirmationSessionId") or "")
    if content.get("confirmed"):
        raise DraftConfirmationLocked("draft is already confirmed")
    if locked_by and locked_by != session_id:
        raise DraftConfirmationLocked("draft confirmation is in progress")
    if locked_by == session_id:
        return _draft_view(record)
    updated = {**content, "confirmationSessionId": session_id}
    saved = store.save_material_draft_cas(
        material_id, expected_revision, updated,
        str(record.get("input_hash") or ""), str(record.get("generator") or "user"), status="confirming",
    )
    return _draft_view(saved)


def unlock_confirmation(material_id: str, expected_revision: str, session_id: str) -> None:
    """Release a failed confirmation lock without overwriting a newer draft revision."""
    store = derived_store.DerivedStore.instance()
    record = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    content = (record or {}).get("content") or {}
    if (
        not record
        or str(content.get("revision") or "") != expected_revision
        or str(content.get("confirmationSessionId") or "") != session_id
        or content.get("confirmed")
    ):
        return
    updated = dict(content)
    updated.pop("confirmationSessionId", None)
    store.save_material_draft_cas(
        material_id, expected_revision, updated,
        str(record.get("input_hash") or ""), str(record.get("generator") or "user"), status="ok",
    )


def mark_confirmed(material_id: str, expected_revision: str, knowledge_id: str) -> dict:
    """将材料唯一草稿标记为已确认，并保留到知识卡片的稳定关联。"""
    store = derived_store.DerivedStore.instance()
    record = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    content = (record or {}).get("content") or {}
    if not record or str(content.get("revision") or "") != expected_revision:
        raise derived_store.DraftRevisionConflict(record or {"content": content})
    if content.get("confirmed") and content.get("knowledgeId") == knowledge_id:
        return _draft_view(record)
    updated = dict(content)
    updated.update({"confirmed": True, "knowledgeId": knowledge_id})
    updated.pop("confirmationSessionId", None)
    saved = store.save_material_draft_cas(
        material_id, expected_revision, updated,
        str(record.get("input_hash") or ""), str(record.get("generator") or "user"),
        status="confirmed",
    )
    return _draft_view(saved)


def reopen_after_card_purged(material_id: str, knowledge_id: str) -> dict | None:
    """Remove a stale confirmed-card link while preserving the editable draft body."""
    store = derived_store.DerivedStore.instance()
    record = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    content = (record or {}).get("content") or {}
    if not record or not content.get("confirmed") or content.get("knowledgeId") != knowledge_id:
        return _draft_view(record) if record else None
    updated = dict(content)
    updated.pop("confirmed", None)
    updated.pop("knowledgeId", None)
    updated.pop("confirmationSessionId", None)
    try:
        saved = store.save_material_draft_cas(
            material_id, str(content.get("revision") or ""), updated,
            str(record.get("input_hash") or ""), str(record.get("generator") or "user"), status="ok",
        )
    except derived_store.DraftRevisionConflict:
        saved = store.get_derived_record("material", material_id, KIND_GENERATED_DRAFT)
    return _draft_view(saved)
