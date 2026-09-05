"""MindOS 原材料处理 worker（阶段 A-A3）。

§6.1：全局只启用 1 个 material worker，上传材料按 FIFO 顺序处理。worker 从
``material_jobs`` 领取任务，做「文本解析（正文 / OCR / ASR）→ 正文快照 → 派生生成」
三件事，全程不依赖 Chroma：

- 解析 / OCR / ASR 从 Chroma 输入解耦：只产出**纯文本**并写入
  ``material_content_snapshots``（§5.1），不再调用 ``add_file_chunks``。
- 派生（摘要 / 标签 / 实体 / 关系）改读正文快照（derived._input_text 已切换）。
- 必经阶段（源读取、解析器）失败 → 任务标 ``failed``，**释放 worker 立刻继续下一条**，
  不阻塞队列、不自动无限重试。
- 无可提取文本（纯视觉图 / 无音轨）不是失败：存 ``empty`` 快照，任务仍可进入
  ``draft_ready``（§6.3）。

starter 线程：``MaterialWorker.instance().start()`` 启动后台轮询循环（阶段 A-A4 接入
server lifespan）；单 worker 由全局单例保证。进程退出 / 正常停启时，任务由启动恢复
统一转 ``paused``（§8.2），本模块不做隐式续跑。

对任务的并发正确性（§5.3）：worker 在写快照前再次比对 ``job_id + target_version``
仍处于 ``processing``；若期间被用户取消 / 暂停，则丢弃本次快照结果，不覆盖可见版本。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- 模块级 run_epoch：同一运维周期内 worker 与上传共用同一轮标签（R20） ----
_RUN_EPOCH: str = uuid.uuid4().hex[:12]
_RUN_EPOCH_LOCK = threading.Lock()


def run_epoch() -> str:
    """返回当前轮标签。worker 领取与「上传/继续处理」入队共用同一 epoch，
    使历史 paused 任务不会在本轮被隐式领取。"""
    return _RUN_EPOCH


def reset_run_epoch() -> str:
    """重置本轮标签（供测试隔离 / 受控迁移）。"""
    global _RUN_EPOCH
    with _RUN_EPOCH_LOCK:
        _RUN_EPOCH = uuid.uuid4().hex[:12]
    return _RUN_EPOCH


# content_format 常量复用 store 定义
FMT_TEXT = "text"
FMT_OCR = "ocr"
FMT_TRANSCRIPT = "transcript"
FMT_EMPTY = "empty"


def _strategy_fingerprint(source_path: str) -> str:
    """解析策略指纹：文件类型 + OCR/whisper 能力开关（完成栅栏 R22 用）。

    使「解析环境变化」（装上/关掉 whisper、OCR 开关）能反映到栅栏中，
    避免旧策略解析的结果被当作新版本提交。
    """
    from config import (
        OCR_ENABLED,
        SUPPORTED_AUDIO_EXTENSIONS,
        SUPPORTED_IMAGE_EXTENSIONS,
        SUPPORTED_VIDEO_EXTENSIONS,
        WHISPER_ENABLED,
    )
    from embedder import whisper_loadable

    ext = Path(source_path).suffix.lower()
    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        kind = "image"
    elif ext in SUPPORTED_AUDIO_EXTENSIONS:
        kind = "audio"
    elif ext in SUPPORTED_VIDEO_EXTENSIONS:
        kind = "video"
    else:
        kind = "text"
    return f"{kind}:o{int(OCR_ENABLED)}:w{int(WHISPER_ENABLED and whisper_loadable())}"


def material_fingerprint(source_path: str) -> str:
    """原文件内容 + 解析策略指纹（完成栅栏 R22）。

    入队时计算并存为 ``source_hash``；worker 提交前再次比对当前值。
    文件被替换 / 解析环境变化会使指纹变化，从而被栅栏识别，避免旧内容被
    当作当前版本提交。读取失败返回空串（不做硬栅栏，走任务自身错误处理）。
    """
    from parser import file_hash

    content_hash = file_hash(source_path)
    if not content_hash:
        return ""
    return f"{content_hash}:{_strategy_fingerprint(source_path)}"


class MaterialExtractError(Exception):
    """必经阶段失败（源读取 / 解析）。code 为稳定错误码（写 failed 用）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ExtractResult:
    text: str = ""
    content_format: str = FMT_TEXT
    parse_status: str = "ok"
    metadata: dict = field(default_factory=dict)


class MaterialTextExtractor:
    """从源文件提取纯文本（正文 / OCR / ASR），不写向量、不依赖 Chroma。

    与 watcher.index_file 的差异：只负责「拿文本」，不负责分块 / 嵌入 / 写库。
    OCR/ASR/VLM 引擎不可用按现有行为优雅降级（返回空，不炸管线）；只有源读取 /
    解析器硬错误抛 MaterialExtractError。
    """

    def extract(self, source_path: str) -> ExtractResult:
        from parser import EmptyFileError, parse_file

        file_type: str
        metadata: dict = {}  # 前置初始化：EmptyFileError 等提前 return 时避免 UnboundLocalError
        try:
            result = parse_file(source_path)
            metadata = dict(result.get("metadata") or {})
            file_type = result.get("file_type", "text")
        except FileNotFoundError:
            raise MaterialExtractError("source_missing", f"源文件不存在: {source_path}") from None
        except EmptyFileError:
            return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=metadata)
        except ValueError as e:
            raise MaterialExtractError("parse_failed", str(e)) from None

        meta = {"file_type": file_type, **metadata}

        if file_type == "text":
            return self._extract_text_document(source_path, result, meta)
        if file_type == "image":
            return self._extract_image(source_path, meta)
        if file_type in ("audio", "video"):
            return self._extract_media(source_path, meta)
        return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)

    def _extract_text_document(self, source_path: str, result: dict, meta: dict) -> ExtractResult:
        text = (result.get("text") or "").strip()
        fmt = FMT_TEXT
        if not text and Path(source_path).suffix.lower() == ".pdf":
            # 扫描版 PDF：走 OCR 补文本。
            from watcher import _ocr_scanned_pdf

            ocr_text = _ocr_scanned_pdf(source_path).strip()
            if ocr_text:
                text, fmt = ocr_text, FMT_OCR
                meta["pdf_ocr"] = True
        if text:
            meta["extract_strategy"] = "text"
            return ExtractResult(text=text, content_format=fmt, parse_status="ok", metadata=meta)
        meta["extract_strategy"] = "text"
        return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)

    def _extract_image(self, source_path: str, meta: dict) -> ExtractResult:
        from config import OCR_ENABLED
        from embedder import ocr_image

        text = ""
        meta["extract_strategy"] = "image"
        if OCR_ENABLED:
            text = (ocr_image(source_path) or "").strip()
            if text:
                meta["ocr_used"] = True
        if not text:
            # VLM 是可选派生，必须在快照提交后交给统一 Ollama 调度器。
            # 不把描述混入正文快照，避免模型失败或设置变化影响材料主任务。
            meta["vlm_pending"] = True
        if text:
            return ExtractResult(text=text, content_format=FMT_OCR, parse_status="ok", metadata=meta)
        return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)

    def _extract_media(self, source_path: str, meta: dict) -> ExtractResult:
        """音频/视频：抽 16k wav → whisper 转写 → 纯文本（不建向量）。"""
        import shutil

        import video

        from config import TRANSCRIPT_CHUNK_SEC, WHISPER_ENABLED
        from embedder import transcribe_audio, whisper_available
        from runtime_paths import VIDEO_WORK_DIR
        from watcher import _merge_transcript

        meta["extract_strategy"] = "media"
        if not (WHISPER_ENABLED and whisper_available()):
            meta["asr_unavailable"] = True
            return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)
        if not video.audio_decode_available():
            meta["asr_unavailable"] = True
            return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)
        try:
            info = video.probe(source_path)
        except video.MediaError as e:
            raise MaterialExtractError("parse_failed", f"媒体不可读: {e}") from None
        if not info.get("has_audio"):
            meta["no_audio"] = True
            return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)

        sha = uuid.uuid4().hex[:12]
        work_dir = str(Path(VIDEO_WORK_DIR) / f"mt_{sha}")
        try:
            wav = video.extract_audio_16k_mono(
                source_path, str(Path(work_dir) / "audio_16k.wav"), info=info
            )
            if not wav:
                meta["asr_unavailable"] = True
                return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)
            dur = info.get("duration") or 0.0
            # 时长窗口用保守值，避免 CPU whisper 长音频卡死 worker 队列。
            max_sec = max(300.0, dur * 2.0) if dur else 300.0
            segs = transcribe_audio(wav, max_seconds=max_sec)
            transcript_chunk_sec = float(meta.get("rag_transcript_chunk_sec") or TRANSCRIPT_CHUNK_SEC)
            chunks, _pcm = _merge_transcript(segs, transcript_chunk_sec)
            text = "\n\n".join(c for c in chunks if c and c.strip())
            if not text:
                return ExtractResult(text="", content_format=FMT_EMPTY, parse_status="empty", metadata=meta)
            meta["asr_used"] = True
            return ExtractResult(text=text, content_format=FMT_TRANSCRIPT, parse_status="ok", metadata=meta)
        except video.MediaError as e:
            raise MaterialExtractError("parse_failed", f"音轨抽取失败: {e}") from None
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


class MaterialWorker:
    """单实例原材料处理 worker：领取 → 解析+快照+派生 → 终态。"""

    _instance: "MaterialWorker | None" = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        *,
        store=None,
        saga=None,
        extractor: MaterialTextExtractor | None = None,
        lease_seconds: float = 600.0,
        poll_interval: float = 1.0,
    ) -> None:
        from .material_snapshot_saga import MaterialSnapshotSaga
        from .stores.material_pipeline_store import MaterialPipelineStore

        self.store = store or MaterialPipelineStore.instance()
        self.saga = saga or MaterialSnapshotSaga(self.store)
        self.extractor = extractor or MaterialTextExtractor()
        self.lease_seconds = float(lease_seconds)
        self.poll_interval = float(poll_interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def instance(cls, **kwargs) -> "MaterialWorker":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = MaterialWorker(**kwargs)
            return cls._instance

    @classmethod
    def reset_for_tests(cls, **kwargs) -> "MaterialWorker":
        import inspect

        params = set(inspect.signature(cls.__init__).parameters)
        accepted = {k: v for k, v in kwargs.items() if k in params}
        with cls._instance_lock:
            cls._instance = MaterialWorker(**accepted)
            return cls._instance

    # ---- 领取与处理 ----

    def process_one(self) -> int:
        """领取并处理一条任务；无任务返回 0。供后台线程循环调用。"""
        job = self.store.claim_next_material_job(
            run_epoch=run_epoch(), lease_seconds=self.lease_seconds
        )
        if job is None:
            return 0
        self._process(job)
        return 1

    def _process(self, job: dict) -> None:
        job_id = job["job_id"]
        material_id = job["owner_id"]
        version = job["target_version"]
        source_path = job["source_path"]
        source_hash = job.get("source_hash") or ""
        try:
            result = self.extractor.extract(source_path)
        except MaterialExtractError as e:
            self.store.finish_material_job(job_id, "failed", error_code=e.code)
            logger.warning("material %s extract failed[%s]: %s", material_id, e.code, e)
            return
        except Exception as e:  # 防御：无法稳定归类的异常按解析失败处理
            self.store.finish_material_job(job_id, "failed", error_code="parse_failed")
            logger.exception("material %s extract unexpected error", material_id)
            return

        # §5.3：提交前再次比对，避免旧 attempt 覆盖新版/被取消。
        if not self._job_still_processing(job_id, material_id, version):
            return

        # R22 完成栅栏：源文件/解析策略指纹未变才能提交当前内容为这版快照。
        # 文件被替换或解析环境变化时指纹不同，丢弃本次结果、标 failed，
        # 绝不把旧内容当作当前版本提交。
        expected = source_hash
        if expected:
            current_fp = material_fingerprint(source_path)
            if current_fp and current_fp != expected:
                self.store.finish_material_job(
                    job_id, "failed", error_code="source_changed"
                )
                logger.warning(
                    "material %s source changed during processing; fence blocked commit", material_id
                )
                return

        snap = self.store.begin_snapshot(
            material_id,
            version,
            source_hash,
            job_id=job_id,
            content_format=result.content_format,
            metadata=json.dumps(result.metadata or {}, ensure_ascii=False),
        )
        if snap is None:
            return
        try:
            self.saga.save_and_commit_snapshot(
                snap["snapshot_id"],
                material_id,
                snap["version"],
                result.text,
                content_format=result.content_format,
                parse_status=result.parse_status,
                metadata=result.metadata or {},
            )
        except Exception:
            self.store.discard_snapshot(snap["snapshot_id"])
            self.store.finish_material_job(job_id, "failed", error_code="write_failed")
            logger.exception("material %s snapshot write failed", material_id)
            return

        # 生命周期操作可在快照提交期间取消任务；取消后不允许再生产派生或终态。
        if not self._job_still_processing(job_id, material_id, version):
            return

        # 派生：快照就绪后异步生成（摘要/标签/实体）；失败不影响 draft_ready。
        self._trigger_derived(material_id, source_path, result.metadata)

        if not self._job_still_processing(job_id, material_id, version):
            return
        self.store.finish_material_job(job_id, "draft_ready")
        logger.info(
            "material %s draft_ready (fmt=%s, status=%s)",
            material_id, result.content_format, result.parse_status,
        )

    def _job_still_processing(self, job_id: str, material_id: str, version: int) -> bool:
        current = self.store.material_job(material_id, version)
        return bool(current and current["job_id"] == job_id and current["state"] == "processing")

    def _trigger_derived(self, material_id: str, source_path: str, metadata: dict | None = None) -> None:
        """提交快照就绪后的独立派生任务。

        草稿初始化、摘要/实体、标签/关系和图片描述互不依赖。某一个提交失败
        不能阻断其余任务，否则材料已显示完成但所有分析项会永久停在空状态。
        """
        try:
            from .derived import submit_analysis, submit_summary, submit_visual_description
            from .material_drafts import ensure_minimal_draft, submit_generation
        except Exception:
            logger.exception("material %s derived modules unavailable", material_id)
            return

        submissions = [
            ("minimal_draft", lambda: ensure_minimal_draft(material_id)),
            ("summary_entities", lambda: submit_summary(material_id, source_path)),
            ("analysis", lambda: submit_analysis(material_id, source_path)),
            ("generated_draft", lambda: submit_generation(material_id, source_path)),
        ]
        if (metadata or {}).get("vlm_pending"):
            submissions.append(("visual_description", lambda: submit_visual_description(material_id, source_path)))
        for name, submit in submissions:
            try:
                submit()
            except Exception:
                logger.exception("material %s derived submission failed: %s", material_id, name)

    # ---- 后台循环 ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="material-worker", daemon=True)
        self._thread.start()
        logger.info("material worker started (epoch=%s)", run_epoch())

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_one()
            except Exception:
                logger.exception("material worker iteration error")
            self._stop.wait(self.poll_interval)
