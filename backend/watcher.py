"""文件夹监控器 — 自动向量化新增/修改的文件（文本/图片/视频）"""
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config import (
    WATCH_FOLDER,
    OCR_ENABLED,
    INDEX_EMPTY_OCR_IMAGES,
    CLIP_ENABLED,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_VIDEO_EXTENSIONS,
    VIDEO_FRAMES_DIR,
    VIDEO_WORK_DIR,
    VIDEO_FRAME_OCR_ENABLED,
    WHISPER_ENABLED,
    WHISPER_TIMEOUT_RTF,
    TRANSCRIPT_CHUNK_SEC,
)
from parser import EmptyFileError, parse_file, is_supported, chunk_text, file_hash
from embedder import (
    embed_batch_texts,
    ocr_image,
    ocr_available,
    embed_image_clip,
    transcribe_audio,
    whisper_available,
    whisper_loadable,
)
from vector_store import (
    add_file_chunks,
    delete_file,
    delete_text_chunks,
    get_source_hash,
    add_image_vector,
    add_image_frames,
    IndexCorruptedError,
    index_health_blocked,
    resolve_index_target,
)
import annotations
import rag_strategy
from mindos.stores import derived_store
from mindos.stores.job_store import JobStore

logger = logging.getLogger(__name__)

# ---- P1-1 文件稳定判断（方案 §P1-1） ----
# 稳定窗口不再只看大小：同时采样「大小 + 修改时间 + 内容指纹前缀」，
# 连续 _STABLE_SAMPLES 次全部一致才判定文件拷贝/保存完成，避免读到半截。
_STABLE_SAMPLES = 3
# 内容指纹取文件头部块前缀的 SHA1，成本 ~1ms，足以识别「同大小/同 mtime 覆写」；
# 对仍在增长（大小变化）的大文件，每次采样不会重复读取已知变化段。
_CONTENT_PREFIX_BYTES = 1 << 20  # 1MB

# ---- P1-1 事件延迟合并（方案 §P1-1） ----
# 不再直接丢弃去重窗口内的事件（可能丢掉最终版本）。改为：窗口内只保留
# 一个「待处理标记」，窗口结束后由后台清扫线程重新检查并提交最后版本。
DEDUP_WINDOW = 5
_PENDING_LOCK = threading.Lock()
_pending_index: dict[str, float] = {}
_PENDING_SWEEPER_STARTED = False
_SWEEPER_INTERVAL = 0.5

# ---------- 后台索引池：server / watcher 共用，单 worker 串行 ----------
# 重活（尤其视频转写、抽帧）一次只跑一个，避免多 whisper 实例 OOM，也避免阻塞
# FastAPI 事件循环 / watchdog 观察者线程（一个长视频卡住会冻结后续所有文件监控）。
_INDEX_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_INDEX_POOL_STOPPING = False
_MINDOS_UPLOAD_DIR = ".mindos_uploads"

# 全量重建的提交栅栏。重建 manifest 之外的 Watcher/API 请求必须延后到集合切换后
# 执行，否则提交线程可能仍在写 __rebuild 时被 commit 改名，导致 generation 注册表
# 与集合内容不同步。
_REBUILD_SESSION: str | None = None
_DEFERRED_SUBMISSIONS: dict[str, dict] = {}

# ---- P0-4 索引任务状态机 ----
# queued -> processing -> validating -> done
#                          └─────────> failed
# validating = 新索引已原子写入、正在完整性校验（P0-2 的 add_* 已在写后校验，
# 此状态对外标记「写入完成待确认」）。done 只在校验通过后落盘。
# 稳定错误码：失败时落盘 error_code，不暴露完整异常文本（防敏感信息泄漏）。
JOB_STATE_QUEUED = "queued"
JOB_STATE_PROCESSING = "processing"
JOB_STATE_VALIDATING = "validating"
JOB_STATE_DONE = "done"
JOB_STATE_FAILED = "failed"
JOB_STATE_UNKNOWN = "unknown"

# 索引稳定错误码（对外/落盘用）。message 只含稳定措辞；详细异常只写日志。
ERRCODE_PARSE_FAILED = "parse_failed"          # 文档/媒体解析、解码失败
ERRCODE_EMPTY = "empty"                        # 空文件／无可提取文字的可判空态
ERRCODE_ASR_UNAVAILABLE = "asr_unavailable"    # 音视频转写不可用
ERRCODE_EMBED_FAILED = "embed_failed"          # 向量化失败
ERRCODE_WRITE_FAILED = "write_failed"          # Chroma 写入失败（旧索引保留）
ERRCODE_READ_FAILED = "read_failed"            # 完整性校验读取失败（旧索引保留）
ERRCODE_INDEX_CORRUPTED = "index_corrupted"    # 索引损坏闸门：写入被拒绝（阶段B）
ERRCODE_UNKNOWN = "unknown"
MAX_ROUTING_RETRIES = 3


def _index_error_code(exc: BaseException) -> str:
    """映射异常到稳定错误码；未知异常归 ERRCODE_UNKNOWN（message 不落库细节）。"""
    name = type(exc).__name__
    if name in ("EmptyFileError", "ValueError") and "空" in str(exc):
        return ERRCODE_EMPTY
    if name in ("MediaError", "ParseError", "UnsupportedFormatError"):
        return ERRCODE_PARSE_FAILED
    if name in ("ChromaAddError", "HNSWError",):
        return ERRCODE_WRITE_FAILED
    if name in ("ChromaReadError", "InternalError"):
        return ERRCODE_READ_FAILED
    if name in ("EmbeddingError", "ModelNotFoundError", "RuntimeError") and "embed" in type(exc).__module__:
        return ERRCODE_EMBED_FAILED
    return ERRCODE_UNKNOWN


_ERRCODE_MSG = {
    ERRCODE_PARSE_FAILED: "文件解析或媒体解码失败",
    ERRCODE_EMPTY: "文件为空或无可提取内容",
    ERRCODE_ASR_UNAVAILABLE: "音视频转写不可用",
    ERRCODE_EMBED_FAILED: "向量化失败",
    ERRCODE_WRITE_FAILED: "索引写入失败（旧索引已保留）",
    ERRCODE_READ_FAILED: "索引校验读取失败（旧索引已保留）",
    ERRCODE_INDEX_CORRUPTED: "索引已损坏，写入被闸门拒绝（请先恢复或重建索引）",
    ERRCODE_UNKNOWN: "索引任务失败（未知原因）",
}


def _under_frames_dir(p: str) -> bool:
    """帧目录护栏：video_frames/ 下的 jpg 不该被当独立图片再索引（代码级兜底）。"""
    try:
        return Path(p).resolve().is_relative_to(Path(VIDEO_FRAMES_DIR).resolve())
    except Exception:
        return False


def _is_mindos_upload(p: str) -> bool:
    """MindOS 原材料只进入解析/索引，不触发旧 Wiki 自动整理。"""
    try:
        return _MINDOS_UPLOAD_DIR in Path(p).resolve().parts
    except OSError:
        return False


def _mark_job_failed(
    file_path: str,
    error: str,
    strategy_id: str | None = None,
    error_code: str = ERRCODE_UNKNOWN,
) -> None:
    src = str(Path(file_path).absolute())
    preserved = False
    try:
        preserved = get_source_hash(src) is not None
    except Exception:
        preserved = False
    with _JOBS_LOCK:
        _JOBS[src] = {
            "state": "failed",
            "error": error,
            "error_code": error_code,
            "strategy_id": strategy_id or annotations.get_rag_override(src),
            "old_index_preserved": preserved,
            "finished_at": time.time(),
        }
    _persist_index_outcome(
        src, "failed", error, strategy_id,
        error_code=error_code, old_index_preserved=preserved,
    )
    try:
        JobStore.instance().finish_index_job(
            src, JOB_STATE_FAILED, error=error, error_code=error_code,
            old_index_preserved=preserved,
        )
    except Exception as exc:
        logger.warning("持久化索引任务失败状态失败 %s: %s", src, type(exc).__name__)


def _persist_index_outcome(
    source_path: str,
    state: str,
    error: str | None = None,
    strategy_id: str | None = None,
    error_code: str | None = None,
    old_index_preserved: bool = False,
) -> None:
    """索引任务终态落盘（done/failed），重启后失败原因与完成记录不丢失。

    纯增量写一行小记录、频率极低，出现异常只告警不阻塞索引主流程。
    error_code 一并持久化（P0-4）：后端重启后依据稳定码精确重试/展示，
    不依赖异常原文。
    """
    try:
        JobStore.instance().save_index_outcome(
            source_path, state, error, strategy_id,
            error_code=error_code,
            old_index_preserved=old_index_preserved, finished_at=None,
        )
    except Exception as e:
        logger.warning(f"持久化索引任务终态失败 {source_path}: {e}")


def _file_stability_signature(file_path: str) -> tuple | None:
    """单次采样返回 (size, mtime, content_prefix_sha1)；文件不可读/为空返回 None。

    内容指纹取头部 1MB 前缀的 SHA1（P1-1）：成本 ~1ms，能识别「大小+mtime 恰好
    一致、但字节被覆写」（如编辑器原地保存）的极端情况。
    """
    try:
        st = Path(file_path).stat()
        if st.st_size <= 0:
            return None
        with open(file_path, "rb") as f:
            prefix = f.read(_CONTENT_PREFIX_BYTES)
    except OSError:
        return None
    return (st.st_size, st.st_mtime, hashlib.sha1(prefix).hexdigest())


def _wait_file_stable(file_path: str, timeout: float = 120.0, interval: float = 0.5) -> None:
    """等文件稳定再索引——拷贝中的大视频不会被提前读到半截。

    P1-1：同时检查大小、修改时间、内容指纹前缀，连续 _STABLE_SAMPLES 次完全一致
    才算稳定；任一信号变化即重置计数。超时即放弃等待（交由索引阶段判空/幂等兜底）。
    """
    stable = 0
    last: tuple | None = None
    waited = 0.0
    while waited < timeout:
        sig = _file_stability_signature(file_path)
        if sig is not None and sig == last:
            stable += 1
            last = sig
            if stable >= _STABLE_SAMPLES:
                return
        else:
            stable = 0
            last = sig
        time.sleep(interval)
        waited += interval


def submit_index(
    file_path: str,
    force: bool = False,
    strategy_id: str | None = None,
    submit_wiki: bool | None = None,
    rebuild_session: str | None = None,
) -> bool:
    """server / watcher 共用入口：把索引重活提交到后台串行池，调用线程立即返回。

    force=True 用于「文件内容没变、但 caption 等需要重嵌」的场景（标注改了说明）——
    跳过内容哈希增量、强制重建该文件分块（含最新 caption 块）。
    """
    src = str(Path(file_path).absolute())
    if _is_mindos_upload(src):
        # 防御：MindOS 受控上传目录绝不进入旧索引链路（即便事件/扫描已过滤，
        # 共享入口仍兜底，防止其他调用方误投）。
        return False
    with _JOBS_LOCK:
        if _INDEX_POOL_STOPPING:
            logger.info("服务正在停止，保留索引任务待下次启动恢复: %s", file_path)
            return False
    if index_health_blocked():
        # 阶段B 闸门：损坏索引禁止再入队，避免写失败风暴（汇总告警由巡检/启动自检负责）。
        logger.warning("索引已损坏（corrupted），拒绝提交索引任务: %s", file_path)
        return False
    if submit_wiki is None:
        # Web 优先运行模式不再隐式派生旧 Electron Wiki；需要迁移时由显式
        # Wiki 管理 API 提交，避免普通材料索引触发额外模型和后台任务。
        submit_wiki = False
    if strategy_id:
        annotations.set_rag_override(src, strategy_id)
    with _JOBS_LOCK:
        if _REBUILD_SESSION is not None and rebuild_session != _REBUILD_SESSION:
            prior = _DEFERRED_SUBMISSIONS.get(src, {})
            _DEFERRED_SUBMISSIONS[src] = {
                "force": bool(prior.get("force")) or force,
                "strategy_id": strategy_id or prior.get("strategy_id"),
                "submit_wiki": submit_wiki if submit_wiki is not None else prior.get("submit_wiki"),
            }
            logger.info("重建期间延后索引提交，待切换后回放: %s", file_path)
            return True
        routing = resolve_index_target("write")
        accepted = JobStore.instance().enqueue_index_job(
            src, force=force, strategy_id=strategy_id or annotations.get_rag_override(src),
            submit_wiki=bool(submit_wiki), rebuild_session_id=rebuild_session,
            routing_epoch=routing.get("routing_epoch"),
            target_generation_id=routing.get("delta_generation_id"),
        )
        if not accepted:
            logger.info(f"索引任务已在持久化队列中，跳过重复提交: {file_path}")
            return False
        _JOBS[src] = {
            "state": "queued",
            "strategy_id": strategy_id or annotations.get_rag_override(src),
            "submit_wiki": submit_wiki,
            "rebuild_session": rebuild_session,
        }
    _submit_index_worker(file_path, force, strategy_id, submit_wiki)
    return True


def _submit_index_worker(
    file_path: str,
    force: bool,
    strategy_id: str | None,
    submit_wiki: bool,
) -> bool:
    """投递已持久化任务；停机栅栏后绝不向关闭中的线程池提交。"""
    with _JOBS_LOCK:
        if _INDEX_POOL_STOPPING:
            return False
        try:
            _INDEX_POOL.submit(_run_index_job, file_path, force, strategy_id, submit_wiki)
            return True
        except RuntimeError:
            # 任务已在 SQLite 中保持 queued，下次启动 recover_pending_jobs 会重新投递。
            logger.info("索引线程池已停止，任务留待恢复: %s", file_path)
            return False


def begin_rebuild_barrier(session_id: str) -> dict:
    """冻结普通索引提交，返回启动前已经在运行的任务。

    调用方必须在 begin_rebuild 前调用。已有任务可能已经绑定旧正式集合，不能拿来
    填充 rebuild；因此维护操作应中止并在 finish_rebuild_barrier 后正常回放新事件。
    """
    global _REBUILD_SESSION
    with _JOBS_LOCK:
        if _REBUILD_SESSION is not None:
            return {"ok": False, "error": "rebuild_barrier_active", "active": []}
        _REBUILD_SESSION = session_id
        active = [
            path for path, job in _JOBS.items()
            if job.get("state") in {JOB_STATE_QUEUED, JOB_STATE_PROCESSING, JOB_STATE_VALIDATING}
        ]
    return {"ok": True, "active": active}


def finish_rebuild_barrier(session_id: str) -> dict:
    """解除提交栅栏并回放重建期间收到的最终索引请求。"""
    global _REBUILD_SESSION
    with _JOBS_LOCK:
        if _REBUILD_SESSION != session_id:
            return {"ok": False, "error": "rebuild_barrier_mismatch", "replayed": []}
        _REBUILD_SESSION = None
        deferred = list(_DEFERRED_SUBMISSIONS.items())
        _DEFERRED_SUBMISSIONS.clear()
    replayed: list[str] = []
    for path, options in deferred:
        if not Path(path).is_file():
            continue
        if submit_index(path, **options):
            replayed.append(path)
    if replayed:
        logger.info("重建切换完成，已回放 %d 个延后索引任务", len(replayed))
    return {"ok": True, "replayed": replayed}


def reset_rebuild_barrier_for_tests() -> None:
    """仅测试用：清理进程内重建栅栏状态。"""
    global _REBUILD_SESSION
    with _JOBS_LOCK:
        _REBUILD_SESSION = None
        _DEFERRED_SUBMISSIONS.clear()


def restore_rebuild_done(session_id: str, source_paths: list[str]) -> None:
    """重启续跑时恢复已经过指纹/完整性复核的 manifest 终态。"""
    with _JOBS_LOCK:
        if _REBUILD_SESSION != session_id:
            return
        for path in source_paths:
            src = str(Path(path).absolute())
            _JOBS[src] = {"state": JOB_STATE_DONE, "rebuild_session": session_id}


def _run_index_job(
    file_path: str,
    force: bool = False,
    strategy_id: str | None = None,
    submit_wiki: bool = True,
) -> None:
    src = str(Path(file_path).absolute())
    claimed = JobStore.instance().claim_index_job(src)
    if claimed is None:
        # 兼容直接调用 worker 的内部测试/维护脚本；正常路径始终由 submit_index 先落库。
        # 该调用方没有经历 watcher 的文件稳定窗口，按 force 执行避免对不存在的
        # 测试路径等待 120 秒；正式队列仍保留原 force 值。
        force = True
        routing = resolve_index_target("write")
        JobStore.instance().enqueue_index_job(
            src, force=force, strategy_id=strategy_id, submit_wiki=submit_wiki,
            routing_epoch=routing.get("routing_epoch"), target_generation_id=routing.get("delta_generation_id"),
        )
        claimed = JobStore.instance().claim_index_job(src)
        if claimed is None:
            return
    # 以持久化记录为准，重启重放时调用方不必保留原始参数。
    force = bool(claimed.get("force"))
    strategy_id = claimed.get("strategy_id") or strategy_id
    submit_wiki = bool(claimed.get("submit_wiki"))
    routing = resolve_index_target("write")
    if (claimed.get("routing_epoch") is not None and
            int(claimed["routing_epoch"]) != int(routing.get("routing_epoch", 0))):
        # 路由已在任务排队期间切换：不允许把旧目标继续写入，重投到新 epoch。
        if int(claimed.get("attempts") or 0) >= MAX_ROUTING_RETRIES:
            JobStore.instance().finish_index_job(
                src, JOB_STATE_FAILED, error="routing_changed_too_often", error_code=ERRCODE_WRITE_FAILED
            )
            return
        JobStore.instance().finish_index_job(src, "retryable", error="routing_epoch_changed")
        JobStore.instance().enqueue_index_job(
            src, force=force, strategy_id=strategy_id, submit_wiki=submit_wiki,
            rebuild_session_id=claimed.get("rebuild_session_id"),
            routing_epoch=routing.get("routing_epoch"),
            target_generation_id=routing.get("delta_generation_id"),
        )
        _submit_index_worker(file_path, force, strategy_id, submit_wiki)
        return

    def _set(state: str, error: str | None = None, error_code: str | None = None) -> None:
        rebuild_session = None
        with _JOBS_LOCK:
            job = _JOBS.get(src, {})
            job.update(
                {
                    "state": state,
                    "strategy_id": strategy_id or annotations.get_rag_override(src),
                    "submit_wiki": submit_wiki,
                }
            )
            if error is not None:
                job["error"] = error
            if error_code is not None:
                job["error_code"] = error_code
            if state in (JOB_STATE_DONE, JOB_STATE_FAILED):
                job["finished_at"] = time.time()
            _JOBS[src] = job
            rebuild_session = job.get("rebuild_session")
        if rebuild_session and state in (JOB_STATE_DONE, JOB_STATE_FAILED):
            try:
                import rebuild_progress
                rebuild_progress.set_path_state(rebuild_session, src, state)
            except Exception as e:
                logger.warning("持久化重建材料终态失败 %s: %s", src, type(e).__name__)
        # index_jobs 是任务状态唯一权威；内存 _JOBS 仅作状态缓存。
        if state in (JOB_STATE_VALIDATING, JOB_STATE_DONE, JOB_STATE_FAILED):
            JobStore.instance().finish_index_job(
                src, state, error=error, error_code=error_code,
                old_index_preserved=bool((_JOBS.get(src) or {}).get("old_index_preserved")),
            )

    def _old_index_preserved() -> bool:
        """失败后旧索引是否仍完整可读（P0-3：get_source_hash 仅在校验通过时返回 hash）。"""
        try:
            return get_source_hash(src) is not None
        except Exception:
            return False

    _set(JOB_STATE_PROCESSING)
    try:
        # 内容未变的 caption-only 重索引无需等文件稳定（不是新上传/拷贝中）；只有 force
        # 才跳过，避免对一个稳定的旧文件白等 1~2 个稳定采样周期。
        if not force:
            _wait_file_stable(file_path)

        # P0-4：写入阶段完成后进入 validating（P0-2 的 add_* 已完成写后完整性校验，
        # 此处是写入成功→终态确认之间的可观测状态），再落 done。失败走稳定错误码。
        ok = index_file(file_path, force=force, strategy_id=strategy_id)
        if ok:
            _set(JOB_STATE_VALIDATING)
            if submit_wiki:
                try:
                    import wiki_store
                    wiki_store.submit_source(src, force=force)
                except Exception as e:
                    logger.warning(f"提交 Wiki 整理失败 {file_path}: {e}")
            _set(JOB_STATE_DONE)
            _persist_index_outcome(src, JOB_STATE_DONE, strategy_id=strategy_id)
        else:
            # index_file 返回 False：部分分支已落稳定错误文案，但统一在此补 fail 终态。
            preserved = _old_index_preserved()
            _set(JOB_STATE_FAILED, "索引器未产出可用向量（文件可能没有可提取文字或媒体内容）",
                 error_code=ERRCODE_EMPTY)
            with _JOBS_LOCK:
                _JOBS[src]["old_index_preserved"] = preserved
            JobStore.instance().finish_index_job(
                src, JOB_STATE_FAILED, error="索引器未产出可用向量（文件可能没有可提取文字或媒体内容）",
                error_code=ERRCODE_EMPTY, old_index_preserved=preserved,
            )
            _persist_index_outcome(
                src, JOB_STATE_FAILED, "索引器未产出可用向量（文件可能没有可提取文字或媒体内容）",
                strategy_id=strategy_id, error_code=ERRCODE_EMPTY,
                old_index_preserved=preserved,
            )
    except EmptyFileError as e:
        logger.warning(f"跳过空文件 {file_path}: {e}")
        _mark_job_failed(file_path, _ERRCODE_MSG[ERRCODE_EMPTY], strategy_id, ERRCODE_EMPTY)
    except IndexCorruptedError as e:
        # 阶段B：索引损坏闸门拒绝写入 → 稳定错误码落盘，不重试、不重入队。
        logger.error("索引损坏闸门：任务失败 %s: %s", file_path, e)
        _set(JOB_STATE_FAILED, _ERRCODE_MSG[ERRCODE_INDEX_CORRUPTED], error_code=ERRCODE_INDEX_CORRUPTED)
        _persist_index_outcome(
            src, JOB_STATE_FAILED, _ERRCODE_MSG[ERRCODE_INDEX_CORRUPTED],
            strategy_id=strategy_id, error_code=ERRCODE_INDEX_CORRUPTED,
        )
    except Exception as e:
        logger.error(f"后台索引失败 {file_path}: {type(e).__name__}: {e}")
        code = _index_error_code(e)
        preserved = _old_index_preserved()
        _set(JOB_STATE_FAILED, _ERRCODE_MSG[code], error_code=code)
        with _JOBS_LOCK:
            _JOBS[src]["old_index_preserved"] = preserved
        JobStore.instance().finish_index_job(
            src, JOB_STATE_FAILED, error=_ERRCODE_MSG[code], error_code=code,
            old_index_preserved=preserved,
        )
        _persist_index_outcome(
            src, JOB_STATE_FAILED, _ERRCODE_MSG[code], strategy_id=strategy_id,
            old_index_preserved=preserved,
        )


def get_job(source_path: str) -> dict:
    src = str(Path(source_path).absolute())
    persisted = JobStore.instance().get_index_job(src)
    if persisted:
        return persisted
    with _JOBS_LOCK:
        return dict(_JOBS.get(src, {"state": "unknown"}))


def pending_jobs() -> int:
    """后台池中尚未结束（queued/processing）的任务数——/api/reindex 等待排空用。"""
    return sum(1 for j in JobStore.instance().list_index_jobs() if j.get("state") in ("queued", "retryable", "processing", "validating"))


def list_active_jobs() -> list[dict]:
    """返回当前所有进行中/排队的索引任务"""
    return [
        {"path": row["source_path"], "name": Path(row["source_path"]).name,
         "state": row["state"], "error": row.get("error", "")}
        for row in JobStore.instance().list_index_jobs()
        if row.get("state") in ("queued", "retryable", "processing", "validating")
    ]


def list_jobs(include_done: bool = False) -> list[dict]:
    """返回任务状态；默认含进行中和失败项，巡检页面可据此重试。"""
    rows = [
        {"path": row["source_path"], "name": Path(row["source_path"]).name, **row}
        for row in JobStore.instance().list_index_jobs(include_done=include_done)
    ]
    rows.sort(key=lambda item: (item.get("state") not in {"processing", "queued"}, item["name"].lower()))
    return rows


def recover_pending_jobs() -> int:
    """后端启动时重放持久化队列；中断中的任务已由 store 归还为 queued。"""
    replayed = 0
    for row in JobStore.instance().recover_index_jobs():
        path = row["source_path"]
        if not Path(path).is_file():
            JobStore.instance().finish_index_job(path, JOB_STATE_FAILED, error="source_missing")
            continue
        with _JOBS_LOCK:
            _JOBS[path] = {"state": JOB_STATE_QUEUED, "strategy_id": row.get("strategy_id"),
                            "submit_wiki": bool(row.get("submit_wiki")),
                            "rebuild_session": row.get("rebuild_session_id")}
        if _submit_index_worker(path, bool(row.get("force")), row.get("strategy_id"),
                                bool(row.get("submit_wiki"))):
            replayed += 1
    return replayed


def recover_infrastructure_failures(limit: int = 32) -> int:
    """健康索引恢复后受控重放基础设施类失败，绝不重跑文件业务失败。"""
    replayed = 0
    for row in JobStore.instance().requeue_recoverable_failures(infrastructure_only=True, limit=limit):
        path = row["source_path"]
        # Historical MindOS uploads must never return to the retired raw
        # material vector queue through an automatic recovery path.
        if _is_mindos_upload(path):
            continue
        if not Path(path).is_file():
            JobStore.instance().finish_index_job(path, JOB_STATE_FAILED, error="source_missing", error_code=ERRCODE_PARSE_FAILED)
            continue
        with _JOBS_LOCK:
            _JOBS[path] = {"state": JOB_STATE_QUEUED, "strategy_id": row.get("strategy_id"),
                            "submit_wiki": bool(row.get("submit_wiki"))}
        if _submit_index_worker(path, bool(row.get("force")), row.get("strategy_id"), bool(row.get("submit_wiki"))):
            replayed += 1
    return replayed


def replay_due_transient_failures(limit: int = 8) -> int:
    """Small periodic retry budget for model/network-like transient failures."""
    replayed = 0
    rows = JobStore.instance().requeue_recoverable_failures(failure_classes=("transient",), limit=limit)
    for row in rows:
        path = row["source_path"]
        if _is_mindos_upload(path):
            continue
        if Path(path).is_file() and _submit_index_worker(path, bool(row.get("force")), row.get("strategy_id"), bool(row.get("submit_wiki"))):
            replayed += 1
    return replayed


def shutdown_pool() -> None:
    """受控停止索引池：取消未开始任务，等待已领取任务到达安全终态。

    ``wait=True`` 是 Chroma/HNSW 安全关闭的关键：已领取任务可能正在执行原子写入，
    不能让 lifecycle 在写入中途继续释放数据目录。被取消的 future 保留 SQLite 的
    queued 状态，下一次启动由 recover_pending_jobs 重放。
    """
    global _INDEX_POOL_STOPPING
    with _JOBS_LOCK:
        _INDEX_POOL_STOPPING = True
    try:
        _INDEX_POOL.shutdown(wait=True, cancel_futures=True)
    except Exception as exc:
        logger.warning("受控停止索引线程池失败: %s", type(exc).__name__)


def _ensure_watcher_sweeper() -> None:
    """确保延迟合清扫线程已启动（首次收到文件事件/启动 watcher 时懒初始化）。"""
    global _PENDING_SWEEPER_STARTED
    with _PENDING_LOCK:
        if _PENDING_SWEEPER_STARTED:
            return
        _PENDING_SWEEPER_STARTED = True
    threading.Thread(
        target=_pending_sweeper_loop, name="watcher-debounce", daemon=True
    ).start()


def _pending_sweeper_loop() -> None:
    """周期检查到期的待处理标记：窗口结束后重新检查文件并提交最后版本（P1-1）。"""
    while True:
        time.sleep(_SWEEPER_INTERVAL)
        now = time.time()
        with _PENDING_LOCK:
            due = [p for p, d in _pending_index.items() if d <= now]
            for p in due:
                _pending_index.pop(p, None)
        for src in due:
            _submit_pending_event(src)
        try:
            replay_due_transient_failures()
        except Exception as exc:
            logger.warning("自动重试暂态索引任务失败: %s", type(exc).__name__)


def _submit_pending_event(src: str) -> None:
    """窗口结束后重新检查文件仍有效，再提交最终版本。文件已删/被移入帧目录则跳过。"""
    try:
        if not Path(src).exists():
            return
        if not is_supported(src) or _under_frames_dir(src):
            return
    except Exception:
        return
    logger.info(f"延迟合并窗口结束，提交索引: {src}")
    # 交后台池：内部 _run_index_job 再执行 _wait_file_stable 多信号稳定确认，
    # 确保读到的是最终完整版本，而非仍在写入的半截文件。
    submit_index(src)


class DocumentHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory:
            self._handle_deletion(event.src_path)

    def _handle(self, file_path: str, event_type: str):
        if not is_supported(file_path):
            return
        if _under_frames_dir(file_path):
            return
        if _is_mindos_upload(file_path):
            # MindOS 受控上传目录（.mindos_uploads）由 material_jobs 处理，
            # 绝不落入旧自动向量化链路（阶段A 准入）。
            return

        # P1-1 延迟合并：窗口内同路径只保留一个待处理标记，不丢最终事件。窗口结束后
        # 由清扫线程重新检查并提交最后版本；连续写入只触发一次索引，减少重复重嵌。
        src = str(Path(file_path).absolute())
        with _PENDING_LOCK:
            if src in _pending_index:
                return  # 窗口内已挂标记，只保留一个（不推迟截止，避免持续写入饿死提交）
            _pending_index[src] = time.time() + DEDUP_WINDOW
        _ensure_watcher_sweeper()
        logger.info(f"检测到{event_type}: {file_path}（延迟{DEDUP_WINDOW}s 合并后提交）")

    def _handle_deletion(self, file_path: str):
        source_path = str(Path(file_path).absolute())
        # 文件已删除，取消该路径的待处理标记，避免清扫线程做无用功
        with _PENDING_LOCK:
            _pending_index.pop(source_path, None)
        delete_file(source_path)
        # 文件从监控目录被删 → 其标注成孤儿，连带清理（与 API 删除口径一致）
        try:
            annotations.delete(source_path)
        except Exception:
            pass
        logger.info(f"已移除: {file_path}")


def _asr_caps_fingerprint() -> str:
    """ASR 能力指纹：装上 whisper 后，旧音视频会被重建以补出转写。"""
    return f"w{int(WHISPER_ENABLED and whisper_loadable())}"


def _video_caps_fingerprint() -> str:
    """视频能力指纹：装上 whisper / 改 OCR 开关 → 同文件被重建以补出转写/OCR。

    用 whisper_loadable()（便宜，不 load 模型）而非 whisper_available()，否则启动扫描
    第一个视频就会强行加载/下载多 GB 模型、拖慢服务就绪（即便该视频已索引会被哈希跳过）。
    """
    o = int(VIDEO_FRAME_OCR_ENABLED and OCR_ENABLED)
    return f"{_asr_caps_fingerprint()}o{o}"


def _merge_transcript(segs: list[dict], window: float) -> tuple[list[str], list[dict]]:
    """把 whisper segment 按 window 秒聚成块，每块 start_time=该块首段 start。"""
    chunks: list[str] = []
    pcm: list[dict] = []
    buf: list[str] = []
    buf_start = None
    last_end = None
    for s in segs:
        if buf_start is None:
            buf_start = s["start"]
        buf.append(s["text"])
        last_end = s["end"]
        if last_end - buf_start >= window:
            chunks.append(" ".join(buf))
            pcm.append({"start_time": float(buf_start), "end_time": float(last_end), "modality": "transcript"})
            buf, buf_start = [], None
    if buf:
        chunks.append(" ".join(buf))
        pcm.append({"start_time": float(buf_start or 0.0), "end_time": float(last_end or 0.0), "modality": "transcript"})
    return chunks, pcm


# 标注「说明」作为一个独立文本块的前缀——给重排/展示一个稳定可辨识的语义锚，
# 也便于命中后在 UI 区分它是用户说明而非正文。
_CAPTION_PREFIX = "说明："


def _append_caption_chunk(
    source_path: str,
    chunks: list[str],
    pcm: list[dict] | None,
) -> tuple[list[str], list[dict] | None]:
    """若该文件有用户「说明」标注，追加一个 caption 文本块（进文本向量空间→可被语义搜到）。

    返回 (chunks, per_chunk_metadata)。caption 块带 modality='caption'，便于检索期识别。
    注意：caption 取自 sidecar，与文件内容无关——每次（重）索引都重新读取并追加，
    所以 sidecar 改了 caption 后对该文件 force 重索引即可让 caption 块同步更新。
    """
    caption = annotations.caption_of(source_path)
    if not caption:
        # 无 caption：保持原样。已有 caption 块会在 add_file_chunks 的「先删后写」中被清掉。
        return chunks, pcm
    cap_chunk = f"{_CAPTION_PREFIX}{caption}"
    new_chunks = list(chunks) + [cap_chunk]
    # per_chunk_metadata 必须与 chunks 等长：原本无 pcm 则给前面的块补空 dict
    if pcm is None:
        new_pcm = [{} for _ in chunks] + [{"modality": "caption"}]
    else:
        new_pcm = list(pcm) + [{"modality": "caption"}]
    return new_chunks, new_pcm


def _chunk_parts_with_meta(parts: list[dict]) -> tuple[list[str], list[dict]]:
    """按结构化 part 分块，并为每个 chunk 附加 part_id / page / table_ordinal 元数据。

    每个 chunk 只归属一个 part，故元数据可精确引用（为后续精确页码/表格引用做准备）。
    内嵌图片的 OCR 文本块带 modality=embedded_image_ocr + part_id（进入文本向量空间，
    可被普通检索命中，且能区分“文字命中”与“视觉命中”）。无 part 元数据时各字段省略。
    """
    chunks: list[str] = []
    pcm: list[dict] = []
    for part in parts:
        text = (part.get("text") or "").strip()
        if not text:
            continue
        meta: dict = {}
        part_id = part.get("id") or part.get("partId")
        if part_id:
            meta["part_id"] = part_id
        location = part.get("location") or {}
        part_type = part.get("part_type")
        if part_type == "table":
            ordinal = part.get("ordinal")
            if ordinal is not None:
                meta["table_ordinal"] = ordinal
        elif part_type == "image":
            meta["modality"] = "embedded_image_ocr"
            page = location.get("page")
            if page is not None:
                meta["page"] = page
            else:
                paragraph = location.get("paragraph")
                if paragraph is not None:
                    meta["paragraph"] = paragraph
        else:
            page = location.get("page")
            if page is not None:
                meta["page"] = page
        for chunk in chunk_text(text):
            chunks.append(chunk)
            pcm.append(dict(meta))
    return chunks, pcm


def _ocr_embedded_images(material_id: str, parts: list[dict]) -> list[dict]:
    """对内嵌图片 part 执行 OCR 并回写文本与状态；OCR 不可用时明确降级状态。

    ocr_status：ok（有文字）| empty（引擎可用但无文字）| unavailable（引擎不可用）。
    OCR 失败只影响派生图片文本，绝不改变资料整体可用状态。
    """
    image_parts = [p for p in parts if p["part_type"] == "image"]
    if not image_parts:
        return parts
    ocr_ready = OCR_ENABLED and ocr_available()
    store = derived_store.DerivedStore.instance()
    for part in image_parts:
        ocr_text = ""
        if ocr_ready:
            image_path = store.image_file_path(material_id, part.get("artifact_key") or "")
            if image_path and image_path.is_file():
                ocr_text = ocr_image(str(image_path)).strip()
        ocr_status = (
            "ok" if ocr_ready and ocr_text else ("empty" if ocr_ready else "unavailable")
        )
        store.set_image_ocr(material_id, part["id"], ocr_text, ocr_status)
        part["text"] = ocr_text
        part["image_meta"] = dict(part.get("image_meta") or {})
        part["image_meta"]["ocr_status"] = ocr_status
    return parts


def _submit_material_summary(source_path: str, force: bool = False) -> None:
    """P14-03：MindOS 资料索引成功后提交自动摘要任务（后台异步，不阻塞索引/HTTP）。

    非 MindOS 资料（material_id 解析不到）不生成摘要；提交失败只记日志。
    force=True 用于「已明确判定文本为空」的早退路径（旧 chunks 刚被主动清除）：
    摘要须落 skipped 而非沿用旧记录，绕过 empty+ok 深度防御（那是防读取故障的）。
    """
    material_id = derived_store.material_id_for_source(source_path)
    if not material_id:
        return
    try:
        from mindos.derived import submit_summary
        submit_summary(material_id, source_path, force=force)
    except Exception as exc:
        logger.warning("提交摘要任务失败 %s: %s", source_path, exc)


def _submit_material_analysis(source_path: str) -> None:
    """P14-04：MindOS 资料索引成功后提交标签候选 + 实体抽取任务（后台异步）。

    与摘要同一套解析链路；非 MindOS 资料不生成；提交失败只记日志。
    """
    material_id = derived_store.material_id_for_source(source_path)
    if not material_id:
        return
    try:
        from mindos.derived import submit_analysis
        submit_analysis(material_id, source_path)
    except Exception as exc:
        logger.warning("提交分析任务失败 %s: %s", source_path, exc)


def _index_video(file_path: str, source_path: str, base_metadata: dict) -> bool:
    """视频索引：关键帧→CLIP视觉+帧OCR；音轨→whisper转写。

    转写块 + 帧OCR 块合并为单次 add_file_chunks（否则二次写会互删）；帧走 add_image_frames。
    graceful：ffmpeg 缺/损坏/无音轨/无视频流/whisper 缺 都按缺失部分跳过，不炸管线。
    """
    import shutil as _sh
    import video

    if not video.ffmpeg_available():
        logger.warning(f"ffmpeg 不可用，跳过视频: {file_path}")
        return False
    try:
        info = video.probe(source_path)
    except video.MediaError as e:
        logger.warning(f"视频损坏/不可读，跳过: {file_path}: {e}")
        return False

    file_sha1 = (base_metadata.get("content_hash") or "nohash").split(":")[0][:16] or "nohash"
    frame_dir = str(Path(VIDEO_FRAMES_DIR) / file_sha1)
    work_dir = str(Path(VIDEO_WORK_DIR) / file_sha1)

    # P0-2：禁止处理开始前 delete_file（原「先删旧再写新」在 ASR/OCR/CLIP/Embedding
    # 任一步失败时会留下空索引）。幂等改由 generation 原子替换保证：新代写入并校验
    # 通过后才切代、删旧代记录与旧帧目录；任一步失败旧转写/旧帧索引原样保留。

    frame_embs: list[list[float]] = []
    frame_meta: list[dict] = []
    ocr_chunks: list[str] = []
    ocr_pcm: list[dict] = []
    frames: list[dict] = []

    # ---- A) 关键帧：抽帧一次，CLIP 视觉向量 + 可选帧 OCR 共用 ----
    do_ocr = VIDEO_FRAME_OCR_ENABLED and OCR_ENABLED
    if info["has_video"] and (CLIP_ENABLED or do_ocr):
        try:
            frames = video.extract_frames(source_path, frame_dir, info)
        except video.MediaError as e:
            logger.warning(f"抽帧失败（继续仅转写）: {e}")
            frames = []
        for fr in frames:
            ts = float(fr.get("timestamp") or 0.0)
            if CLIP_ENABLED:
                emb = embed_image_clip(fr["path"])
                if emb:
                    frame_embs.append(emb)
                    frame_meta.append({"frame_path": fr["path"], "start_time": ts})
            if do_ocr:
                t = ocr_image(fr["path"]).strip()
                if t:
                    ocr_chunks.append(t)
                    ocr_pcm.append({"start_time": ts, "modality": "ocr"})
    frames_ok = True
    if frame_embs:
        # P0-2：帧索引原子替换失败（旧帧保留）须整体判失败，让管线可重试
        frames_ok = add_image_frames(source_path, frame_embs, frame_meta, base_metadata)
    elif frames:
        # 抽了帧但没建视觉向量（如 CLIP 未启用，只用帧做了 OCR）：磁盘帧无任何
        # 图片集合记录引用，delete 时无从清理 → 直接删帧目录，避免孤儿 jpg。
        import shutil as _sh0
        _sh0.rmtree(frame_dir, ignore_errors=True)

    # ---- B) 转写：抽音轨 → whisper → 按 TRANSCRIPT_CHUNK_SEC 合并块 ----
    tr_chunks: list[str] = []
    tr_pcm: list[dict] = []
    if info["has_audio"] and WHISPER_ENABLED and whisper_available():
        wav = None
        try:
            wav = video.extract_audio_16k_mono(
                source_path, str(Path(work_dir) / "audio_16k.wav"), info=info
            )
        except video.MediaError as e:
            logger.warning(f"抽音轨失败（仅帧）: {e}")
        if wav:
            # 转写墙钟上限：max(600s, 时长×RTF)，封顶单视频对后台池的占用
            dur = info.get("duration") or 0.0
            max_sec = max(600.0, dur * WHISPER_TIMEOUT_RTF) if dur else 600.0
            segs = transcribe_audio(wav, max_seconds=max_sec)
            tr_chunks, tr_pcm = _merge_transcript(
                segs, float(base_metadata.get("rag_transcript_chunk_sec") or rag_strategy.transcript_chunk_sec())
            )
        _sh.rmtree(work_dir, ignore_errors=True)  # wav 是中间产物，索引完即删

    # ---- C) 文本集合：转写块 + 帧OCR块 + 用户「说明」块 合并为单次写（否则二次写互删！）----
    all_chunks = tr_chunks + ocr_chunks
    all_pcm = tr_pcm + ocr_pcm
    # 用户说明：让纯视觉/静音视频也能被「公司宣传片」这类语义搜到。即便上面无任何文本块，
    # 只要有 caption 也要单独写一条 caption 块。
    all_chunks, all_pcm = _append_caption_chunk(source_path, all_chunks, all_pcm)
    if all_chunks:
        embs = embed_batch_texts(all_chunks)
        if embs:
            add_file_chunks(
                source_path, "video", all_chunks, embs, base_metadata,
                per_chunk_metadata=all_pcm,
            )

    logger.info(
        f"视频已索引: {base_metadata.get('file_name', source_path)} "
        f"（{len(frame_embs)} 帧 / 文本块 {len(all_chunks)}：转写 {len(tr_chunks)} + 帧OCR {len(ocr_chunks)}）"
    )
    # 成功判据：至少建了帧 或 文本块，且已建部分全部写入成功
    return bool(frame_embs or all_chunks) and frames_ok


def _index_audio(file_path: str, source_path: str, base_metadata: dict) -> bool:
    """音频索引：抽 16k wav → whisper 转写 → 文本向量化。

    手机录音和普通音频文件都走这个分支。输出块带 start_time/end_time，便于 App
    后续做录音时间轴回跳或片段展示。
    """
    import shutil as _sh
    import video

    if not video.audio_decode_available():
        logger.warning(f"音频解码器不可用（需要 ffmpeg/ffprobe 或 PyAV），跳过音频: {file_path}")
        return False
    if not (WHISPER_ENABLED and whisper_available()):
        logger.warning(f"ASR 不可用，跳过音频: {file_path}")
        return False
    try:
        info = video.probe(source_path)
    except video.MediaError as e:
        logger.warning(f"音频损坏/不可读，跳过: {file_path}: {e}")
        return False
    if not info.get("has_audio"):
        logger.warning(f"音频文件无音轨，跳过: {file_path}")
        return False

    file_sha1 = (base_metadata.get("content_hash") or "nohash").split(":")[0][:16] or "nohash"
    work_dir = str(Path(VIDEO_WORK_DIR) / f"audio_{file_sha1}")
    # P0-2：禁止处理开始前 delete_file——抽音轨/转写/向量化任一步失败时，
    # 旧转写块原样保留仍可检索；成功写入由 add_file_chunks 原子替换完成切换。

    chunks: list[str] = []
    pcm: list[dict] = []
    try:
        wav = video.extract_audio_16k_mono(
            source_path,
            str(Path(work_dir) / "audio_16k.wav"),
            info=info,
        )
        if wav:
            dur = info.get("duration") or 0.0
            max_sec = max(600.0, dur * WHISPER_TIMEOUT_RTF) if dur else 600.0
            segs = transcribe_audio(wav, max_seconds=max_sec)
            chunks, pcm = _merge_transcript(
                segs, float(base_metadata.get("rag_transcript_chunk_sec") or TRANSCRIPT_CHUNK_SEC)
            )
    except video.MediaError as e:
        logger.warning(f"音频抽取失败: {e}")
    finally:
        _sh.rmtree(work_dir, ignore_errors=True)

    chunks, pcm = _append_caption_chunk(source_path, chunks, pcm)
    if not chunks:
        logger.warning(f"音频无可索引转写内容: {file_path}")
        # P14-03：无有效转写 → 提交空文本摘要任务，使详情显示「暂无摘要」而非 pending
        _submit_material_summary(source_path)
        # P14-04：空文本下标签候选/实体同样标记 skipped（不展示 pending 闪烁）
        _submit_material_analysis(source_path)
        return False

    embeddings = embed_batch_texts(chunks)
    if not embeddings:
        logger.warning(f"音频向量为空，跳过: {file_path}")
        return False

    ok = add_file_chunks(
        source_path,
        "audio",
        chunks,
        embeddings,
        base_metadata,
        per_chunk_metadata=pcm,
    )
    if ok:
        _submit_material_summary(source_path)
        _submit_material_analysis(source_path)
        logger.info(f"音频已索引: {base_metadata.get('file_name', source_path)}（转写块 {len(chunks)}）")
    return ok


def _file_type_hint(ext: str) -> str:
    if ext in SUPPORTED_VIDEO_EXTENSIONS:
        return "video"
    if ext in SUPPORTED_AUDIO_EXTENSIONS:
        return "audio"
    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    return "text"


def _ocr_scanned_pdf(file_path: str) -> str:
    """Render textless PDF pages and OCR them, so scanned brochures/contracts are indexable."""
    if not OCR_ENABLED:
        return ""
    import tempfile
    import fitz

    Path(VIDEO_WORK_DIR).mkdir(parents=True, exist_ok=True)
    texts: list[str] = []
    try:
        document = fitz.open(file_path)
        with tempfile.TemporaryDirectory(prefix="pdf-ocr-", dir=str(VIDEO_WORK_DIR)) as temp_dir:
            for page_index, page in enumerate(document):
                image_path = Path(temp_dir) / f"page-{page_index + 1}.png"
                page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False).save(str(image_path))
                text = ocr_image(str(image_path)).strip()
                if text:
                    texts.append(f"第 {page_index + 1} 页\n{text}")
        document.close()
    except Exception as exc:
        logger.warning("扫描 PDF OCR 失败 %s: %s", file_path, exc)
        return ""
    if texts:
        logger.info("扫描 PDF OCR 完成: %s（%d 页有文字）", file_path, len(texts))
    return "\n\n".join(texts)


def _index_fingerprint(file_path: str, strategy_id: str | None = None) -> str:
    ext = Path(file_path).suffix.lower()
    content_hash = file_hash(file_path)
    if not content_hash:
        return ""
    effective_strategy = strategy_id or annotations.get_rag_override(str(Path(file_path).absolute()))
    rag_fingerprint = rag_strategy.fingerprint_for_file_type(_file_type_hint(ext), effective_strategy)
    if ext in SUPPORTED_VIDEO_EXTENSIONS:
        return f"{content_hash}:{_video_caps_fingerprint()}:{rag_fingerprint}"
    if ext in SUPPORTED_AUDIO_EXTENSIONS:
        return f"{content_hash}:{_asr_caps_fingerprint()}:{rag_fingerprint}"
    return f"{content_hash}:{rag_fingerprint}"


def index_file(file_path: str, force: bool = False, strategy_id: str | None = None) -> bool:
    """解析 → 分块 → 批量嵌入 → 按源文件写入（文本/图片走 BGE 文本空间；视频走专用分支）。

    增量：内容哈希与库内一致则跳过。视频的比对哈希额外掺入能力指纹（whisper/OCR 开关），
    装上 whisper 后同一视频会被重建以补出转写。
    """
    source_path = str(Path(file_path).absolute())
    if index_health_blocked():
        # 阶段B 闸门：损坏索引不得再写，调用方（后台任务）会落稳定错误码。
        logger.warning("索引已损坏（corrupted），拒绝索引: %s", file_path)
        raise IndexCorruptedError("index_corrupted: 索引已损坏，拒绝索引任务")
    effective_strategy = strategy_id or annotations.get_rag_override(source_path)
    cmp_hash = _index_fingerprint(file_path, effective_strategy)

    if not force and cmp_hash and get_source_hash(source_path) == cmp_hash:
        logger.info(f"内容未变，跳过: {file_path}")
        return True

    result = parse_file(file_path)
    file_type = result["file_type"]
    result["metadata"]["content_hash"] = cmp_hash
    chunk_size, chunk_overlap, resolved_strategy_id = rag_strategy.chunk_params_for_file_type(
        file_type, effective_strategy
    )
    result["metadata"]["rag_strategy"] = resolved_strategy_id
    result["metadata"]["chunk_size"] = chunk_size
    result["metadata"]["chunk_overlap"] = chunk_overlap
    result["metadata"]["rag_transcript_chunk_sec"] = rag_strategy.transcript_chunk_sec(resolved_strategy_id)

    if file_type == "video":
        return _index_video(file_path, source_path, result["metadata"])

    if file_type == "audio":
        return _index_audio(file_path, source_path, result["metadata"])

    caption = annotations.caption_of(source_path)
    # P14-01：DOCX/PDF 结构化 part（段落/表格/页面）；仅文本类文档可能产出。
    parts = result.get("parts") or []
    # 文档类型始终以 material_id + input_hash 幂等写入 parts——即使本次解析结果为空
    # （如扫描版 PDF 无文本层），也要用空列表触发清理，避免详情页残留旧表格/内容块。
    if file_type == "text":
        material_id = derived_store.material_id_for_source(source_path)
        if material_id:
            parts = derived_store.DerivedStore.instance().upsert_document_parts(
                material_id, str(cmp_hash or ""), parts
            )
            # P14-02：内嵌图片 OCR（复用 embedder.ocr_image），回写文本与状态。
            parts = _ocr_embedded_images(material_id, parts)

    if file_type == "image":
        # ① 视觉向量 → 图片集合（Chinese-CLIP，支持以中文描述搜图）
        if CLIP_ENABLED:
            clip_emb = embed_image_clip(file_path)
            if clip_emb:
                add_image_vector(source_path, clip_emb, result["metadata"])
        else:
            clip_emb = []
        # ② OCR 文本 → 文本集合（与文本统一空间，可被分块/重排/阈值一致处理）
        text = ocr_image(file_path) if OCR_ENABLED else ""
        if not text.strip():
            if INDEX_EMPTY_OCR_IMAGES:
                # 旧策略：无 OCR 文字时按文件名兜底入库
                text = result["metadata"].get("file_name", "")
                result["metadata"]["ocr_empty"] = True
            elif not caption:
                # VLM 不得在 watcher 中直接调用。已迁移到 material_jobs 的图片由
                # MaterialWorker 在快照提交后统一调度；兼容旧索引入口仅登记同一低优先级
                # 派生任务，不阻塞索引，也不把未确认材料描述写入 RAG 文本集合。
                material_id = derived_store.material_id_for_source(source_path)
                if material_id:
                    from mindos.derived import submit_visual_description

                    submit_visual_description(material_id, source_path)
                # 顺手清掉可能残留的旧 caption 块（用户刚把 caption 删空 + force 重索引的情形）。
                delete_text_chunks(source_path)
                if clip_emb:
                    logger.info(f"纯图：仅建立视觉索引，VLM 描述已排队: {file_path}")
                else:
                    logger.warning("图片无文字且视觉索引不可用，跳过: %s", file_path)
                # P14-03：无 OCR 文本 → 空文本摘要标记 skipped
                _submit_material_summary(source_path, force=True)
                # P14-04：空文本下标签候选/实体同样标记 skipped
                _submit_material_analysis(source_path)
                return bool(clip_emb)
            # else: 有 caption、无 OCR → 正文留空，靠下方 caption 块入文本集合（LOGO 场景）
        result["text"] = text
    else:
        if (
            file_type == "text" and Path(file_path).suffix.lower() == ".pdf"
            and not result["text"].strip()
        ):
            result["text"] = _ocr_scanned_pdf(file_path)
            if result["text"].strip():
                result["metadata"]["pdf_ocr"] = True
        if not result["text"].strip() and not caption and not parts:
            # 正文为空、无用户说明且无结构化 part（如只有表格的 DOCX）→ 跳过。
            logger.warning(f"文件无文字内容: {file_path}")
            # P14-03：先清掉旧文本块，再提交空文本摘要任务——否则摘要输入会读到
            # 旧 chunks、可能沿用旧 hash 保留旧摘要；清理后判空落为 skipped。
            delete_text_chunks(source_path)
            _submit_material_summary(source_path, force=True)
            # P14-04：空文本同样清掉旧派生输入后标记 skipped（旧候选/实体不残留）
            _submit_material_analysis(source_path)
            return False
        # else: 正文空但有 caption（如扫描件/无文字 PDF + 用户说明）→ 靠下方 caption 块入库

    # P14-01：有结构化 part 时按 part 分块——每个 chunk 携带 part_id / page /
    # table_ordinal 元数据，供后续精确引用；表格由此进入可检索文本。无 part（如
    # 扫描版 PDF 仅靠 OCR 文本）时退回平铺文本分块，与既有链路一致。
    if file_type == "text" and parts:
        chunks, pcm = _chunk_parts_with_meta(parts)
    else:
        chunks = chunk_text(result["text"]) if result["text"].strip() else []
        pcm = None
    # 追加用户「说明」块（若有）——进文本向量空间，可被语义搜到
    chunks, pcm = _append_caption_chunk(source_path, chunks, pcm)
    if not chunks:
        # 既无正文也无 caption：清掉旧文本块后退出（图片已建视觉索引者视情况成功）
        delete_text_chunks(source_path)
        logger.warning(f"分块为空，跳过文本集合: {file_path}")
        # P14-03：空文本 → 提交空文本摘要任务，使详情显示「暂无摘要」而非 pending
        _submit_material_summary(source_path, force=True)
        # P14-04：空文本下标签候选/实体同样标记 skipped
        _submit_material_analysis(source_path)
        return file_type == "image"

    embeddings = embed_batch_texts(chunks)
    if not embeddings:
        logger.warning(f"向量为空，跳过: {file_path}")
        return False

    ok = add_file_chunks(
        source_path, file_type, chunks, embeddings, result["metadata"],
        per_chunk_metadata=pcm,
    )
    # P14-03：文本/OCR 块入库成功后提交自动摘要（文字/OCR 材料；音频在 _index_audio 提交）
    if ok:
        _submit_material_summary(source_path)
        # P14-04：入库成功后同步提交标签候选 + 实体抽取（后台池，hash 未变自动跳过）
        _submit_material_analysis(source_path)
    return ok


def scan_existing(force: bool = False, rebuild_session: str | None = None) -> dict:
    """扫描 watch 目录并提交增量索引任务。

    返回本轮统计（/api/reindex 据此等待任务终态并核对提交数）：
    {"total": 非空支持文件数, "skipped": 已索引跳过数, "submitted": 新提交路径,
     "already_pending": 因已在队列被去重跳过的路径,
     "candidates": 本轮应当被索引的全部材料路径}。

    force=True 用于 schema 迁移和 /api/reindex：旧正式集合仍在线供查询，不能用
    其中的 content_hash 判定 __rebuild 已完成，否则会产生空/半成品 rebuild 集合。
    """
    folder = Path(WATCH_FOLDER)
    if not folder.exists():
        folder.mkdir(parents=True)
        return {
            "total": 0, "skipped": 0, "submitted": [], "already_pending": [],
            "candidates": [], "fingerprints": {}, "sources": [],
        }

    files = list(folder.rglob("*"))
    supported = []
    empty_files = []
    for file_path in files:
        if not file_path.is_file() or not is_supported(str(file_path)) or _under_frames_dir(str(file_path)):
            continue
        if _is_mindos_upload(str(file_path)):
            continue
        try:
            if file_path.stat().st_size == 0:
                empty_files.append(str(file_path))
                continue
        except OSError as e:
            logger.warning(f"扫描文件状态失败，跳过 {file_path}: {e}")
            continue
        supported.append(str(file_path))

    for file_path in empty_files:
        logger.warning(f"扫描时跳过空文件: {file_path}")
        _mark_job_failed(file_path, f"文件为空，无法索引: {file_path}")

    # 过滤：已索引且内容哈希未变的文件直接跳过，不占后台池
    skipped = 0
    to_index = []
    fingerprints: dict[str, str] = {}
    for file_path in supported:
        try:
            cmp_hash = _index_fingerprint(file_path, annotations.get_rag_override(file_path))
        except Exception:
            cmp_hash = ""
        if not force and cmp_hash and get_source_hash(file_path) == cmp_hash:
            skipped += 1
            fingerprints[str(Path(file_path).absolute())] = cmp_hash
            continue
        to_index.append(file_path)
        if cmp_hash:
            fingerprints[str(Path(file_path).absolute())] = cmp_hash

    logger.info(
        f"扫描到 {len(supported)} 个非空文件，{len(empty_files)} 个空文件跳过，"
        f"{skipped} 个已索引跳过，提交 {len(to_index)} 个后台索引"
    )
    submitted: list[str] = []
    already_pending: list[str] = []
    for file_path in to_index:
        # submit_index 对已在队列的任务去重（返回 False）——这些 in-flight 任务
        # 同样写当前重建目标集合，调用方（reindex）等待时必须把它们计入
        if force:
            submitted_now = submit_index(
                file_path, force=True, rebuild_session=rebuild_session
            )
        elif rebuild_session:
            submitted_now = submit_index(file_path, rebuild_session=rebuild_session)
        else:
            submitted_now = submit_index(file_path)
        if submitted_now:
            submitted.append(str(Path(file_path).absolute()))
        else:
            already_pending.append(str(Path(file_path).absolute()))
    return {
        "total": len(supported),
        "skipped": skipped,
        "submitted": submitted,
        "already_pending": already_pending,
        "candidates": [str(Path(path).absolute()) for path in to_index],
        "fingerprints": fingerprints,
        "sources": [str(Path(path).absolute()) for path in supported],
    }


def start_watcher(*, initial_scan: bool = True, force_initial_scan: bool = False):
    """启动目录监听。

    调用方若已在启动前完成扫描，可设 initial_scan=False，避免同一轮文件被提交两次。
    """
    # 首次启动或清空运行数据后，数据根可能刚被创建而监控目录尚不存在。
    # Windows watchdog 不能对不存在的目录建立句柄，必须在扫描和 schedule 之前创建。
    Path(WATCH_FOLDER).mkdir(parents=True, exist_ok=True)
    if initial_scan:
        scan_existing(force=force_initial_scan)
    _ensure_watcher_sweeper()
    observer = Observer()
    handler = DocumentHandler()
    observer.schedule(handler, WATCH_FOLDER, recursive=True)
    observer.start()
    logger.info(f"开始监控文件夹: {WATCH_FOLDER}")
    return observer
