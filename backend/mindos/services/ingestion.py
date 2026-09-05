"""MindOS 导入/处理编排（P2 / P13 开放音频）。

校验 → 落盘 → 登记状态 → 提交旧项目后台索引池 → 状态映射。
浏览器只接触业务 ID 与 MindOS 统一状态词（已上传/处理中/可用/失败），
不接触物理路径或旧项目任务字段。
"""
import logging
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import WATCH_FOLDER
from watcher import get_job
from parser import parse_file
from vector_store import get_source_chunks, get_source_hash
from annotations import get as _ann_get, set_annotation as _ann_set

from ..stores.job_store import JobStore
from ..stores import derived_store
from ..stores.material_pipeline_store import (
    MaterialPipelineStore,
    MaterialJobNotFoundError,
    MaterialJobConflictError,
)

logger = logging.getLogger(__name__)

# MindOS 统一状态词（与前端共享语义）
ST_UPLOADED = "uploaded"      # 已上传
ST_QUEUED = "queued"          # 等待处理
ST_PROCESSING = "processing"  # 处理中
ST_AVAILABLE = "available"    # 可用
ST_FAILED = "failed"          # 失败


class RetryNotAllowed(ValueError):
    """任务当前不是失败状态，不能重复提交。"""


class SnapshotVersionConflict(ValueError):
    """`expectedSnapshotVersion` 与当前可见快照版本不一致（§9.1 双 CAS 之一）。

    ``current_version`` 供调用方返回 409 时附带最新版本提示前端重新加载。
    """

    def __init__(self, current_version: int, message: str = "内容已在他处更新，请重新加载") -> None:
        super().__init__(message)
        self.current_version = int(current_version)


def _map_watcher_state(state: str, source_path: str | None = None) -> str:
    """旧项目 watcher 内部状态 → MindOS 状态词。

    unknown（后端重启后 _JOBS 内存任务表丢失）时兜底查向量库：若该文件已有
    索引哈希，说明此前已完成 → 可用；否则仍视为处理中（任务可能正在排队）。
    """
    if state in ("queued", "processing"):
        return ST_PROCESSING
    if state == "done":
        return ST_AVAILABLE
    if state == "failed":
        return ST_FAILED
    # 刚提交或文件系统事件先于任务记录到达时，尚未回写状态前视为处理中；
    # 内存任务表丢失但文件已入向量库 → 恢复为可用。
    if source_path and get_source_hash(source_path):
        return ST_AVAILABLE
    return ST_PROCESSING


def new_material_id() -> str:
    return f"mindos_{uuid.uuid4().hex[:12]}"


def destination_path(safe_name: str) -> str:
    """落盘路径：监控目录内、带随机前缀防冲突（物理路径仅服务端内部使用）。"""
    return str(Path(WATCH_FOLDER) / ".mindos_uploads" / f"{uuid.uuid4().hex[:8]}_{safe_name}")


# ---- 阶段 A-A5：material_job 状态 → MindOS 公开状态词 ----
# material_jobs 是原材料处理任务的唯一事实来源；前端仅识别的四个状态词
# （uploaded/queued/processing/available/failed）由 worker 任务状态映射得出：
#   queued              → queued
#   processing          → processing
#   draft_ready         → available
#   failed / canceled   → failed
#   paused              → processing（附 errorCode=service_interrupted，前端仍持续轮询，
#                         用户可通过 /resume 继续；不映射为终态，避免误判失败）。
def _material_job_view(material_id: str) -> tuple | None:
    """返回最近一条 material_job 的公开视图 (status, error_message, error_code, index_degraded)；
    无任务（迁移期）返回 None，调用方回落旧 watcher 映射。"""
    try:
        job = MaterialPipelineStore.instance().material_job(material_id)
    except Exception:
        job = None
    if not job:
        return None
    state = job.get("state")
    if state == "draft_ready":
        return (ST_AVAILABLE, None, None, False)
    if state == "failed":
        return (ST_FAILED, job.get("error_detail"), job.get("error_code"), False)
    if state == "canceled":
        return (ST_FAILED, "用户已取消上传/处理", None, False)
    if state == "paused":
        return (ST_QUEUED, "服务中断，任务已暂停，可继续处理", "service_interrupted", False)
    if state == "queued":
        return (ST_QUEUED, None, None, False)
    # processing / 未知 → 处理中
    return (ST_PROCESSING, None, None, False)


def _current_snapshot_version(material_id: str) -> int:
    """返回当前可见快照的版本号；无快照返回 0（retry 的 CAS 基准）。"""
    try:
        snap = MaterialPipelineStore.instance().current_snapshot(material_id)
    except Exception:
        snap = None
    return int(snap["version"]) if snap else 0


def _submit_material_job(record: dict, device_scope: str = "global") -> None:
    """新上传入队 material_job（阶段 A-A5）：替代旧 ``submit_index`` 建向量索引。

    worker 领取后做「解析 → 正文快照 → 派生」，不再直接 ``add_file_chunks``；
    全程不依赖 Chroma。同一 (material, target_version) 已存在活动任务时抛
    ``MaterialJobConflictError``（新入队逻辑保证不会发生）。
    """
    from ..material_worker import material_fingerprint, run_epoch

    source_path = str(record["source_path"])
    try:
        source_hash = material_fingerprint(source_path) or ""
    except Exception:
        source_hash = ""
    MaterialPipelineStore.instance().enqueue_material_job(
        record["material_id"],
        int(record.get("version_number") or 1),
        source_path,
        source_hash=source_hash,
        run_epoch=run_epoch(),
        device_scope=device_scope,
    )


def start_ingestion(
    material_id: str,
    file_name: str,
    file_type: str,
    source_path: str,
    folder: str = "",
    folder_id: int | None = None,
    material_family_id: str | None = None,
    supersedes_material_id: str | None = None,
    version_note: str | None = None,
    device_scope: str = "global",
) -> dict:
    """登记 MindOS 资料并入队 material_job，返回当前公开状态记录。

    P14-06：新写入一律使用 folder_id（NULL=未分类）；folder 参数仅兼容旧调用。
    阶段A-A5：不再调用旧 watcher 建向量索引，改由 material worker（FIFO）解析并
    写正文快照，全程不依赖 Chroma。device_scope 由上传请求的票据身份写入。
    """
    record = JobStore.instance().register(
        material_id, file_name, file_type, source_path, folder, folder_id,
        material_family_id, supersedes_material_id, version_note,
        device_scope=device_scope,
    )
    try:
        _submit_material_job(record, device_scope=device_scope)
    except Exception:
        # job_records 与 material_jobs 分属两库，无法共享事务。入队失败时撤销
        # 刚创建的可见记录，避免列表把无 worker 任务的材料永久显示为“处理中”。
        JobStore.instance().delete(material_id)
        raise
    logger.info(
        "MindOS 上传已入队 material_job: %s file=%s v%s",
        material_id, file_name, record.get("version_number", 1),
    )
    # 首次响应保留“已上传”，由下一次轮询显示 queued/processing，便于前端完整呈现状态链。
    return public_record(record, ST_UPLOADED, None)


def _repair_missing_material_job(record: dict) -> bool:
    """补偿曾在“登记后入队前”中断的上传，不重跑已有任务或已完成旧索引。"""
    material_id = str(record.get("material_id") or "")
    version = int(record.get("version_number") or 1)
    source_path = str(record.get("source_path") or "")
    if (
        not material_id
        or not source_path
        or not Path(source_path).is_file()
        or JobStore.instance().is_canceled(material_id)
        or MaterialPipelineStore.instance().material_job(material_id, version) is not None
    ):
        return False
    _submit_material_job(record, device_scope=str(record.get("device_scope") or "global"))
    logger.warning("恢复缺失 material_job 的上传记录: %s", material_id)
    return True


def _restored_job(source_path: str) -> dict:
    """内存任务合并持久化终态：运行中/终态以内存为准（最新实时值）；
    后端重启后内存 _JOBS 丢失时，从 SQLite 恢复任务终态。
    """
    job = get_job(source_path)
    if job.get("state") in ("queued", "processing", "done", "failed"):
        return job
    outcome = JobStore.instance().index_outcome(source_path)
    if outcome is None:
        return {"state": "unknown"}
    return {
        "state": outcome["state"],
        "error": outcome.get("error"),
        "error_code": outcome.get("error_code"),
        "old_index_preserved": outcome.get("old_index_preserved", False),
    }


def status_of(material_id: str, device_scope: str = "global") -> dict | None:
    """返回资料当前状态（公开结构，不含物理路径）。

    阶段 2：传入 device_scope 时强制校验——资料不属于当前设备作用域视为不存在
    （返回 None），杜绝请求侧跨设备/账号读取状态。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None:
        return None
    if (rec.get("device_scope") or "global") != device_scope:
        return None
    # 用户已取消的任务：对外固定呈现为失败（停止轮询、提供重试入口）
    if JobStore.instance().is_canceled(material_id):
        return public_record(rec, ST_FAILED, "用户已取消上传/处理")
    # 阶段A-A5：material_job（worker 处理）为状态唯一事实来源；迁移期无任务时回落旧 watcher 映射。
    mj = _material_job_view(material_id)
    if mj is not None:
        status, error_message, error_code, index_degraded = mj
        if status == ST_AVAILABLE:
            JobStore.instance().finalize_version_link(material_id)
            rec = JobStore.instance().get(material_id) or rec
        return public_record(
            rec, status, error_message,
            error_code=error_code,
            old_index_preserved=None,
            index_degraded=index_degraded,
        )
    # ---------- 迁移期回落：旧 watcher 映射 ----------
    job = _restored_job(rec["source_path"])
    status = _map_watcher_state(job.get("state", "unknown"), rec["source_path"])
    # 仅修复“旧 watcher 无任务且未完成”的异常窗口：历史已索引材料仍保持原有
    # available 回退逻辑，绝不因浏览列表就重新处理。
    if status == ST_PROCESSING:
        try:
            if _repair_missing_material_job(rec):
                return public_record(rec, ST_QUEUED, None)
        except Exception:
            logger.exception("补偿缺失 material_job 失败: %s", material_id)
    index_degraded = False
    # A failed attempt is not equivalent to unavailable material.  If the
    # previous source hash still verifies, keep the material usable and expose
    # a repair hint rather than permanently painting it as failed after a
    # transient Chroma/model incident.
    if job.get("state") == "failed":
        failure_class = job.get("failure_class")
        try:
            verified_old_index = bool(job.get("old_index_preserved")) or bool(get_source_hash(rec["source_path"]))
        except Exception:
            verified_old_index = bool(job.get("old_index_preserved"))
        if verified_old_index:
            status = ST_AVAILABLE
            index_degraded = True
        elif failure_class in {"infrastructure", "transient"}:
            status = ST_PROCESSING
            index_degraded = True
    error_message = job.get("error") if status == ST_FAILED else None
    # P0-4：稳定错误码与旧索引保留标记随失败状态对外暴露（重启后仍可从
    # 持久化终态恢复），前端/调用方可据此决定重试策略与提示文案。
    error_code = job.get("error_code") if status == ST_FAILED else None
    old_index_preserved = job.get("old_index_preserved") if status == ST_FAILED else None
    if status == ST_AVAILABLE:
        JobStore.instance().finalize_version_link(material_id)
        rec = JobStore.instance().get(material_id) or rec
    return public_record(
        rec, status, error_message,
        error_code=error_code, old_index_preserved=old_index_preserved,
        index_degraded=index_degraded,
    )


def retry_ingestion(
    material_id: str,
    expected_snapshot_version: int | None = None,
) -> dict | None:
    """失败后重试（阶段A-A5）：重新入队 material_job，使状态回到处理中。

    ``expectedSnapshotVersion`` 用于 §9.1 的正文快照乐观锁：若用户基于旧快照视图
    发起重试而当前可见版本已前进，抛 ``SnapshotVersionConflict``（409）而非覆盖。
    前端未传该值时跳过校验，保持兼容。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None:
        return None
    version = int(rec.get("version_number") or 1)
    store = MaterialPipelineStore.instance()
    job = store.material_job(material_id, version)
    if job is None:
        # 迁移期无任务：兜底退回旧 → 重新入队一条任务并等待 worker。
        raise RetryNotAllowed("该资料没有处理任务，无法重试")
    if job["state"] in ("queued", "processing") or not job.get("finished_at"):
        raise RetryNotAllowed("任务仍在处理中，不能重复重试")
    current = _current_snapshot_version(material_id)
    if expected_snapshot_version is not None and current != expected_snapshot_version:
        raise SnapshotVersionConflict(current)
    from ..material_worker import material_fingerprint, run_epoch

    # 重试时重绑完成栅栏指纹：源文件在失败后可能已被替换，重算当前内容指纹，
    # 避免旧指纹导致重试被永久栅栏阻断。
    try:
        src_hash = material_fingerprint(rec["source_path"])
    except Exception:
        src_hash = None
    store.retry_material_job(
        material_id, version, run_epoch=run_epoch(), source_hash=src_hash
    )
    return status_of(material_id)


def resume_ingestion(material_id: str) -> dict | None:
    """继续处理被暂停（paused）的任务：重新入队并生成一次性 resume_token。

    仅允许 paused → queued；其他状态抛 RetryNotAllowed（409）。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None:
        return None
    version = int(rec.get("version_number") or 1)
    from ..material_worker import run_epoch

    try:
        MaterialPipelineStore.instance().resume_material_job(
            material_id, version, run_epoch=run_epoch()
        )
    except MaterialJobNotFoundError:
        return None
    except MaterialJobConflictError as e:
        raise RetryNotAllowed(str(e))
    return status_of(material_id)


def cancel_material_job(material_id: str) -> None:
    """尽力取消仍在排队/暂停的 material_job（worker 不再处理），可失败忽略。"""
    rec = JobStore.instance().get(material_id)
    if rec is None:
        return
    version = int(rec.get("version_number") or 1)
    try:
        MaterialPipelineStore.instance().cancel_material_job(material_id, version)
    except Exception:
        logger.info("material %s material_job cancel 跳过（非 queued/paused）", material_id)


def remove_from_queue(material_id: str) -> bool:
    """删除尚未完成的导入队列项及其受控上传文件。

    已开始处理或已完成的资料必须走生命周期回收/永久清除，避免删除快照、草稿或
    已被引用的内容。本操作只覆盖 queued/paused/failed/canceled 四种无可见成品态。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None:
        return False
    store = MaterialPipelineStore.instance()
    job = store.material_job(material_id, int(rec.get("version_number") or 1))
    state = str((job or {}).get("state") or "")
    if state not in {"queued", "paused", "failed", "canceled"}:
        raise RetryNotAllowed("仅等待处理、已暂停或失败的资料可以移出队列")
    source = Path(str(rec["source_path"]))
    try:
        if source.is_file():
            source.unlink()
    except OSError as exc:
        raise RetryNotAllowed(f"移除上传文件失败：{type(exc).__name__}") from exc
    try:
        from annotations import delete as delete_annotation
        delete_annotation(str(rec["source_path"]))
    except Exception:
        logger.info("material %s annotation cleanup skipped", material_id)
    derived = derived_store.DerivedStore.instance()
    derived.delete_for_material(material_id)
    derived.delete_derived_records_for_material(material_id)
    store.remove_material(material_id)
    return JobStore.instance().delete(material_id)


def processing_view(material_id: str, device_scope: str = "global") -> dict | None:
    """返回资料的处理任务视图（§9.1 GET /processing）：任务状态、阶段、
    失败码、可执行动作与当前快照版本。无任务时也返回结构（job=None）。

    阶段 2：material_id 定位受限到当前设备作用域，跨设备资料视为不存在。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None or (rec.get("device_scope") or "global") != device_scope:
        return None
    version = int(rec.get("version_number") or 1)
    store = MaterialPipelineStore.instance()
    job = store.material_job(material_id, version)
    snapshot_version = _current_snapshot_version(material_id)

    def _actions(state: str | None) -> list[str]:
        if state == "draft_ready":
            return ["retry", "regenerate"]
        if state == "failed":
            return ["retry"]
        if state == "paused":
            return ["resume", "cancel"]
        if state in ("queued", "processing"):
            return ["cancel"]
        if state == "canceled":
            return ["retry"]
        return []

    if job is None:
        return {
            "materialId": material_id,
            "job": None,
            "snapshotVersion": snapshot_version,
            "publicStatus": status_of(material_id, device_scope=device_scope)["status"]
            if status_of(material_id, device_scope=device_scope) else "processing",
        }
    return {
        "materialId": material_id,
        "job": {
            "jobId": job["job_id"],
            "state": job["state"],
            "attempts": int(job.get("attempts") or 0),
            "createdAt": job.get("created_at"),
            "startedAt": job.get("started_at"),
            "finishedAt": job.get("finished_at"),
            "errorCode": job.get("error_code"),
            "failureClass": job.get("failure_class"),
            "errorMessage": job.get("error_detail") if job["state"] == "failed" else None,
            "actions": _actions(job["state"]),
        },
        "snapshotVersion": snapshot_version,
        "publicStatus": status_of(material_id, device_scope=device_scope)["status"]
        if status_of(material_id, device_scope=device_scope) else "processing",
    }


def public_record(
    rec: dict,
    status: str,
    error_message: str | None,
    error_code: str | None = None,
    old_index_preserved: bool | None = None,
    index_degraded: bool = False,
) -> dict:
    """内部记录 → 浏览器可见结构；绝不返回 source_path / saved_path。

    P0-4：失败终态额外暴露 errorCode（稳定错误码，如 embed_failed）与
    oldIndexPreserved（失败时旧索引是否仍可检索），供前端精确提示与重试决策。
    """
    return {
        "materialId": rec["material_id"],
        "fileName": rec["file_name"],
        "fileType": rec["file_type"],
        "status": status,
        "jobId": rec["job_id"],
        "errorMessage": error_message,
        "errorCode": error_code,
        "oldIndexPreserved": bool(old_index_preserved) if old_index_preserved is not None else None,
        "indexDegraded": index_degraded,
        # P14-06：folder 为兼容旧前端读字段；folderId 是目录树唯一事实来源（NULL=未分类）。
        "folder": rec.get("folder", "未分类"),
        "folderId": rec.get("folder_id"),
        # JobStore records always contain created_at. Use a stable fallback for
        # legacy records and minimal test doubles created before that field existed.
        "createdAt": datetime.fromtimestamp(rec.get("created_at") or 0, tz=timezone.utc).isoformat(),
        "materialFamilyId": rec.get("material_family_id") or rec["material_id"],
        "versionNumber": int(rec.get("version_number") or 1),
        "supersedesMaterialId": rec.get("supersedes_material_id") or None,
        "supersededByMaterialId": rec.get("superseded_by_material_id") or None,
        "versionNote": rec.get("version_note") or None,
        # P15-05：是否已回收（回收仅隐藏，记录保留可恢复；普通列表默认排除）。
        "recycled": bool(rec.get("recycled")),
    }


def recycled_material_ids(device_scope: str = "global") -> set[str]:
    """返回当前设备作用域内已回收材料 ID（P15-05 回收站），供列表/搜索/问答/图谱/关联统一排除。

    阶段 2：只能看到本设备作用域的回收状态，跨设备/账号回收状态不可见。
    """
    return JobStore.instance().recycled_ids(device_scope=device_scope)


def is_recycled(material_id: str, device_scope: str = "global") -> bool:
    return JobStore.instance().is_recycled(material_id, device_scope=device_scope)


def list_materials(
    file_type: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    folder_id: int | None = None,
    device_scope: str = "global",
) -> list[dict]:
    """查询原材料公开列表，所有状态均通过 watcher 映射。

    P14-06：folder_id 筛选取「选中目录 + 全部后代」子树；folder 字符串筛选仅兼容旧调用。
    阶段 2：只返回当前设备作用域下的资料（device_scope 由票据身份决定），
    跨设备/账号材料互不可见；本机调试模式固定为 global。
    """
    keyword_lower = keyword.strip().lower() if keyword else ""
    tag_lower = tag.strip().lower() if tag else ""
    folder_ids: set[int] | None = None
    if folder_id is not None:
        folder_ids = JobStore.instance().folder_descendants(folder_id)
        if not folder_ids:
            return []
    rows = []
    for rec in JobStore.instance().list(device_scope=device_scope):
        if file_type and rec.get("file_type") != file_type:
            continue
        # 子树筛选（含选中节点自身及其全部后代）
        if folder_ids is not None and rec.get("folder_id") not in folder_ids:
            continue
        # 兼容旧调用：按字符串名精确匹配（DEPRECATED）
        if folder and rec.get("folder", "未分类") != folder:
            continue
        if keyword_lower and keyword_lower not in rec.get("file_name", "").lower():
            continue
        public = status_of(rec["material_id"], device_scope=device_scope)
        if public is None:
            continue
        if status and public["status"] != status:
            continue
        if tag_lower:
            ann = _ann_get(str(rec["source_path"]))
            tags_lower = [t.lower() for t in ann.get("tags", [])]
            if tag_lower not in tags_lower:
                continue
        rows.append(public)
    rows.sort(key=lambda item: item["createdAt"], reverse=True)
    return rows


def list_folders() -> list[str]:
    """[DEPRECATED] 返回字符串文件夹名（旧前端兼容）。新调用方使用 list_folder_nodes。"""
    return JobStore.instance().list_folders()


def list_folder_nodes(scope: str = "RAW") -> list[dict]:
    """返回目录树节点扁平数组（id/parentId/name/materialCount/subtreeMaterialCount…）。"""
    return JobStore.instance().list_folder_nodes(scope)


def material_for_source(source_path: str, device_scope: str = "global") -> dict | None:
    """Resolve an internal index source path back to its MindOS material record.

    阶段 2：仅在当前设备作用域内查找，避免索引跨设备串料。
    """
    for record in JobStore.instance().list(device_scope=device_scope):
        if record.get("source_path") == source_path:
            return record
    return None


def _finite_seconds(value) -> float | None:
    """将索引块时间戳转为有限秒数；缺失、非法或 NaN/Inf 一律返回 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _snapshot_text_of(material_id: str) -> str | None:
    """读取当前可见（ready 且未被 supersede）的正文快照文本；无快照/读取异常返回 None。

    阶段A-A6：详情正文一律优先读正文快照（worker 解析产物），使新上传材料在
    **不依赖 Chroma** 的情况下即可查看正文/OCR/转写（验收标准 #3）。
    """
    try:
        from ..material_snapshot_saga import MaterialSnapshotSaga
        from ..stores.material_pipeline_store import MaterialPipelineStore

        store = MaterialPipelineStore.instance()
        snap = store.current_snapshot(material_id)
        if snap is None:
            return None
        return MaterialSnapshotSaga(store).read_snapshot_text(snap)
    except Exception as exc:
        logger.info("MindOS 详情快照读取失败 %s: %s", material_id, exc)
        return None


def _transcript_segments(source_path: str) -> list[dict]:
    """尽力读取音频时间轴分段（Chroma 增强，仅用于播放器跳转）。

    缺失/损坏/非法（end<=start）分段一律丢弃，绝不因 Chroma 故障阻断详情正文
    （正文已由正文快照提供）。
    """
    from ..stage_d_admin import legacy_read_enabled
    if not legacy_read_enabled():
        return []
    try:
        chunks = get_source_chunks(source_path, limit=500)
    except Exception:
        return []
    segments: list[dict] = []
    for chunk in chunks:
        if chunk.get("metadata", {}).get("modality") != "transcript":
            continue
        meta = chunk.get("metadata") or {}
        start = _finite_seconds(meta.get("start_time"))
        end = _finite_seconds(meta.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        segments.append(
            {"start": round(start, 3), "end": round(end, 3), "text": str(chunk.get("text") or "")}
        )
    return segments


def detail_of(material_id: str, device_scope: str = "global") -> dict | None:
    """Build a read-only detail payload from the internal material mapping.

    阶段 2：资料不属于当前设备作用域时视为不存在（返回 None → 404），
    杜绝跨设备/账号读取详情。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None:
        return None
    if (rec.get("device_scope") or "global") != device_scope:
        return None
    public = status_of(material_id, device_scope=device_scope)
    if public is None:
        return None
    source = Path(rec["source_path"])
    metadata: dict = {"fileSize": None, "modifiedAt": None}
    text = ""
    transcript: list[dict] = []
    # 阶段A-A6：正文一律优先读正文快照（worker 解析产物），不依赖 Chroma。
    snap_text = _snapshot_text_of(material_id)
    if source.is_file():
        stat = source.stat()
        metadata.update(fileSize=stat.st_size, modifiedAt=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat())
        if rec["file_type"] == "document":
            if snap_text:
                text = snap_text
            else:
                # 迁移期无快照：回落直接解析源文件（旧路径，不用 Chroma）
                try:
                    text = str(parse_file(str(source)).get("text") or "")
                except Exception as exc:
                    logger.info("MindOS 详情解析失败 %s: %s", rec["file_name"], exc)
        elif rec["file_type"] == "audio":
            # 音频逐字稿：正文读快照转写（无 Chroma 依赖）；时间轴分段仅作增强读旧索引，
            # 缺失/损坏不阻断文本。仅保留 modality='transcript' 且时间戳合法递增的分段。
            # 为新上传（暂无旧索引）时 transcript 为空，但正文仍可读。
            if not snap_text:
                transcript = _transcript_segments(str(source))
                text = "\n\n".join(item["text"] for item in transcript)
            else:
                text = snap_text
                transcript = _transcript_segments(str(source))
        else:
            # image 等：正文读快照 OCR 结果；迁移期无快照回落旧索引扁平文本
            if snap_text:
                text = snap_text
            else:
                from ..stage_d_admin import legacy_read_enabled
                text = ("\n\n".join(
                    str(chunk.get("text") or "") for chunk in get_source_chunks(str(source), limit=200)
                ).strip() if legacy_read_enabled() else "")
    # P14-03：摘要为派生数据（derived_records），status ok 才展示文本；模型不可用/失败
    # 显示「摘要暂不可用，可重试」。excerpt 仅作纯预览，绝不命名为 summary。
    from .. import derived as derived_svc
    summary = derived_svc.summary_of(material_id)
    excerpt = text.strip()[:200]
    # 主题：暂不接入 LLM 生成（避免伪造 AI 结论），预留字段供后续 B5 接入后填充。
    topic = ""
    ann = _ann_get(str(rec["source_path"]))
    # P14-01：结构化 content parts（DOCX/PDF 段落/表格/页面）与表格计数。
    # 仅 document 类型可能有 parts；contentParts 只返回业务字段，不含物理路径。
    content_parts: list[dict] = []
    table_count = 0
    # P14-02：内嵌图片（可预览缩略图 + OCR 文本/状态）。
    embedded_images: list[dict] = []
    if rec["file_type"] == "document":
        for part in derived_store.DerivedStore.instance().parts_for_material(material_id):
            item = {
                "partId": part["id"],
                "partType": part["part_type"],
                "ordinal": part["ordinal"],
                "text": part["text"],
                "location": part["location"],
            }
            if part["part_type"] == "table":
                # 表格按行列切分为二维数组，供前端直接渲染；TSV 空单元格保留空串。
                item["rows"] = [
                    row.split("\t") for row in (part["text"] or "").split("\n") if row
                ]
                table_count += 1
            elif part["part_type"] == "image":
                meta = part.get("image_meta") or {}
                embedded_images.append({
                    "partId": part["id"],
                    "previewUrl": f"/api/mindos/materials/{material_id}/parts/{part['id']}/file",
                    "location": part["location"],
                    "ocrText": part["text"],
                    "ocrStatus": meta.get("ocr_status", "empty"),
                    "mime": meta.get("mime", ""),
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                })
                continue
            content_parts.append(item)
    return {
        **public,
        # P14-06：目录路径（如 根目录/子目录），用于详情页展示；未分类为空串。
        "folderPath": JobStore.instance().folder_path(rec.get("folder_id")),
        "previewUrl": f"/api/mindos/materials/{material_id}/file",
        "metadata": metadata,
        "summary": summary,
        "topic": topic,
        "text": text,
        "textLabel": {
            "document": "解析文本",
            "image": "OCR 结果",
            "audio": "转写结果",
        }.get(rec["file_type"], "文本"),
        "transcript": transcript,
        "contentParts": content_parts,
        "tableCount": table_count,
        "embeddedImages": embedded_images,
        "excerpt": excerpt,
        "tags": ann.get("tags", []),
        "readOnly": True,
    }


def source_path_of(material_id: str, device_scope: str = "global") -> str | None:
    """Return the internal source_path for a material, or None.

    阶段 2：material_id 的定位结果受限到当前设备作用域，跨设备资料视为不存在。
    """
    rec = JobStore.instance().get(material_id)
    if rec is None or (rec.get("device_scope") or "global") != device_scope:
        return None
    return str(rec["source_path"])


def material_tags(material_id: str, device_scope: str = "global") -> list[str]:
    """Return the tags for a material via the annotations bridge."""
    sp = source_path_of(material_id, device_scope=device_scope)
    if not sp:
        return []
    return _ann_get(sp).get("tags", [])


def summary_text_of(detail: dict) -> str:
    """从资料详情中提取摘要纯文本，兼容对象（P14-03 起）与旧字符串两种形态。

    所有需要“摘要文本”的既有调用方（关联推荐 / 标签推荐等）应统一走此函数，
    避免把 summary 对象误当字符串。
    """
    raw = detail.get("summary") if isinstance(detail, dict) else None
    if isinstance(raw, dict):
        return str(raw.get("text") or "")
    return str(raw or "")


def set_material_tags(material_id: str, tags: list[str], mode: str) -> list[str]:
    """Add or remove tags on a material. mode is 'add' or 'remove'."""
    sp = source_path_of(material_id)
    if not sp:
        return []
    current = _ann_get(sp).get("tags", [])
    if mode == "add":
        merged = current + [t for t in tags if t not in current]
    else:
        remove_set = set(tags)
        merged = [t for t in current if t not in remove_set]
    new_ann, _ = _ann_set(sp, {"tags": merged}, merge=True)
    return new_ann.get("tags", [])
