"""配置管理"""
import os
from pathlib import Path

from runtime_paths import (
    CHROMA_DATA_DIR as CHROMA_DATA_PATH,
    DATA_ROOT,
    MEMORY_DIR as MEMORY_PATH,
    PROJECT_ROOT,
    VIDEO_FRAMES_DIR as VIDEO_FRAMES_PATH,
    VIDEO_WORK_DIR as VIDEO_WORK_PATH,
    WATCH_FOLDER as WATCH_FOLDER_PATH,
    WIKI_DB_PATH as WIKI_DB_FILE,
    WIKI_DIR as WIKI_PATH,
)

# 数据目录
CHROMA_DATA_DIR = str(CHROMA_DATA_PATH)
WATCH_FOLDER = str(WATCH_FOLDER_PATH)
MODELS_CACHE = str(Path(__file__).parent / "models_cache")

# 嵌入模型（本地路径，通过 modelscope/HuggingFace 下载）
TEXT_MODEL_PATH = str(Path(__file__).parent / "models_cache" / "BAAI" / "bge-small-zh-v1.5")
IMAGE_MODEL_PATH = str(Path(__file__).parent / "models_cache" / "models--openai--clip-vit-base-patch32" / "snapshots" / "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268")

# 服务器
HOST = "127.0.0.1"
PORT = 8618

# 空闲自动卸载：连续无 API 请求超过 IDLE_UNLOAD_MINUTES 分钟后，自动释放
# 模型 + ChromaDB 向量索引内存（服务常驻时通常占 2~4GB），下次请求按需懒加载重建。
# 0 = 关闭该功能。环境变量覆盖（索引可靠性方案阶段 A 止血项）：
# VDB_IDLE_UNLOAD_MINUTES / VDB_IDLE_UNLOAD_CHECK_SECONDS（非法值回落默认）。
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default

IDLE_UNLOAD_MINUTES = _env_int("VDB_IDLE_UNLOAD_MINUTES", 30)
# 守护线程巡检间隔（秒），下限 10 防止配置过小导致忙轮询
IDLE_UNLOAD_CHECK_SECONDS = max(10, _env_int("VDB_IDLE_UNLOAD_CHECK_SECONDS", 60))

# 模型任务仅保存拉取/预热/卸载的脱敏执行元数据。终态记录默认保留 30 天后清理，
# 不影响 Ollama 模型文件、运行时配置或索引；设为 0 可禁用清理。
MODEL_JOB_RETENTION_DAYS = max(0, _env_int("CENTAUR_MODEL_JOB_RETENTION_DAYS", 30))
MODEL_JOB_CLEANUP_INTERVAL_SECONDS = max(
    3600, _env_int("CENTAUR_MODEL_JOB_CLEANUP_INTERVAL_SECONDS", 24 * 3600)
)

# 局域网访问（环境变量控制: VDB_LAN_ENABLED=true VDB_ADMIN_PASSWORD=xxx）
LAN_ENABLED = os.getenv("VDB_LAN_ENABLED", "").lower() == "true"
ADMIN_PASSWORD = os.getenv("VDB_ADMIN_PASSWORD", "")

# ChromaDB
CHROMA_COLLECTION = "documents"
# 数据 schema 版本——升级检索管线(分块/OCR/统一空间)后递增，
# 启动时若检测到旧版本数据会自动清库重建。
SCHEMA_VERSION = "2"

# P0-5：候选 sync_threshold（HNSW 段级落盘阈值，上游 issue #7090 缓解项）。
# 仅在同一版本隔离测试通过后才可应用到生产 collection(见方案 §P0-5)；默认不注入
# 该 metadata，避免未经验证就改动现有集合。测试/演练环境通过该环境变量覆盖。
CHROMA_SYNC_THRESHOLD_ENABLED = os.getenv("CENTAUR_CHROMA_SYNC_THRESHOLD", "").strip().lower() in ("1", "true", "yes")
CHROMA_SYNC_THRESHOLD = os.getenv("CENTAUR_CHROMA_SYNC_THRESHOLD_VALUE", "100000")

# C2：delta 合并与代际容量治理。默认阈值偏大，升级后不会因存量立即触发合并；
# 生产环境可通过环境变量按磁盘和资料规模收紧。
INDEX_DELTA_MAX_DOCUMENTS = _env_int("CENTAUR_INDEX_DELTA_MAX_DOCUMENTS", 2000)
INDEX_DELTA_MAX_VECTORS = _env_int("CENTAUR_INDEX_DELTA_MAX_VECTORS", 20000)
INDEX_DELTA_MAX_AGE_SECONDS = _env_int("CENTAUR_INDEX_DELTA_MAX_AGE_SECONDS", 7 * 24 * 3600)
INDEX_DELTA_MAX_BYTES = _env_int("CENTAUR_INDEX_DELTA_MAX_BYTES", 2 * 1024 * 1024 * 1024)
INDEX_MIN_FREE_RATIO = max(0.05, min(0.50, _env_float("CENTAUR_INDEX_MIN_FREE_RATIO", 0.15)))
INDEX_RETIRED_KEEP = max(1, _env_int("CENTAUR_INDEX_RETIRED_KEEP", 1))

# 支持的文件格式
SUPPORTED_TEXT_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".xls", ".md", ".txt"
}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SUPPORTED_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}
SUPPORTED_EXTENSIONS = (
    SUPPORTED_TEXT_EXTENSIONS
    | SUPPORTED_IMAGE_EXTENSIONS
    | SUPPORTED_VIDEO_EXTENSIONS
    | SUPPORTED_AUDIO_EXTENSIONS
)

# ---------- 检索质量参数 ----------
# 文本分块：bge-small-zh 上限 512 token（中文约 400~500 字），留余量取 400 字 + 重叠
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

# BGE 查询指令前缀（仅查询加，passage 不加）——bge-zh 官方推荐，能显著提升召回
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# ---------- 文本嵌入模型选择 ----------
# bge-small-zh：24MB，中文优化，CPU 快（默认）。
# bge-m3：560M，8192 长上下文 + 多语言，CPU 较慢——置 True 后须 POST /api/reindex 切换。
def _find_bge_m3() -> str:
    d = Path(__file__).parent / "models_cache_ms" / "BAAI" / "bge-m3"
    if (d / "pytorch_model.bin").exists() or list(d.glob("*.safetensors")):
        return str(d)
    return ""

BGE_M3_PATH = _find_bge_m3()
# 默认 bge-small-zh（中文优化、CPU 快、占用小）。文档含英文/多语言时改为 True，
# 重启会自动检测模型变更并重建索引。bge-m3 权重已就绪（models_cache_ms/BAAI/bge-m3）。
USE_BGE_M3 = False

if USE_BGE_M3 and BGE_M3_PATH:
    TEXT_MODEL_ACTIVE = BGE_M3_PATH
    TEXT_MODEL_ID = "bge-m3"
    USE_QUERY_INSTRUCTION = False  # bge-m3 不需要查询指令前缀
else:
    TEXT_MODEL_ACTIVE = TEXT_MODEL_PATH
    TEXT_MODEL_ID = "bge-small-zh"
    USE_QUERY_INSTRUCTION = True

# 重排：交叉编码器对向量召回的候选做精排（模型缺失时自动降级为纯向量）
# 优先用本地（modelscope 下载）路径——自动探测 models_cache_ms 下任一镜像源目录，
# 缺失则回落到 HF 仓库 id
def _find_local_reranker() -> str:
    base = Path(__file__).parent / "models_cache_ms"
    for cfg in base.glob("*/bge-reranker-base/config.json"):
        return str(cfg.parent)
    return "BAAI/bge-reranker-base"

RERANKER_MODEL = _find_local_reranker()
RERANK_ENABLED = True
# 向量召回阶段过度召回倍数，给重排更多候选
RECALL_MULTIPLIER = 2
RECALL_MIN_CANDIDATES = 10
# 重排候选池硬上限——重排耗时随候选数线性增长，封顶以控延迟
RERANK_MAX_CANDIDATES = 12
# BM25 词面只补少量稠密漏掉的精确匹配候选（不淹没语义召回）
BM25_EXTRA_CANDIDATES = 4

# 重排只看 passage 前若干字即可判断相关性——截断后 CPU 重排提速 ~40%，质量几乎不变
# （实测 256 字时 top 相关分 0.730 vs 全文 0.731）。返回给用户的全文不受影响。
RERANK_MAX_PASSAGE_CHARS = 256

# CPU 推理线程数。上限 4：批量嵌入（记忆重建等）会长时间持续跑，
# 拉满物理核会让整机不可用；交互式重排 4 线程的延迟增加有限
import os as _os
TORCH_THREADS = min(4, _os.cpu_count() or 4)

# 相关度阈值：低于阈值的结果视为不相关，直接丢弃（避免把噪声塞进 prompt）
# 命中重排时用重排概率(sigmoid)，否则用向量余弦相似度(1-distance)
# 实测 bge-reranker 在本语料上：确信相关 ≈0.73，不确定/无关 ≈0.50，中间有明显空档，
# 故取 0.6 干净切分。可按语料调（偏召回调低、偏精度调高）。
RERANK_SCORE_THRESHOLD = 0.60
VECTOR_SIM_THRESHOLD = 0.30

# 图片 OCR：把图片文字提取后走文本嵌入，与文本统一向量空间
OCR_ENABLED = True
# OCR 为空的纯图是否仍按文件名入库。默认 False——纯图的文件名片段会在任何
# 查询下被重排打到 ~0.5 的中性分，形成噪声地板。纯视觉检索走下面的 Chinese-CLIP。
INDEX_EMPTY_OCR_IMAGES = False

# ---------- 纯视觉图片的 VLM 自动描述（方案 A）----------
# OCR 无文字时，调用“材料处理”通道配置的本机 Ollama 多模态模型生成中文描述，
# 作为文本块入 BGE 文本集合——不引入新向量空间，可被任意文本查询/重排/BM25 命中。
# 地址、模型名和超时由运行时设置页统一管理，不保留第二套 VLM 地址/模型配置。
# 模型未安装或调用失败时静默降级（走原 CLIP 视觉索引/跳过分支），不影响已有链路。
VLM_CAPTION_ENABLED = True

# ---------- 视觉检索（Chinese-CLIP，图片↔中文文本同空间）----------
# 自动探测已下载的 chinese-clip 权重；缺失则关闭视觉检索（不影响文本检索）
def _find_chinese_clip() -> str:
    base = Path(__file__).parent / "models_cache_ms"
    for cfg in sorted(base.rglob("chinese-clip-vit-base-patch16/config.json")):
        d = cfg.parent
        if (d / "pytorch_model.bin").exists() or list(d.glob("*.safetensors")):
            return str(d)
    return ""

CHINESE_CLIP_PATH = _find_chinese_clip()
CLIP_ENABLED = bool(CHINESE_CLIP_PATH)
IMAGE_COLLECTION = "images_clip"
# CLIP 图文余弦相似度阈值（CLIP 分布偏低，实测相关 ~0.30+）
IMAGE_SIM_THRESHOLD = 0.28

# HuggingFace 直连 + 禁用代理（本机网络已验证可用）
os.environ["HF_ENDPOINT"] = "https://huggingface.co"
# 清除代理，避免本地代理干扰模型加载
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)

# ======================= 视频支持 =======================
# 帧图与中间产物目录：与 watch_folder 同级、不在其内 —— watchdog 递归监控的是
# WATCH_FOLDER，帧落在这里就不会被当成独立图片再次索引（watcher 另有代码级护栏兜底）。
VIDEO_FRAMES_DIR = str(VIDEO_FRAMES_PATH)   # 帧按 video_frames/{file_sha1}/ 分目录
VIDEO_WORK_DIR = str(VIDEO_WORK_PATH)       # 临时 wav，索引完即删

# 上传体积上限（字节）：GB 级视频塞满磁盘的护栏，超限 server 返回 413
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024               # 4 GB

# ---------- 抽帧策略 ----------
SCENE_THRESHOLD = 0.3          # ffmpeg select='gt(scene,X)'，越大帧越少；0.3 干净切分点
MAX_FRAMES_PER_VIDEO = 40      # 单视频帧硬上限，封顶磁盘 + CLIP 嵌入量
MIN_SCENE_FRAMES = 3           # 场景抽帧少于此 → 判定低动态，回落定时抽帧
FRAME_INTERVAL_SEC = 30.0      # 定时回退：每 N 秒一帧；时间戳 = index*N（计算，非读 metadata）
FRAME_JPEG_QUALITY = 3         # ffmpeg -q:v，2~5 合理，3=高质量

# 帧 OCR：对每帧跑 OCR 把画面文字（幻灯片/烧录字幕）纳入文本检索。默认开，CPU 弱可关。
VIDEO_FRAME_OCR_ENABLED = True

# ---------- faster-whisper 语音转写（ASR）----------
# 优先用本地已下载的 flat 目录（whisper_models/faster-whisper-{size}）；缺失则回落到 size 名
# 触发联网下载（注意 hf-mirror 对 whisper 仓库 HEAD 缺元数据头，下载需直连 huggingface.co）。
def _find_whisper_model() -> tuple[str, bool]:
    base = Path(__file__).parent / "whisper_models"
    for size in ("small", "base", "medium", "large-v3"):
        d = base / f"faster-whisper-{size}"
        if (d / "model.bin").exists():
            return str(d), True   # 本地路径 + local_files_only=True
    return "small", False         # size 名 + 允许联网下载

WHISPER_ENABLED = True            # 总开关；import/加载失败会自动降级，此处仅控是否尝试
WHISPER_MODEL, WHISPER_LOCAL_FILES_ONLY = _find_whisper_model()
# 默认 small（中文质量/CPU 速度甜点，RTF≈0.2~0.4）。追质量改 medium/large-v3（更慢）。
WHISPER_COMPUTE = "int8"          # 纯 CPU 用 int8（float16 在 CPU 无收益）
WHISPER_LANGUAGE = "zh"           # 中文为主强制 zh，避免 auto 被前几秒英文/音乐误判整段
WHISPER_BEAM_SIZE = 5             # 想更快设 1（greedy）；中文建议 5
WHISPER_DOWNLOAD_ROOT = str(Path(__file__).parent / "whisper_models")
# 转写墙钟硬上限 = max(600s, 视频时长 × 该倍率)；封顶超长/卡死视频对后台串行池的占用
WHISPER_TIMEOUT_RTF = 4.0

# ffmpeg/ffprobe 调用超时（秒）
FFPROBE_TIMEOUT = 30
FFMPEG_AUDIO_TIMEOUT = 1800
FFMPEG_FRAME_TIMEOUT = 1800

# 转写块切分：每块约多少秒 transcript 合并成一个文本 chunk（各带独立 start_time）
TRANSCRIPT_CHUNK_SEC = 30.0

# whisper 对中文默认输出繁体；本库主体与 bge-zh 偏简体，统一繁→简（opencc）保持一致。
# 关闭或 opencc 缺失则保留原文。
TRANSCRIPT_TO_SIMPLIFIED = True
# ========================================================

# ======================= Agent 记忆系统 =======================
# OpenClaw 标准统一身份：SOUL / AGENTS / IDENTITY / USER。
# 旧 MEMORY.md 与 journal 数据继续保留和索引，但不再属于身份文件集合。
# 全部向量化存入 ChromaDB collection: agent_memory，支持语义搜索
MEMORY_DIR = str(MEMORY_PATH)
MEMORY_COLLECTION = "agent_memory"
# 记忆文件列表（相对 MEMORY_DIR 的路径）
MEMORY_FILES = ["SOUL.md", "AGENTS.md", "IDENTITY.md", "USER.md"]
# 日记目录
JOURNAL_DIR_NAME = "journal"
# Agent 上下文注入：默认注入最近 N 天的日记
MEMORY_CONTEXT_RECENT_DAYS = 7
# 注入上下文字符上限
MEMORY_CONTEXT_CHAR_LIMIT = 4000
# 自动同步外部 Agent 原生记忆到 memory/imports/*.md
MEMORY_IMPORT_AUTO_SYNC = True
# 定时同步间隔。reindex 已按 content_hash 增量跳过，每小时一次足够；
# 间隔过短曾导致重建风暴（单次全量重建耗时可能超过间隔本身）。
MEMORY_IMPORT_INTERVAL_SECONDS = 3600

# ======================= Wiki 知识层 =======================
# Markdown Vault 是长期可读写的知识库；SQLite/Chroma 仅存派生索引。
WIKI_DIR = str(WIKI_PATH)
WIKI_DB_PATH = str(WIKI_DB_FILE)
WIKI_COLLECTION = "wiki_pages"
# Wiki 自动整理与材料识别共用同一个受控 Ollama 服务地址和模型名，不接受外部
# LLM API 地址或密钥。旧识别配置变量作为迁移兼容项；新部署统一使用
# CENTAUR_LOCAL_OLLAMA_URL / CENTAUR_LOCAL_OLLAMA_MODEL。
LOCAL_OLLAMA_URL = (
    os.getenv("CENTAUR_LOCAL_OLLAMA_URL", "")
    or os.getenv("CENTAUR_RECOGNITION_AI_OLLAMA_URL", "")
    or "http://127.0.0.1:11434"
).rstrip("/")
LOCAL_OLLAMA_MODEL = (
    os.getenv("CENTAUR_LOCAL_OLLAMA_MODEL", "")
    or os.getenv("CENTAUR_RECOGNITION_AI_MODEL", "")
    or os.getenv("CENTAUR_WIKI_AI_MODEL", "")
    or "qwen3:1.7b"
)
WIKI_AI_OLLAMA_URL = LOCAL_OLLAMA_URL
WIKI_AI_MODEL = LOCAL_OLLAMA_MODEL
WIKI_AI_TIMEOUT_SECONDS = 180
WIKI_AI_KEEP_ALIVE = 0
WIKI_AI_CONTEXT_WINDOW = 4096
# QA 问答模型提供方：ollama（本机，默认）或 openai（外部兼容接口，测试阶段用）。
# 注意：此开关只影响 /api/mindos/qa 问答；Wiki 自动整理仍强制本机 Ollama（资料不出设备）。
WIKI_AI_PROVIDER = os.getenv("CENTAUR_WIKI_AI_PROVIDER", "ollama").lower()
WIKI_AI_OPENAI_BASE_URL = os.getenv("CENTAUR_WIKI_AI_BASE_URL", "").rstrip("/")
WIKI_AI_OPENAI_API_KEY = os.getenv("CENTAUR_WIKI_AI_API_KEY", "")
WIKI_AI_MIN_AVAILABLE_MEMORY_MB = 2600
WIKI_AI_MAX_CHARS = 12000
WIKI_AI_BLOCK_START = "<!-- CENTAUR_AI_SUMMARY_START -->"
WIKI_AI_BLOCK_END = "<!-- CENTAUR_AI_SUMMARY_END -->"
# ============================================================

# ======================= 材料识别与对话问答 LLM 双通道 =======================
# 材料识别（derived/tag_suggest/generations）强制本机 Ollama，数据不出设备（D1）；
# 对话问答（qa.py）默认本地，可显式开启外部 OpenAI 兼容 API，外部失败时回落本地（D2）。
# 新变量未设置时仅在明确迁移规则内回退旧 CENTAUR_WIKI_AI_*，严格按通道隔离（§5.2）。

# ---- 材料识别通道：强制本机 Ollama（不接受远程地址/密钥） ----
# 服务地址与模型名统一由 LOCAL_OLLAMA_* 提供；旧识别模型变量仅作迁移兼容。永不
# 读取 provider、外部地址或 API Key。
RECOGNITION_AI_OLLAMA_URL = LOCAL_OLLAMA_URL
RECOGNITION_AI_MODEL = LOCAL_OLLAMA_MODEL
RECOGNITION_AI_TIMEOUT_SECONDS = _env_int("CENTAUR_RECOGNITION_AI_TIMEOUT", WIKI_AI_TIMEOUT_SECONDS)
# Ollama 路径专用（识别通道内部常量，不受环境变量控制；问答通道本地回落复用）
RECOGNITION_AI_KEEP_ALIVE = 0
RECOGNITION_AI_CONTEXT_WINDOW = 4096

# ---- 对话问答通道：默认本地，可显式启用外部 API（OpenAI 兼容） ----
# QA_AI_PROVIDER 未设置时可回退旧 WIKI_AI_PROVIDER，但仅当 provider=openai 且
# QA_AI_EXTERNAL_ENABLED=true、URL/Key/Model 均完整时才允许外发（受保护迁移）。
QA_AI_PROVIDER = os.getenv("CENTAUR_QA_AI_PROVIDER", "").lower() or WIKI_AI_PROVIDER
QA_AI_BASE_URL = (os.getenv("CENTAUR_QA_AI_BASE_URL", "") or WIKI_AI_OPENAI_BASE_URL).rstrip("/")
QA_AI_API_KEY = os.getenv("CENTAUR_QA_AI_API_KEY", "") or WIKI_AI_OPENAI_API_KEY
QA_AI_MODEL = os.getenv("CENTAUR_QA_AI_MODEL", "") or WIKI_AI_MODEL
QA_AI_TIMEOUT_SECONDS = _env_int("CENTAUR_QA_AI_TIMEOUT", WIKI_AI_TIMEOUT_SECONDS)
QA_AI_FALLBACK_OLLAMA = os.getenv("CENTAUR_QA_AI_FALLBACK_OLLAMA", "true").lower() == "true"
QA_AI_EXTERNAL_ENABLED = os.getenv("CENTAUR_QA_AI_EXTERNAL_ENABLED", "false").lower() == "true"
# 整次问答的总超时预算（秒）：覆盖「外部调用 + 本地 fallback + 证据优先重试」全部
# 模型生成，超过即返回 504，避免外部超时后回退本地再次累加等待（§7.2 L180-181）。
QA_AI_TOTAL_BUDGET_SECONDS = _env_int("CENTAUR_QA_AI_TOTAL_BUDGET_SECONDS", 90)

# ======================= 模型运行时设置（P1 设置页） =======================
# ============================================================

# ======================= 用户手动标注 =======================
# 标注（标签/重要度/置顶/备注/说明）存独立 sidecar（backend/../annotations.json），
# 检索期叠加；reindex 清库时不丢。下列参数控制「重要度加权」与「置顶必回」的强度。

# 重要度每级对检索分的加成（重排分/向量分都在 0~1 区间）。importance 0~5，
# 满级 5 → +0.10。刻意小幅：让人工重要度在相近相关度间起「微调排序」作用，
# 而不至于把明显不相关的重要文件顶到前面（仍受 PIN/阈值约束）。
IMPORTANCE_WEIGHT_STEP = 0.02

# 置顶「必回」的最低相关性门槛：被召回的置顶项原始分 ≥ 此值即绕过主阈值强制返回。
# 远低于 RERANK_SCORE_THRESHOLD(0.6)，但 > 0，避免置顶项对任意无关查询都强行出现。
# 取 0.2：基本等价于「该查询确实把这个文件召回了且不是垫底噪声」。
PIN_MIN_RELEVANCE = 0.20
# ============================================================
