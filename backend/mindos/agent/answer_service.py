"""Agent 问答门面（AG-03）。

只读复用既有 MindOS 问答能力（mindos/qa.py::answer_question：混合检索证据 +
本机/外部模型生成），不复制 RAG 排序、模型调用或纠错逻辑。本模块负责：

- 参数校验（question 2–500 字；options 强类型，拒绝未知字段）；
- options.sourceIds 作为检索范围前置限定（服务端只在该范围内检索）；
- options.maxEvidence 服务端执行证据数量上限（≤ qa.MAX_EVIDENCE）；
- options.includeEvidence=false 时隐藏完整片段，但保留 citation 元数据 /
  evidenceRef / locator；
- citations 使用 QA 保留的真实命中 chunk（_chunkKey / _sourcePath）签发精确
  evidenceRef，并返回 locator；
- qa 层 HTTPException → Agent 统一错误契约（503 模型不可用 / 504 超时，均不泄露
  模型名称、API 地址或内部异常）。

响应不含 source_path / chunk_id / 内部模型配置。
"""
from __future__ import annotations

from fastapi import HTTPException

from .. import qa
from . import evidence
from .auth import AgentPrincipal
from .errors import AgentError

ANSWER_QUESTION_CHARS_MIN = 2
ANSWER_QUESTION_CHARS_MAX = 500
SOURCE_IDS_MAX = 20


def _map_qa_error(exc: HTTPException) -> AgentError:
    """qa 层业务异常 → Agent 统一错误契约；模型侧故障保留 503/504 语义但不泄露细节。"""
    status = exc.status_code
    if status == 400:
        return AgentError(400, "VALIDATION_ERROR", str(exc.detail))
    if status == 429:
        return AgentError(429, "RATE_LIMITED", "问答请求过于频繁，请稍后重试", retryable=True)
    if status == 503:
        return AgentError(503, "SERVICE_UNAVAILABLE", "问答模型暂时不可用，请稍后重试", retryable=True)
    if status == 504:
        return AgentError(504, "GATEWAY_TIMEOUT", "问答模型响应超时，请稍后重试", retryable=True)
    # 其它服务端故障：统一 500/INTERNAL_ERROR（可重试），不返回模型/内部异常信息。
    return AgentError(500, "INTERNAL_ERROR", "问答服务暂时不可用，请稍后重试", retryable=True)


def _sign_citation_evidence(citation: dict, client_id: str) -> str | None:
    """为引用签发证据句柄（AG-02 体系）。

    材料引用必须携带真实命中 chunk_key（精确句柄）才签发；缺失 chunk_key 或
    source_path 时不签发（返回 None），绝不回退为按 source_path 取首个分块——
    否则可能展开非原始命中片段，无法可靠复核。知识卡片引用走卡片正文展开，恒可签发。
    """
    source_type = str(citation.get("sourceType") or "")
    title = str(citation.get("title") or "")
    if source_type == "material":
        material_id = str(citation.get("materialId") or "")
        chunk_key = citation.get("_chunkKey")
        source_path = citation.get("_sourcePath")
        if not material_id or not chunk_key or not source_path:
            return None
        return evidence.sign_evidence_ref(
            client_id=client_id,
            source_type="material",
            source_id=material_id,
            chunk_key=str(chunk_key),
            source_path=str(source_path),
            title=title,
        )
    if source_type == "knowledge":
        knowledge_id = str(citation.get("knowledgeId") or "")
        if not knowledge_id:
            return None
        return evidence.sign_evidence_ref(
            client_id=client_id,
            source_type="knowledge",
            source_id=knowledge_id,
            chunk_key=None,
            source_path=None,
            title=title,
        )
    return None


def _project_citation(
    citation: dict, client_id: str, *, include_evidence: bool
) -> dict:
    source_type = str(citation.get("sourceType") or "")
    source_id = str(citation.get("materialId") or citation.get("knowledgeId") or "")
    snippet = str(citation.get("snippet") or "")
    return {
        "citationId": str(citation.get("citationId") or ""),
        "sourceType": source_type,
        "id": source_id,
        "title": str(citation.get("title") or ""),
        "snippet": snippet if include_evidence else "",
        "evidenceRef": _sign_citation_evidence(citation, client_id),
        # QA 保留的真实定位；无法确认时为 None（不伪造页码/时间）。
        "locator": citation.get("locator") if isinstance(citation.get("locator"), dict) else None,
    }


def answer(req, principal: AgentPrincipal) -> dict:
    """执行带引用的 Agent 问答。

    复用 qa.answer_question 的完整证据与模型管线；options 作为强类型范围/预算
    控制传入。meta 不返回内部模型名称，只返回检索统计。
    """
    question = str(req.question or "").strip()
    if len(question) < ANSWER_QUESTION_CHARS_MIN or len(question) > ANSWER_QUESTION_CHARS_MAX:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"question 长度必须在 {ANSWER_QUESTION_CHARS_MIN}–{ANSWER_QUESTION_CHARS_MAX} 字之间",
        )
    options = req.options
    source_ids = None
    if options is not None and options.sourceIds:
        source_ids = set(options.sourceIds)
        if len(source_ids) > SOURCE_IDS_MAX:
            raise AgentError(400, "VALIDATION_ERROR", f"sourceIds 最多 {SOURCE_IDS_MAX} 个")
    max_evidence = options.maxEvidence if options is not None else None
    if max_evidence is not None and not (1 <= max_evidence <= qa.MAX_EVIDENCE):
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"maxEvidence 必须在 1–{qa.MAX_EVIDENCE} 之间",
        )
    try:
        result = qa.answer_question(
            qa.QaRequest(question=question),
            source_ids=source_ids,
            limit=max_evidence or qa.MAX_EVIDENCE,
            include_internal_meta=True,
        )
    except HTTPException as exc:
        raise _map_qa_error(exc) from exc
    include_evidence = options.includeEvidence if options is not None else True
    meta = result.get("meta") or {}
    return {
        "status": str(result.get("status") or "INSUFFICIENT_EVIDENCE"),
        "question": str(result.get("question") or question),
        "answer": str(result.get("answer") or ""),
        "citations": [
            _project_citation(citation, principal.client_id, include_evidence=include_evidence)
            for citation in result.get("citations") or []
        ],
        "correctionNotices": list(result.get("correctionNotices") or []),
        "meta": {
            "retrievedCount": int(meta.get("retrievedCount") or 0),
            "usedEvidenceCount": int(meta.get("usedEvidenceCount") or 0),
        },
    }
