"""MindOS 派生内容服务层（P14-03 自动摘要 / P14-04 关键词与实体）。

摘要、标签候选、实体抽取都作为派生数据保存到 derived_records
（kind=SUMMARY / TAG_SUGGESTIONS / ENTITY_EXTRACTION，owner=material）：
- 只在输入文本 hash 变化时重新调用模型；hash 未变绝不重复生成；
- 模型不可用 / 失败只把派生状态标为 unavailable/failed，绝不影响资料可用状态，
  也绝不改写原材料或知识卡片；标签候选、实体绝不自动写入正式数据；
- 空文本不调用模型（status=skipped）；
- 生成在独立后台池执行（提交自 watcher.index_file 成功后），不在 HTTP 请求里调用 LLM；
- 实体输出要求严格 JSON schema（type/name/confidence/evidence），不合法输出回退到
  正则 + jieba 降级；降级结果标记 source=fallback，绝不展示模型原始文本；
- 摘要与实体由同一次 LLM 调用生成（结构化 JSON 一次返回，分开落两条派生记录），
  实体缺失/失败时由 refresh_analysis 单独重算；实体名必须是自然语言词/短语，
  纯符号（###、--- 等 markdown 标记）会被过滤。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import llm_transport
from .ollama_material_scheduler import (
    PRIORITY_BATCH_BACKGROUND,
    PRIORITY_MANUAL_REGENERATE,
    PRIORITY_RELATIONS,
    PRIORITY_SUMMARY_ENTITIES,
    PRIORITY_TAGS,
    PRIORITY_VLM_IMAGE,
    _scheduler as _ollama_scheduler,
)
from .runtime_config_provider import get_provider
from .stores import derived_store

logger = logging.getLogger(__name__)

OWNER_MATERIAL = "material"
KIND_SUMMARY = "SUMMARY"
KIND_TAG_SUGGESTIONS = "TAG_SUGGESTIONS"
KIND_ENTITY_EXTRACTION = "ENTITY_EXTRACTION"
KIND_RELATION_EXTRACTION = "RELATION_EXTRACTION"
# P14-10：内容生成草稿（owner_type=generation，owner_id=draft_id）。
# 草稿只作为派生数据保存，不写入向量索引，不进入普通检索 / 问答证据，
# 直到用户显式「另存为知识卡片」。
KIND_GENERATED_DRAFT = "GENERATED_DRAFT"
KIND_VISUAL_DESCRIPTION = "VISUAL_DESCRIPTION"

# 摘要输入/输出上限
# qwen3-vl:2b 同时承担纯视觉描述和文本派生。12k 中文字符 + 1500 输出 token
# 在局域网 CPU Ollama 上容易超时；摘要只需要材料开头和主要段落，控制输入避免
# 每次手工重试都把服务拖入长推理。
MAX_INPUT_CHARS = 6000
MAX_SUMMARY_CHARS = 200
_MODEL_RETRY_DELAY_SECONDS = 90

# 实体 schema 约束（P14-04）
ENTITY_TYPES = {"person", "place", "organization", "term"}
MAX_ENTITY_NAME = 64
MAX_ENTITY_ITEMS = 30
MAX_EVIDENCE_CHARS = 200
# 实体名必须包含中文字符 / 字母 / 数字，排除 ###、---、纯标点等 markdown 符号
_NATURAL_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]")

# 正则降级抽取时使用的后缀（粗粒度启发式，仅作为无 LLM 时的兜底）
_ORG_SUFFIX = (
    "公司|集团|大学|学院|医院|银行|协会|委员会|研究院|研究所|中心|实验室|"
    "学校|政府|法院|机构|事务所|基金会|出版社|连锁"
)
_PLACE_SUFFIX = (
    "市|省|自治区|自治州|县|区|镇|乡|村|岛|半岛|群岛|山脉|高原|平原|盆地|"
    "江|河|湖|海湾|海峡|大洋|沙漠"
)

# 关系三元组 schema 约束（P0-1）
# 谓词固定到白名单内，LLM 输出的差异化谓词做同义映射；不在白名单则丢弃该三元组。
_RELATION_PREDICATES = ("替代", "衍生", "属于", "任职于", "采用", "提出", "比对", "组成")
_RELATION_SYNONYMS = {
    "替代": ("取代", "替换"),
    "衍生": ("派生", "演化"),
    "属于": ("隶属", "归属"),
    "任职于": ("就职于", "供职于"),
    "采用": ("使用", "选用"),
    "提出": ("发表", "首创"),
    "比对": ("比较", "对比"),
    "组成": ("构成", "包含"),
}
# 单材料最多保留的关系条数（落库上限）；图谱展示再由 _SEMANTIC_LIMIT 另行限制。
MAX_RELATION_ITEMS = 100
# fallback 谓词匹配时两侧实体查找窗口（字）。
_RELATION_WINDOW = 15
# 关系 evidence 截取上限（字符）。
_MAX_RELATION_EVIDENCE = 120
# 谓词中出现这些否定前缀时视为否定（证据/兜底匹配需排除，不得误识别为正向关系）。
_NEGATION_PREFIXES = ("不是", "并非", "并未", "不属于", "没有", "未", "不", "非")
# 原文中谓词前紧邻否定的启发式窗口（字）。
_NEGATION_SCAN = 3

# fallback 执行结果：executed=True 表示兜底「正常执行完毕」（无论是否命中关系），
# items 为其产出的三元组；executed=False 表示兜底自身执行异常（与模型不可用区分，
# 不会有「正常无关系」误报为失败）。
class _FallbackResult:
    __slots__ = ("executed", "items")

    def __init__(self, executed: bool, items: list):
        self.executed = executed
        self.items = items


# 阶段 B §6.2：摘要/标签/实体/关系/图片 VLM 全部经统一单并发调度器
# (_ollama_scheduler) 提交，取代旧的双线程池与任务级去重锁，不留双并发路径。
# 调度器只做优先级排队与并发控制；幂等（输入 hash 未变不重算）仍由派生逻辑判定。
# 派生种类 → 默认优先级（§6.2： 摘要/实体 > 标签 > 关系 > 批量后台 > 图片 VLM）。
_KIND_PRIORITY = {
    KIND_SUMMARY: PRIORITY_SUMMARY_ENTITIES,
    KIND_ENTITY_EXTRACTION: PRIORITY_SUMMARY_ENTITIES,
    KIND_TAG_SUGGESTIONS: PRIORITY_TAGS,
    KIND_RELATION_EXTRACTION: PRIORITY_RELATIONS,
}
# 生成草稿（GENERATED_DRAFT）等批量/背景派生默认归后台。
_KIND_PRIORITY_DEFAULT = PRIORITY_BATCH_BACKGROUND


class _IndexReadError(RuntimeError):
    """向量库读取失败（P0-3 三态契约的 read_error 载体）。

    与「真空文本」严格区分：捕获方必须写可重试的 unavailable（或不写），
    绝不能写 skipped——2026-08-22 事故正是 read_error 被当成 empty 写了
    skipped，导致 20 个材料的 ok 关系记录被覆盖。
    """


def _safe_model_error(exc: Exception) -> dict:
    """返回可展示、不可泄露 URL/请求正文的派生失败诊断。"""
    if isinstance(exc, urllib.error.HTTPError):
        return {"errorCode": f"http_{exc.code}", "errorDetail": f"Ollama 返回 HTTP {exc.code}"}
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return {"errorCode": "timeout", "errorDetail": "Ollama 响应超时"}
    if isinstance(exc, urllib.error.URLError):
        return {"errorCode": "transport_error", "errorDetail": "无法完成与 Ollama 的连接"}
    if isinstance(exc, (ConnectionError, OSError)):
        return {"errorCode": "connection_error", "errorDetail": "Ollama 连接失败"}
    return {"errorCode": type(exc).__name__.lower(), "errorDetail": "Ollama 返回无效响应或生成失败"}


def _failed_content(base: dict, exc: Exception) -> dict:
    """失败记录统一带冷却时间，避免详情轮询触发无限重试。"""
    return {
        **base,
        **_safe_model_error(exc),
        "retryAfter": time.time() + _MODEL_RETRY_DELAY_SECONDS,
    }


def _retry_due(record: dict | None) -> bool:
    if record is None or record.get("status") not in ("failed", "unavailable"):
        return record is None
    content = record.get("content") or {}
    try:
        return time.time() >= float(content.get("retryAfter") or 0)
    except (TypeError, ValueError):
        return True


def _submit_derived_task(kind: str, fn, material_id: str, source_path: str, force: bool) -> bool:
    """把一次派生生成提交到统一调度器（阶段 B §6.2）。

    优先级：手动重新生成(force) 最高；否则按 kind 查 _KIND_PRIORITY。
    调度器为单并发，串行执行所有 LLM 派生；幂等由派生逻辑节流。
    """
    priority = PRIORITY_MANUAL_REGENERATE if force else _KIND_PRIORITY.get(kind, _KIND_PRIORITY_DEFAULT)
    return _ollama_scheduler.submit(
        priority,
        lambda: fn(material_id, source_path, force),
        material_id=material_id,
        kind=kind.lower(),
    )


def _generate_visual_description(material_id: str, source_path: str) -> None:
    """为 OCR 为空的图片生成可选 VLM 描述，失败不影响材料主任务。"""
    from embedder import caption_image_with_vlm_result

    snap = get_provider().get_local_snapshot()
    input_hash = hashlib.sha256(f"{source_path}:{snap.model}".encode("utf-8")).hexdigest()[:16]
    store = derived_store.DerivedStore.instance()
    try:
        description, reason = caption_image_with_vlm_result(source_path, snapshot=snap)
    except Exception as exc:  # 兼容 VLM 实现意外异常，确保派生失败不冒泡至 worker。
        logger.warning("图片 VLM 描述失败 %s: %s", material_id, type(exc).__name__)
        description, reason = "", type(exc).__name__.lower()
    if description:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_VISUAL_DESCRIPTION, "ok",
            {"text": description, "source": "vlm"}, input_hash, _generator_name(snap),
        )
        return
    status = "unavailable" if reason in {"disabled", "timeout", "image_read_error"} else "failed"
    store.set_derived_record(
        OWNER_MATERIAL, material_id, KIND_VISUAL_DESCRIPTION, status,
        {"text": "", "source": "vlm", "errorCode": reason or "empty_response"},
        input_hash, _generator_name(snap),
    )


def submit_visual_description(material_id: str, source_path: str) -> bool:
    """将纯视觉图片 VLM 描述放入统一调度器的最低优先级队列。"""
    return _ollama_scheduler.submit(
        PRIORITY_VLM_IMAGE,
        lambda: _generate_visual_description(material_id, source_path),
        material_id=material_id,
        kind=KIND_VISUAL_DESCRIPTION.lower(),
    )


def reset_derived_task_flags() -> None:
    """历史遗留的进程内去重已迁入统一调度器，此函数为无操作，保留兼容入口。

    阶段 B 起派生并发/去重由 `ollama_material_scheduler` 统一管理，不再有
    进程内 in-flight 标记可清。测试如需隔离，请替换/停用调度器单例。
    """


def _read_snapshot_input(source_path: str) -> str | None:
    """阶段A：优先从正文快照读派生输入（§5.1 快照是唯一输入来源，禁止依赖 Chroma）。

    三态区分（fail-closed，评审 P1）：
    - 已确认不存在快照（来源非 MindOS 材料 / 从未建立快照）：返回 None，
      仅此迁移期情形可回落旧「读已索引 chunk」；
    - 读取成功：返回正文（可为空串），**不再回落** Chroma；
    - 任何 DB/查询/快照文件异常：**raise _IndexReadError**，由调用方上报派生
      失败/不可用——绝不静默回退 Chroma 用陈旧内容生成。
    """
    try:
        from .stores.derived_store import material_id_for_source
        from .stores.material_pipeline_store import MaterialPipelineStore
        from .material_snapshot_saga import MaterialSnapshotSaga

        material_id = material_id_for_source(source_path)
        if not material_id:
            return None  # 来源未登记为 MindOS 材料 → 确认无快照，迁移期回退
        store = MaterialPipelineStore.instance()
        snap = store.current_snapshot(material_id)
        if snap is None:
            return None  # 确认无当前快照 → 迁移期回退
        return MaterialSnapshotSaga(store).read_snapshot_text(snap)
    except _IndexReadError:
        raise
    except Exception:  # 快照查询/读取异常统一 fail-closed：禁止回落 Chroma
        raise _IndexReadError(source_path) from None


def _input_text(source_path: str) -> str:
    """读取派生输入文本（正文 / OCR / 转写）；排除用户「说明」（caption）块。

    阶段A起优先读正文快照；无快照（迁移期）回落已索引的扁平 chunk。
    长度受 MAX_INPUT_CHARS 约束；读取失败（read_error）抛 _IndexReadError，
    由调用方决定降级策略。
    """
    snap_text = _read_snapshot_input(source_path)
    if snap_text is not None:
        return snap_text[:MAX_INPUT_CHARS]

    from vector_store import READ_ERROR, read_source_chunks

    status, chunks = read_source_chunks(source_path, limit=200)
    if status == READ_ERROR:
        raise _IndexReadError(source_path)
    parts: list[str] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        if meta.get("modality") == "caption":
            continue
        text = (chunk.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)[:MAX_INPUT_CHARS]


def _truncate_summary(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """超过 limit 时在句末安全截断；找不到句末则硬切。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for punct in "。！？.!?；;\n":
        index = head.rfind(punct)
        if index >= limit * 0.5:
            return head[: index + 1].strip()
    return head.strip()


def _is_unavailable(exc: Exception) -> bool:
    """连接类错误视为“模型不可用”，其余为“生成失败”。"""
    return isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError))


def _call_summary_entities_model(text: str, snap) -> str:
    """一次 LLM 调用同时生成摘要与实体，要求严格 JSON 对象输出。失败抛异常。

    返回形如 {"summary": "...", "entities": [...]} 的原始文本，由
    _parse_summary_entities 解析；模型不可用时由调用方降级。
    """
    prompt = (
        "你是 MindOS 本地知识库的内容分析助手。请仅根据下面资料内容，输出一个 JSON 对象，含两个字段：\n"
        '1. "summary"：用中文写一段不超过 200 字的摘要，准确概括要点，'
        "不补充资料之外的事实，不做评价。\n"
        '2. "entities"：数组，最多 15 个，抽取资料中的人物、地点、组织、专业术语等实体，'
        '每个元素形如 {"type": "person|place|organization|term", "name": "实体名", '
        '"confidence": 0.95}，name 去重，confidence 为 0 到 1 之间的小数。\n'
        "要求：type 只能是 person/place/organization/term 之一；实体名必须是资料中的"
        "自然语言词或短语，排除 markdown 符号、纯标点、分隔线（如 ###、---）等无意义内容；"
        "没有实体时 entities 返回 []。\n"
        '只输出 JSON 本身，形如 {"summary": "...", "entities": [...]}，'
        "不要解释、不要 markdown 围栏。\n\n"
        f"资料内容：\n{text}"
    )
    return _call_llm(
        "你是 MindOS 本地知识库的内容分析助手。",
        prompt,
        temperature=0.3,
        max_tokens=700,
        snap=snap,
    )


def _call_llm(system: str, prompt: str, temperature: float, max_tokens: int, snap) -> str:
    """材料识别强制走本机 Ollama，数据不出设备（D1）。失败抛异常。

    模型、地址、超时取自材料通道快照（snap，随运行时配置联动）。
    """
    body = {
        "model": snap.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        # 由 Ollama 使用模型自身的默认输出预算，避免截断 qwen3-vl 的 thinking 或正文。
        "options": {"temperature": temperature},
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
    return (answer or "").strip()


def _call_entity_model(text: str, snap) -> str:
    """调用已配置的问答模型抽取实体，要求严格 JSON 数组输出。失败抛异常。"""
    prompt = (
        "你是 MindOS 本地知识库的实体抽取助手。请仅根据下面资料内容，抽取其中的人物、地点、组织、"
        "专业术语等实体。用严格 JSON 数组输出，最多 15 个，每个元素形如："
        '{"type": "person|place|organization|term", "name": "实体名", '
        '"confidence": 0.95, "evidence": "资料中出现该实体的原文片段"}。'
        "要求：type 只能是 person/place/organization/term 之一；name 为实体名称并去重；"
        "confidence 为 0 到 1 之间的小数，代表抽取把握；evidence 必须是资料原文片段。"
        "没有实体时返回 []。只输出 JSON 本身，不要解释、不要 markdown 围栏。\n\n"
        f"资料内容：\n{text}"
    )
    return _call_llm(
        "你是 MindOS 本地知识库的实体抽取助手。",
        prompt,
        temperature=0.2,
        max_tokens=700,
        snap=snap,
    )


def _generator_name(snap) -> str:
    return f"ollama:{getattr(snap, 'model', '') or ''}"


def _mark_read_error(store, material_id: str, kinds_content: list[tuple[str, dict]], snap) -> None:
    """read_error 的统一降级（P0-3，2026-08-22 事故的正式修复）。

    - 已有 ok 记录：保持不覆盖（历史产物是有效输入算出的，读取失败不构成重算理由）；
    - 无 ok 记录：写可重试的 unavailable（可观测、refresh 会重投），绝不写 skipped。
    kinds_content: [(kind, 空内容结构), ...]
    """
    for kind, empty_content in kinds_content:
        probe = store.get_derived_record(OWNER_MATERIAL, material_id, kind)
        if probe is not None and probe.get("status") == "ok":
            logger.warning(
                "向量库读取失败但 %s 产物 ok，保持原记录不覆盖: %s", kind, material_id
            )
            continue
        store.set_derived_record(
            OWNER_MATERIAL, material_id, kind, "unavailable",
            empty_content, "", _generator_name(snap),
        )


def _generate_summary_and_entities(material_id: str, source_path: str, force: bool = False) -> None:
    """一次 LLM 调用同时生成摘要与实体（同步核心逻辑，供后台池线程调用）。

    摘要与实体分别落两条派生记录，保持现有公开结构不变；同源输入、同一 hash 去重：
    - 空文本：两条记录都落 skipped，不调用模型；
    - LLM 不可用/失败：摘要记 failed/unavailable；实体走 jieba 降级（有结果则 ok），
      否则与摘要同状态；
    - 摘要与实体各自独立容错：解析出摘要但实体为空 → 实体走降级，不影响摘要。
    """
    # §5.1.1：任务边界取一次材料快照，下传给模型调用（URL/模型/超时随运行时配置联动）。
    snap = get_provider().get_local_snapshot()
    store = derived_store.DerivedStore.instance()
    try:
        text = _input_text(source_path)
    except _IndexReadError:
        # P0-3 三态契约：read_error ≠ 空文本。已有 ok 不覆盖，否则写可重试的
        # unavailable（绝不写 skipped——2026-08-22 事故根因即 read_error 被当 empty）。
        _mark_read_error(store, material_id, [
            (KIND_SUMMARY, {"text": ""}),
            (KIND_ENTITY_EXTRACTION, {"items": []}),
        ], snap)
        return
    if not text.strip():
        # 深度防御（read_error 已在上方显式拦截，此处是「查询成功但空」的矛盾态）：
        # 任一产物 ok 说明该材料历史上有可读文本——典型场景是 schema 迁移/
        # 全量重建窗口（集合刚清空、scan 未完成）读到临时性空集合。写 skipped
        # 会覆盖 ok 且 skipped 不被 refresh 重投，因此保持原记录不写。
        # force=True 例外：调用方（watcher 空文本早退路径）已明确判定文件无文字、
        # 旧 chunks 是被主动清除的合法空，此时必须落 skipped 而非沿用旧摘要。
        if force:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_SUMMARY, "skipped",
                {"text": ""}, "", _generator_name(snap),
            )
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "skipped",
                {"items": []}, "", _generator_name(snap),
            )
            return
        for kind in (KIND_SUMMARY, KIND_ENTITY_EXTRACTION):
            probe = store.get_derived_record(OWNER_MATERIAL, material_id, kind)
            if probe is not None and probe.get("status") == "ok":
                logger.warning(
                    "摘要/实体输入为空但 %s 产物 ok，疑似向量库读取故障，跳过不覆盖: %s",
                    kind, material_id,
                )
                return
        # 空文本 / 无有效转写 / OCR 为空 → 不调用模型
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_SUMMARY, "skipped",
            {"text": ""}, "", _generator_name(snap),
        )
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "skipped",
            {"items": []}, "", _generator_name(snap),
        )
        return
    input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    summary_rec = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_SUMMARY)
    entity_rec = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION)
    if (
        not force
        and summary_rec is not None and summary_rec["input_hash"] == input_hash
        and summary_rec["status"] == "ok"
        and summary_rec.get("generator") == _generator_name(snap)
        and entity_rec is not None and entity_rec["input_hash"] == input_hash
        and entity_rec["status"] == "ok"
        and entity_rec.get("generator") == _generator_name(snap)
    ):
        return  # 内容未变且两条记录均就绪（generator 一致），不重复调用模型
    try:
        answer = _call_summary_entities_model(text, snap)
    except Exception as exc:
        status = "unavailable" if _is_unavailable(exc) else "failed"
        diagnostic = _safe_model_error(exc)
        logger.warning(
            "摘要/实体生成失败 %s: %s (%s)",
            material_id, diagnostic["errorCode"], diagnostic["errorDetail"],
        )
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_SUMMARY, status,
            _failed_content({"text": ""}, exc), input_hash, _generator_name(snap),
        )
        items = _entity_fallback(text)
        if items:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "ok",
                {"items": items, "source": "fallback"}, input_hash, _generator_name(snap),
            )
        else:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, status,
                {"items": []}, input_hash, _generator_name(snap),
            )
        return
    summary, entities = _parse_summary_entities(answer, text)
    summary = _truncate_summary(summary)
    if summary:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_SUMMARY, "ok",
            {"text": summary}, input_hash, _generator_name(snap),
        )
    else:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_SUMMARY, "failed",
            {"text": ""}, input_hash, _generator_name(snap),
        )
    if entities is not None:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "ok",
            {"items": entities, "source": "llm"}, input_hash, _generator_name(snap),
        )
    else:
        items = _entity_fallback(text)
        if items:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "ok",
                {"items": items, "source": "fallback"}, input_hash, _generator_name(snap),
            )
        else:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "failed",
                {"items": []}, input_hash, _generator_name(snap),
            )
            return  # 实体不可用，关系依赖实体，不触发关系任务
    # 实体刚就绪 → 链式触发关系抽取（解决关系任务早于实体就绪的竞态，
    # 保证「上传后不打开分析页面」关系最终也能生成）。
    _submit_relations(material_id, source_path, False)


def submit_summary(material_id: str, source_path: str, force: bool = False) -> None:
    """提交摘要+实体生成任务到后台池（不阻塞索引 worker / HTTP）。

    摘要与实体由同一次 LLM 调用生成（合并调用）；内容 hash 未变且两条记录
    均就绪时 _generate_summary_and_entities 自动跳过，不会重复调用模型。
    """
    _submit_derived_task(
        KIND_SUMMARY, _generate_summary_and_entities, material_id, source_path, force
    )


def summary_of(material_id: str) -> dict:
    """返回某资料当前摘要的公开结构（detail_of 与接口共用）。"""
    rec = derived_store.DerivedStore.instance().get_derived_record(
        OWNER_MATERIAL, material_id, KIND_SUMMARY
    )
    if rec is None:
        return {"text": "", "status": "pending", "generatedAt": None, "errorCode": None, "diagnostic": None}
    generated_at = None
    if rec.get("updated_at"):
        generated_at = datetime.fromtimestamp(rec["updated_at"], tz=timezone.utc).isoformat()
    return {
        "text": (rec.get("content") or {}).get("text", ""),
        "status": rec.get("status", "pending"),
        "generatedAt": generated_at,
        "errorCode": (rec.get("content") or {}).get("errorCode"),
        "diagnostic": (rec.get("content") or {}).get("errorDetail"),
    }


def _evidence_snippet(text: str, term: str, limit: int = 80) -> str:
    """从原文中截取包含某实体的片段作为证据（找不到返回空串）。"""
    index = text.find(term)
    if index < 0:
        return ""
    start = max(0, index - 20)
    return text[start : index + len(term) + 40].strip()[:limit]


def _make_entity(typ: str, name: str, confidence: float, evidence: str) -> dict:
    return {
        "entityId": f"entity:{typ}:{name}",
        "type": typ,
        "name": name,
        "confidence": round(float(confidence), 3),
        "evidence": (evidence or "")[:MAX_EVIDENCE_CHARS],
    }


def _parse_entity_json(answer: str):
    """容错解析模型输出的实体 JSON（允许 ```json 围栏 / 多余空白）。

    返回 list 表示解析成功；返回 None 表示输出不合法（调用方决定降级）。
    """
    cleaned = (answer or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("entities", "items", "result", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return None
    except Exception:
        pass
    bracket = re.search(r"\[[\s\S]*\]", cleaned)
    if bracket:
        try:
            data = json.loads(bracket.group(0))
            if isinstance(data, list):
                return data
        except Exception:
            return None
    return None


def _normalize_entity(raw, source_text: str) -> dict | None:
    """严格校验单个实体对象并绑定原文证据。

    - 类型 / 名称 / 置信度不合法 → 拒绝；
    - 实体名必须在输入原文中命中，否则视为模型幻觉直接丢弃；
    - 实体名须含中文字符 / 字母 / 数字，排除 ###、---、纯标点等 markdown 符号；
    - 不信任模型传回的 evidence，统一由 _evidence_snippet(source_text, name)
      从原文截取；无法生成证据则同样丢弃。
    """
    if not isinstance(raw, dict):
        return None
    typ = str(raw.get("type") or "").strip().lower()
    if typ not in ENTITY_TYPES:
        return None
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > MAX_ENTITY_NAME:
        return None
    if not _NATURAL_TOKEN_RE.search(name):
        return None  # 纯符号/标点（如 ###、---）不是自然语言实体
    if source_text.find(name) < 0:
        return None  # 实体不在原文中 → 模型幻觉，不进入公开结果
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.6
    if not (0 <= confidence <= 1):
        confidence = 0.6
    # evidence 只取原文命中片段，绝不使用模型返回的片段
    evidence = _evidence_snippet(source_text, name)
    if not evidence:
        return None
    return _make_entity(typ, name, confidence, evidence)


def _dedupe_entities(items: list[dict]) -> list[dict]:
    """按 (type, name) 去重，保留置信度最高者；超过上限则截断。"""
    best: dict[tuple[str, str], dict] = {}
    for it in items:
        key = (it["type"], it["name"])
        prev = best.get(key)
        if prev is None or it["confidence"] > prev["confidence"]:
            best[key] = it
    return list(best.values())[:MAX_ENTITY_ITEMS]


def _entities_from_llm(answer: str, source_text: str) -> list[dict] | None:
    """解析并严格校验 LLM JSON 输出。

    - 返回 None：输出不合法（调用方走 fallback / failed）；
    - 返回 []：模型明确输出空数组 → 视为合法空；
    - 输出非空但全部未通过原文命中校验 → 视为幻觉，同样返回 None，
      由调用方继续走 fallback / failed，避免把模型幻觉当 ok 展示。
    """
    data = _parse_entity_json(answer)
    if data is None:
        return None
    items = [it for raw in data if (it := _normalize_entity(raw, source_text)) is not None]
    if not data:
        return []  # 模型合法地认为没有实体
    if not items:
        return None  # 输出的实体全部不在原文中 → 模型幻觉，不落库
    return _dedupe_entities(items)


def _parse_summary_entities(answer: str, source_text: str) -> tuple[str, list[dict] | None]:
    """解析一次调用返回的 {summary, entities}，返回 (摘要文本, 实体列表或 None)。

    - 能解析出 JSON 对象 → 分别取摘要与实体（实体复用严格校验，纯符号/幻觉被剔除）；
    - 模型只输出纯文本摘要（未按 JSON 指令）→ 整段按摘要处理，实体为 None，
      由调用方走 jieba 降级，不丢摘要。
    """
    cleaned = (answer or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    summary = ""
    entities: list[dict] | None = None
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            summary = str(data.get("summary") or "").strip()
            ent = data.get("entities")
            if isinstance(ent, list):
                items = [it for raw in ent if (it := _normalize_entity(raw, source_text)) is not None]
                entities = _dedupe_entities(items)
    except Exception:
        pass
    if not summary and entities is None:
        summary = cleaned  # 非 JSON 纯文本 → 整段按摘要处理
    return summary, entities


def _entity_fallback(text: str) -> list[dict]:
    """无模型 / 模型输出不合法时，用正则（组织/地点后缀）+ jieba 关键词降级抽取。"""
    from .tag_suggest import _keyword_fallback

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(typ: str, name: str, confidence: float) -> None:
        name = (name or "").strip().rstrip("的，,。、 ")
        if (
            not name
            or len(name) > MAX_ENTITY_NAME
            or name[0] in "一二三四五这那某该我你他几一"
            or not _NATURAL_TOKEN_RE.search(name)
        ):
            return
        key = (typ, name)
        if key in seen:
            return
        seen.add(key)
        items.append(_make_entity(typ, name, confidence, _evidence_snippet(text, name)))

    for suffix, typ in ((_ORG_SUFFIX, "organization"), (_PLACE_SUFFIX, "place")):
        for m in re.finditer(rf"[\u4e00-\u9fa5A-Za-z0-9]{{2,20}}?({suffix})", text):
            add(typ, m.group(0), 0.45)
    for term in _keyword_fallback(text, limit=8):
        add("term", term, 0.5)
    return items[:MAX_ENTITY_ITEMS]


def _generate_tag_suggestions(material_id: str, source_path: str, force: bool = False) -> None:
    """生成并保存候选标签（同步核心逻辑，供后台池线程调用）。"""
    from .tag_suggest import suggest_tags_with_source

    # §5.1.1：任务边界取一次材料快照，下传用于 generator 指纹与重算判定。
    snap = get_provider().get_local_snapshot()
    store = derived_store.DerivedStore.instance()
    try:
        text = _input_text(source_path)
    except _IndexReadError:
        # P0-3：read_error 时已有 ok 不覆盖，否则写可重试的 unavailable（不写 skipped）
        _mark_read_error(
            store, material_id, [(KIND_TAG_SUGGESTIONS, {"items": [], "source": "fallback"})], snap
        )
        return
    if not text.strip():
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_TAG_SUGGESTIONS, "skipped",
            {"items": [], "source": "fallback"}, "", _generator_name(snap),
        )
        return
    input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    existing = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_TAG_SUGGESTIONS)
    if (
        not force
        and existing is not None
        and existing["input_hash"] == input_hash
        and existing["status"] == "ok"
        and existing.get("generator") == _generator_name(snap)
    ):
        return  # 内容未变且 generator 一致，不重复调用模型
    result = suggest_tags_with_source(text, Path(source_path).name)
    tags = result["items"]
    source = result["source"]
    if not tags:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_TAG_SUGGESTIONS, "failed",
            {
                "items": [], "source": source,
                **({"diagnostic": result["diagnostic"]} if result.get("diagnostic") else {}),
            }, input_hash, _generator_name(snap),
        )
        return
    # 重新生成时保留已确认标记（按名称），避免用户已采用的候选被重置为未确认。
    confirmed: set[str] = set()
    if existing is not None and existing["status"] == "ok":
        confirmed = {
            it.get("name") for it in (existing["content"] or {}).get("items", [])
            if it.get("confirmed")
        }
    items = [
        {"suggestionId": f"tag:{name}", "name": name, "confirmed": name in confirmed}
        for name in tags
    ]
    store.set_derived_record(
        OWNER_MATERIAL, material_id, KIND_TAG_SUGGESTIONS, "ok",
        {
            "items": items, "source": source,
            **({"diagnostic": result["diagnostic"]} if result.get("diagnostic") else {}),
        }, input_hash, _generator_name(snap),
    )


def _generate_entities(material_id: str, source_path: str, force: bool = False) -> None:
    """生成并保存实体抽取（同步核心逻辑，供后台池线程调用）。"""
    # §5.1.1：任务边界取一次材料快照，下传给模型调用。
    snap = get_provider().get_local_snapshot()
    store = derived_store.DerivedStore.instance()
    try:
        text = _input_text(source_path)
    except _IndexReadError:
        # P0-3：read_error 时已有 ok 不覆盖，否则写可重试的 unavailable
        _mark_read_error(store, material_id, [(KIND_ENTITY_EXTRACTION, {"items": []})], snap)
        return
    if not text.strip():
        # 深度防御（read_error 已显式拦截，此处是「查询成功但空」的矛盾态）：
        # 摘要 ok 说明该材料历史上有可读文本（schema 迁移/重建窗口的临时性
        # 空集合）。写 skipped 会覆盖可重试状态并让 refresh 永久跳过，因此
        # 保持原记录不写。
        sum_probe = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_SUMMARY)
        if sum_probe is not None and sum_probe.get("status") == "ok":
            logger.warning(
                "实体抽取输入为空但摘要产物 ok，疑似向量库读取故障，跳过不覆盖: %s",
                material_id,
            )
            return
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "skipped",
            {"items": []}, "", _generator_name(snap),
        )
        return
    input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    existing = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION)
    if (
        not force
        and existing is not None
        and existing["input_hash"] == input_hash
        and existing["status"] == "ok"
        and existing.get("generator") == _generator_name(snap)
    ):
        return  # 内容未变且 generator 一致，不重复调用模型
    try:
        answer = _call_entity_model(text, snap)
    except Exception as exc:
        status = "unavailable" if _is_unavailable(exc) else "failed"
        diagnostic = _safe_model_error(exc)
        logger.warning(
            "实体抽取失败 %s: %s (%s)",
            material_id, diagnostic["errorCode"], diagnostic["errorDetail"],
        )
        items = _entity_fallback(text)
        if items:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "ok",
                {"items": items, "source": "fallback"}, input_hash, _generator_name(snap),
            )
            # 手动重生成的强制语义必须传递给后续关系任务。否则调度器会以
            # 此普通任务替换尚未执行的强制关系任务，重新受 hash 节流影响。
            _submit_relations(material_id, source_path, force)
            return
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, status,
            _failed_content({"items": []}, exc), input_hash, _generator_name(snap),
        )
        return
    items = _entities_from_llm(answer, text)
    if items is not None:  # 模型输出了合法 JSON（可为空数组）→ ok
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "ok",
            {"items": items, "source": "llm"}, input_hash, _generator_name(snap),
        )
        _submit_relations(material_id, source_path, force)
        return
    # 输出不合法 / 实体全部未通过原文校验 → 正则/jieba 降级；
    # 仍无结果则 failed（绝不展示模型原始文本或幻觉）
    items = _entity_fallback(text)
    if items:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "ok",
            {"items": items, "source": "fallback"}, input_hash, _generator_name(snap),
        )
        _submit_relations(material_id, source_path, force)
        return
    store.set_derived_record(
        OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION, "failed",
        {"items": []}, input_hash, _generator_name(snap),
    )


def _normalize_relation_predicate(value: str) -> str | None:
    """把模型/兜底输出的谓词规范化到白名单。

    - 先去空格、标点，再与白名单/同义词做**精确匹配**（不做包含匹配，
      避免「不属于/未采用」这类否定被误识别为「属于/采用」）；
    - 命中否定前缀视作否定，一律返回 None（调用方丢弃该三元组）；
    - 其它一律返回 None，保证谓词一致性。
    """
    raw = (value or "").strip()
    cleaned = re.sub(r"[\s，。．、；；：:\u3000]", "", raw)
    if not cleaned:
        return None
    if any(cleaned.startswith(p) for p in _NEGATION_PREFIXES):
        return None  # 否定谓词（不属于、未采用、并非属于…）不视为正向关系
    if cleaned in _RELATION_PREDICATES:
        return cleaned
    for canonical, aliases in _RELATION_SYNONYMS.items():
        if cleaned in aliases:
            return canonical
    return None


def _is_negated_prefix(text: str, token_start: int) -> bool:
    """判断 token 前紧邻窗口内是否是否定前缀（decision 谓词字面匹配用）。

    fallback 按字面扫描白名单谓词时，若命中处前有「不/未/非…」否定，
    说明原文是「不采用/未属于」等否定表达，不应产出正向关系。
    """
    if token_start <= 0:
        return False
    head = text[max(0, token_start - _NEGATION_SCAN):token_start]
    for p in _NEGATION_PREFIXES:
        if head.endswith(p):
            return True
    return False


def _relation_evidence(source_text: str, subject: str, predicate: str, obj: str) -> str:
    """从原文截取包含三元组的片段作为证据。

    要求 subject → predicate(或其同义词字面) → object **按序出现且两端各在
    _RELATION_WINDOW 内**；找不到完整关系片段返回空串（调用方据此丢弃该三元组，
    不使用「只命中主语」的降级证据，避免放行模型幻觉）。
    """
    if not (subject and predicate and obj) or not source_text:
        return ""
    pred_tokens = sorted((predicate,) + _RELATION_SYNONYMS.get(predicate, ()), key=len, reverse=True)
    for sm in re.finditer(re.escape(subject), source_text):
        s_end = sm.end()
        pred_hit = None
        for ptok in pred_tokens:
            pm = re.search(re.escape(ptok), source_text[s_end:s_end + _RELATION_WINDOW])
            if pm:
                pred_hit = (pm.start(), pm.end(), ptok)
                break
        if not pred_hit:
            continue
        if _is_negated_prefix(source_text, s_end + pred_hit[0]):
            continue  # 谓词前紧邻否定 → 不是正向关系
        p_end_abs = s_end + pred_hit[1]
        om = re.search(re.escape(obj), source_text[p_end_abs:p_end_abs + _RELATION_WINDOW])
        if not om:
            continue
        o_abs = p_end_abs + om.start()
        start = max(0, sm.start() - 20)
        end = min(len(source_text), o_abs + len(obj) + 30)
        return source_text[start:end].strip()[:_MAX_RELATION_EVIDENCE]
    return ""


def _make_relation(subject: dict, predicate: str, obj: dict, confidence: float, evidence: str) -> dict:
    key = f"{subject['name']}\x1f{predicate}\x1f{obj['name']}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return {
        "relationId": f"rel:{digest}",
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": round(float(confidence), 3),
        "evidence": (evidence or "")[:_MAX_RELATION_EVIDENCE],
    }


def _dedupe_relations(items: list[dict]) -> list[dict]:
    """按 relationId 去重，保留 confidence 最高者；超过上限截断。"""
    best: dict[str, dict] = {}
    for it in items:
        rid = it.get("relationId") or ""
        prev = best.get(rid)
        if prev is None or (it.get("confidence") or 0) > (prev.get("confidence") or 0):
            best[rid] = it
    return list(best.values())[:MAX_RELATION_ITEMS]


def _parse_relation_json(answer: str, entity_index: dict, source_text: str) -> list[dict] | None:
    """解析并严格校验 LLM 输出的关系三元组 JSON 数组。

    - 端点 name 必须原样命中本材料实体产物（不在则丢弃）；
    - type 以实体产物为准（覆盖模型返回的 type）；
    - predicate 归一化到白名单，未命中丢弃；
    - confidence 必须为 0..1 有限数，非法丢弃；
    - evidence 由原文截取三元组片段，取不到时丢弃；
    - 返回 None 表示输出无法解析（调用方走 fallback）；空数组合法返回 []；
    - 全部被过滤但模型确有输出 → 视为幻觉，返回 None。
    """
    try:
        payload = json.loads((answer or "").strip())
    except Exception:
        return None
    raw_list = None
    if isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict):
        for key in ("relations", "items", "triples", "result", "data"):
            if isinstance(payload.get(key), list):
                raw_list = payload[key]
                break
    if raw_list is None:
        return None

    def _resolve(name):
        if not isinstance(name, str):
            return None
        key = name.strip()
        rec = entity_index.get(key)
        if rec is None:
            rec = entity_index.get(key.lower())
        return rec

    items: list[dict] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        sub = raw.get("subject")
        obj = raw.get("object")
        if not (isinstance(sub, dict) and isinstance(obj, dict)):
            continue
        sub_rec, obj_rec = _resolve(sub.get("name")), _resolve(obj.get("name"))
        if sub_rec is None or obj_rec is None:
            continue  # 端点不在实体产物内
        if sub_rec is obj_rec:
            continue  # 自指关系
        predicate = _normalize_relation_predicate(raw.get("predicate"))
        if not predicate:
            continue
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence) or not (0 <= confidence <= 1):
            continue
        evidence = _relation_evidence(source_text, sub_rec["name"], predicate, obj_rec["name"])
        if not evidence:
            continue
        items.append(_make_relation(
            {"type": sub_rec["type"], "name": sub_rec["name"]},
            predicate,
            {"type": obj_rec["type"], "name": obj_rec["name"]},
            confidence, evidence,
        ))
    if not raw_list:
        return []  # 模型合法地认为没有关系
    if not items:
        return None  # 输出的三元组全部不合法 → 视为幻觉
    return _dedupe_relations(items)


def _call_relation_model(text: str, entity_names: list[str], snap) -> str:
    """调用已配置模型抽取关系三元组，端点强制选自实体产物。失败抛异常。"""
    names = "\n".join(f"- {n}" for n in entity_names) or "（无）"
    prompt = (
        "你是 MindOS 本地知识库的关系抽取助手。下面给出资料正文和一份「可用实体名」列表。"
        "请抽取资料中实体之间的语义关系，用严格 JSON 数组输出，每个元素形如："
        '{"subject": {"name": "实体A"}, "predicate": "替代", "object": {"name": "实体B"}, '
        '"confidence": 0.9}。\n'
        "硬性要求：\n"
        f"1. subject.name 与 object.name 必须原样选自上面的可用实体名列表，不得自创新名；\n"
        "2. predicate 只能是下列之一：替代、衍生、属于、任职于、采用、提出、比对、组成；\n"
        "3. confidence 为 0 到 1 之间的小数；\n"
        "4. 无有意义关系时返回 []；只输出 JSON 本身，不要解释、不要 markdown 围栏。\n\n"
        f"可用实体名：\n{names}\n\n"
        f"资料内容：\n{text}"
    )
    return _call_llm(
        "你是 MindOS 本地知识库的关系抽取助手。",
        prompt,
        temperature=0.3,
        max_tokens=1500,
        snap=snap,
    )


def _relation_fallback(text: str, entity_index: dict) -> _FallbackResult:
    """无模型时的高精度低召回兜底：白名单谓词（含同义词）字面出现，
    且两侧较近窗口内各命中一个实体产物实体，才产出三元组；否则宁可不产。

    端点类型**必须读取实体产物的真实 type**（不硬编码 term），
    保证人名/组织/地点不被错误标记为 term；否定表达由 _relation_evidence 拒绝。

    返回 _FallbackResult：
    - executed=True 表示兜底正常执行完毕（items 可能为空 → 属「无关系」而非失败）；
    - executed=False 表示兜底自身抛异常（调用方据此标记 failed/unavailable，
      不与「正常无关系」混淆）。
    """
    try:
        items = _relation_fallback_items(text, entity_index)
        return _FallbackResult(True, items)
    except Exception as exc:
        logger.warning("关系规则兜底执行异常: %s", type(exc).__name__)
        return _FallbackResult(False, [])


def _relation_fallback_items(text: str, entity_index: dict) -> list:
    if not text or not entity_index:
        return []
    # name -> 原始名（唯一性按原文大小写/span）
    names = list(dict.fromkeys(n for n in entity_index if str(n).strip()))
    items: list[dict] = []
    seen: set[str] = set()

    def _nearest_before(pos: int) -> tuple[str, int] | None:
        best, best_end = None, -1
        for n in names:
            i = text.rfind(n, 0, pos)
            if i >= 0:
                end = i + len(n)
                if end > best_end and pos - end <= _RELATION_WINDOW:
                    best, best_end = n, end
        return (best, best_end) if best else None

    def _nearest_after(pos: int) -> tuple[str, int] | None:
        best, best_start = None, -1
        for n in names:
            i = text.find(n, pos)
            if i >= 0:
                if best_start < 0 or i < best_start:
                    best, best_start = n, i
        return (best, best_start) if best else None

    for predicate in _RELATION_PREDICATES:
        words = (predicate,) + _RELATION_SYNONYMS.get(predicate, ())
        for word in words:
            for m in re.finditer(re.escape(word), text):
                lb = _nearest_before(m.start())
                ra = _nearest_after(m.end())
                if not lb or not ra or lb[0] == ra[0]:
                    continue
                if (ra[1] - m.end()) > _RELATION_WINDOW:
                    continue
                key = (lb[0], predicate, ra[0])
                if key in seen:
                    continue
                seen.add(key)
                evidence = _relation_evidence(text, lb[0], predicate, ra[0])
                if not evidence:
                    continue
                sub_rec = entity_index.get(lb[0]) or {}
                obj_rec = entity_index.get(ra[0]) or {}
                items.append(_make_relation(
                    {"type": sub_rec.get("type", "term"), "name": lb[0]},
                    predicate,
                    {"type": obj_rec.get("type", "term"), "name": ra[0]},
                    0.6, evidence,
                ))
    return _dedupe_relations(items)


def _clean_entity_items(ent_rec: dict | None) -> list[dict]:
    """Return only structurally valid entity items from a derived record.

    Historical records may contain malformed ``content`` or item values. Relation
    generation must skip those records rather than failing inside the worker.
    """
    if not isinstance(ent_rec, dict):
        return []
    content = ent_rec.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("items"), list):
        return []
    return [
        item for item in content["items"]
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
        and item.get("type") in ENTITY_TYPES
    ]


def _relation_input_hash(text: str, ent_rec: dict) -> str:
    """关系抽取的幂等 hash：正文 + 实体产物 + 生成器/版本共同参与。

    实体产物被修正（名称/类型/来源变化）而正文未变时，关系也必须重算；
    只 hash 正文会让过期的实体导致关系记录被误判为「内容未变」而跳过。
    """
    ent_items = sorted(
        (str(it.get("type") or ""), str(it.get("name") or ""))
        for it in _clean_entity_items(ent_rec)
    )
    content = ent_rec.get("content") if isinstance(ent_rec, dict) else {}
    content = content if isinstance(content, dict) else {}
    gen = (ent_rec.get("generator") or "") or content.get("source") or ""
    ent_json = json.dumps(ent_items, ensure_ascii=False)
    payload = f"{text}\x1f{ent_json}\x1f{gen}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _generate_relations(material_id: str, source_path: str, force: bool = False) -> None:
    """生成并保存关系三元组（同步核心逻辑，供后台池线程调用）。"""
    # §5.1.1：任务边界取一次材料快照，下传给模型调用。
    snap = get_provider().get_local_snapshot()
    store = derived_store.DerivedStore.instance()
    try:
        text = _input_text(source_path)
    except _IndexReadError:
        # P0-3：read_error 时已有 ok 不覆盖，否则写可重试的 unavailable。
        # 2026-08-22 事故正是此处把 read_error 当 empty 写了 skipped，
        # 20 个材料的 ok 关系记录被覆盖成不可重投的稳定终态。
        _mark_read_error(
            store, material_id,
            [(KIND_RELATION_EXTRACTION, {"items": [], "source": "fallback"})], snap,
        )
        return
    if not text.strip():
        # 深度防御（read_error 已显式拦截，此处是「查询成功但空」的矛盾态）：
        # 实体产物 ok 说明该材料历史上有可读文本（schema 迁移/重建窗口的
        # 临时性空集合）。写 skipped 会把已有 ok 记录覆盖成「稳定终态」造成
        # 数据丢失，因此保持原记录不写，待环境恢复后由 refresh_analysis 重新投递。
        ent_probe = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION)
        if ent_probe is not None and ent_probe.get("status") == "ok":
            logger.warning(
                "关系抽取输入为空但实体产物 ok，疑似向量库读取故障，跳过不覆盖: %s",
                material_id,
            )
            return
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, "skipped",
            {"items": [], "source": "fallback"}, "", _generator_name(snap),
        )
        return
    # 依赖实体产物（端点必须选自实体，实体未就绪时保持可重试的 unavailable，不写成完成状态）
    ent_rec = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION)
    if ent_rec is None or ent_rec.get("status") != "ok":
        # 实体尚未就绪：不立即写完成态，也不此处重试（避免高频竞争）。
        # 由实体写入点（_generate_summary_and_entities / _generate_entities）在产物落库后
        # 主动 _submit_relations 重试；refresh_analysis 也会对 unavailable 补算。
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, "unavailable",
            {"items": [], "source": "fallback"}, _relation_input_hash(text, ent_rec or {}),
            _generator_name(snap),
        )
        return
    input_hash = _relation_input_hash(text, ent_rec)
    existing = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION)
    if (
        not force
        and existing is not None
        and existing["input_hash"] == input_hash
        and existing["status"] == "ok"
        and existing.get("generator") == _generator_name(snap)
    ):
        return  # 正文与实体产物均未变且 generator 一致，不重复调用
    entity_index = {str(it["name"]).strip(): it for it in _clean_entity_items(ent_rec)}
    entity_names = sorted(entity_index.keys())
    if not entity_names:
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, "ok",
            {"items": [], "source": "fallback"}, input_hash, _generator_name(snap),
        )
        return
    try:
        answer = _call_relation_model(text, entity_names, snap)
    except Exception as exc:
        # 仅模型不可用时走规则兜底。兜底正常执行（即便无匹配）→ ok/source=fallback，
        # 表示「已尽力、确实没有可见关系」；其它调用异常按 failed 保留重试机会。
        logger.warning("关系抽取失败 %s: %s", material_id, type(exc).__name__)
        fallback = _relation_fallback(text, entity_index) if _is_unavailable(exc) else _FallbackResult(False, [])
        if fallback.executed:
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, "ok",
                {"items": fallback.items, "source": "fallback"}, input_hash, _generator_name(snap),
            )
        else:
            status = "unavailable" if _is_unavailable(exc) else "failed"
            store.set_derived_record(
                OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, status,
                _failed_content({"items": []}, exc), input_hash, _generator_name(snap),
            )
        return
    items = _parse_relation_json(answer, entity_index, text)
    if items is not None:  # 模型输出了合法 JSON（可为空数组）→ ok
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, "ok",
            {"items": items, "source": "llm"}, input_hash, _generator_name(snap),
        )
        return
    # 模型响应无法通过严格校验（含全部幻觉）不是“模型不可用”。按状态契约写
    # failed，保留后续重试机会；不能以空 fallback 固化为 ok。
    store.set_derived_record(
        OWNER_MATERIAL, material_id, KIND_RELATION_EXTRACTION, "failed",
        {
            "items": [],
            "errorCode": "invalid_model_response",
            "errorDetail": "Ollama 返回的关系三元组不符合校验规则",
            "retryAfter": time.time() + _MODEL_RETRY_DELAY_SECONDS,
        }, input_hash, _generator_name(snap),
    )


def relations_of(material_id: str) -> dict:
    return _derived_items_view(material_id, KIND_RELATION_EXTRACTION)


def submit_analysis(material_id: str, source_path: str, force: bool = False) -> None:
    """提交标签候选与关系三元组生成任务到后台池（不阻塞索引 worker / HTTP）。

    实体已随摘要由同一次 LLM 调用生成（见 submit_summary）；标签候选与关系抽取
    依赖实体产物，实体未就绪时关系任务会写 unavailable，随后由实体写入点链式
    重试或 refresh_analysis 补算（见 _submit_relations）。
    """
    _submit_derived_task(
        KIND_TAG_SUGGESTIONS, _generate_tag_suggestions,
        material_id, source_path, force,
    )
    # 用户显式「重新生成分析」必须同时刷新实体。否则实体失败/过期时，关系
    # 任务会因没有可用端点直接短路，表面看已重新生成，实际上不会请求 Ollama。
    if force:
        _submit_derived_task(
            KIND_ENTITY_EXTRACTION, _generate_entities,
            material_id, source_path, True,
        )
    _submit_relations(material_id, source_path, force)


def reparse_all(material_id: str, source_path: str) -> dict:
    """强制重新生成摘要、标签、实体和关系，并立即公开 pending 状态。

    不能只提交后台任务：旧产物若仍为 ``ok``，前端会在第一次轮询时误判任务已
    完成并停止刷新。这里先保留旧内容、将四项记录统一标为 pending，再提交所有
    强制 LLM 任务；任务完成后会照常覆盖对应产物。
    """
    store = derived_store.DerivedStore.instance()
    for kind in (
        KIND_SUMMARY,
        KIND_TAG_SUGGESTIONS,
        KIND_ENTITY_EXTRACTION,
        KIND_RELATION_EXTRACTION,
    ):
        record = store.get_derived_record(OWNER_MATERIAL, material_id, kind)
        if record is None:
            continue
        store.set_derived_record(
            OWNER_MATERIAL,
            material_id,
            kind,
            "pending",
            dict(record.get("content") or {}),
            str(record.get("input_hash") or ""),
            str(record.get("generator") or _generator_name(get_provider().get_local_snapshot())),
        )
    submit_summary(material_id, source_path, force=True)
    submit_analysis(material_id, source_path, force=True)
    return analysis_of(material_id)


def _submit_relations(material_id: str, source_path: str, force: bool = False) -> bool:
    """把关系三元组生成任务提交到统一调度器（阶段 B §6.2）。

    统一调度器单并发执行；同材料的待执行关系任务由调度器按最新请求替换。
    最终幂等仍由 _generate_relations 内的复合 hash 保证。
    """
    priority = PRIORITY_MANUAL_REGENERATE if force else PRIORITY_RELATIONS
    return _ollama_scheduler.submit(
        priority,
        lambda: _generate_relations(material_id, source_path, force),
        material_id=material_id,
        kind=KIND_RELATION_EXTRACTION.lower(),
    )


def reset_relation_task_flags() -> None:
    """历史遗留的关系任务去重已迁入统一调度器，此函数为无操作，保留兼容入口。

    阶段 B 起关系任务的排队/并发由 `ollama_material_scheduler` 统一管理，
    不再有进程内 in-flight 标记可清。
    """


def refresh_analysis(material_id: str, source_path: str) -> dict:
    """缺失 / 失败 / 不可用 / hash 过期的分析记录补算（后台异步）。

    状态为 ok 且 hash 未变时生成器自动跳过。``skipped`` 只有在当前正文
    仍为空时才是稳定终态；若快照已补齐正文，必须重新提交，不能让先前的
    「无可用文本」永久留在详情页。
    摘要、标签和实体缺失/失败时按冷却时间受控补算；
    关系记录额外用「正文+实体产物」复合 hash 判断是否过期（实体被修正后必须重算）。
    """
    store = derived_store.DerivedStore.instance()
    summary = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_SUMMARY)
    tracked_kinds = (
        KIND_SUMMARY,
        KIND_TAG_SUGGESTIONS,
        KIND_ENTITY_EXTRACTION,
        KIND_RELATION_EXTRACTION,
    )
    records = {
        kind: store.get_derived_record(OWNER_MATERIAL, material_id, kind)
        for kind in tracked_kinds
    }
    # 旧版本可能在快照落库前写入 skipped。只在发现 skipped 时读取正文，避免
    # 给正常详情轮询增加无意义的存储读取。
    current_input_hash = ""
    if any(record is not None and record.get("status") == "skipped" for record in records.values()):
        try:
            current_text = _input_text(source_path)
            if current_text.strip():
                current_input_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()[:16]
        except _IndexReadError:
            logger.warning("refresh_analysis 读取正文失败，保留 skipped 状态: %s", material_id)

    def skipped_with_new_text(record: dict | None) -> bool:
        return bool(
            current_input_hash
            and record is not None
            and record.get("status") == "skipped"
            and record.get("input_hash") != current_input_hash
        )

    # 先公开 pending，再提交后台任务。否则本次接口虽已成功重投任务，前端仍会
    # 收到 skipped 并停止轮询，直到用户手动刷新页面才看得到新结果。
    recovered_kinds: set[str] = set()
    for kind, record in records.items():
        if not skipped_with_new_text(record):
            continue
        store.set_derived_record(
            OWNER_MATERIAL,
            material_id,
            kind,
            "pending",
            dict(record.get("content") or {}),
            current_input_hash,
            str(record.get("generator") or _generator_name(get_provider().get_local_snapshot())),
        )
        records[kind] = {**record, "status": "pending", "input_hash": current_input_hash}
        recovered_kinds.add(kind)

    summary = records[KIND_SUMMARY]

    summary_scheduled = False
    if summary is None or _retry_due(summary) or KIND_SUMMARY in recovered_kinds:
        summary_scheduled = _submit_derived_task(
            KIND_SUMMARY, _generate_summary_and_entities,
            material_id, source_path, False,
        )

    tasks = {
        KIND_TAG_SUGGESTIONS: _generate_tag_suggestions,
        KIND_ENTITY_EXTRACTION: _generate_entities,
    }
    scheduled: dict[str, bool] = {}
    for kind, fn in tasks.items():
        rec = records[kind]
        if rec is None or _retry_due(rec) or kind in recovered_kinds:
            scheduled[kind] = _submit_derived_task(
                kind, fn, material_id, source_path, False,
            )
    # 关系独立处理：缺失/失败/不可用，或输入 hash 相对当前实体产物已过期
    rel = records[KIND_RELATION_EXTRACTION]
    needs_relation = False
    if rel is None or _retry_due(rel) or KIND_RELATION_EXTRACTION in recovered_kinds:
        needs_relation = True
    else:
        ent_rec = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_ENTITY_EXTRACTION)
        if ent_rec is not None and ent_rec.get("status") == "ok":
            try:
                text = _input_text(source_path)
                if _relation_input_hash(text, ent_rec) != rel.get("input_hash"):
                    needs_relation = True  # 实体产物已迭代，关系为过期 ok → 需重算
            except _IndexReadError:
                # 读取失败时无法计算当前 hash，保守不触发重投（refresh 重投会
                # 在 _generate_relations 里再次 read_error → unavailable 循环）
                logger.warning("refresh_analysis 读取正文失败，跳过关系过期判断: %s", material_id)
    relation_scheduled = False
    if needs_relation:
        relation_scheduled = _submit_relations(material_id, source_path, False)
    return {
        "summaryScheduled": summary_scheduled,
        "tagScheduled": scheduled.get(KIND_TAG_SUGGESTIONS, False),
        "entityScheduled": scheduled.get(KIND_ENTITY_EXTRACTION, False),
        "relationScheduled": relation_scheduled,
    }


def confirm_tag_suggestion(material_id: str, suggestion_id: str) -> None:
    """把候选标签标记为「已确认」（审计已由调用方写入正式标签）。"""
    store = derived_store.DerivedStore.instance()
    rec = store.get_derived_record(OWNER_MATERIAL, material_id, KIND_TAG_SUGGESTIONS)
    if rec is None or rec.get("status") != "ok":
        return
    items = list((rec.get("content") or {}).get("items") or [])
    changed = False
    for it in items:
        if it.get("suggestionId") == suggestion_id and not it.get("confirmed"):
            it["confirmed"] = True
            changed = True
    if changed:
        content = dict(rec.get("content") or {})
        content["items"] = items
        store.set_derived_record(
            OWNER_MATERIAL, material_id, KIND_TAG_SUGGESTIONS, "ok",
            content, rec.get("input_hash") or "",
            rec.get("generator") or _generator_name(get_provider().get_local_snapshot()),
        )


def _derived_items_view(material_id: str, kind: str) -> dict:
    """返回某派生记录（标签候选 / 实体）的公开视图。"""
    rec = derived_store.DerivedStore.instance().get_derived_record(
        OWNER_MATERIAL, material_id, kind
    )
    if rec is None:
        return {"status": "pending", "items": [], "source": None, "generatedAt": None}
    generated_at = None
    if rec.get("updated_at"):
        generated_at = datetime.fromtimestamp(rec["updated_at"], tz=timezone.utc).isoformat()
    return {
        "status": rec.get("status", "pending"),
        "items": (rec.get("content") or {}).get("items", []),
        "source": (rec.get("content") or {}).get("source"),
        "diagnostic": (rec.get("content") or {}).get("diagnostic"),
        "errorCode": (rec.get("content") or {}).get("errorCode"),
        "generatedAt": generated_at,
    }


def tag_suggestions_of(material_id: str) -> dict:
    return _derived_items_view(material_id, KIND_TAG_SUGGESTIONS)


def entities_of(material_id: str) -> dict:
    return _derived_items_view(material_id, KIND_ENTITY_EXTRACTION)


def analysis_of(material_id: str) -> dict:
    """聚合摘要 / 标签候选 / 实体 / 关系三元组及其状态（详情页「分析」区与接口共用）。"""
    return {
        "summary": summary_of(material_id),
        "tagSuggestions": tag_suggestions_of(material_id),
        "entities": entities_of(material_id),
        "relations": relations_of(material_id),
    }


def shutdown_pool() -> None:
    """进程退出时停止统一 Ollama 调度器：丢弃排队任务、不阻塞在运行中的 LLM 调用。"""
    _ollama_scheduler.stop()
