"""商量模式：从对话整理「判断草稿」，用户一键确认后写入判断簿（growth_decisions）。

硬规则（D3 / D6 的技术落点）：
- ``leaning / choice / rationale / expectedOutcome`` 只能来自用户原话：草稿抽取结果必须能在用户消息里找到对应文本，否则置空。
- ``confidence`` 只接受用户给出的 0–100 整数；模型不得替用户估把握。
- ``zhijunView`` 是知君自己的看法，只进草稿的看法栏，永远不写进 choice / rationale。
- 确认时用户可亲自填写或明确选用候选（仍可修改）；候选不当成用户原话，写入仍需用户确认。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from ..stores.conversation_store import ConversationError, ConversationNotFoundError, ConversationStore
from ..stores.ontology_store import normalize_text
from .context_lookup import strip_citation_markers
from .provider import ChatProvider, ChatRequest

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_DAYS = 14
USER_ONLY_FIELDS = ("leaning", "choice", "rationale", "expectedOutcome")
MAX_OPTIONS = 30

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "context": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "leaning": {"type": ["string", "null"]},
        "choice": {"type": ["string", "null"]},
        "rationale": {"type": ["string", "null"]},
        "confidence": {"type": ["integer", "null"]},
        "expectedOutcome": {"type": ["string", "null"]},
        "reviewAt": {"type": ["string", "null"]},
        "keyQuestion": {"type": ["string", "null"]},
        "zhijunView": {"type": ["string", "null"]},
        "userQuotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "context", "options", "leaning", "choice", "rationale", "confidence", "expectedOutcome", "reviewAt", "keyQuestion", "zhijunView", "userQuotes"],
    "additionalProperties": False,
}

_DRAFT_SYSTEM = """你是知君的判断记录助手。从这段对话里整理出用户「正在考虑的判断」草稿，输出 JSON。
规则：
- title ≤ 30 字；context 概括背景与约束（≤ 200 字）；options 列出对话里摆出的选项（每项 ≤ 40 字，没有就空数组）。
- leaning（倾向）/ choice（最终选择）/ rationale（理由）/ expectedOutcome（预期结果）只能来自用户原话，必须逐字出自用户消息；用户没说就填 null。不要替用户补。
- confidence 是用户自己说的把握，0–100 的整数（「七成」→ 70）；用户没说就 null。
- reviewAt：用户说了回访时间就写 ISO 日期，否则 null。
- keyQuestion：知君提出的那个关键问题。zhijunView：知君自己的看法（来自知君的话，不要混入用户的话）。
- userQuotes：你依据的用户原句，逐字。
只输出 JSON。"""

_CONFIDENCE_RE = re.compile(r"(\d{1,3})\s*%|([一二三四五六七八九十])成")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def default_fields() -> dict:
    return {
        "title": "",
        "context": "",
        "options": [],
        "leaning": None,
        "choice": None,
        "rationale": None,
        "confidence": None,
        "expectedOutcome": None,
        "reviewAt": None,
        "keyQuestion": None,
        "zhijunView": None,
        "relatedEntityIds": [],
        "relatedDecisionIds": [],
        "evidenceRefs": [],
        "userQuotes": [],
        "assistedFields": [],
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_review_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_confidence(value) -> int | None:
    """接受整数、'70%'、'七成' 这类表达；越界或不可解析返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if 0 <= number <= 100 else None
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        return number if 0 <= number <= 100 else None
    match = _CONFIDENCE_RE.search(text)
    if not match:
        return None
    if match.group(1):
        number = int(match.group(1))
        return number if 0 <= number <= 100 else None
    return _CN_NUM.get(match.group(2), 0) * 10 or None


def _in_user_text(value: str | None, user_texts: list[str]) -> bool:
    if not value:
        return False
    norm = normalize_text(value)
    if not norm:
        return False
    return any(norm in normalize_text(text) for text in user_texts)


def build_draft_request(user_texts: list[str], assistant_texts: list[str], prev_fields: dict | None) -> ChatRequest:
    lines: list[str] = []
    if prev_fields and prev_fields.get("title"):
        lines.append("上一版草稿：" + json.dumps({k: prev_fields.get(k) for k in ("title", "options", "leaning", "choice", "confidence")}, ensure_ascii=False))
    lines.append("对话记录（只有用户的话可作为 choice/rationale/confidence/expectedOutcome 的来源）：")
    for i, text in enumerate(user_texts[-8:], start=1):
        lines.append(f"用户{i}：{text.strip()}")
    assistant_text = strip_citation_markers(assistant_texts[-1]).strip() if assistant_texts else ""
    if assistant_text:
        lines.append(f"知君最近一句：{assistant_text[:400]}")
    return ChatRequest(
        system=_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        max_tokens=800,
        temperature=0.0,
        json_schema=DRAFT_SCHEMA,
        effort="low",
        debug={"task": "decision_draft", "userTexts": list(user_texts), "assistantText": assistant_text},
    )


def validate_draft(raw: dict, *, user_texts: list[str], prev_fields: dict | None) -> tuple[dict, list[str]]:
    prev = {**default_fields(), **(prev_fields or {})}
    fields = default_fields()
    raw = raw if isinstance(raw, dict) else {}

    title = str(raw.get("title") or "").strip().replace("\n", " ")[:60]
    fields["title"] = title or prev.get("title") or (user_texts[0].strip().replace("\n", " ")[:30] if user_texts else "")
    context = str(raw.get("context") or "").strip()[:500]
    fields["context"] = context or prev.get("context") or "；".join(t.strip() for t in user_texts)[:300]

    options: list[str] = []
    for item in raw.get("options") or []:
        text = str(item or "").strip().replace("\n", " ")[:40]
        if text and text not in options:
            options.append(text)
    fields["options"] = options[:MAX_OPTIONS] or list(prev.get("options") or [])

    for key in USER_ONLY_FIELDS:
        value = raw.get(key)
        value = str(value).strip() if value else None
        fields[key] = value[:2000] if value and _in_user_text(value, user_texts) else prev.get(key)

    confidence = parse_confidence(raw.get("confidence"))
    if confidence is not None:
        # 数字本身也必须出现在用户话里（防止模型替用户估把握）。
        if not any(str(confidence) in t or f"{confidence // 10}成" in t or _CN_NUM_REVERSE.get(confidence // 10, "") + "成" in t for t in user_texts):
            confidence = None
    fields["confidence"] = confidence if confidence is not None else prev.get("confidence")

    review_at = parse_review_at(raw.get("reviewAt"))
    fields["reviewAt"] = _iso(review_at) if review_at else prev.get("reviewAt")

    for key in ("keyQuestion", "zhijunView"):
        value = raw.get(key)
        value = str(value).strip()[:300] if value else None
        fields[key] = value or prev.get(key)

    quotes = [str(q).strip() for q in (raw.get("userQuotes") or []) if str(q).strip()]
    fields["userQuotes"] = [q for q in quotes if _in_user_text(q, user_texts)][:10]
    fields["relatedEntityIds"] = list(prev.get("relatedEntityIds") or [])
    fields["relatedDecisionIds"] = list(prev.get("relatedDecisionIds") or [])
    fields["evidenceRefs"] = list(prev.get("evidenceRefs") or [])

    changed = [key for key in fields if fields[key] != prev.get(key)]
    return fields, changed


_CN_NUM_REVERSE = {v: k for k, v in _CN_NUM.items()}


def run_draft(*, provider: ChatProvider, conv_store: ConversationStore, conversation_id: str, message_id: str | None) -> tuple[dict, list[str]]:
    messages = conv_store.list_messages(conversation_id)
    user_texts = [m["content"] for m in messages if m["role"] == "user" and m["content"].strip()]
    assistant_texts = [m["content"] for m in messages if m["role"] == "assistant" and m["content"].strip()]
    prev = conv_store.get_draft(conversation_id)
    prev_fields = prev["fields"] if prev and prev["status"] == "draft" else None
    from .routing import GuardedProvider
    if isinstance(provider, GuardedProvider):
        selected = [m for m in messages if m["role"] == "user" and m["content"].strip()][-8:]
        selected += [m for m in messages if m["role"] == "assistant" and m["content"].strip()][-1:]
        provider.refs = [provider.router.ref("message", m["id"]) for m in selected]
        if prev_fields:
            provider.refs.append(provider.router.ref("draft", conversation_id))
    raw = provider.complete_json(build_draft_request(user_texts, assistant_texts, prev_fields))
    fields, changed = validate_draft(raw, user_texts=user_texts, prev_fields=prev_fields)
    if isinstance(provider, GuardedProvider):
        sources = [s["ref"] for s in provider.last_preview["sources"] if s["key"] != "draft:" + conversation_id]
        fields["evidenceRefs"].append(json.dumps({"kind": "routing", "routingSources": sources}, ensure_ascii=False))
        fields["charterBasis"] = provider.last_preview.get("charterBasis")
    else:
        fields["charterBasis"] = None
    if message_id:
        ref = json.dumps({"kind": "message", "conversationId": conversation_id, "messageId": message_id}, ensure_ascii=False)
        if ref not in fields["evidenceRefs"]:
            fields["evidenceRefs"] = (fields["evidenceRefs"] + [ref])[-20:]
    # 相似的历史判断：只引用用户自己记下的判断 id，进证据引用；不加评价。
    try:
        from .history import similar_decisions
        from .alignment import scope_for
        from .charter_policy import record_in_scope

        scope = scope_for(conversation_id, conv_store)
        related = [d for d in similar_decisions("\n".join(user_texts), k=12)
                   if record_in_scope(d, conv_store, scope)][:3]
        fields["relatedDecisionIds"] = [d["id"] for d in related]
        for d in related:
            ref = json.dumps({"kind": "decision", "id": d["id"]}, ensure_ascii=False)
            if ref not in fields["evidenceRefs"]:
                fields["evidenceRefs"] = (fields["evidenceRefs"] + [ref])[-20:]
    except Exception:  # noqa: BLE001
        fields.setdefault("relatedDecisionIds", [])
    if isinstance(provider, GuardedProvider):
        provider.assert_current()
    draft = conv_store.upsert_draft(conversation_id, fields, message_id=message_id)
    return draft, changed


def run_onboarding_draft(
    *, provider: ChatProvider, conv_store: ConversationStore, conversation_id: str, message_id: str
) -> tuple[dict, list[str]]:
    """只用建档第 4 问的答案建立判断草稿，避免姓名与项目答案混入标题。"""
    message = conv_store.get_message(message_id)
    if message is None or message.get("role") != "user":
        raise ConversationNotFoundError("判断答案不存在")
    history = conv_store.list_messages(conversation_id)
    previous = [item["content"] for item in history if item["role"] == "assistant" and item["seq"] < message["seq"]]
    raw = provider.complete_json(build_draft_request([message["content"]], previous[-1:], None))
    fields, changed = validate_draft(raw, user_texts=[message["content"]], prev_fields=None)
    ref = json.dumps({"kind": "message", "conversationId": conversation_id, "messageId": message_id}, ensure_ascii=False)
    fields["evidenceRefs"] = [ref]
    from .routing import GuardedProvider
    fields["charterBasis"] = None
    if isinstance(provider, GuardedProvider):
        provider.assert_current()
        fields["charterBasis"] = provider.last_preview.get("charterBasis")
        fields["evidenceRefs"].append(json.dumps({"kind": "routing", "routingSources": [s["ref"] for s in provider.last_preview["sources"]]}, ensure_ascii=False))
    draft = conv_store.upsert_draft(conversation_id, fields, message_id=message_id)
    return draft, changed


def confirm_draft(conversation_id: str, overrides: dict, *, conv_store: ConversationStore | None = None) -> dict:
    conv_store = conv_store or ConversationStore.instance()
    draft = conv_store.get_draft(conversation_id)
    if draft is None:
        raise ConversationNotFoundError("这段对话还没有判断草稿")
    if draft["status"] != "draft":
        raise ConversationError(f"草稿已{ '确认' if draft['status'] == 'confirmed' else '丢弃' }，请重新商量")
    fields = {**default_fields(), **draft["fields"]}
    for key, value in (overrides or {}).items():
        if value is not None and key in fields:
            fields[key] = value
    confidence = parse_confidence(fields.get("confidence"))
    missing = [k for k, v in (("choice", fields.get("choice")), ("rationale", fields.get("rationale")), ("expectedOutcome", fields.get("expectedOutcome"))) if not (v or "").strip()]
    if confidence is None:
        missing.append("confidence")
    if missing:
        raise ConversationError("确认前需要你填写或选择：" + "、".join({"choice": "选择", "rationale": "理由", "confidence": "把握（0–100）", "expectedOutcome": "预期结果"}[m] for m in missing))
    review_at = parse_review_at(fields.get("reviewAt")) or (_now() + timedelta(days=DEFAULT_REVIEW_DAYS))
    options = [str(o).strip() for o in (fields.get("options") or []) if str(o).strip()] or [str(fields["choice"]).strip()]
    if str(fields["choice"]).strip() not in options:
        options.append(str(fields["choice"]).strip())

    from .. import growth as growth_api  # 复用现有校验与章程绑定

    from ..chat_imports import protected_conversation
    from ..stores.ontology_store import OntologyStore
    from . import alignment
    from .charter_artifacts import recall_lineage
    receipt = recall_lineage(OntologyStore.instance(), conversation_id, "decision_suggestions")
    if receipt:
        fields["evidenceRefs"].append(json.dumps({"kind": "helper_lineage", **receipt}, ensure_ascii=False))
    # Confirming wording is not consent to export profile/file-derived content.
    if protected_conversation(conversation_id, conv_store) or alignment.protected(conversation_id, conv_store, OntologyStore.instance()):
        marker = json.dumps({"kind": "local_only_decision", "conversationId": conversation_id})
        if marker not in fields["evidenceRefs"]:
            fields["evidenceRefs"] = [*fields["evidenceRefs"], marker]

    try:
        req = growth_api.DecisionCreate(
            title=str(fields.get("title") or "").strip() or str(fields["choice"]).strip()[:60],
            context=str(fields.get("context") or "").strip() or "（对话中商量）",
            options=options[:MAX_OPTIONS],
            choice=str(fields["choice"]).strip(),
            rationale=str(fields["rationale"]).strip(),
            confidence=confidence,
            expectedOutcome=str(fields["expectedOutcome"]).strip(),
            reviewAt=review_at,
            relatedEntityIds=list(fields.get("relatedEntityIds") or []),
            evidenceRefs=list(fields.get("evidenceRefs") or []),
        )
    except ValidationError as exc:
        raise ConversationError("判断字段不合法：" + "；".join(str(e.get("msg")) for e in exc.errors())[:300]) from exc
    from .alignment import scope_for
    # The decision retains the version used to generate this draft, even if a
    # newer charter was published while the user was reviewing it.
    decision = growth_api.create_decision(req, charter_basis=fields.get("charterBasis"),
                                         scope=scope_for(conversation_id, conv_store))
    fields["confidence"] = confidence
    fields["reviewAt"] = _iso(review_at)
    fields["options"] = options
    updated = conv_store.set_draft_status(draft["id"], "confirmed", decision_id=decision["id"], fields=fields)
    try:
        conv_store.append_message(
            conversation_id,
            "system",
            f"你记下了一个判断：{decision['title']}（选了「{decision['choice']}」，把握 {decision['confidence']}%）。{review_at.date().isoformat()} 知君会来回访。",
            meta={"kind": "decision_confirmed", "decisionId": decision["id"], "draftId": draft["id"], "reviewAt": _iso(review_at),
                  "assistedFields": fields.get("assistedFields", [])},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("追加判断备注失败：%s", type(exc).__name__)
    return {"draft": updated, "decision": decision}


def discard_draft(conversation_id: str, *, conv_store: ConversationStore | None = None) -> dict:
    conv_store = conv_store or ConversationStore.instance()
    draft = conv_store.get_draft(conversation_id)
    if draft is None:
        raise ConversationNotFoundError("这段对话还没有判断草稿")
    if draft["status"] != "draft":
        raise ConversationError("草稿已不是进行中状态")
    return conv_store.set_draft_status(draft["id"], "discarded")  # type: ignore[return-value]
