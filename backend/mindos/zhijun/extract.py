"""从用户原话抽取候选理解（Claim）。

入口规则（D2 的技术安全阀）：
- quote 必须是用户消息的精确子串，否则整条丢弃——「用户说了」≠「模型转述对了」。
- content ≤ 120 字；observed 不允许来自对话；aspirational / hypothesis 永远 working。
- self_declared 需要 quote 含第一人称，或紧接着助手的提问；置信度 ≥ 0.8 时直接 confirmed（trust_origin=utterance）。
- 每轮最多 4 条；不足 8 字或纯提问的消息直接跳过，不调用模型。
- 去重：哈希或词面近似命中活跃理解 → 追加证据 + 刷新重申时间；旧 working 遇到用户再次亲口陈述 → 晋升 confirmed。
- 墓碑抑制：命中被撤回 / 被替代的理解 → 丢弃；用户本人再次陈述例外（新理解 supersedes 墓碑）。
抽取模型 = 产生该轮回复的通道（文本已经发过去了，不产生新的出设备）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..stores.ontology_store import (
    DEFAULT_PREDICATE,
    LAYERS,
    ME_ENTITY_ID,
    PREDICATES,
    SECTIONS,
    OntologyConflictError,
    OntologyError,
    OntologyStore,
    normalize_text,
)
from .provider import ChatProvider, ChatRequest, ProviderError

logger = logging.getLogger(__name__)

MAX_CLAIMS_PER_TURN = 4
# 中文信息密度高：「我是产品经理」6 字已是完整自述；再短的（「好的」「嗯」）不值得调用模型。
MIN_TEXT_CHARS = 6
AUTO_CONFIRM_CONFIDENCE = 0.8
SIMILAR_THRESHOLD = 0.9

_FIRST_PERSON_RE = re.compile(r"我|咱|俺")
_ASPIRATION_RE = re.compile(r"想|希望|打算|目标|要成为|愿|计划")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?;\n]")


def _sentence_around(text: str, quote: str) -> str:
    """引用所在的整句：愿望词常在句首（「我想……，然后能把周末还给家里」），只看片段会误降层。"""
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        if quote and quote in sentence:
            return sentence
    return quote

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "layer": {"type": "string", "enum": ["self_declared", "aspirational", "hypothesis"]},
                    "predicate": {"type": "string"},
                    "subject": {"type": "string"},
                    "object": {"type": ["string", "null"]},
                    "content": {"type": "string"},
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                    "scope_hint": {"type": "string", "enum": ["long_term", "context_only", "unknown"]},
                    "privacy_hint": {"type": "string", "enum": ["private", "sensitive"]},
                    "merge_into": {"type": ["string", "null"]},
                    "why_it_matters": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                },
                "required": ["section", "layer", "predicate", "subject", "object", "content", "quote", "confidence", "scope_hint", "privacy_hint", "merge_into", "why_it_matters", "date"],
                "additionalProperties": False,
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["person", "organization", "project", "place", "topic", "event", "term"]},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "type", "aliases"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims", "entities"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = """你是知君的记忆整理助手。任务：从「用户这一句话」里抽取关于用户本人的、值得长期记住的理解，输出 JSON。

规则：
- 只抽用户亲口说的关于自己的事（self_declared）、用户表达的愿望或目标（aspirational）、以及你从这句话里推测出的模式（hypothesis，谨慎使用）。不要把资料或常识当作用户的事实。
- 每条理解：subject 用 "me" 表示用户本人，或写出具体人名 / 项目名；content 是一句 ≤ 60 字的原子陈述，用第一人称；quote 必须是用户原话里的一段精确文本（一字不改）。
- section 只能是：who（我是谁）、people（我的人）、matters（我的事）、principles（我的原则）、ways（我的做法）、direction（我的方向）。
- predicate 按分区选：who: is/has_trait/background/role；people: knows/works_with/relationship/attitude_toward；matters: working_on/committed_to/happened/owns；principles: holds_principle/boundary；ways: prefers/tends_to/decides_by；direction: wants_to/goal/avoids。
- 最多 4 条；没有值得记的就返回空数组。不要编造。
- 如果某条与「已有的理解」说的是同一件事（换个说法、补一个细节），填 "merge_into": 那条的 id，不要新建。
- 每条加 "why_it_matters"：一句话说明这条对以后帮他做判断有什么用；说不出用处的不要抽。
- hypothesis（我推测的）只在同一模式在用户话里至少出现两次、或与已有理解形成明显对照时才输出，并在 content 里写明依据（「你两次提到……，可能……」）。
- 用户明确说「想 / 希望 / 打算 / 目标是」的，是 aspirational（你想成为的），不要降成 hypothesis。
- entities 只收具体的人、组织、项目、地点、话题；不要把用户本人（他让你用的称呼）、「小组」「团队」「客户」这类泛指或临时团体当实体；公司 / 团队类用 organization。
- 用户提到期限（「三个月内」「下周五前」「年底」）时，把它换算成 ISO 日期填在 "date"（今天的日期在输入里给出）；没有就 null。
- entities 列出这句话里出现的人 / 组织 / 项目 / 地点名称。
输出格式：{"claims":[{"section":"...","layer":"...","predicate":"...","subject":"me","object":null,"content":"...","quote":"...","confidence":0.0-1.0,"scope_hint":"long_term|context_only|unknown","privacy_hint":"private|sensitive","merge_into":null,"why_it_matters":"...","date":null}],"entities":[{"name":"...","type":"person","aliases":[]}]}
只输出 JSON。"""


@dataclass
class ValidatedClaim:
    section: str
    layer: str
    predicate: str
    subject: str
    object: str | None
    content: str
    quote: str
    confidence: float
    scope: str
    privacy_level: str
    downgraded: bool = False
    merge_into: str | None = None
    why_it_matters: str = ""
    valid_to: str | None = None


def should_extract(user_text: str) -> tuple[bool, str]:
    text = (user_text or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        return False, "too_short"
    if not _FIRST_PERSON_RE.search(text) and text.rstrip().endswith(("？", "?")):
        return False, "pure_question"
    return True, "ok"


def build_request(
    user_text: str,
    prev_assistant: str | None,
    known_entities: list[str],
    *,
    existing_claims: list[dict] | None = None,
    debug: dict | None = None,
) -> ChatRequest:
    from datetime import date

    context_lines = [f"今天的日期：{date.today().isoformat()}"]
    if prev_assistant:
        context_lines.append(f"知君上一句话：{prev_assistant.strip()[:300]}")
    if known_entities:
        context_lines.append("本次对话里已出现的名字：" + "、".join(known_entities[:20]))
    if existing_claims:
        context_lines.append("已有的理解（供去重与合并；同一件事请填 merge_into）：")
        for claim in existing_claims[:20]:
            context_lines.append(f"- {claim['id']}：{claim['content'][:60]}")
    context_lines.append(f"用户这一句话：{user_text.strip()}")
    return ChatRequest(
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(context_lines)}],
        max_tokens=800,
        temperature=0.0,
        json_schema=EXTRACTION_SCHEMA,
        effort="low",
        debug={**(debug or {}), "userText": user_text},
    )


def _quote_ok(quote: str, user_text: str) -> bool:
    quote = (quote or "").strip()
    if not quote or len(quote) > 300:
        return False
    if quote in user_text:
        return True
    return normalize_text(quote) != "" and normalize_text(quote) in normalize_text(user_text)


def _parse_date(value) -> str | None:
    """只接受 ISO 日期（模型已按输入里的今天换算）；返回 UTC Z 时间戳（当天 23:59）。"""
    if not value:
        return None
    from datetime import datetime, timezone

    text = str(value).strip()[:10]
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(hour=23, minute=59, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


_SELF_NAME_RE = re.compile(r"(?:叫我|称呼我|喊我|我叫|我是)\s*([\u4e00-\u9fa5A-Za-z·]{1,8}?)(?:就行|就好|吧|即可|，|,|。|；|;|\s|$)")
_GENERIC_GROUP_RE = re.compile(r"^\d*\s*(?:人|个)?\s*(?:小组|团队|客户|同事|朋友|家人|员工|用户|公司)$")


def filter_entities(entities: list, *, user_text: str) -> list:
    """硬规则：用户本人的称呼（「叫我阿远」）和泛指团体（「5人小组」）不成为实体，模型提示词说了也常不听。"""
    self_names = {normalize_text(m.group(1)) for m in _SELF_NAME_RE.finditer(user_text or "") if m.group(1)}
    kept = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name or normalize_text(name) in self_names or _GENERIC_GROUP_RE.match(name):
            continue
        kept.append(ent)
    return kept


def validate(raw: dict, *, user_text: str, prev_assistant: str | None, existing_ids: set[str] | None = None) -> list[ValidatedClaim]:
    items = raw.get("claims") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    prev_asked = bool(prev_assistant and prev_assistant.rstrip().endswith(("？", "?")))
    existing_ids = existing_ids or set()
    valid: list[ValidatedClaim] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        layer = item.get("layer")
        if section not in SECTIONS or layer not in LAYERS:
            continue
        if layer == "observed":
            continue  # 对话不产生资料观察
        content = str(item.get("content") or "").strip().replace("\n", " ")
        quote = str(item.get("quote") or "").strip()
        if not content or not _quote_ok(quote, user_text):
            continue
        if len(content) > 120:
            content = content[:120]
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        downgraded = False
        predicate = str(item.get("predicate") or "").strip()
        if predicate not in PREDICATES[section]:
            predicate = DEFAULT_PREDICATE[section]
            confidence = max(0.0, confidence - 0.1)
            downgraded = True
        if layer == "self_declared" and not (_FIRST_PERSON_RE.search(quote) or prev_asked):
            layer = "hypothesis"
            downgraded = True
        if layer == "aspirational" and not _ASPIRATION_RE.search(_sentence_around(user_text, quote)):
            layer = "self_declared" if _FIRST_PERSON_RE.search(quote) else "hypothesis"
            downgraded = True
        subject = str(item.get("subject") or "me").strip() or "me"
        obj = item.get("object")
        obj = str(obj).strip() if obj else None
        scope_hint = item.get("scope_hint") or "unknown"
        scope = "context_only" if scope_hint == "context_only" else "long_term"
        privacy = "sensitive" if item.get("privacy_hint") == "sensitive" else "private"
        merge_into = item.get("merge_into")
        merge_into = str(merge_into) if merge_into and str(merge_into) in existing_ids else None
        valid_to = _parse_date(item.get("date")) if predicate == "committed_to" else None
        valid.append(
            ValidatedClaim(
                section=section,
                layer=layer,
                predicate=predicate,
                subject=subject,
                object=obj,
                content=content,
                quote=quote,
                confidence=confidence,
                scope=scope,
                privacy_level=privacy,
                downgraded=downgraded,
                merge_into=merge_into,
                why_it_matters=str(item.get("why_it_matters") or "").strip()[:120],
                valid_to=valid_to,
            )
        )
    valid.sort(key=lambda c: c.confidence, reverse=True)
    return valid[:MAX_CLAIMS_PER_TURN]


def _entity_id(store: OntologyStore, name: str, entity_types: dict[str, str]) -> str | None:
    norm = normalize_text(name)
    if not norm or norm in ("me", "我", "本人", "我自己", "用户"):
        return ME_ENTITY_ID
    try:
        entity = store.upsert_entity(name, entity_types.get(norm, "person"))
    except OntologyError:
        return None
    return entity["id"]


def persist(
    valid: list[ValidatedClaim],
    entities: list[dict],
    *,
    store: OntologyStore,
    conversation_id: str,
    message_id: str,
) -> dict:
    entity_types: dict[str, str] = {}
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        etype = ent.get("type") if ent.get("type") in ("person", "organization", "project", "place", "topic", "event", "term") else "person"
        entity_types[normalize_text(name)] = etype
        try:
            store.upsert_entity(name, etype, aliases=[str(a) for a in (ent.get("aliases") or []) if a])
        except OntologyError:
            continue

    created: list[str] = []
    reaffirmed: list[str] = []
    promoted: list[str] = []
    suppressed = 0
    for claim in valid:
        subject_id = _entity_id(store, claim.subject, entity_types)
        if subject_id is None:
            continue
        object_id = _entity_id(store, claim.object, entity_types) if claim.object else None
        if object_id == subject_id:
            object_id = None
        auto_confirm = claim.layer == "self_declared" and claim.confidence >= AUTO_CONFIRM_CONFIDENCE and not claim.downgraded
        evidence = [{"kind": "conversation_turn", "conversation_id": conversation_id, "message_id": message_id, "quote": claim.quote}]

        existing = None
        if claim.merge_into:
            existing = store.get_claim(claim.merge_into, with_evidence=False)
            if existing is not None and existing["trustState"] not in ("working", "confirmed"):
                existing = None
        if existing is None:
            existing = store.find_active_by_hash(subject_id, claim.predicate, claim.content) or store.find_similar_active(
                claim.content, threshold=SIMILAR_THRESHOLD, section=claim.section
            )
        if existing is not None:
            store.add_evidence(existing["id"], evidence, reaffirm=True)
            reaffirmed.append(existing["id"])
            if existing["trustState"] == "working" and auto_confirm:
                try:
                    store.transition(
                        existing["id"],
                        "confirm",
                        surface="conversation",
                        conversation_id=conversation_id,
                        message_id=message_id,
                        note="用户再次亲口说到，视为确认",
                    )
                    promoted.append(existing["id"])
                except OntologyConflictError:
                    pass
            continue

        tombstone = store.find_tombstone_by_hash(subject_id, claim.predicate, claim.content)
        if tombstone is not None and not auto_confirm:
            suppressed += 1
            continue

        payload = {
            "subject_entity_id": subject_id,
            "object_entity_id": object_id,
            "predicate": claim.predicate,
            "content": claim.content,
            "section": claim.section,
            "layer": claim.layer,
            "confidence": claim.confidence,
            "scope": claim.scope,
            "context_ref": conversation_id if claim.scope == "context_only" else None,
            "privacy_level": claim.privacy_level,
            "valid_to": claim.valid_to,
        }
        try:
            result = store.create_claim(
                payload,
                evidence,
                trust_state="confirmed" if auto_confirm else "working",
                trust_origin="utterance" if auto_confirm else "model",
                surface="conversation",
                conversation_id=conversation_id,
                message_id=message_id,
                supersedes_id=tombstone["id"] if tombstone else None,
                note=(("用户原话，抽取校验通过" if auto_confirm else "模型抽取的候选") + (f"；为何重要：{claim.why_it_matters}" if claim.why_it_matters else "")),
            )
        except OntologyConflictError:
            continue
        except OntologyError as exc:
            logger.debug("候选理解写入被拒：%s", exc)
            continue
        created.append(result["id"])
    return {"created": created, "reaffirmed": reaffirmed, "promoted": promoted, "suppressed": suppressed}


def run_extraction(
    *,
    provider: ChatProvider,
    store: OntologyStore,
    conversation_id: str,
    message_id: str,
    user_text: str,
    prev_assistant: str | None,
    debug: dict | None = None,
) -> dict:
    ok, reason = should_extract(user_text)
    if not ok:
        return {"state": "skipped", "reason": reason, "created": [], "reaffirmed": [], "promoted": [], "suppressed": 0}
    known = store.entity_names_for_conversation(conversation_id)
    existing = store.list_claims(trust_states=("confirmed", "working"), limit=20)
    request = build_request(user_text, prev_assistant, known, existing_claims=existing, debug=debug)
    raw = provider.complete_json(request)  # ProviderError 由 worker 分类
    valid = validate(raw, user_text=user_text, prev_assistant=prev_assistant, existing_ids={c["id"] for c in existing})
    entities = filter_entities(raw.get("entities") or [], user_text=user_text)
    summary = persist(valid, entities, store=store, conversation_id=conversation_id, message_id=message_id)
    summary.update({"state": "done", "reason": "ok", "candidates": len(valid), "provider": provider.name})
    return summary


__all__ = [
    "EXTRACTION_SCHEMA",
    "MAX_CLAIMS_PER_TURN",
    "ProviderError",
    "ValidatedClaim",
    "build_request",
    "persist",
    "run_extraction",
    "should_extract",
    "validate",
    "json",
]


FIRST_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": ["string", "null"]},
        "basis_claim_ids": {"type": "array", "items": {"type": "string"}},
        "section": {"type": "string", "enum": list(SECTIONS)},
        "question": {"type": ["string", "null"]},
    },
    "required": ["content", "basis_claim_ids", "section", "question"],
    "additionalProperties": False,
}

_FIRST_OBSERVATION_SYSTEM = """你是知君。建档刚结束，请基于用户已确认的理解给出「第一次观察」：恰好一条对他做事模式的推测。
规则：必须把至少两条已确认理解连起来（basis_claim_ids 填它们的 id），content 用第二人称写明依据（「你提到……和……，我猜你……」，≤ 80 字），section 选最贴切的分区，question 是一句邀请确认的话（以「对吗？」结尾）。依据不足就 content 填 null。只输出 JSON。"""


def first_observation(*, provider: ChatProvider, store: OntologyStore, conversation_id: str, message_id: str | None) -> dict:
    """建档收尾：一条【我推测的】工作理解，等用户点头。"""
    basis = store.list_claims(trust_states=("confirmed",), limit=12)
    if len(basis) < 2:
        return {"state": "skipped", "reason": "not_enough_basis"}
    lines = ["已确认的理解："] + [f"- {c['id']}：{c['content']}" for c in basis]
    request = ChatRequest(
        system=_FIRST_OBSERVATION_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        max_tokens=600,
        temperature=0.3,
        json_schema=FIRST_OBSERVATION_SCHEMA,
        effort="medium",
        debug={"task": "first_observation", "basisClaims": [{"id": c["id"], "content": c["content"]} for c in basis]},
    )
    raw = provider.complete_json(request)
    content = str(raw.get("content") or "").strip()[:120]
    ids = [str(i) for i in (raw.get("basis_claim_ids") or []) if str(i) in {c["id"] for c in basis}]
    if not content or len(ids) < 2:
        return {"state": "skipped", "reason": "no_observation"}
    section = raw.get("section") if raw.get("section") in SECTIONS else "ways"
    by_id = {c["id"]: c for c in basis}
    quote = "；".join(by_id[i]["content"] for i in ids)[:300]
    try:
        claim = store.create_claim(
            {"subject_entity_id": ME_ENTITY_ID, "predicate": DEFAULT_PREDICATE[section], "content": content, "section": section, "layer": "hypothesis", "confidence": 0.5},
            [{"kind": "conversation_turn", "conversation_id": conversation_id, "message_id": message_id, "quote": quote}],
            trust_state="working",
            trust_origin="model",
            surface="onboarding",
            conversation_id=conversation_id,
            message_id=message_id,
            note="建档收尾的第一次观察；依据：" + "，".join(ids),
        )
    except (OntologyConflictError, OntologyError) as exc:
        return {"state": "skipped", "reason": str(exc)[:80]}
    return {"state": "done", "claimId": claim["id"], "question": str(raw.get("question") or "")[:120]}
