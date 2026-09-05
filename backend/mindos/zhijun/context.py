"""每轮对话的上下文组装与隐私过滤。

顺序与字符预算（超出从尾部丢）：人格 → 章程 → 已确认理解 → 未确认印象 → 被纠正块 → 资料片段 → 近期轮次。
本地通道用「简版上下文」（预算更小、默认不带资料片段）；外部通道只送 public/private 的理解，
sensitive/restricted 永不外发。每轮组装结果同时产出 ``provenance``（给前端）与回执字段（落库）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ..stores.ontology_store import LAYER_TITLES, SECTION_TITLES, OntologyStore
from . import persona
from .context_lookup import strip_citation_markers
from .provider import ChatProvider

logger = logging.getLogger(__name__)

BUDGETS = {
    "external": {"charter": 800, "confirmed": 1800, "working": 600, "retracted": 300, "materials": 2400, "recent": 2400, "total": 24000},
    "local": {"charter": 500, "confirmed": 900, "working": 400, "retracted": 200, "materials": 0, "recent": 1500, "total": 6000},
}
RECENT_TURNS = 12
EXTERNAL_PRIVACY_ALLOWED = ("public", "private")


@dataclass
class Assembled:
    system: str
    messages: list[dict]
    provenance: dict
    prompt_chars: int
    debug: dict = field(default_factory=dict)
    confirmed_ids: list[str] = field(default_factory=list)
    working_ids: list[str] = field(default_factory=list)
    material_chunk_keys: list[str] = field(default_factory=list)
    retracted_count: int = 0


def _brief(claim: dict) -> dict:
    return {"id": claim["id"], "content": claim["content"], "section": claim["section"], "layer": claim["layer"]}


def _claim_line(claim: dict) -> str:
    from .alignment import description
    section = SECTION_TITLES.get(claim["section"], claim["section"])
    layer = LAYER_TITLES.get(claim["layer"], claim["layer"])
    obj = f"（涉及：{claim['objectName']}）" if claim.get("objectName") else ""
    scope = "（只适用于当时那件事）" if claim.get("scope") == "context_only" else ""
    details = claim.get("contextual") or {}
    situation = (" 情境：" + details["situation"][:250]) if details.get("situation") else ""
    exceptions = (" 例外/未知：" + details["exceptions"][:150]) if details.get("exceptions") else ""
    frame = {"current": "阶段状态，不是永久特征", "aspirational": "愿望，不表示已经做到",
             "long_term": "用户认同的倾向，不是行为预测已获证实", "context_only": "限这次情境"}.get(details.get("framing"), "")
    return f"- [{section}] {claim['content']}{obj}（{layer}）{scope}{description(claim)}{situation}{exceptions} {frame}".rstrip()


def _fit(lines: list[str], budget: int) -> list[str]:
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return kept


def _material_evidence(user_text: str, limit: int = 4, device_scope: str = "global") -> list:
    if os.environ.get("ZHIJUN_MATERIAL_EVIDENCE", "1").strip().lower() in ("0", "false", "no"):
        return []
    try:
        from .. import qa as _qa  # 延迟导入：拉起 embedder / vector_store，缺模型时直接跳过

        return list(_qa.build_evidence(user_text, limit=limit, device_scope=device_scope) or [])
    except Exception as exc:  # noqa: BLE001 - 资料证据是加速器，不是门禁
        logger.debug("资料片段检索不可用，跳过：%s", type(exc).__name__)
        return []


def _render_history(messages: list[dict], budget: int) -> list[dict]:
    """把近期消息渲染为模型消息；系统备注折叠为用户侧「（备注）」，并合并连续同角色。"""
    rendered: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if role == "assistant":
            content = strip_citation_markers(content)
        if not content:
            continue
        if role == "system":
            role = "user"
            content = f"（备注：{content}）"
        if role not in ("user", "assistant"):
            continue
        if rendered and rendered[-1]["role"] == role:
            rendered[-1]["content"] = rendered[-1]["content"] + "\n" + content
        else:
            rendered.append({"role": role, "content": content})
    # 从最旧的开始丢，直到进入预算；但永远保留最后一条（当前用户消息）。
    while len(rendered) > 1 and sum(len(m["content"]) for m in rendered) > budget:
        rendered.pop(0)
    if rendered and rendered[0]["role"] == "assistant":
        first_kind = str(((messages[0] if messages else {}).get("meta") or {}).get("kind") or "")
        opener = "（回访由知君先开口）" if first_kind == "review_open" else "（此前的对话已省略）"
        rendered.insert(0, {"role": "user", "content": opener})
    return rendered


def assemble(
    *,
    conversation: dict,
    user_text: str,
    depth: str,
    provider: ChatProvider,
    ontology: OntologyStore,
    recent_messages: list[dict],
    user_turns: int,
    summary: dict | None = None,
    charter: dict | None = None,
    turn_mode: str = "chat",
    decision: dict | None = None,
    past_decisions: list[dict] | None = None,
    material_refs: list[dict] | None = None,
    device_scope: str = "global",
    conversation_store=None,
) -> Assembled:
    channel = "external" if provider.external else "local"
    budget = dict(BUDGETS[channel])
    attachment_text, attachment_sources = "", []
    if material_refs:
        from ..chat_imports import attachment_context
        attachment_text, attachment_sources = attachment_context(material_refs, device_scope, user_text, external=provider.external)
        budget["total"] -= len(attachment_text)
    mode = conversation.get("mode") or "chat"
    outcome_recorded = bool(decision and decision.get("status") in ("outcome_recorded", "reviewed"))

    parts: list[str] = [persona.PERSONA_CORE]
    if mode == "onboarding":
        parts.append(persona.onboarding_instruction(user_turns))
    if mode == "review":
        parts.append(persona.review_instruction(decision, outcome_recorded))
    if turn_mode == "deliberate":
        parts.append(persona.DELIBERATE_INSTRUCTION)
    if depth == "deep":
        parts.append(persona.DEEP_INSTRUCTION)

    charter_text = persona.charter_block(charter, budget["charter"])
    if charter_text:
        parts.append(charter_text)

    confirmed = ontology.search_claims(user_text, k=12, trust_states=("confirmed",), include_hidden=True)
    # Only relevant principles are eligible; unrelated high-affinity anchors
    # must not displace facts needed to answer the current question.
    anchor_ids: list[str] = []
    if turn_mode == "deliberate" or mode == "review" or depth == "deep":
        seen = {c["id"] for c in confirmed}
        anchors = ontology.search_claims(user_text, k=6, trust_states=("confirmed",), sections=("principles", "ways"), include_hidden=True, min_score=0.08)
        anchor_ids = [c["id"] for c in anchors]
        confirmed = confirmed + [c for c in anchors if c["id"] not in seen]
    working = ontology.search_claims(user_text, k=6, trust_states=("working",), include_hidden=False)
    retracted = ontology.search_claims(
        user_text, k=5, trust_states=("retracted", "superseded"), include_hidden=True, min_score=0.35
    )
    if provider.external:
        from .source_policy import SourcePolicy
        policy = SourcePolicy(ontology, conversation_store)
        confirmed = [c for c in confirmed if not policy.claim_local(c)]
        working = [c for c in working if not policy.claim_local(c)]
        retracted = [c for c in retracted if not policy.claim_local(c)]

    from . import alignment
    from ..stores.conversation_store import ConversationStore
    # The caller passes its store explicitly in tests and isolated runtimes.
    convs = conversation_store or ConversationStore.instance()
    confirmed = alignment.context_claims(confirmed, provider, ontology, convs, device_scope, user_text)
    parts.append(alignment.INSTRUCTION)
    confirmed_lines = _fit([_claim_line(c) for c in confirmed], budget["confirmed"])
    confirmed = confirmed[: len(confirmed_lines)]
    if confirmed_lines:
        parts.append("## 已确认的理解（可作为事实引用；来自用户原话的用" + persona.LABEL_TOLD + "，来自资料的用" + persona.LABEL_MATERIAL + "）\n" + "\n".join(confirmed_lines))

    working_lines = _fit([_claim_line(c) for c in working], budget["working"])
    working = working[: len(working_lines)]
    if working_lines:
        parts.append(
            "## 未确认的印象（只能用" + persona.LABEL_GUESS + "加保留语气提出；最多挑一条与当前话题相关的向用户确认，无关的不要提）\n"
            + "\n".join(working_lines)
        )

    retracted_lines = _fit([f"- {c['content']}" for c in retracted], budget["retracted"])
    retracted = retracted[: len(retracted_lines)]
    if retracted_lines:
        parts.append("## 用户已纠正、不得再复述或暗示的旧理解\n" + "\n".join(retracted_lines))

    materials: list = []
    material_items: list[dict] = []
    if budget["materials"] > 0:
        materials = _material_evidence(user_text, device_scope=device_scope)
        lines: list[str] = []
        for i, ev in enumerate(materials, start=len(attachment_sources) + 1):
            title = getattr(ev, "title", "") or ""
            snippet = (getattr(ev, "snippet", "") or "").strip()
            lines.append(f"[m{i}] 《{title}》：{snippet}")
        lines = _fit(lines, budget["materials"])
        materials = materials[: len(lines)]
        if lines:
            parts.append("## 资料片段（引用时用" + persona.LABEL_MATERIAL + "并标 [m1] 这类编号）\n" + "\n".join(lines))
        material_items = [
            {
                "materialId": getattr(ev, "material_id", None),
                "knowledgeId": getattr(ev, "knowledge_id", None),
                "title": getattr(ev, "title", "") or "",
                "chunkKey": getattr(ev, "chunk_key", None),
                "locator": getattr(ev, "locator", None),
            }
            for ev in materials
        ]

    history_text = persona.past_decisions_block(past_decisions or [], budget=900 if channel == "external" else 400)
    if history_text:
        parts.append(history_text)

    if summary and summary.get("summary"):
        parts.append("## 本次对话此前的摘要\n" + str(summary["summary"])[:600])
    themes_text = persona.themes_block(summary)
    if themes_text:
        parts.append(themes_text)

    system = "\n\n".join(p for p in parts if p)
    messages = _render_history(recent_messages, budget["recent"])
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": user_text})

    prompt_chars = len(system) + sum(len(m["content"]) for m in messages)
    # 总预算兜底：先丢资料片段，再丢未确认印象，最后压缩历史。
    if prompt_chars > budget["total"] and material_items:
        system = system.split("## 资料片段")[0].rstrip()
        material_items, materials = [], []
        prompt_chars = len(system) + sum(len(m["content"]) for m in messages)
    if prompt_chars > budget["total"] and working:
        head, _, tail = system.partition("## 未确认的印象")
        rest = tail.split("\n\n", 1)
        system = (head.rstrip() + ("\n\n" + rest[1] if len(rest) > 1 else "")).strip()
        working = []
        prompt_chars = len(system) + sum(len(m["content"]) for m in messages)
    while prompt_chars > budget["total"] and len(messages) > 1:
        messages.pop(0)
        prompt_chars = len(system) + sum(len(m["content"]) for m in messages)

    if attachment_sources:
        system += attachment_text
        material_items = attachment_sources + material_items
        prompt_chars = len(system) + sum(len(m["content"]) for m in messages)

    provenance = {
        "localOnlyDerived": False,
        "alignmentSources": [c["alignmentSource"] for c in confirmed if c.get("alignmentSource")],
        "confirmedClaims": [_brief(c) for c in confirmed],
        "workingClaims": [_brief(c) for c in working],
        "materials": material_items,
        "retractedNotices": len(retracted),
        "charterVersion": charter.get("version") if charter else None,
        "promptChars": prompt_chars,
        "channel": channel,
        # 出处显式化：本轮引用的过去判断，以及无论词面是否命中都带上的原则 / 做法锚点（只列最终进入上下文的）。
        "pastDecisions": [
            {"id": d["id"], "title": d.get("title"), "choice": d.get("choice"), "status": d.get("status"), "createdAt": d.get("createdAt")}
            for d in (past_decisions or [])
        ],
        "anchorClaimIds": [cid for cid in anchor_ids if cid in {c["id"] for c in confirmed}],
    }
    if not provider.external:
        from .source_policy import SourcePolicy
        policy = SourcePolicy(ontology, convs)
        provenance["localOnlyDerived"] = any(policy.claim_local(c) for c in confirmed + working + retracted)
    debug = {
        "mode": mode,
        "turnMode": turn_mode,
        "depth": depth,
        "userText": user_text,
        "userTexts": [m.get("content") or "" for m in recent_messages if m.get("role") == "user"],
        "userTurns": user_turns,
        "decision": {k: decision.get(k) for k in ("id", "title", "choice", "confidence", "expectedOutcome", "status")} if decision else None,
        "outcomeRecorded": outcome_recorded,
        "pastDecisions": [{"id": d["id"], "title": d.get("title"), "choice": d.get("choice"), "confidence": d.get("confidence")} for d in (past_decisions or [])],
        "confirmedClaims": [c["content"] for c in confirmed],
        "workingClaims": [c["content"] for c in working],
        "retractedNotices": len(retracted),
    }
    return Assembled(
        system=system,
        messages=messages,
        provenance=provenance,
        prompt_chars=prompt_chars,
        debug=debug,
        confirmed_ids=[c["id"] for c in confirmed],
        working_ids=[c["id"] for c in working],
        material_chunk_keys=[str(m.get("chunkKey")) for m in material_items if m.get("chunkKey")],
        retracted_count=len(retracted),
    )
