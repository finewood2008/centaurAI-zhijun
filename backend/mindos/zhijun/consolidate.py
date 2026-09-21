"""整合器：夜间（或每新增 20 条理解后）对本体做一次整理。绝不自动合并实体、绝不自动改动已确认理解。

1. 实体去重候选：别名相同或名称词面近似（≥ 0.9，同类型）→ ``entity_merge_proposals``，等用户裁决。
2. 矛盾：同主语同分区的活跃理解两两词面近似（≥ 0.55）时问模型「矛盾 / 等价 / 无关」（演示模型用否定词启发式）：
   - working ↔ confirmed 矛盾 → working 打 ``challenged``（退出上下文与 inbox）；
   - 两条 confirmed 矛盾 → ``claim_conflicts`` 复核卡；
   - 等价 → 新的一条并入旧的（追加证据，新的 working 标 superseded 语义用 reject 处理）。
3. 原则-行为张力：confirmed 原则 与 7 天内 confirmed 的做法 / 事 被判矛盾 → ``principle_tension`` 提醒（措辞是问句）。
4. 晋升：≥ 2 个独立来源的 working → ``promotion_ready``，浮到 inbox 顶部。
5. 衰减：challenged 超过 30 天未确认 → 自动撤回（唯一允许的自动状态变化，只对 working）；60 天单证据未提及的 working → 远期推迟。
"""
from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timedelta, timezone

from ..stores.conversation_store import ConversationStore
from ..stores.ontology_store import OntologyStore, lexical_similarity, normalize_text, tokenize
from .provider import ChatProvider, ChatRequest, ProviderError

logger = logging.getLogger(__name__)

ENTITY_SIMILARITY = 0.9
CLAIM_SIMILARITY = 0.55
CHALLENGE_DECAY_DAYS = 30
STALE_DEFER_DAYS = 60
TENSION_WINDOW_DAYS = 7
MAX_PAIRS_PER_RUN = 30
# P1 内观的两种张力（数据层 4.9）。这两条是「照见」的唯一来源：
# A 言行不一 —— 他对自己的评价，和记录下来的行为对不上。
# B 说了没动 —— 同一件心里的事反复回来，相关的地方却一直没有任何进展。
SELF_VIEW_SIMILARITY = 0.25   # 比原则-做法宽一点：自评用词和行为记录本来就不重合
BURDEN_MIN_MENTIONS = 3       # 反复到这个次数才算「反复」
BURDEN_MIN_SPAN_DAYS = 30     # 且要跨过这么久，否则只是这一阵心烦
BURDEN_STALL_MAX = 1          # 一轮最多提一件，这种话说多了就成了催
NEW_CLAIMS_TRIGGER = 20

_NEG_RE = re.compile(r"不|没|别|从不|绝不|无法|不再|放弃|停")

CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["contradict", "equivalent", "unrelated"]}, "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}
_CONFLICT_SYSTEM = """你是知君的本体整理助手。给你两条关于同一个人的理解，判断它们是「矛盾」（不可能同时成立）、「等价」（说的是同一件事）还是「无关」（可以同时成立）。只输出 JSON：{"verdict":"contradict|equivalent|unrelated","reason":"一句话"}。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def heuristic_verdict(a: str, b: str) -> str:
    """无模型时的启发式：词面高度相近但否定词数量奇偶不同 → 矛盾；几乎相同 → 等价；否则无关。"""
    ta, tb = tokenize(a), tokenize(b)
    sim = lexical_similarity(ta, tb)
    neg_a = len(_NEG_RE.findall(a))
    neg_b = len(_NEG_RE.findall(b))
    if normalize_text(a) == normalize_text(b) or (sim >= 0.92 and neg_a == neg_b):
        return "equivalent"
    if sim >= 0.5 and (neg_a % 2) != (neg_b % 2):
        return "contradict"
    return "unrelated"


def judge_pair(provider: ChatProvider | None, a: dict, b: dict) -> tuple[str, str]:
    # A temporary state, aspiration and a stable preference are not interchangeable.
    # Avoid both false contradictions and equivalence merges across conditions.
    if (a.get("layer") == "aspirational") != (b.get("layer") == "aspirational"):
        return "unrelated", "aspiration_is_not_observed_behavior"
    for key in ("scope", "contextRef", "validFrom", "validTo", "contextual"):
        if a.get(key) != b.get(key):
            return "unrelated", "different_contexts"
    if provider is None or provider.name == "fake":
        return heuristic_verdict(a["content"], b["content"]), "heuristic"
    request = ChatRequest(
        system=_CONFLICT_SYSTEM,
        messages=[{"role": "user", "content": json.dumps([
            {k: c.get(k) for k in ("content", "scope", "contextRef", "layer", "validFrom", "validTo", "contextual", "evidence")}
            for c in (a, b)], ensure_ascii=False)}],
        max_tokens=200,
        temperature=0.0,
        json_schema=CONFLICT_SCHEMA,
        effort="low",
        debug={"task": "conflict_judge"},
    )
    try:
        from .gate import provider_gate
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=30, background=True):
            raise ProviderError("整理暂停，优先处理交互请求", code="PROVIDER_BUSY")
        try:
            raw = provider.complete_json(request)
        finally:
            provider_gate.release(channel)
    except (ProviderError, ValueError):
        return "unrelated", "model_unavailable"
    verdict = str(raw.get("verdict") or "unrelated")
    return (verdict if verdict in ("contradict", "equivalent", "unrelated") else "unrelated"), str(raw.get("reason") or "")[:200]


def _person_to_talk_to(store, claim) -> str | None:
    """原则 7「把人推回人」：这件压着的事，是不是该去找一个真人谈。

    只在两个条件同时成立时才说：这条困扰**指向一个具体的人**，而且那个人是用户自己
    提过的。泛泛的「建议你找朋友聊聊」不算把人推回人，那只是一句漂亮话。

    指向项目、公司、抽象话题的困扰不触发——「你该和『年底融资』谈谈」是胡话。
    """
    entity_id = claim.get("objectEntityId")
    if not entity_id:
        return None
    try:
        entity = store.get_entity(entity_id)
    except Exception:  # noqa: BLE001 - 查不到就不说这句，不该让整轮整理挂掉
        return None
    if not entity or entity.get("type") != "person":
        return None
    name = (entity.get("canonicalName") or claim.get("objectName") or "").strip()
    return name or None


def _stalled_burdens(store, conv_store, active, charter, current, report) -> None:
    """张力 B（说了没动）：同一件心里的事反复回来，相关的地方却一直没有任何进展。

    这一条不调模型：它判断的不是两句话矛不矛盾，而是「说过很多次」与「什么都没发生」
    之间的落差，那是可以直接数出来的。不送模型也意味着这些内容少出一次门。

    三个条件同时成立才算：提及 ≥3 次、跨度 ≥30 天、相关的事项里没有这条困扰出现之后
    的新进展。一轮最多提一件——这种话说多了就成了催。
    """
    from . import burdens as burden_meta
    from .charter_policy import check_action

    if not check_action(charter, "proactive")["allowed"]:
        return
    stalled = [c for c in active if c["section"] == "burdens" and c["trustState"] == "confirmed"]
    matters = [c for c in active if c["section"] == "matters"]
    raised = 0
    for claim in stalled:
        if raised >= BURDEN_STALL_MAX:
            break
        first = _parse(claim["firstSeen"])
        last = _parse(claim["lastReaffirmed"]) or first
        if not first or not last or (last - first) < timedelta(days=BURDEN_MIN_SPAN_DAYS):
            continue
        mentions = burden_meta.mention_count(store, claim["content"], object_name=claim.get("objectName"))
        if mentions < BURDEN_MIN_MENTIONS:
            continue
        # 「相关的地方有没有进展」按实体判，不按词面：分词会把「林岚」和「林岚谈」
        # 切成不同的词，纯词面匹配在这里几乎必然漏判，一漏判就会去催一个其实已经
        # 动起来的人。没有实体可依时才退回词面。
        words = tokenize(claim["content"])
        subject = claim.get("objectEntityId")
        moved = any(
            (_parse(m["firstSeen"]) or current) > first
            and ((subject and m.get("objectEntityId") == subject)
                 or (not subject and lexical_similarity(words, tokenize(m["content"])) >= 0.3))
            for m in matters
        )
        if moved:
            continue
        span_days = max(1, (last - first).days)
        person = _person_to_talk_to(store, claim)
        # 同样是观察在先：给出次数与跨度这两个事实，不替他解释为什么。
        opening = (f"「{claim['content'][:40]}」这件事，{span_days} 天里你提过 {mentions} 次。"
                   f"我想问的不是你为什么没动——是这件事本身难，还是别的什么难？")
        if person:
            # 原则 7「把人推回人」：知君的目标是减少孤独，不是替代人。
            # 必须具体到人，引用他自己提过的那个人，不能是泛泛的「找朋友聊聊」。
            opening += f"\n\n还有一句：这件事你真正要谈的人可能是{person}，不是我。"
        conv_store.create_nudge(
            kind="burden_stalled",
            trigger_key=f"burden:{claim['id']}",
            trigger_ref={"burdenId": claim["id"], **({"talkToEntityId": claim.get("objectEntityId")} if person else {})},
            why_now=f"「{claim['content'][:40]}」在 {span_days} 天里被提过 {mentions} 次，相关的事项没有新进展",
            message=opening,
            scheduled_for=_iso(current),
            dedupe_days=60,
            now=_iso(current),
        )
        report["tensions"] += 1
        raised += 1


def run(*, store: OntologyStore | None = None, conv_store: ConversationStore | None = None, provider: ChatProvider | None = None, now: datetime | None = None, router=None) -> dict:
    store = store or OntologyStore.instance()
    conv_store = conv_store or ConversationStore.instance()
    current = (now or _now()).astimezone(timezone.utc)
    report = {"mergeProposals": 0, "challenged": 0, "conflicts": 0, "merged": 0, "tensions": 0, "promoted": 0, "decayed": 0, "deferred": 0, "pairsJudged": 0,
              "managed": bool(router)}
    from .charter_policy import scope_policy, check_action, assert_current
    charter = scope_policy(router.scope if router else "global")
    if not check_action(charter, "memory_auto")["allowed"]:
        return {**report, "reason": "charter_memory_manual"}

    # 1. 实体去重候选
    entities = [e for e in store.list_entities(limit=2000) if e["type"] != "me"]
    seen_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(entities):
        for b in entities[i + 1 :]:
            if a["type"] != b["type"]:
                continue
            key = (a["id"], b["id"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            a_names = {normalize_text(a["canonicalName"]), *(normalize_text(x) for x in a["aliases"])}
            b_names = {normalize_text(b["canonicalName"]), *(normalize_text(x) for x in b["aliases"])}
            score = 1.0 if a_names & b_names else lexical_similarity(tokenize(a["canonicalName"]), tokenize(b["canonicalName"]))
            if score >= ENTITY_SIMILARITY:
                reason = "别名相同" if score >= 1.0 else f"名称相近（{score:.2f}）"
                if store.create_merge_proposal(b["id"], a["id"], reason=reason, score=score):
                    report["mergeProposals"] += 1

    # 2/3. 矛盾与张力
    active = store.list_claims(trust_states=("working", "confirmed"), limit=5000, include_hidden=True)
    from .alignment import visible
    if router:
        active = [c for c in active if visible(c, conv_store, router.scope)]
    if provider and provider.external and not router:
        from .source_policy import SourcePolicy
        policy = SourcePolicy(store, conv_store)
        active = [c for c in active if not policy.claim_local(c)]
    by_key: dict[tuple[str, str], list[dict]] = {}
    for claim in active:
        by_key.setdefault((claim["subjectEntityId"], claim["section"]), []).append(claim)
    principles = [c for c in active if c["section"] == "principles" and c["trustState"] == "confirmed"]
    recent_actions = [
        c for c in active
        if c["section"] in ("ways", "matters") and c["trustState"] == "confirmed"
        and (_parse(c["firstSeen"]) or current) >= current - timedelta(days=TENSION_WINDOW_DAYS)
    ]
    candidates: list[tuple[dict, dict, str]] = []
    for group in by_key.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if lexical_similarity(tokenize(a["content"]), tokenize(b["content"])) >= CLAIM_SIMILARITY:
                    candidates.append((a, b, "conflict"))
    for p in principles:
        for act in recent_actions:
            if lexical_similarity(tokenize(p["content"]), tokenize(act["content"])) >= 0.3:
                candidates.append((p, act, "tension"))
    # 张力 A（言行不一）：self_view 的自我评价 ⟂ ways / matters 里 observed 层的行为记录。
    # 只配 observed：那是被观察到的事，不是他自己又说了一遍。拿自评去配自评，
    # 得到的只是措辞差异，不是张力。
    self_views = [c for c in active if c["section"] == "self_view"
                  and c["trustState"] == "confirmed" and c["layer"] == "self_declared"]
    observed_behaviour = [c for c in active if c["section"] in ("ways", "matters")
                          and c["trustState"] == "confirmed" and c["layer"] == "observed"]
    for view in self_views:
        for act in observed_behaviour:
            if lexical_similarity(tokenize(view["content"]), tokenize(act["content"])) >= SELF_VIEW_SIMILARITY:
                candidates.append((view, act, "self_view_tension"))
    for a, b, kind in candidates[:MAX_PAIRS_PER_RUN]:
        pair_provider = provider
        if router and provider:
            from .routing import GuardedProvider
            pair_provider = GuardedProvider(router, provider, "consolidate", [router.ref("claim", c["id"]) for c in (a, b)], background=True)
        verdict, note = judge_pair(pair_provider, a, b)
        assert_current(charter)
        if router and provider:
            pair_provider.assert_current()
        report["pairsJudged"] += 1
        if verdict == "unrelated":
            continue
        if kind == "self_view_tension":
            if verdict != "contradict":
                continue
            if not store.create_conflict(a["id"], b["id"], kind="tension", verdict_by=note or "model",
                                         note="自我评价与行为记录之间可能有张力"):
                continue
            report["tensions"] += 1
            if not check_action(charter, "proactive")["allowed"]:
                continue
            # 第一句永远是观察，不是评价（PRD 5.1 / 数据层 4.9）。这里只并排放两件事，
            # 不下结论，也不问「你是不是其实……」。
            conv_store.create_nudge(
                kind="self_view_tension",
                trigger_key=f"selfview:{a['id']}:{b['id']}",
                trigger_ref={"selfViewId": a["id"], "behaviourId": b["id"]},
                why_now=f"你说过「{a['content'][:40]}」，而记录里有「{b['content'][:40]}」",
                message=f"你说过「{a['content'][:40]}」。我这边记着的是「{b['content'][:40]}」。这两件事放在一起，我没看明白——是我记错了，还是有我不知道的原因？",
                scheduled_for=_iso(current),
                dedupe_days=45,
                now=_iso(current),
            )
            continue
        if kind == "tension":
            if verdict == "contradict":
                if store.create_conflict(a["id"], b["id"], kind="tension", verdict_by=note or "model", note="原则与最近做法之间可能有张力"):
                    report["tensions"] += 1
                    if not check_action(charter, "proactive")["allowed"]:
                        continue
                    conv_store.create_nudge(
                        kind="principle_tension",
                        trigger_key=f"tension:{a['id']}:{b['id']}",
                        trigger_ref={"principleId": a["id"], "actionId": b["id"]},
                        why_now=f"你最近确认了「{b['content'][:40]}」，它和你的原则「{a['content'][:40]}」放在一起时看起来有张力",
                        message=f"「{a['content'][:40]}」是你确认过的原则，而最近「{b['content'][:40]}」——是原则变了，还是这次情况特殊？",
                        scheduled_for=_iso(current),
                        dedupe_days=30,
                        now=_iso(current),
                    )
            continue
        if verdict == "equivalent":
            if router:
                # Managed models suggest equivalence; only the user may merge records.
                if store.create_conflict(a["id"], b["id"], kind="tension", verdict_by=note or "model", note="两条理解可能等价，等待用户核对；尚未合并"):
                    report["mergeProposals"] += 1
                continue
            newer, older = (a, b) if (a["createdAt"] > b["createdAt"]) else (b, a)
            if newer["trustState"] == "working":
                store.add_evidence(older["id"], [ev for ev in newer.get("evidence", [])] or [], reaffirm=True) if newer.get("evidence") else None
                store.transition(newer["id"], "reject", surface="system", actor="system", note=f"与 {older['id']} 等价，已并入")
                report["merged"] += 1
            continue
        # contradict
        states = {a["trustState"], b["trustState"]}
        if states == {"working", "confirmed"}:
            working = a if a["trustState"] == "working" else b
            confirmed = b if working is a else a
            store.set_challenged(working["id"], f"与已确认的「{confirmed['content'][:40]}」矛盾")
            report["challenged"] += 1
        elif states == {"confirmed"}:
            if store.create_conflict(a["id"], b["id"], kind="contradiction", verdict_by=note or "model", note="两条已确认的理解看起来矛盾"):
                report["conflicts"] += 1
        else:  # 两条 working 矛盾：都留在 inbox，但互相标注
            store.set_challenged(b["id"], f"与另一条候选「{a['content'][:40]}」矛盾")
            report["challenged"] += 1

    # 4/5. 晋升 / 衰减 / 推迟（V3：router 下同样生效；只作用于 working，永不改动已确认的理解）
    for claim in store.list_claims(trust_states=("working",), limit=5000, include_hidden=True):
        if router and not visible(claim, conv_store, router.scope):
            continue
        if claim["challenged"]:
            challenged_since = _parse(claim["updatedAt"]) or current
            if current - challenged_since >= timedelta(days=CHALLENGE_DECAY_DAYS):
                if store.system_retract(claim["id"], "decayed_contradicted", note="被挑战超过 30 天未确认"):
                    report["decayed"] += 1
            continue
        sources = store.evidence_source_count(claim["id"])
        if sources >= 2 and not claim.get("promotionReady"):
            store.set_promotion_ready(claim["id"], True)
            report["promoted"] += 1
        last = _parse(claim["lastReaffirmed"]) or current
        if sources <= 1 and current - last >= timedelta(days=STALE_DEFER_DAYS) and not claim.get("deferredUntil"):
            store.system_defer(claim["id"], _iso(current + timedelta(days=365)))
            report["deferred"] += 1

    _stalled_burdens(store, conv_store, active, charter, current, report)

    store.meta_set("last_consolidate_at", _iso(current))
    store.meta_set("claims_at_last_consolidate", str(store.stats()["claims"]["working"] + store.stats()["claims"]["confirmed"]))
    from .jobs import enqueue_core_profile_quietly
    enqueue_core_profile_quietly(router.scope if router else "global", store=store, conv_store=conv_store)  # V3：整理后重建核心画像
    return report


def should_run(store: OntologyStore, *, now: datetime | None = None) -> bool:
    current = now or _now()
    last = _parse(store.meta_get("last_consolidate_at"))
    if last is None or current - last >= timedelta(hours=24):
        return True
    try:
        baseline = int(store.meta_get("claims_at_last_consolidate", "0") or 0)
    except ValueError:
        baseline = 0
    stats = store.stats()["claims"]
    return (stats["working"] + stats["confirmed"]) - baseline >= NEW_CLAIMS_TRIGGER
