"""知君的主动性（V3 M8）：有理由才找你、有节律、记得自己说过什么。

每小时扫描（``proactive_scan``，与提醒扫描同频）：从触发源里挑最多一条，直接创建一段普通会话并先说一句，
带「为何现在」；出现在会话列表（``initiated``）与今日页（``initiated`` 列表）；回不回都行。

触发源（``candidates``）按优先级：
- 提醒：``review_due`` / ``commitment_due`` / ``principle_tension`` / ``weekly_review``（pending / shown，到了计划时间）。
- 求知：``inquiry.targets`` 里的 ``open_loop`` / ``nod`` / ``stale``；``gap`` 只在认识的前 14 天。
- 关系：认识第 7 / 30 / 100 天；多日没聊的轻问候（``greetAfterDays``，0 = 关）。

节律（``allowed``，策略在 ``conversation_store.nudge_policy()["proactive"]``）：
开关 → 「先别找我」（snoozeUntil）→ 安静时段（本地时间；起止相同视为不设）→ 每日上限 → 最小间隔 →
用户两小时内活跃过就不打扰 → 退避（连续 3 次没回应，7 天只找一次，写回 backoffUntil）。
章程 ``no_proactive`` 与静默领域照旧；同一件事（key）7 天内不重复（``zhijun_proactive_recent_v1``）。

不调模型、不假装有情绪或自己的生活；开场白全部来自 ``persona.proactive_opening`` 模板。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..stores.conversation_store import ConversationStore
from . import persona

logger = logging.getLogger(__name__)

COOLDOWN_DAYS = 7
RECENT_LIMIT = 40
GAP_WINDOW_DAYS = 14
MILESTONES = (7, 30, 100)
ACTIVE_HOURS = 2
BACKOFF_STREAK = 3
BACKOFF_DAYS = 7
NUDGE_KINDS = ("review_due", "commitment_due", "principle_tension", "weekly_review")
INQUIRY_KINDS = ("open_loop", "nod", "stale", "gap")
# 三种张力同一档：它们都是「照见」，彼此之间由 scheduledFor 排先后。
# 排在回访与承诺之后——那两件有确定的时间约定，照见没有。
_PRIORITY = {"review_due": 0, "commitment_due": 1,
             "principle_tension": 2, "self_view_tension": 2, "burden_stalled": 2,
             "weekly_review": 3,
             "open_loop": 4, "milestone": 5, "nod": 6, "stale": 7, "gap": 8, "greeting": 9}
_CACHE_KEY = "zhijun_proactive_recent_v1"


# ---------------------------------------------------------------- 时间
def local_tz():
    """本机时区；测试可替换。"""
    return datetime.now().astimezone().tzinfo


def _now(now=None) -> datetime:
    """naive 视为本地时间；返回 UTC。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_tz())
    return current.astimezone(timezone.utc)


def _local(now: datetime) -> datetime:
    return _now(now).astimezone(local_tz())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minutes(value: str) -> int | None:
    try:
        hours, minutes = str(value).split(":", 1)
        hours, minutes = int(hours), int(minutes)
    except (TypeError, ValueError):
        return None
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        return None
    return hours * 60 + minutes


def in_quiet_hours(local: datetime, quiet: dict | None) -> bool:
    """安静时段按本地时间判断；跨午夜（22:00 → 08:00）也算；起止相同视为不设。"""
    quiet = quiet or {}
    start, end = _minutes(quiet.get("start", "")), _minutes(quiet.get("end", ""))
    if start is None or end is None or start == end:
        return False
    current = local.hour * 60 + local.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


# ---------------------------------------------------------------- 7 天不重复
def _cache_key(scope: str) -> str:
    return _CACHE_KEY if scope == "global" else _CACHE_KEY + ":" + scope


def recent_keys(store, scope: str, *, now=None) -> dict:
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


def _ref_key(ref: dict) -> str:
    return f"ref:{ref.get('kind')}:{ref.get('id')}"


def mark_sent(store, scope: str, key: str, *, refs: list[dict] | None = None, now=None) -> None:
    """记下这次找过他的 key 与引用的对象（同一件事换个入口也算重复）。"""
    if not key:
        return
    current = _now(now)
    recent = recent_keys(store, scope, now=current)
    recent[str(key)] = _iso(current)
    for ref in refs or []:
        if isinstance(ref, dict) and ref.get("kind") and ref.get("id"):
            recent[_ref_key(ref)] = _iso(current)
    ordered = sorted(recent.items(), key=lambda item: item[1], reverse=True)[:RECENT_LIMIT]
    store.meta_set(_cache_key(scope), json.dumps(dict(ordered), ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------- 节律
def _initiated_at(conversation: dict) -> datetime | None:
    info = conversation.get("initiated") or {}
    return _parse(info.get("createdAt")) or _parse(conversation.get("createdAt"))


def allowed(now, policy: dict, convs: ConversationStore, scope: str = "global") -> tuple[bool, str]:
    """现在能不能找他：(允许, 原因)。``policy`` 可以是整份提醒策略，也可以只是其中的 ``proactive`` 对象。"""
    proactive = policy.get("proactive") if isinstance(policy.get("proactive"), dict) else policy
    current = _now(now)
    if not proactive.get("enabled", True):
        return False, "disabled"
    snooze = _parse(proactive.get("snoozeUntil"))
    if snooze and snooze > current:
        return False, "snoozed"
    local = _local(current)
    if in_quiet_hours(local, proactive.get("quietHours")):
        return False, "quiet_hours"
    initiated = convs.list_initiated(device_scope=scope, limit=BACKOFF_STREAK + 10)
    stamped = [(c, at) for c in initiated if (at := _initiated_at(c)) is not None]
    stamped.sort(key=lambda pair: pair[1], reverse=True)
    today = [c for c, at in stamped if at.astimezone(local.tzinfo).date() == local.date()]
    if len(today) >= int(proactive.get("maxPerDay", 2)):
        return False, "daily_cap"
    if stamped and current - stamped[0][1] < timedelta(hours=int(proactive.get("minGapHours", 4))):
        return False, "min_gap"
    last_user = _parse(convs.last_user_message_at(device_scope=scope))
    if last_user and last_user > current - timedelta(hours=ACTIVE_HOURS):
        return False, "user_active"
    # 退避：最近 3 次都没回应 → 7 天只找一次；一旦他回了，退避解除。
    streak = stamped[:BACKOFF_STREAK]
    ignored = len(streak) == BACKOFF_STREAK and all(not (c.get("initiated") or {}).get("answeredAt") for c, _ in streak)
    backoff_until = _parse(proactive.get("backoffUntil"))
    if ignored:
        if backoff_until and backoff_until > current:
            return False, "backoff"
        last_at = streak[0][1]
        if current - last_at < timedelta(days=BACKOFF_DAYS):
            _persist_backoff(convs, _iso(last_at + timedelta(days=BACKOFF_DAYS)))
            return False, "backoff"
    elif backoff_until is not None:
        _persist_backoff(convs, None)
    return True, "ok"


def _persist_backoff(convs: ConversationStore, until: str | None) -> None:
    try:
        convs.save_nudge_policy(proactive={"backoffUntil": until})
    except Exception as exc:  # noqa: BLE001 - 退避只是节律标记，写不进也不影响判断
        logger.debug("写入退避标记失败：%s", type(exc).__name__)


# ---------------------------------------------------------------- 触发源
def _candidate(kind: str, *, key: str, why_now: str, payload: dict, refs: list[dict] | None = None,
               order: str = "", nudge: dict | None = None, from_inquiry: bool = False) -> dict:
    item = {
        "kind": kind,
        "priority": _PRIORITY[kind],
        "key": key,
        "whyNow": why_now,
        "opening": persona.proactive_opening(kind, payload),
        "title": persona.proactive_title(kind, payload),
        "refs": [dict(r) for r in (refs or []) if isinstance(r, dict)],
        "payload": payload,
        "order": order,
        "inquiry": from_inquiry,
    }
    if nudge is not None:
        item["nudgeId"] = nudge["id"]
        item["triggerKey"] = nudge["triggerKey"]
    return item


def _from_nudge(nudge: dict, store, growth) -> dict | None:
    ref = nudge.get("triggerRef") or {}
    kind = nudge["kind"]
    key = "nudge:" + str(nudge.get("triggerKey") or nudge["id"])
    if kind == "review_due":
        decision = growth.get_decision(str(ref.get("decisionId") or "")) if ref.get("decisionId") else None
        if not decision or decision.get("status") != "open":
            return None
        payload = {"title": decision.get("title") or ref.get("title") or "", "decisionId": decision["id"]}
        return _candidate(kind, key=key, why_now=nudge["whyNow"], payload=payload,
                          refs=[{"kind": "decision", "id": decision["id"]}], order=str(nudge.get("scheduledFor") or ""), nudge=nudge)
    if kind == "commitment_due":
        claim = store.get_claim(str(ref.get("claimId") or ""), with_evidence=False) if ref.get("claimId") else None
        if not claim or claim.get("trustState") not in ("confirmed", "working"):
            return None
        due = _parse(claim.get("validTo"))
        payload = {"content": claim["content"], "date": due.astimezone(local_tz()).date().isoformat() if due else "", "claimId": claim["id"]}
        return _candidate(kind, key=key, why_now=nudge["whyNow"], payload=payload,
                          refs=[{"kind": "claim", "id": claim["id"]}], order=str(nudge.get("scheduledFor") or ""), nudge=nudge)
    if kind == "principle_tension":
        refs, payload = [], {"message": nudge.get("message") or ""}
        ids = [str(i) for i in (ref.get("claimIds") or [ref.get("claimA"), ref.get("claimB")]) if i]
        claims = [c for c in (store.get_claim(i, with_evidence=False) for i in ids) if c and c.get("trustState") in ("confirmed", "working")]
        if len(claims) == 2:
            payload.update(a=claims[0]["content"], b=claims[1]["content"])
        refs = [{"kind": "claim", "id": c["id"]} for c in claims]
        return _candidate(kind, key=key, why_now=nudge["whyNow"], payload=payload, refs=refs, order=str(nudge.get("scheduledFor") or ""), nudge=nudge)
    if kind == "self_view_tension":
        # 照见 A（言行不一）。两条理解任意一条被撤回或改写，这条照见就不该再出现
        # ——撤回的永不回流（原则 3）。
        view = store.get_claim(str(ref.get("selfViewId") or ""), with_evidence=False) if ref.get("selfViewId") else None
        behaviour = store.get_claim(str(ref.get("behaviourId") or ""), with_evidence=False) if ref.get("behaviourId") else None
        if not view or not behaviour or view.get("trustState") != "confirmed" or behaviour.get("trustState") != "confirmed":
            return None
        payload = {"a": view["content"], "b": behaviour["content"], "message": nudge.get("message") or ""}
        return _candidate(kind, key=key, why_now=nudge["whyNow"], payload=payload,
                          refs=[{"kind": "claim", "id": view["id"]}, {"kind": "claim", "id": behaviour["id"]}],
                          order=str(nudge.get("scheduledFor") or ""), nudge=nudge)
    if kind == "burden_stalled":
        # 照见 B（说了没动）。消息里已经写好了次数与跨度这两个事实，以及可能的
        # 「把人推回人」那一句，直接用，不在这里重新拼一遍。
        burden = store.get_claim(str(ref.get("burdenId") or ""), with_evidence=False) if ref.get("burdenId") else None
        if not burden or burden.get("trustState") != "confirmed":
            return None
        payload = {"content": burden["content"], "message": nudge.get("message") or "",
                   "talkToEntityId": ref.get("talkToEntityId")}
        return _candidate(kind, key=key, why_now=nudge["whyNow"], payload=payload,
                          refs=[{"kind": "claim", "id": burden["id"]}],
                          order=str(nudge.get("scheduledFor") or ""), nudge=nudge)
    if kind == "weekly_review":
        payload = {"summary": ref.get("summary") or "", "weekStart": ref.get("weekStart")}
        return _candidate(kind, key=key, why_now=nudge["whyNow"], payload=payload, refs=[], order=str(nudge.get("scheduledFor") or ""), nudge=nudge)
    return None


def relationship_days(store, convs: ConversationStore, growth, scope: str = "global", *, now=None) -> int:
    """与首页 ``map.relationshipDays`` 同一算法、同一取材。"""
    current = _now(now)
    from ..zhijun_home import _relationship_days
    from .alignment import visible
    from .charter_policy import record_in_scope
    conversations = convs.list_conversations(limit=50, status="all", device_scope=scope)
    if not any(item.get("mode") == "onboarding" for item in conversations):
        conversations.extend(convs.list_conversations(limit=1, status="all", mode="onboarding", device_scope=scope))
    claims = [c for c in store.list_claims(trust_states=("confirmed", "working"), limit=500, include_hidden=False) if visible(c, convs, scope)]
    decisions = [d for d in growth.list_decisions() if record_in_scope(d, convs, scope, growth=growth)]
    return _relationship_days(conversations, claims, decisions, current)


def _milestone_line(store, convs: ConversationStore, scope: str) -> dict | None:
    """周年那句里引用的一条：最近重申过、已确认、不敏感、关于他自己。"""
    from ..stores.ontology_store import ME_ENTITY_ID
    from .alignment import visible
    for claim in store.list_claims(trust_states=("confirmed",), limit=50, include_hidden=False):
        if claim.get("subjectEntityId") != ME_ENTITY_ID or claim.get("privacyLevel") in ("sensitive", "restricted"):
            continue
        if claim.get("scope") == "context_only" or not visible(claim, convs, scope):
            continue
        return claim
    return None


def candidates(now, *, store, convs: ConversationStore, growth, scope: str = "global") -> list[dict]:
    """排序后的候选（已应用章程 no_proactive、静默词、7 天不重复）。每条：kind / priority / whyNow / opening / title / refs / key。"""
    current = _now(now)
    from . import inquiry
    from .charter_policy import scope_policy, check_action
    from .nudges import _quiet_words
    charter = scope_policy(scope, growth=growth)
    if not check_action(charter, "proactive")["allowed"]:
        return []
    quiet = [w for w in _quiet_words(charter["charter"], store, scope=scope, conversations=convs) if w]
    cooled = recent_keys(store, scope, now=current)
    found: list[dict] = []
    used_refs: set[tuple[str, str]] = set()

    # 提醒：pending / shown、到了计划时间、属于本 scope。
    for nudge in convs.list_nudges(statuses=("pending", "shown"), limit=50):
        if nudge["kind"] not in NUDGE_KINDS:
            continue
        basis = (nudge.get("triggerRef") or {}).get("charterBasis") or {}
        if basis.get("scope", "global") != scope:
            continue
        scheduled = _parse(nudge.get("scheduledFor"))
        if scheduled and scheduled > current:
            continue
        try:
            item = _from_nudge(nudge, store, growth)
        except Exception as exc:  # noqa: BLE001 - 单条提醒取材失败不影响其它候选
            logger.debug("提醒候选取材失败：%s", type(exc).__name__)
            item = None
        if item is not None:
            found.append(item)
            used_refs.update((r["kind"], r["id"]) for r in item["refs"])

    # 求知：open_loop / nod / stale；gap 只在认识的前 14 天。
    try:
        days = relationship_days(store, convs, growth, scope, now=current)
    except Exception as exc:  # noqa: BLE001
        logger.debug("关系天数计算失败：%s", type(exc).__name__)
        days = 0
    try:
        targets = inquiry.targets(store, convs, growth, scope, now=current, limit=12)
    except Exception as exc:  # noqa: BLE001
        logger.debug("求知目标取材失败：%s", type(exc).__name__)
        targets = []
    for target in targets:
        kind = target["kind"]
        if kind not in INQUIRY_KINDS or (kind == "gap" and days > GAP_WINDOW_DAYS):
            continue
        refs = [r for r in (target.get("refs") or []) if isinstance(r, dict)]
        if any((r.get("kind"), r.get("id")) in used_refs for r in refs):
            continue
        payload = {"question": target["question"], "why": target.get("why") or "", "targetType": target.get("targetType"), "targetId": target.get("targetId")}
        if kind == "open_loop" and str(target["key"]).startswith("loop:"):
            question = str(target["question"])
            if "「" in question and "」" in question:
                payload["loop"] = question.split("「", 1)[1].split("」", 1)[0]
        found.append(_candidate(kind, key=target["key"], why_now=target.get("why") or "", payload=payload, refs=refs,
                                order=str(target.get("order") or ""), from_inquiry=True))
        used_refs.update((r.get("kind"), r.get("id")) for r in refs)

    # 关系：认识第 7 / 30 / 100 天。
    if days in MILESTONES:
        line = None
        try:
            line = _milestone_line(store, convs, scope)
        except Exception as exc:  # noqa: BLE001
            logger.debug("周年引用取材失败：%s", type(exc).__name__)
        payload = {"n": days, "line": line["content"] if line else None}
        found.append(_candidate("milestone", key=f"milestone:{days}", why_now=f"今天是我们认识的第 {days} 天", payload=payload,
                                refs=[{"kind": "claim", "id": line["id"]}] if line else []))

    # 关系：多日没聊的轻问候（greetAfterDays = 0 关闭；从没聊过不算）。
    try:
        greet_after = int((convs.nudge_policy().get("proactive") or {}).get("greetAfterDays") or 0)
    except Exception:  # noqa: BLE001
        greet_after = 0
    last_user = _parse(convs.last_user_message_at(device_scope=scope))
    if greet_after > 0 and last_user and current - last_user >= timedelta(days=greet_after):
        idle = (current - last_user).days
        found.append(_candidate("greeting", key="greeting", why_now=f"已经 {idle} 天没聊了", payload={"days": idle}))

    def keep(item: dict) -> bool:
        if item["key"] in cooled or any(_ref_key(r) in cooled for r in item["refs"]):
            return False
        haystack = item["opening"] + " " + item["title"] + " " + item["whyNow"]
        return not any(word in haystack for word in quiet)

    found = [item for item in found if keep(item)]
    found.sort(key=lambda item: (item["priority"], item["order"], item["key"]))
    return found


# ---------------------------------------------------------------- 动作
def _verify_refs(refs: list[dict], store, convs: ConversationStore, scope: str) -> tuple[list[dict], bool]:
    """逐条核实引用：返回（此刻可核实的带版本引用, 是否全部可核实）。不可追溯的引用不写进历史。"""
    if not refs:
        return [], True
    from .routing import Router
    verified: list[dict] = []
    complete = True
    try:
        router = Router(store, convs, "scope:" + scope)
    except Exception as exc:  # noqa: BLE001
        logger.debug("主动对话路由器不可用：%s", type(exc).__name__)
        return [], False
    for ref in refs:
        try:
            closure = router.resolve(ref)
        except Exception as exc:  # noqa: BLE001 - HTTPException / ValueError 都视为不可核实
            logger.debug("主动对话来源核实失败：%s", type(exc).__name__)
            complete = False
            continue
        if any(node.get("blocked") for node in closure):
            complete = False
            continue
        verified.append(closure[0]["ref"])
    return verified, complete


def _inquiry_keys(item: dict) -> list[str]:
    if item.get("inquiry"):
        return [item["key"]]
    payload = item.get("payload") or {}
    if item["kind"] == "review_due" and payload.get("decisionId"):
        return ["due:" + str(payload["decisionId"])]
    if item["kind"] == "commitment_due" and payload.get("claimId"):
        return ["commit:" + str(payload["claimId"])]
    return []


def run(now=None, *, store=None, convs: ConversationStore | None = None, growth=None, scope: str = "global") -> dict:
    """挑最多一条候选，创建会话并先说一句。返回 {created: 0|1, reason, conversationId?}。"""
    from ..stores.growth_store import GrowthStore
    from ..stores.ontology_store import OntologyStore
    store = store or OntologyStore.instance()
    convs = convs or ConversationStore.instance()
    growth = growth or GrowthStore.instance()
    current = _now(now)
    ok, reason = allowed(current, convs.nudge_policy(), convs, scope)
    if not ok:
        return {"created": 0, "reason": reason}
    from . import inquiry
    from .charter_policy import scope_policy, check_action, assert_current, basis
    charter = scope_policy(scope, growth=growth)
    if not check_action(charter, "proactive")["allowed"]:
        return {"created": 0, "reason": "charter_no_proactive"}
    found = candidates(current, store=store, convs=convs, growth=growth, scope=scope)
    if not found:
        return {"created": 0, "reason": "no_candidate"}
    for top in found:
        sources, complete = _verify_refs(top["refs"], store, convs, scope)
        # 提醒类候选（用户已在应用内看过同一条提醒）沿用回访开场的先例：来源不可恢复也开口，只是不写不可追溯的引用；
        # 引用画像 / 摘要的求知与周年候选则和普通开场一样，来源不可核实就换下一条。
        if not complete and not top.get("nudgeId"):
            continue
        initiated = {"by": "zhijun", "kind": top["kind"], "whyNow": top["whyNow"], "createdAt": _iso(current), "answeredAt": None}
        decision_id = top["payload"].get("decisionId") if top["kind"] == "review_due" else None
        conversation = convs.create_conversation(mode="chat", title=top["title"], device_scope=scope, decision_id=decision_id, initiated=initiated)
        try:
            # 与 API 创建的会话一致：沿用本 scope 的默认路由模式。
            from ..stores.routing_store import RoutingStore
            routing = RoutingStore(store)
            default = routing.mode("default:" + scope)
            if default["mode"] != "legacy":
                routing.set_mode(conversation["id"], default["mode"], default["service"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("主动会话路由默认值写入失败：%s", type(exc).__name__)
        meta = {
            "kind": "zhijun_initiated",
            "reason": top["kind"],
            "whyNow": top["whyNow"],
            "routingOrigin": {"service": "", "external": False},
            "routingSources": sources,
            "charterBasis": basis(charter),
        }
        if top.get("nudgeId"):
            meta["nudgeId"] = top["nudgeId"]
        assert_current(charter, growth=growth)
        message = convs.append_message(conversation["id"], "assistant", top["opening"], provider="template", model="template", meta=meta)
        if top.get("triggerKey"):
            convs.act_nudges(top["triggerKey"])
        mark_sent(store, scope, top["key"], refs=top["refs"], now=current)
        # 求知引擎的冷却一起记上：新开的普通对话开场不再问同一件事。
        for key in _inquiry_keys(top):
            inquiry.mark_asked(store, scope, key, now=current)
        return {"created": 1, "reason": top["kind"], "conversationId": conversation["id"], "messageId": message["id"], "kind": top["kind"]}
    return {"created": 0, "reason": "sources_unavailable"}
