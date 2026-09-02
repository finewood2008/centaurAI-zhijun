"""知君关系首页：真实数据聚合、可解释模板与异步来信润色。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter

from .stores.conversation_store import ConversationStore
from .stores.growth_store import GrowthStore
from .stores.ontology_store import OntologyStore

_CACHE_KEY = "zhijun_home_snapshot_v1"
_PREFIX = "/api/mindos/zhijun/home"
_SECTION_LABELS = {
    "who": "你自己",
    "people": "重要的人",
    "matters": "在意的事",
    "principles": "原则",
    "ways": "做事方式",
    "direction": "方向",
}


def _iso_now(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _source_ref(node: dict) -> dict:
    trust = "confirmed" if node["ring"] == "remembered" else "working" if node["ring"] == "uncertain" else "recorded"
    label = "我的推测" if trust == "working" else "判断簿" if node["sourceType"] == "decision" else "你确认过"
    return {
        "id": node["id"],
        "sourceType": node["sourceType"],
        "label": label,
        "title": _clip(node["title"], 34),
        "trust": trust,
    }


def _claim_node(claim: dict, ring: str) -> dict:
    return {
        "id": f"claim:{claim['id']}",
        "ring": ring,
        "sourceType": "commitment" if claim.get("predicate") == "committed_to" else "claim",
        "title": claim["content"],
        "summary": (
            "这是我还没有把握的理解，等你点头或纠正"
            if ring == "uncertain"
            else f"关于{_SECTION_LABELS.get(claim.get('section'), '你')}，你已经确认过"
        ),
        "occurredAt": claim.get("lastReaffirmed") or claim.get("firstSeen"),
        "claim": claim,
        "decision": None,
    }


def _decision_node(decision: dict, now: datetime) -> dict:
    review_at = _parse_time(decision.get("reviewAt"))
    status = decision.get("status")
    if status == "outcome_recorded":
        summary = "结果已经回来，等我们一起复盘"
    elif review_at and review_at <= now:
        summary = "到了约好回看结果的时候"
    elif review_at:
        summary = f"我们约好在 {review_at.month}月{review_at.day}日 回看"
    else:
        summary = "这个选择还在等待真实结果"
    return {
        "id": f"decision:{decision['id']}",
        "ring": "tracking",
        "sourceType": "decision",
        "title": decision["title"],
        "summary": summary,
        "occurredAt": decision.get("updatedAt") or decision.get("createdAt"),
        "claim": None,
        "decision": decision,
    }


def _relationship_days(conversations: list[dict], claims: list[dict], decisions: list[dict], now: datetime) -> int:
    candidates = [
        *(item.get("createdAt") for item in conversations),
        *(item.get("firstSeen") for item in claims),
        *(item.get("createdAt") for item in decisions),
    ]
    parsed = [dt for value in candidates if (dt := _parse_time(value)) is not None]
    if not parsed:
        return 0
    return max(1, (now.date() - min(parsed).date()).days + 1)


def _state(stats: dict, conversations: list[dict]) -> tuple[str, dict | None]:
    onboarding = next((item for item in conversations if item.get("mode") == "onboarding"), None)
    if onboarding and (int(onboarding.get("messageCount") or 0) + 1) // 2 < 8:
        return "building", onboarding
    if stats.get("hasOntology"):
        return "established", onboarding
    if onboarding:
        return "building", onboarding
    return "first_meet", None


def _tracking_nodes(decisions: list[dict], commitments: list[dict], now: datetime) -> list[dict]:
    ranked: list[tuple[int, str, dict]] = []
    for decision in decisions:
        status = decision.get("status")
        if status == "reviewed":
            continue
        review_at = _parse_time(decision.get("reviewAt"))
        if status == "outcome_recorded":
            priority = 1
        elif review_at and review_at <= now:
            priority = 0
        else:
            priority = 3
        ranked.append((priority, str(decision.get("updatedAt") or ""), _decision_node(decision, now)))
    for claim in commitments:
        due = _parse_time(claim.get("validTo"))
        priority = 2 if due and due <= now else 4
        ranked.append((priority, str(claim.get("lastReaffirmed") or ""), _claim_node(claim, "tracking")))
    ranked.sort(key=lambda item: (item[0], -((_parse_time(item[1]) or datetime(1970, 1, 1, tzinfo=timezone.utc)).timestamp())))
    return [item[2] for item in ranked[:3]]


def _timeline(confirmed: list[dict], decisions: list[dict]) -> list[dict]:
    events: list[dict] = []
    for claim in confirmed:
        events.append({
            "id": f"remembered:{claim['id']}",
            "kind": "remembered",
            "title": "我记住了",
            "detail": claim["content"],
            "occurredAt": claim.get("lastReaffirmed") or claim.get("firstSeen"),
            "sourceRef": {"id": f"claim:{claim['id']}", "sourceType": "claim", "label": "你确认过", "title": _clip(claim["content"], 34), "trust": "confirmed"},
        })
    for decision in decisions:
        ref = {"id": f"decision:{decision['id']}", "sourceType": "decision", "label": "判断簿", "title": _clip(decision["title"], 34), "trust": "recorded"}
        events.append({"id": f"decision:{decision['id']}", "kind": "decision", "title": "我们做了一个判断", "detail": decision["title"], "occurredAt": decision.get("createdAt"), "sourceRef": ref})
        outcome = decision.get("outcome") or {}
        if outcome.get("recordedAt"):
            events.append({"id": f"outcome:{decision['id']}", "kind": "outcome", "title": "结果回来了", "detail": _clip(outcome.get("result") or decision["title"], 90), "occurredAt": outcome["recordedAt"], "sourceRef": ref})
        review = decision.get("review") or {}
        if review.get("createdAt"):
            events.append({"id": f"review:{decision['id']}", "kind": "review", "title": "我们完成了一次复盘", "detail": _clip(review.get("reflection") or decision["title"], 90), "occurredAt": review["createdAt"], "sourceRef": ref})
    events.sort(key=lambda item: _parse_time(item.get("occurredAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return events[:6]


def _template_brief(state: str, nodes: list[dict]) -> dict:
    if state == "first_meet":
        return {
            "status": "ready",
            "headline": "这张地图，会随着我们认识彼此慢慢亮起来。",
            "message": "你说过的原则、做过的选择和后来发生的结果，都会在这里留下位置。",
            "generatedBy": "template",
            "sourceRefs": [],
        }
    remembered = next((node for node in nodes if node["ring"] == "remembered"), None)
    tracking = next((node for node in nodes if node["ring"] == "tracking"), None)
    uncertain = next((node for node in nodes if node["ring"] == "uncertain"), None)
    if state == "building":
        headline = "我们正在慢慢认识彼此。"
    elif tracking:
        headline = "有一件事，我还在陪你等结果。"
    elif uncertain:
        headline = "有些关于你的理解，我想再听你说说。"
    else:
        headline = "我把我们最近走过的，整理在这里。"
    parts: list[str] = []
    refs: list[dict] = []
    if remembered:
        parts.append(f"我记得你说过「{_clip(remembered['title'], 30)}」")
        refs.append(_source_ref(remembered))
    if tracking:
        parts.append(f"我们还在跟进「{_clip(tracking['title'], 26)}」")
        refs.append(_source_ref(tracking))
    if uncertain:
        parts.append(f"关于「{_clip(uncertain['title'], 24)}」，我还没有把握")
        refs.append(_source_ref(uncertain))
    message = "；".join(parts) or "我们可以从最近让你在意的一件事继续。"
    return {"status": "ready", "headline": _clip(headline, 28), "message": _clip(message + "。", 120), "generatedBy": "template", "sourceRefs": refs}


def _next_action(state: str, onboarding: dict | None, tracking: list[dict], uncertain: list[dict], conversations: ConversationStore, now: datetime) -> dict:
    if state == "first_meet":
        return {"kind": "onboarding", "title": "从第一次认识开始", "description": "用七个问题，让这张共同地图开始亮起来", "targetId": None, "say": None}
    if state == "building" and onboarding:
        remaining = max(1, 8 - ((int(onboarding.get("messageCount") or 0) + 1) // 2))
        return {"kind": "resume_onboarding", "title": "继续上次的认识", "description": f"还差大约 {remaining} 问，这张地图就会更完整", "targetId": onboarding["id"], "say": None}
    for node in tracking:
        if node["sourceType"] == "decision":
            decision = node["decision"] or {}
            review_at = _parse_time(decision.get("reviewAt"))
            if decision.get("status") == "open" and review_at and review_at <= now:
                return {"kind": "review", "title": f"回看「{_clip(decision['title'], 32)}」", "description": "到了约好核对结果的时候", "targetId": decision["id"], "say": None}
    for node in tracking:
        if node["sourceType"] == "decision" and (node["decision"] or {}).get("status") == "outcome_recorded":
            return {"kind": "reflect", "title": f"复盘「{_clip(node['title'], 32)}」", "description": "结果已经记下，把经验留下来", "targetId": (node["decision"] or {}).get("id"), "say": None}
    for node in tracking:
        if node["sourceType"] == "commitment":
            claim = node["claim"] or {}
            due = _parse_time(claim.get("validTo"))
            if due and due <= now:
                return {"kind": "commitment", "title": f"说说「{_clip(node['title'], 30)}」的进展", "description": "你给自己的承诺到了回看的时候", "targetId": claim.get("id"), "say": f"关于「{node['title']}」，说说进展："}
    if uncertain:
        return {"kind": "confirm", "title": "看看我有没有理解对", "description": _clip(uncertain[0]["title"], 54), "targetId": (uncertain[0]["claim"] or {}).get("id"), "say": None}
    nudges = [item for item in conversations.list_nudges(statuses=("pending", "shown"), limit=10) if (_parse_time(item.get("scheduledFor")) or now) <= now]
    if nudges:
        item = nudges[0]
        return {"kind": "nudge", "title": _clip(item.get("message") or "有件事想和你聊聊", 38), "description": _clip(item.get("whyNow") or "", 64), "targetId": item.get("id"), "say": item.get("message")}
    return {"kind": "chat", "title": "聊聊最近的变化", "description": "带一件最近让你在意的事来，我们从这里继续", "targetId": None, "say": "最近有件事，我想和你聊聊："}


def _source_hash(now: datetime, stats: dict, conversations: list[dict], decisions: list[dict]) -> str:
    latest_conversation = max((str(item.get("updatedAt") or "") for item in conversations), default="")
    latest_decision = max((str(item.get("updatedAt") or "") for item in decisions), default="")
    raw = json.dumps([now.astimezone().date().isoformat(), stats.get("revision", 0), latest_conversation, latest_decision], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _load_cache(store: OntologyStore) -> dict | None:
    try:
        value = json.loads(store.meta_get(_CACHE_KEY, "") or "")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_cached_brief(cache: dict | None, nodes: list[dict]) -> dict | None:
    if not cache or not isinstance(cache.get("brief"), dict):
        return None
    allowed = {node["id"] for node in nodes}
    refs = cache["brief"].get("sourceRefs") or []
    if any(ref.get("id") not in allowed for ref in refs if isinstance(ref, dict)):
        return None
    return dict(cache["brief"])


def _base_overview(*, now: datetime, ontology: OntologyStore, conversations: ConversationStore, growth: GrowthStore) -> dict:
    stats = ontology.stats()
    conversation_items = conversations.list_conversations(limit=50)
    decisions = growth.list_decisions()
    state, onboarding = _state(stats, conversation_items)
    confirmed = ontology.list_claims(trust_states=("confirmed",), limit=50, include_hidden=False)
    working = ontology.inbox(limit=3)
    commitments = [item for item in confirmed if item.get("predicate") == "committed_to"]
    tracking = _tracking_nodes(decisions, commitments, now)
    tracking_claim_ids = {(node.get("claim") or {}).get("id") for node in tracking}
    remembered = [_claim_node(item, "remembered") for item in confirmed if item.get("id") not in tracking_claim_ids][:4]
    uncertain = [_claim_node(item, "uncertain") for item in working][:3]
    nodes = [*remembered, *tracking, *uncertain]
    source_hash = _source_hash(now, stats, conversation_items, decisions)
    return {
        "state": state,
        "brief": _template_brief(state, nodes),
        "map": {"relationshipDays": _relationship_days(conversation_items, [*confirmed, *working], decisions, now), "nodes": nodes},
        "nextAction": _next_action(state, onboarding, tracking, uncertain, conversations, now),
        "timeline": _timeline(confirmed, decisions),
        "generatedAt": _iso_now(now),
        "sourceHash": source_hash,
    }


def build_home_overview(*, now: datetime | None = None, enqueue: bool = True, ontology: OntologyStore | None = None, conversations: ConversationStore | None = None, growth: GrowthStore | None = None) -> dict:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    onto = ontology or OntologyStore.instance()
    convs = conversations or ConversationStore.instance()
    growth_store = growth or GrowthStore.instance()
    overview = _base_overview(now=current, ontology=onto, conversations=convs, growth=growth_store)
    if overview["state"] != "established":
        return overview
    cache = _load_cache(onto)
    cached_brief = _valid_cached_brief(cache, overview["map"]["nodes"])
    if cache and cache.get("sourceHash") == overview["sourceHash"] and cached_brief:
        cached_brief["status"] = "ready"
        overview["brief"] = cached_brief
        return overview
    if cached_brief:
        cached_brief["status"] = "refreshing"
        overview["brief"] = cached_brief
    else:
        overview["brief"]["status"] = "refreshing"
    if enqueue:
        from .zhijun.jobs import enqueue_home_brief

        enqueue_home_brief(overview["sourceHash"], store=onto)
    return overview


_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "message": {"type": "string"},
        "focusIds": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["headline", "message", "focusIds"],
    "additionalProperties": False,
}

_BRIEF_SYSTEM = """你是知君，一位温暖、克制的长期思考伙伴。根据给定事实写一封极短的今日来信。
只允许引用候选列表里的事实；已确认内容可以说“我记得”，未确认内容必须说“我还不确定”；不得诊断人格、制造焦虑或编造。
headline 不超过 28 个汉字，message 不超过 120 个汉字，focusIds 只能从候选 id 中选，最多 3 个。只输出 JSON。"""


def generate_home_brief(expected_hash: str, *, store: OntologyStore | None = None, conv_store: ConversationStore | None = None) -> dict:
    onto = store or OntologyStore.instance()
    convs = conv_store or ConversationStore.instance()
    overview = _base_overview(now=datetime.now(timezone.utc), ontology=onto, conversations=convs, growth=GrowthStore.instance())
    if overview["sourceHash"] != expected_hash or overview["state"] != "established":
        return {"state": "skipped", "reason": "source_changed"}
    fallback = dict(overview["brief"])
    fallback["status"] = "ready"
    brief = fallback
    generated_by = "template"
    try:
        from .zhijun.context import EXTERNAL_PRIVACY_ALLOWED
        from .zhijun.gate import provider_gate
        from .zhijun.provider import ChatRequest, ProviderError, build_provider

        provider = build_provider()
        candidates = []
        for node in overview["map"]["nodes"]:
            claim = node.get("claim") or {}
            if provider.external and claim and claim.get("privacyLevel") not in EXTERNAL_PRIVACY_ALLOWED:
                continue
            item = {"id": node["id"], "relation": node["ring"], "text": node["title"]}
            if node["sourceType"] == "decision":
                decision = node.get("decision") or {}
                item.update({"choice": decision.get("choice"), "status": decision.get("status"), "reviewAt": decision.get("reviewAt")})
            candidates.append(item)
        if provider.name != "fake" and candidates:
            channel = "external" if provider.external else "local"
            if not provider_gate.acquire(channel, timeout=30.0):
                raise ProviderError("模型通道繁忙", status_code=429, code="PROVIDER_BUSY", retryable=True)
            try:
                raw = provider.complete_json(ChatRequest(system=_BRIEF_SYSTEM, messages=[{"role": "user", "content": json.dumps(candidates, ensure_ascii=False)}], max_tokens=500, temperature=0.2, json_schema=_BRIEF_SCHEMA, effort="low", debug={"task": "home_brief"}))
            finally:
                provider_gate.release(channel)
            allowed = {item["id"] for item in candidates}
            focus_ids = list(dict.fromkeys(str(item) for item in (raw.get("focusIds") or [])))[:3]
            headline = _clip(str(raw.get("headline") or ""), 28)
            message = _clip(str(raw.get("message") or ""), 120)
            if headline and message and focus_ids and all(item in allowed for item in focus_ids):
                node_by_id = {node["id"]: node for node in overview["map"]["nodes"]}
                brief = {"status": "ready", "headline": headline, "message": message, "generatedBy": provider.name, "sourceRefs": [_source_ref(node_by_id[item]) for item in focus_ids]}
                generated_by = provider.name
    except Exception:
        brief = fallback
        generated_by = "template"
    onto.meta_set(_CACHE_KEY, json.dumps({"sourceHash": expected_hash, "brief": brief, "generatedAt": _iso_now()}, ensure_ascii=False, separators=(",", ":")))
    return {"state": "done", "generatedBy": generated_by, "sourceHash": expected_hash}


def home_overview():
    return build_home_overview()


router = APIRouter(prefix=_PREFIX, tags=["zhijun-home"])
router.add_api_route("", home_overview, methods=["GET"])
