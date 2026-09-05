"""MindOS AI 标签推荐（P1-KG-001）。

对资料正文/卡片正文生成候选标签（最多 5 个，短文本关键词不足时允许少于 3 个）：
- 优先调用本机 Ollama 模型（材料识别通道，RECOGNITION_AI_*，数据不出设备）。
- LLM 不可用或超时时降级为 jieba 关键词提取。
- 仅返回候选供用户确认，绝不自动写入任何标签。
"""
import json
import logging
import re
import socket
import urllib.error

import jieba
import jieba.analyse

from . import llm_transport
from .runtime_config_provider import get_provider

logger = logging.getLogger(__name__)

# 停用词：排除常见无信息量词汇，避免标签无意义
_STOPWORDS = {
    "一个", "这个", "那个", "以及", "并且", "对于", "关于", "我们", "你们", "他们",
    "可以", "需要", "进行", "通过", "使用", "提供", "支持", "相关", "内容", "资料",
    "文档", "知识", "系统", "功能", "阶段", "测试", "验证", "结果", "问题", "主要",
    "包括", "表示", "说明", "用于", "方面", "其中", "这样", "这样", "不会", "没有",
    "应该", "可能", "因为", "所以", "如果", "但是", "以及", "以及", "还是", "或者",
    "不是", "就是", "都", "也", "很", "能", "会", "要", "让", "给", "等",
}


def _keyword_fallback(text: str, limit: int = 5) -> list[str]:
    """jieba TF-IDF 关键词提取（排除停用词，去重保序）。"""
    text = (text or "").strip()
    if not text:
        return []
    tags = jieba.analyse.extract_tags(text, topK=max(limit * 3, 12), withWeight=False)
    result: list[str] = []
    for tag in tags:
        tag = str(tag).strip()
        if not tag or tag in _STOPWORDS or len(tag) > 24:
            continue
        if tag not in result:
            result.append(tag)
        if len(result) >= limit:
            break
    return result


def _llm_suggest(text: str, title: str, snap) -> list[str]:
    """调用本机 Ollama 模型生成候选标签（材料识别通道，强制本地）；失败/超时抛异常由调用方降级。

    模型、地址、超时取自材料通道快照（snap）。
    """
    prompt = (
        "你是 MindOS 本地知识库的标签助手。请为下面资料生成 3~5 个精炼的中文标签，"
        "每个标签不超过 6 个字，覆盖主题、类型与核心概念。"
        '只输出 JSON 数组，例如 ["知识管理", "RAG", "文档处理"]。'
        "不要 markdown 围栏、编号、解释或其它字段。\n\n"
        f"标题：{title}\n正文：\n{text[:1200]}"
    )
    body = {
        "model": snap.model,
        "messages": [
            {"role": "system", "content": "你是 MindOS 本地知识库的标签助手。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 120},
    }
    resp = llm_transport.allowed_urlopen(
        snap.base_url.rstrip("/") + "/api/chat",
        channel="material",
        store=get_provider().store,
        timeout=snap.timeout_seconds,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with resp:
        payload = json.loads(resp.read().decode("utf-8"))
    answer = payload["message"]["content"]

    return _parse_tags(answer)


def _parse_tags(answer: str) -> list[str]:
    """解析模型标签输出，优先 JSON，兼容旧模型的逗号/换行分隔格式。"""
    raw = (answer or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()

    candidates = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            candidates = parsed
        elif isinstance(parsed, dict):
            for key in ("tags", "items", "labels", "result", "data"):
                if isinstance(parsed.get(key), list):
                    candidates = parsed[key]
                    break
    except (TypeError, ValueError):
        pass

    if candidates is None:
        candidates = re.split(r"[，,、\n;；]", raw)

    parts: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("name") or candidate.get("tag") or candidate.get("label") or ""
        chunk = str(candidate).strip().strip("#-*· ").strip()
        if chunk and len(chunk) <= 24 and chunk not in parts:
            parts.append(chunk)
        if len(parts) >= 5:
            break
    return parts


def _safe_diagnostic(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"Ollama 返回 HTTP {exc.code}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Ollama 响应超时"
    if isinstance(exc, urllib.error.URLError):
        return "无法完成与 Ollama 的连接"
    if isinstance(exc, (ConnectionError, OSError)):
        return "Ollama 连接失败"
    return "Ollama 返回无效响应或生成失败"


def suggest_tags_with_source(text: str, title: str = "", limit: int = 5) -> dict:
    """返回 {items, source}，source 为 'llm'（模型生成）或 'fallback'（关键词降级）。

    最多返回 limit 个候选标签；短文本关键词不足时允许少于 3 个。
    """
    text = (text or "").strip()
    if not text:
        return {"items": [], "source": "fallback"}
    # §5.1.1：任务边界取一次材料快照，下传给模型调用。
    snap = get_provider().get_local_snapshot()
    diagnostic = None
    try:
        tags = _llm_suggest(text, title, snap)
        if tags:
            return {"items": tags[:limit], "source": "llm"}
        diagnostic = "Ollama 未返回可用标签"
    except Exception as exc:
        diagnostic = _safe_diagnostic(exc)
        logger.info("标签推荐 LLM 不可用，降级关键词提取: %s", diagnostic)
    result = {"items": _keyword_fallback(text, limit=limit), "source": "fallback"}
    if diagnostic:
        result["diagnostic"] = diagnostic
    return result


def suggest_tags(text: str, title: str = "", limit: int = 5) -> list[str]:
    """兼容包装：返回最多 limit 个候选标签数组（不区分来源，保持旧接口）。

    降级来源统一由 suggest_tags_with_source() 提供。
    """
    return suggest_tags_with_source(text, title, limit)["items"]
