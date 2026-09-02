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
    if provider is None or provider.name == "fake":
        return heuristic_verdict(a["content"], b["content"]), "heuristic"
    request = ChatRequest(
        system=_CONFLICT_SYSTEM,
        messages=[{"role": "user", "content": f"理解一：{a['content']}\n理解二：{b['content']}"}],
        max_tokens=200,
        temperature=0.0,
        json_schema=CONFLICT_SCHEMA,
        effort="low",
        debug={"task": "conflict_judge"},
    )
    try:
        raw = provider.complete_json(request)
    except (ProviderError, ValueError):
        return "unrelated", "model_unavailable"
    verdict = str(raw.get("verdict") or "unrelated")
    return (verdict if verdict in ("contradict", "equivalent", "unrelated") else "unrelated"), str(raw.get("reason") or "")[:200]


def run(*, store: OntologyStore | None = None, conv_store: ConversationStore | None = None, provider: ChatProvider | None = None, now: datetime | None = None) -> dict:
    store = store or OntologyStore.instance()
    conv_store = conv_store or ConversationStore.instance()
    current = (now or _now()).astimezone(timezone.utc)
    report = {"mergeProposals": 0, "challenged": 0, "conflicts": 0, "merged": 0, "tensions": 0, "promoted": 0, "decayed": 0, "deferred": 0, "pairsJudged": 0}

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
    for a, b, kind in candidates[:MAX_PAIRS_PER_RUN]:
        verdict, note = judge_pair(provider, a, b)
        report["pairsJudged"] += 1
        if verdict == "unrelated":
            continue
        if kind == "tension":
            if verdict == "contradict":
                if store.create_conflict(a["id"], b["id"], kind="tension", verdict_by=note or "model", note="原则与最近做法之间可能有张力"):
                    report["tensions"] += 1
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

    # 4/5. 晋升 / 衰减 / 推迟
    for claim in store.list_claims(trust_states=("working",), limit=5000, include_hidden=True):
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

    store.meta_set("last_consolidate_at", _iso(current))
    store.meta_set("claims_at_last_consolidate", str(store.stats()["claims"]["working"] + store.stats()["claims"]["confirmed"]))
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
