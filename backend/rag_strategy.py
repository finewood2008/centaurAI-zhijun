"""RAG retrieval strategy profiles for the team vector database."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from config import (
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    RECALL_MULTIPLIER,
    RECALL_MIN_CANDIDATES,
    RERANK_MAX_CANDIDATES,
    BM25_EXTRA_CANDIDATES,
    RERANK_SCORE_THRESHOLD,
    VECTOR_SIM_THRESHOLD,
    IMAGE_SIM_THRESHOLD,
    TRANSCRIPT_CHUNK_SEC,
)
from runtime_paths import RAG_CONFIG_PATH

STRATEGIES: dict[str, dict[str, Any]] = {
    "balanced": {
        "id": "balanced",
        "label": "均衡",
        "description": "通用文档默认策略，兼顾召回、速度和结果干净度。",
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "recall_multiplier": RECALL_MULTIPLIER,
        "recall_min": RECALL_MIN_CANDIDATES,
        "rerank_max": RERANK_MAX_CANDIDATES,
        "bm25_extra": BM25_EXTRA_CANDIDATES,
        "rerank_score_threshold": RERANK_SCORE_THRESHOLD,
        "vector_sim_threshold": VECTOR_SIM_THRESHOLD,
        "image_sim_threshold": IMAGE_SIM_THRESHOLD,
        "transcript_chunk_sec": TRANSCRIPT_CHUNK_SEC,
    },
    "precise": {
        "id": "precise",
        "label": "精准",
        "description": "更高阈值和更小候选池，适合制度、合同、FAQ 等需要少而准的语料。",
        "chunk_size": 320,
        "chunk_overlap": 50,
        "recall_multiplier": 2,
        "recall_min": 8,
        "rerank_max": 8,
        "bm25_extra": 3,
        "rerank_score_threshold": 0.68,
        "vector_sim_threshold": 0.38,
        "image_sim_threshold": 0.32,
        "transcript_chunk_sec": 30.0,
    },
    "high_recall": {
        "id": "high_recall",
        "label": "高召回",
        "description": "扩大候选池并降低阈值，适合资料分散、同义表达多的知识库。",
        "chunk_size": 500,
        "chunk_overlap": 120,
        "recall_multiplier": 4,
        "recall_min": 24,
        "rerank_max": 20,
        "bm25_extra": 8,
        "rerank_score_threshold": 0.50,
        "vector_sim_threshold": 0.22,
        "image_sim_threshold": 0.24,
        "transcript_chunk_sec": 45.0,
    },
    "long_context": {
        "id": "long_context",
        "label": "长文档",
        "description": "更大的分块和重叠，适合长 PDF、手册、会议纪要。",
        "chunk_size": 700,
        "chunk_overlap": 140,
        "recall_multiplier": 3,
        "recall_min": 18,
        "rerank_max": 16,
        "bm25_extra": 6,
        "rerank_score_threshold": 0.56,
        "vector_sim_threshold": 0.25,
        "image_sim_threshold": 0.26,
        "transcript_chunk_sec": 45.0,
    },
    "visual_ocr": {
        "id": "visual_ocr",
        "label": "图片/OCR",
        "description": "对图片文字和视觉向量放宽召回，适合截图、海报、扫描件。",
        "chunk_size": 280,
        "chunk_overlap": 60,
        "recall_multiplier": 3,
        "recall_min": 16,
        "rerank_max": 14,
        "bm25_extra": 6,
        "rerank_score_threshold": 0.54,
        "vector_sim_threshold": 0.24,
        "image_sim_threshold": 0.24,
        "transcript_chunk_sec": 30.0,
    },
    "video_hybrid": {
        "id": "video_hybrid",
        "label": "视频混合",
        "description": "兼顾转写、帧 OCR 和画面检索，适合课程、演示、宣传片。",
        "chunk_size": 360,
        "chunk_overlap": 90,
        "recall_multiplier": 4,
        "recall_min": 24,
        "rerank_max": 18,
        "bm25_extra": 6,
        "rerank_score_threshold": 0.52,
        "vector_sim_threshold": 0.22,
        "image_sim_threshold": 0.23,
        "transcript_chunk_sec": 25.0,
    },
}

DEFAULT_FILE_TYPE_STRATEGIES = {
    "text": "balanced",
    "image": "visual_ocr",
    "video": "video_hybrid",
}

_LOCK = threading.RLock()
_CACHE_MTIME: float | None = None
_CACHE_CONFIG: dict[str, Any] | None = None


def _default_config() -> dict[str, Any]:
    return {
        "default_strategy": "balanced",
        "file_type_strategies": dict(DEFAULT_FILE_TYPE_STRATEGIES),
    }


def _normalize_strategy_id(strategy_id: Any, fallback: str = "balanced") -> str:
    value = str(strategy_id or "").strip()
    return value if value in STRATEGIES else fallback


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _default_config()
    raw = raw or {}
    default_strategy = _normalize_strategy_id(raw.get("default_strategy"), "balanced")
    cfg["default_strategy"] = default_strategy

    incoming = raw.get("file_type_strategies")
    if isinstance(incoming, dict):
        for file_type in DEFAULT_FILE_TYPE_STRATEGIES:
            cfg["file_type_strategies"][file_type] = _normalize_strategy_id(
                incoming.get(file_type),
                cfg["file_type_strategies"][file_type],
            )
    return cfg


def load_config() -> dict[str, Any]:
    global _CACHE_CONFIG, _CACHE_MTIME
    with _LOCK:
        try:
            mtime = RAG_CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = None

        if _CACHE_CONFIG is not None and _CACHE_MTIME == mtime:
            return deepcopy(_CACHE_CONFIG)

        raw: dict[str, Any] = {}
        if mtime is not None:
            try:
                raw = json.loads(RAG_CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = {}

        _CACHE_CONFIG = normalize_config(raw)
        _CACHE_MTIME = mtime
        return deepcopy(_CACHE_CONFIG)


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    global _CACHE_CONFIG, _CACHE_MTIME
    with _LOCK:
        current = load_config()
        merged = {
            **current,
            **{k: v for k, v in (patch or {}).items() if k in {"default_strategy", "file_type_strategies"}},
        }
        cfg = normalize_config(merged)
        RAG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAG_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        _CACHE_MTIME = RAG_CONFIG_PATH.stat().st_mtime
        _CACHE_CONFIG = deepcopy(cfg)
        return deepcopy(cfg)


def list_strategies() -> list[dict[str, Any]]:
    return [deepcopy(item) for item in STRATEGIES.values()]


def get_strategy(strategy_id: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    sid = _normalize_strategy_id(strategy_id or cfg["default_strategy"], cfg["default_strategy"])
    return deepcopy(STRATEGIES[sid])


def strategy_for_file_type(file_type: str | None) -> dict[str, Any]:
    cfg = load_config()
    ft = (file_type or "text").strip().lower()
    sid = cfg["file_type_strategies"].get(ft) or cfg["default_strategy"]
    return get_strategy(sid)


def query_strategy(file_type: str | None = None) -> dict[str, Any]:
    """Return the strategy that controls query-time candidate pool sizes."""
    if file_type:
        return strategy_for_file_type(file_type)

    cfg = load_config()
    configured = [get_strategy(cfg["default_strategy"])]
    configured.extend(get_strategy(sid) for sid in cfg["file_type_strategies"].values())
    out = get_strategy(cfg["default_strategy"])
    for key in ("recall_multiplier", "recall_min", "rerank_max", "bm25_extra"):
        out[key] = max(float(s[key]) for s in configured)
        if key != "recall_multiplier":
            out[key] = int(out[key])
    return out


def threshold_for_file_type(file_type: str | None, reranked: bool) -> float:
    strategy = strategy_for_file_type(file_type)
    key = "rerank_score_threshold" if reranked else "vector_sim_threshold"
    return float(strategy[key])


def image_threshold_for_file_type(file_type: str | None) -> float:
    return float(strategy_for_file_type(file_type or "image")["image_sim_threshold"])


def strategy_for_index(file_type: str | None, strategy_id: str | None = None) -> dict[str, Any]:
    """Resolve an indexing profile, honoring an optional per-file override."""
    return get_strategy(strategy_id) if strategy_id else strategy_for_file_type(file_type)


def chunk_params_for_file_type(file_type: str | None, strategy_id: str | None = None) -> tuple[int, int, str]:
    strategy = strategy_for_index(file_type, strategy_id)
    return int(strategy["chunk_size"]), int(strategy["chunk_overlap"]), str(strategy["id"])


def transcript_chunk_sec(strategy_id: str | None = None) -> float:
    return float(strategy_for_index("video", strategy_id)["transcript_chunk_sec"])


def fingerprint_for_file_type(file_type: str | None, strategy_id: str | None = None) -> str:
    strategy = strategy_for_index(file_type, strategy_id)
    keys = ("id", "chunk_size", "chunk_overlap", "transcript_chunk_sec")
    return "rag-" + "-".join(f"{k}:{strategy[k]}" for k in keys if k in strategy)


def config_payload() -> dict[str, Any]:
    cfg = load_config()
    return {
        "config": cfg,
        "strategies": list_strategies(),
        "effective": {
            file_type: strategy_for_file_type(file_type)
            for file_type in DEFAULT_FILE_TYPE_STRATEGIES
        },
        "config_path": str(RAG_CONFIG_PATH),
    }
