"""求知引擎：知君还不知道 / 该核对 / 有张力 / 未了结的目标，每条配一句温和的问句。

只排序，不调模型。供提示词（一轮最多一问）、开场白、今日来信使用。
- gap：分区已确认理解 < 2；问句模板必须能被 ``extract._answer_section`` 识别，用户的回答才会落到该分区。
- stale：已确认、单来源、超过 60 天未重申（≤ 1 条）。
- tension：``list_conflicts(pending)``，措辞沿用整合器。
- open_loop：14 天内摘要里的「待办：」、到期判断（→ 回访会话）、到期承诺。
- nod：inbox 里的待确认理解，promotionReady 优先。
- onboarding_topic：建档会话里还没聊到的话题。
过滤：``nudges._quiet_words`` 命中即丢；章程 ``no_proactive`` 只留 gap / nod；7 天冷却（meta 记 key → ts，≤ 20 条）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from ..chat_imports import service_info
from ..stores.ontology_store import INWARD_SECTIONS, ME_ENTITY_ID, SECTIONS

COOLDOWN_DAYS = 7
RECENT_LIMIT = 20
STALE_DAYS = 60
LOOP_DAYS = 14
GAP_MIN_CONFIRMED = 2
BLOCK_LIMIT = 160
_CACHE_KEY = "zhijun_inquiry_recent_v1"
HEADING = "## 如果自然，可以问的一个问题（不是必须；用户在倾诉或已明确不想展开时不要问；一轮最多一问）"

# 问句必须能被 extract._answer_section 映射回分区（见 test_inquiry）。
GAP_QUESTIONS = {
    "who": "我还不太了解你现在的处境。方便说说你的职业或角色吗？",
    "people": "对你来说，最重要的人是谁？",
    "matters": "你目前主要在做哪件事、负责什么？",
    "principles": "做重要决定时，你最看重的原则是什么？",
    "ways": "遇到拿不准的事，你更喜欢我怎么配合你？",
    "direction": "未来几年，你最希望自己走向哪里？",
}
GAP_WHY = {
    "who": "还不了解你是谁", "people": "还不了解谁对你重要", "matters": "还不了解你在做什么",
    "principles": "还不了解你的原则", "ways": "还不了解你希望怎样被帮助", "direction": "还不了解你的方向",
}
_PRIORITY = {"open_loop": 0, "tension": 1, "nod": 2, "stale": 3, "gap": 4, "onboarding_topic": 5}
_QUESTION_RE = re.compile(r"[？?]\s*$|[吗呢]\s*$|怎么|如何|为什么|什么|哪|多少|能不能|是不是|有没有|要不要|帮我|请问|建议")


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clip(value, limit):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def is_question(text):
    """用户本句是提问或求助时，不再往里塞知君自己的问题。"""
    return bool(_QUESTION_RE.search(str(text or "").strip()))


def _cache_key(scope):
    return _CACHE_KEY if scope == "global" else _CACHE_KEY + ":" + scope


def recent_keys(store, scope, *, now=None):
    """7 天内问过的目标 key → 时间；过期项在读取时剔除。"""
    current = _now(now)
    try:
        raw = json.loads(store.meta_get(_cache_key(scope), "") or "")
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        when = _parse(value)
        if when and current - when < timedelta(days=COOLDOWN_DAYS):
            result[str(key)] = _iso(when)
    return result


def mark_asked(store, scope, key, *, now=None):
    """记录已问过（7 天冷却，最多保留 20 条最近的）。"""
    if not key:
        return
    current = _now(now)
    recent = recent_keys(store, scope, now=current)
    recent[str(key)] = _iso(current)
    ordered = sorted(recent.items(), key=lambda item: item[1], reverse=True)[:RECENT_LIMIT]
    store.meta_set(_cache_key(scope), json.dumps(dict(ordered), ensure_ascii=False, separators=(",", ":")))


def _target(kind, *, key, question, why, priority=None, ref=None, refs=None, target_type=None, target_id=None, order=""):
    return {"kind": kind, "key": key, "priority": _PRIORITY[kind] if priority is None else priority,
            "question": question, "why": why, "ref": ref, "refs": refs or ([ref] if ref else []),
            "targetType": target_type, "targetId": target_id, "order": order}


def _sensitive(claim):
    return claim.get("privacyLevel") in ("sensitive", "restricted")


def targets(store, convs, growth, scope="global", *, now=None, limit=12):
    """排序后的目标列表（已应用静默词、章程 no_proactive 与 7 天冷却）。"""
    current = _now(now)
    from .alignment import visible
    from .charter_policy import scope_policy, check_action, record_in_scope
    from .nudges import _quiet_words
    policy = scope_policy(scope, growth=growth)
    proactive = check_action(policy, "proactive")["allowed"]
    quiet = [w for w in _quiet_words(policy["charter"], store, scope=scope, conversations=convs) if w]
    cooled = recent_keys(store, scope, now=current)
    found = []

    confirmed = [c for c in store.list_claims(trust_states=("confirmed",), limit=-1, include_hidden=False)
                 if visible(c, convs, scope) and c.get("scope") != "context_only"]
    mine = [c for c in confirmed if c.get("subjectEntityId") == ME_ENTITY_ID]
    # gap：分区已确认 < 2
    if confirmed:
        # 内观两分区**不参与缺口提问**。「你有什么事压在心里」当成一道待填的空去问，
        # 正是 PRD 警告的窥探：心里的事只能从反复提及里看出来，自我评价只收用户
        # 自己说出口的原话（PRD 6.1）。这也是为什么第 11 节不给它们定覆盖指标。
        for section in (s for s in SECTIONS if s not in INWARD_SECTIONS):
            count = sum(1 for c in confirmed if c["section"] == section)
            if count < GAP_MIN_CONFIRMED:
                found.append(_target("gap", key="gap:" + section, question=GAP_QUESTIONS[section], why=GAP_WHY[section],
                                     target_type="section", target_id=section, order=f"{count}:{SECTIONS.index(section)}"))
    # stale：单来源、超过 60 天未重申（≤ 1 条）
    stale = []
    for claim in mine:
        if _sensitive(claim):
            continue
        seen = _parse(claim.get("lastReaffirmed")) or _parse(claim.get("firstSeen"))
        if seen and current - seen >= timedelta(days=STALE_DAYS) and store.evidence_source_count(claim["id"]) <= 1:
            stale.append((seen, claim))
    stale.sort(key=lambda pair: (pair[0], pair[1]["id"]))
    for seen, claim in stale[:1]:
        found.append(_target("stale", key="stale:" + claim["id"], question=f"上次你说「{_clip(claim['content'], 40)}」，现在还是这样吗？",
                             why="这条理解已经有一段时间没再听你提起", ref={"kind": "claim", "id": claim["id"]},
                             target_type="claim", target_id=claim["id"], order=_iso(seen)))
    # tension：待处理的矛盾 / 张力
    for item in store.list_conflicts(status="pending", limit=50):
        a, b = item.get("claimA") or {}, item.get("claimB") or {}
        if not a or not b or not visible(a, convs, scope) or not visible(b, convs, scope) or _sensitive(a) or _sensitive(b):
            continue
        if item.get("kind") == "tension":
            # 库里按 id 排序存 claimA / claimB；张力问句要求原则在前、最近做法在后，按分区恢复语义顺序（确定性）。
            if b.get("section") == "principles" and a.get("section") != "principles":
                a, b = b, a
            question = f"「{_clip(a['content'], 40)}」是你确认过的原则，而最近「{_clip(b['content'], 40)}」——是原则变了，还是这次情况特殊？"
        else:
            question = f"「{_clip(a['content'], 40)}」和「{_clip(b['content'], 40)}」放在一起看有点矛盾——哪一条更接近现在的你？"
        found.append(_target("tension", key="tension:" + item["id"], question=question, why="两条理解放在一起有张力，想听你怎么看",
                             refs=[{"kind": "claim", "id": a["id"]}, {"kind": "claim", "id": b["id"]}],
                             target_type="conflict", target_id=item["id"], order=str(item.get("createdAt") or "")))
    # open_loop：到期判断（→ 回访）、到期承诺、14 天内摘要里的待办
    for decision in growth.list_decisions("open"):
        review_at = _parse(decision.get("reviewAt"))
        if review_at is None or review_at > current or not record_in_scope(decision, convs, scope, growth=growth):
            continue
        found.append(_target("open_loop", key="due:" + decision["id"], question=f"「{_clip(decision.get('title'), 32)}」到了回访的时候，结果怎么样了？",
                             why="到了约好核对结果的时候", ref={"kind": "decision", "id": decision["id"]},
                             target_type="decision", target_id=decision["id"], order=_iso(review_at)))
    for claim in mine:
        due = _parse(claim.get("validTo"))
        if claim.get("predicate") != "committed_to" or due is None or due > current or _sensitive(claim):
            continue
        found.append(_target("open_loop", key="commit:" + claim["id"], question=f"你说过「{_clip(claim['content'], 40)}」，进展怎么样？",
                             why="你给自己的期限到了", ref={"kind": "claim", "id": claim["id"]},
                             target_type="claim", target_id=claim["id"], order=_iso(due)))
    with convs._connect() as db:
        rows = db.execute(
            """SELECT s.conversation_id, MAX(s.revision) FROM conversation_summaries s
               JOIN conversations c ON c.id = s.conversation_id WHERE c.device_scope = ?
               GROUP BY s.conversation_id ORDER BY c.updated_at DESC, s.conversation_id LIMIT 3""", (scope,)).fetchall()
    for cid, revision in rows:
        summary = convs.get_summary(cid, int(revision))
        created = _parse((summary or {}).get("createdAt"))
        if not summary or created is None or current - created > timedelta(days=LOOP_DAYS):
            continue
        for index, point in enumerate(summary.get("keyPoints") or []):
            point = str(point).strip()
            if not point.startswith("待办："):
                continue
            loop = point[3:].strip()
            found.append(_target("open_loop", key=f"loop:{cid}:{revision}:{index}", question=f"上次你说要「{_clip(loop, 40)}」，后来怎么样了？",
                                 why="上次聊到还没做完的事", ref={"kind": "summary", "id": f"{cid}:{revision}"},
                                 target_type="summary", target_id=f"{cid}:{revision}", order="z" + _iso(created)))
    # nod：待确认理解，promotionReady 优先
    for claim in store.inbox(limit=50):
        if not visible(claim, convs, scope) or _sensitive(claim) or claim.get("subjectEntityId") != ME_ENTITY_ID:
            continue
        found.append(_target("nod", key="nod:" + claim["id"], question=f"我印象里{_clip(claim['content'], 40)}，对吗？",
                             why="这是我还没有把握的理解", ref={"kind": "claim", "id": claim["id"]},
                             target_type="claim", target_id=claim["id"],
                             order=("0" if claim.get("promotionReady") else "1") + str(claim.get("lastReaffirmed") or "")))
    # onboarding_topic：建档会话里还没聊到的话题
    onboarding = convs.list_conversations(limit=1, status="all", mode="onboarding", device_scope=scope)
    if onboarding:
        from .charter import topic_progress, TOPICS
        progress = {item["id"]: item for item in topic_progress(convs.list_messages(onboarding[0]["id"]))}
        pending = next((t for t in TOPICS if progress.get(t[0], {}).get("state") == "pending"), None)
        if pending:
            found.append(_target("onboarding_topic", key="topic:" + pending[0], question=pending[2], why="第一次认识时还没聊到「" + pending[1] + "」",
                                 target_type="topic", target_id=pending[0]))

    def allowed(target):
        if target["key"] in cooled:
            return False
        if not proactive and target["kind"] not in ("gap", "nod"):
            return False
        haystack = target["question"] + " " + str(target.get("why") or "")
        return not any(word in haystack for word in quiet)

    found = [t for t in found if allowed(t)]
    found.sort(key=lambda t: (t["priority"], t["order"], t["key"]))
    return found[: max(0, int(limit))]


def pick(store, convs, growth, scope="global", *, now=None):
    """只取 1 条；没有就 None。"""
    found = targets(store, convs, growth, scope, now=now, limit=1)
    return found[0] if found else None


def render(target):
    """≤ 160 字的提示词块。"""
    question = _clip(target["question"], 70)
    why = _clip(target.get("why") or "", 30)
    text = HEADING + "\n- " + question + (f"（{why}）" if why else "")
    if len(text) > BLOCK_LIMIT:
        text = HEADING + "\n- " + question
    if len(text) > BLOCK_LIMIT:
        text = HEADING + "\n- " + _clip(question, max(10, BLOCK_LIMIT - len(HEADING) - 3))
    return text[:BLOCK_LIMIT]


def prompt_block(router, provider, *, purpose="chat", now=None):
    """供 prepare_chat 注入：目标引用的来源逐条核对，未授权就不问（不阻塞对话）。"""
    from fastapi import HTTPException
    from ..stores.growth_store import GrowthStore
    empty = {"text": "", "refs": [], "info": None, "excluded": []}
    service = service_info(provider)["id"]
    for target in targets(router.onto, router.convs, GrowthStore.instance(), router.scope, now=now, limit=6):
        refs, blocked = [], False
        for ref in target["refs"]:
            try:
                closure = router.resolve(ref)
                router.check_lifecycle(closure)
            except (HTTPException, ValueError, KeyError):
                blocked = True
                break
            if any(s["blocked"] for s in closure) or (provider.external and any(not router.allowed(s, service, purpose) for s in closure)):
                blocked = True
                break
            refs.append(closure[0]["ref"])
        if blocked:
            continue
        info = {"kind": target["kind"], "key": target["key"], "targetType": target["targetType"], "targetId": target["targetId"]}
        return {"text": render(target), "refs": refs, "info": info, "excluded": []}
    return empty
