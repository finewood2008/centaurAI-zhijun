"""嵌入模型管理器 — BGE 文本嵌入 + 图片 OCR + 交叉编码器重排 + 语音转写"""
import base64
import json
import logging
import importlib.util
import socket
import threading
import time
from pathlib import Path
from config import (
    TEXT_MODEL_ACTIVE,
    TEXT_MODEL_ID,
    USE_QUERY_INSTRUCTION,
    IMAGE_MODEL_PATH,
    MODELS_CACHE,
    BGE_QUERY_INSTRUCTION,
    RERANKER_MODEL,
    RERANK_ENABLED,
    RERANK_MAX_PASSAGE_CHARS,
    TORCH_THREADS,
    CLIP_ENABLED,
    CHINESE_CLIP_PATH,
    VLM_CAPTION_ENABLED,
)

logger = logging.getLogger(__name__)

_text_model = None
_local_text_failure = None
_image_model = None
_image_processor = None
_reranker = None
_reranker_failed = False
_ocr_engine = None
_ocr_failed = False
_clip_model = None
_clip_processor = None
_clip_failed = False

# 模型懒加载锁（RLock：loader 内会再调 _get_device，可重入）。
# 后端从多个线程触发加载：启动 warmup（主线程）、后台索引池（单 worker 线程）、
# /api/search（anyio 线程）。无锁的 check-then-set 会让两线程同时各加载一次
# 多 GB 模型 → 内存翻倍/OOM；此锁串行化加载，确保每个模型只载一次、发布原子。
_LOAD_LOCK = threading.RLock()


_threads_set = False


def _get_device():
    import torch
    global _threads_set
    if not _threads_set:
        with _LOAD_LOCK:
            if not _threads_set:
                try:
                    torch.set_num_threads(TORCH_THREADS)
                except Exception:
                    pass
                _threads_set = True
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def get_text_embedder():
    global _text_model
    if _text_model is None:
        with _LOAD_LOCK:
            if _text_model is None:
                from sentence_transformers import SentenceTransformer
                logger.info(f"加载文本嵌入模型: {TEXT_MODEL_ID} ({TEXT_MODEL_ACTIVE})")
                _text_model = SentenceTransformer(
                    TEXT_MODEL_ACTIVE,
                    device=_get_device(),
                )
    return _text_model


def get_local_text_embedder():
    """Reuse the shared encoder, or load an installed local directory, never a hub ID.

    A failed path is not retried for every chat turn. Restart after installing or
    repairing that model; a changed configured path may be tried independently.
    This does not change the general-purpose loader or initiate a model download.
    """
    global _text_model, _local_text_failure
    with _LOAD_LOCK:
        if _text_model is not None:
            return _text_model
        configured = str(TEXT_MODEL_ACTIVE or "")
        if _local_text_failure and _local_text_failure[0] == configured:
            return None
        try:
            path = Path(configured).expanduser()
            if not configured or not path.is_absolute() or not path.is_dir():
                raise ValueError("installed local model directory required")
            if not any((path / name).is_file() for name in ("config.json", "modules.json")):
                raise ValueError("local model configuration missing")
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(
                str(path.resolve()), device=_get_device(),
                local_files_only=True, trust_remote_code=False,
            )
        except Exception as error:
            _local_text_failure = (configured, type(error).__name__)
            logger.warning("本地本体检索模型未就绪（%s），使用关键词检索；未下载或改用在线模型。修复后重启可再试。", type(error).__name__)
            return None
        _text_model = model
        _local_text_failure = None
        return _text_model


def get_image_embedder():
    global _image_model, _image_processor
    if _image_model is None:
        with _LOAD_LOCK:
            if _image_model is None:
                from transformers import CLIPModel, CLIPProcessor
                logger.info(f"加载图片嵌入模型: {IMAGE_MODEL_PATH}")
                model = CLIPModel.from_pretrained(IMAGE_MODEL_PATH).to(_get_device())
                processor = CLIPProcessor.from_pretrained(IMAGE_MODEL_PATH)
                _image_model, _image_processor = model, processor  # 原子发布
    return _image_model, _image_processor


def embed_text(text: str) -> list[float]:
    """嵌入 passage（入库用，不加查询前缀）"""
    if not text or not text.strip():
        return []
    model = get_text_embedder()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def embed_query(text: str) -> list[float]:
    """嵌入查询（检索用）。bge-zh 加指令前缀提升召回；bge-m3 不加。"""
    if not text or not text.strip():
        return []
    model = get_text_embedder()
    q = (BGE_QUERY_INSTRUCTION + text) if USE_QUERY_INSTRUCTION else text
    embedding = model.encode(q, normalize_embeddings=True)
    return embedding.tolist()

def embed_image(image_path: str) -> list[float]:
    """将图片转为向量（CLIP 模型未就绪时返回空）"""
    try:
        from PIL import Image
        import torch

        model, processor = get_image_embedder()
        image = Image.open(image_path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        inputs = {k: v.to(model.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model.get_image_features(**inputs)
            features = outputs.pooler_output if hasattr(outputs, 'pooler_output') else outputs

        features = features / features.norm(dim=-1, keepdim=True)
        return features[0].cpu().tolist()
    except Exception as e:
        logger.warning(f"图片嵌入失败（模型未就绪）: {e}")
        return []


def embed_batch_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入 passage（入库分块用）"""
    if not texts:
        return []
    model = get_text_embedder()
    embeddings = model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()


# ========== 图片 OCR ==========

def _get_ocr_engine():
    global _ocr_engine, _ocr_failed
    if _ocr_engine is None and not _ocr_failed:
        with _LOAD_LOCK:
            if _ocr_engine is None and not _ocr_failed:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    logger.info("加载 OCR 引擎: rapidocr-onnxruntime")
                    _ocr_engine = RapidOCR()
                except Exception as e:
                    logger.warning(f"OCR 引擎不可用，图片将仅按文件名索引: {e}")
                    _ocr_failed = True
    return _ocr_engine


def ocr_available() -> bool:
    """OCR 引擎是否可用（首次调用会触发惰性加载，与 ocr_image 同路径）。"""
    return _get_ocr_engine() is not None


def ocr_image(image_path: str) -> str:
    """提取图片中的文字（引擎缺失或无文字时返回空串）"""
    engine = _get_ocr_engine()
    if engine is None:
        return ""
    try:
        result, _ = engine(image_path)
        if not result:
            return ""
        return " ".join(line[1] for line in result).strip()
    except Exception as e:
        logger.warning(f"OCR 失败 {image_path}: {e}")
        return ""


# ========== 图片 VLM 自动描述（无文字纯图 → 中文派生描述）==========

def caption_image_with_vlm_result(image_path: str, *, snapshot=None) -> tuple[str, str | None]:
    """调用材料处理通道的本机 Ollama 多模态模型为图片生成中文描述。

    描述由材料派生任务持久化为 VISUAL_DESCRIPTION，不写入原材料 RAG 文本集合。
    地址、模型和超时与摘要、实体、关系等材料处理任务共用设置页运行时快照，
    避免纯图分支仍访问旧的 localhost/默认模型。
    模型未启用/不可用/超时返回空串，绝不向上抛异常（由调用方降级）。第二返回值
    是脱敏失败原因，供 watcher 写可排查日志，不能包含 URL、图片数据或请求正文。
    """
    if not VLM_CAPTION_ENABLED:
        return "", "disabled"
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as e:
        logger.warning(f"VLM 图片读取失败 {image_path}: {e}")
        return "", "image_read_error"
    # 延迟导入避免 embedder 在启动阶段与运行时配置模块形成循环依赖。
    from mindos import llm_transport
    from mindos.runtime_config_provider import get_provider

    provider = get_provider()
    # 调度器任务在开始时捕获 snapshot 后传入，保证执行中的请求不受设置变更影响。
    snap = snapshot or provider.get_local_snapshot()
    body = {
        "model": snap.model,
        "messages": [{
            "role": "user",
            "content": (
                "请用简体中文描述这张图片的内容，包括主体、场景、颜色和关键细节。"
                "只输出图片描述，不要输出思考过程。"
            ),
            "images": [b64],
        }],
        "stream": False,
        # qwen3-vl 默认会输出 reasoning；关闭它，避免 token 预算耗尽后 content 仍为空。
        "think": False,
        "options": {"num_predict": 300},
    }
    try:
        resp = llm_transport.allowed_urlopen(
            snap.base_url.rstrip("/") + "/api/chat",
            channel="material",
            store=provider.store,
            timeout=snap.timeout_seconds,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(resp.read().decode("utf-8"))
    except (TimeoutError, socket.timeout):
        logger.warning(f"VLM 图片描述超时 {image_path}")
        return "", "timeout"
    except Exception as e:
        logger.warning("VLM 图片描述失败 %s: %s", image_path, type(e).__name__)
        return "", f"{type(e).__name__}"
    message = payload.get("message")
    if not isinstance(message, dict):
        return "", "invalid_response"
    caption = str(message.get("content") or "").strip()
    return (caption, None) if caption else ("", "empty_response")


def caption_image_with_vlm(image_path: str) -> str:
    """兼容入口：仅返回描述文本。"""
    return caption_image_with_vlm_result(image_path)[0]


# ========== 交叉编码器重排 ==========

def _get_reranker():
    """懒加载重排模型；下载/加载失败则永久降级（返回 None）"""
    global _reranker, _reranker_failed
    if not RERANK_ENABLED:
        return None
    if _reranker is None and not _reranker_failed:
        with _LOAD_LOCK:
            if _reranker is None and not _reranker_failed:
                try:
                    from sentence_transformers import CrossEncoder
                    logger.info(f"加载重排模型: {RERANKER_MODEL}")
                    _reranker = CrossEncoder(
                        RERANKER_MODEL,
                        device=_get_device(),
                        cache_folder=MODELS_CACHE,
                    )
                except Exception as e:
                    logger.warning(f"重排模型不可用，降级为纯向量检索: {e}")
                    _reranker_failed = True
    return _reranker


def rerank(query: str, passages: list[str]) -> list[float] | None:
    """对候选 passage 打分；返回 0~1 概率列表。重排不可用时返回 None。"""
    if not passages:
        return []
    model = _get_reranker()
    if model is None:
        return None
    try:
        import numpy as np
        # 截断 passage：重排只需前若干字判断相关性，截断后 CPU 提速显著、质量几乎不变
        pairs = [(query, p[:RERANK_MAX_PASSAGE_CHARS]) for p in passages]
        scores = model.predict(
            pairs,
            convert_to_numpy=True,
            batch_size=len(pairs),
        )
        # bge-reranker 输出 logits，过 sigmoid 归一到 0~1
        probs = 1.0 / (1.0 + np.exp(-np.asarray(scores, dtype="float64")))
        return probs.tolist()
    except Exception as e:
        logger.warning(f"重排失败，降级为纯向量: {e}")
        return None


def reranker_available() -> bool:
    return _get_reranker() is not None


def reranker_loadable() -> bool:
    """Report local reranker readiness without loading the model."""
    if not RERANK_ENABLED or _reranker_failed:
        return False
    if _reranker is not None:
        return True
    model_path = Path(RERANKER_MODEL)
    return model_path.is_dir() and (model_path / "config.json").is_file()


def warmup():
    """启动时预热文本嵌入模型，避免首个用户查询冷启动卡顿。重排/视觉模型懒加载。"""
    try:
        get_text_embedder()
        embed_query("预热")
    except Exception as e:
        logger.warning(f"文本模型预热失败: {e}")
    # 重排模型体积大（~1.1GB），改为懒加载，不阻塞启动
    # CLIP 同样是懒加载


def unload_all():
    """卸载全部已加载模型，归还内存（供空闲自动卸载调用）。

    所有 getter 都是懒加载——卸载后下次调用会自动重建，服务功能不受影响。
    注意：必须在确认当前没有正在进行的嵌入/重排/转写请求时调用（由调用方把关）。
    """
    global _text_model, _image_model, _image_processor, _reranker, _ocr_engine
    global _clip_model, _clip_processor, _whisper_model
    with _LOAD_LOCK:
        _text_model = None
        _image_model = None
        _image_processor = None
        _reranker = None
        _ocr_engine = None
        _clip_model = None
        _clip_processor = None
        _whisper_model = None
    import gc

    gc.collect()
    logger.info("已卸载全部模型，内存已归还")


# ========== Chinese-CLIP 视觉检索（图片↔中文文本同空间）==========

def _get_clip():
    """懒加载 Chinese-CLIP；加载失败则永久降级（关闭视觉检索）"""
    global _clip_model, _clip_processor, _clip_failed
    if not CLIP_ENABLED:
        return None, None
    if _clip_model is None and not _clip_failed:
        with _LOAD_LOCK:
            if _clip_model is None and not _clip_failed:
                try:
                    from transformers import ChineseCLIPModel, ChineseCLIPProcessor
                    logger.info(f"加载视觉模型(Chinese-CLIP): {CHINESE_CLIP_PATH}")
                    _get_device()  # 确保线程数已设
                    model = ChineseCLIPModel.from_pretrained(CHINESE_CLIP_PATH).eval()
                    processor = ChineseCLIPProcessor.from_pretrained(CHINESE_CLIP_PATH)
                    _clip_model, _clip_processor = model, processor  # 原子发布
                except Exception as e:
                    logger.warning(f"视觉模型不可用，关闭视觉检索: {e}")
                    _clip_failed = True
    return _clip_model, _clip_processor


def embed_image_clip(image_path: str) -> list[float]:
    """图片 → CLIP 视觉向量（512维，已归一化）。不可用返回空。

    注：transformers 5.x 的 get_image_features 对 ChineseCLIP 返回未投影输出，
    故手动取 pooler_output 再过 visual_projection。
    """
    model, processor = _get_clip()
    if model is None:
        return []
    try:
        import torch
        import torch.nn.functional as F
        from PIL import Image
        inputs = processor(images=Image.open(image_path).convert("RGB"), return_tensors="pt")
        with torch.no_grad():
            out = model.vision_model(**inputs)
            vec = F.normalize(model.visual_projection(out.pooler_output), dim=-1)
        return vec[0].tolist()
    except Exception as e:
        logger.warning(f"图片视觉嵌入失败 {image_path}: {e}")
        return []


def embed_query_clip(text: str) -> list[float]:
    """查询文本 → CLIP 文本向量（与图片同空间）。不可用返回空。"""
    if not text or not text.strip():
        return []
    model, processor = _get_clip()
    if model is None:
        return []
    try:
        import torch
        import torch.nn.functional as F
        inputs = processor(text=[text], return_tensors="pt", padding=True)
        with torch.no_grad():
            out = model.text_model(**inputs)
            vec = F.normalize(model.text_projection(out.last_hidden_state[:, 0, :]), dim=-1)
        return vec[0].tolist()
    except Exception as e:
        logger.warning(f"查询视觉嵌入失败: {e}")
        return []


def clip_available() -> bool:
    m, _ = _get_clip()
    return m is not None


# ========== 语音转写（faster-whisper，CPU）==========

_whisper_model = None
_whisper_failed = False


def _get_whisper():
    """懒加载 faster-whisper；import/加载失败永久降级（返回 None），视频仍走帧+OCR。

    完全镜像 _get_ocr_engine / _get_reranker 的降级模式——转写不可用绝不能
    冒泡到 index_file 而中断整条视频索引。
    """
    global _whisper_model, _whisper_failed
    from config import WHISPER_ENABLED
    if not WHISPER_ENABLED:
        return None
    if _whisper_model is None and not _whisper_failed:
        with _LOAD_LOCK:
            if _whisper_model is None and not _whisper_failed:
                try:
                    from faster_whisper import WhisperModel
                    from config import (
                        WHISPER_MODEL,
                        WHISPER_COMPUTE,
                        WHISPER_DOWNLOAD_ROOT,
                        WHISPER_LOCAL_FILES_ONLY,
                        TORCH_THREADS,
                    )
                    logger.info(f"加载 ASR 模型(faster-whisper): {WHISPER_MODEL}")
                    _whisper_model = WhisperModel(
                        WHISPER_MODEL,
                        device="cpu",
                        compute_type=WHISPER_COMPUTE,
                        cpu_threads=TORCH_THREADS,
                        num_workers=1,
                        download_root=WHISPER_DOWNLOAD_ROOT,
                        local_files_only=WHISPER_LOCAL_FILES_ONLY,
                    )
                except Exception as e:
                    logger.warning(f"ASR(faster-whisper) 不可用，视频将仅走帧+OCR: {e}")
                    _whisper_failed = True
    return _whisper_model


def whisper_available() -> bool:
    return _get_whisper() is not None


def whisper_loadable() -> bool:
    """便宜判断转写「会不会发生」——不真正加载模型（供 watcher 算视频能力指纹用）。

    判据：未永久降级 + WHISPER_ENABLED + faster_whisper 可导入 + (本地模型存在 或 允许联网下载)。
    避免 whisper_available() 在启动扫描第一个视频时强行 load/下载多 GB 模型、拖慢就绪。
    """
    if _whisper_failed:
        return False
    if _whisper_model is not None:
        return True
    from config import WHISPER_ENABLED, WHISPER_MODEL, WHISPER_LOCAL_FILES_ONLY
    if not WHISPER_ENABLED:
        return False
    if importlib.util.find_spec("faster_whisper") is None:
        return False
    # 本地模型目录(含 model.bin)存在 → 一定可载；否则需允许联网下载
    from pathlib import Path
    if (Path(WHISPER_MODEL) / "model.bin").exists():
        return True
    return not WHISPER_LOCAL_FILES_ONLY


_opencc = None
_opencc_failed = False


def _to_simplified(text: str) -> str:
    """繁体→简体。whisper 对 zh 默认出繁体，统一为简体与库主体/bge-zh 一致。

    config.TRANSCRIPT_TO_SIMPLIFIED 关闭、或 opencc 缺失/失败 → 原样返回（不阻断转写）。
    """
    global _opencc, _opencc_failed
    if not text or _opencc_failed:
        return text
    from config import TRANSCRIPT_TO_SIMPLIFIED
    if not TRANSCRIPT_TO_SIMPLIFIED:
        return text
    if _opencc is None:
        with _LOAD_LOCK:
            if _opencc is None and not _opencc_failed:
                try:
                    from opencc import OpenCC
                    _opencc = OpenCC("t2s")
                except Exception as e:
                    logger.warning(f"opencc 不可用，转写保留原文（繁/简不转换）: {e}")
                    _opencc_failed = True
    if _opencc is None:
        return text
    try:
        return _opencc.convert(text)
    except Exception:
        return text


def transcribe_audio(wav_path: str, max_seconds: float | None = None) -> list[dict]:
    """转写 16k mono wav → [{start, end, text}]（秒）。

    不可用 / 失败 / 无语音 → 返回 []。绝不抛异常到调用方。
    max_seconds：墙钟硬上限——segments 是惰性生成器，超时即停止迭代、返回已转出的
    部分（不强杀正在解码的当前段），把超长/卡死视频对单 worker 池的占用封顶。
    """
    model = _get_whisper()
    if model is None:
        return []
    try:
        from config import WHISPER_LANGUAGE, WHISPER_BEAM_SIZE
        segments, _info = model.transcribe(
            wav_path,
            language=WHISPER_LANGUAGE,            # "zh" 强制，避免整段语言误判
            vad_filter=True,                       # 切静音，防幻觉、提速（Silero VAD）
            vad_parameters=dict(min_silence_duration_ms=500),
            beam_size=WHISPER_BEAM_SIZE,
            condition_on_previous_text=False,      # 长视频防重复/幻觉传染
            word_timestamps=False,
        )
        deadline = (time.monotonic() + max_seconds) if max_seconds else None
        out = []
        for seg in segments:                       # 惰性生成器：迭代才真正解码
            t = _to_simplified((seg.text or "").strip())
            if t:
                out.append({"start": float(seg.start), "end": float(seg.end), "text": t})
            if deadline and time.monotonic() > deadline:
                logger.warning(
                    f"转写超时（>{max_seconds:.0f}s），截断于 {seg.end:.0f}s，返回已转出 {len(out)} 段: {wav_path}"
                )
                break
        return out
    except Exception as e:
        logger.warning(f"转写失败 {wav_path}: {e}")
        return []
