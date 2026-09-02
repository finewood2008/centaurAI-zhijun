"""提醒：事件触发、每日 ≤ N、必须说明「为何现在」、遵守章程的静默领域、可按主题永久静默。

P2 只做 ``review_due``：判断簿里到期 / 逾期且还没记结果的判断。扫描每小时由本体 worker 触发，
也可通过 ``POST /api/mindos/nudges/scan`` 立即执行。投递只在应用内（对话页顶部条）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..stores.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

HORIZON_DAYS = 1
DEDUPE_DAYS = 3


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


def _quiet_words(charter: dict | None) -> list[str]:
    if not charter:
        return []
    return [str(w).strip() for w in (charter.get("quietDomains") or []) if str(w).strip()]


def trigger_key_for(decision_id: str) -> str:
    return f"review_due:{decision_id}"


def scan(*, conv_store: ConversationStore | None = None, growth=None, now: datetime | None = None) -> dict:
    conv_store = conv_store or ConversationStore.instance()
    if growth is None:
        from ..stores.growth_store import GrowthStore

        growth = GrowthStore.instance()
    policy = conv_store.nudge_policy()
    current = (now or _now()).astimezone(timezone.utc)
    if not policy["enabled"]:
        return {"created": 0, "checked": 0, "skipped": "disabled"}
    quiet = _quiet_words(growth.current_charter())
    horizon = current + timedelta(days=HORIZON_DAYS)
    created = 0
    checked = 0
    for decision in growth.list_decisions("open"):
        review_at = _parse(decision.get("reviewAt"))
        if review_at is None or review_at > horizon:
            continue
        checked += 1
        title = str(decision.get("title") or "")
        if any(word and word in title for word in quiet):
            continue
        days = (current - review_at).days
        if days > 0:
            why_now = f"这个判断的回访时间是 {review_at.date().isoformat()}，已经过了 {days} 天"
        elif review_at <= current:
            why_now = f"今天（{review_at.date().isoformat()}）是你当时定的回访日"
        else:
            why_now = f"明天（{review_at.date().isoformat()}）是你当时定的回访日"
        message = (
            f"「{title}」到了回访的时候：当时你选了「{decision.get('choice', '')}」，"
            f"预期「{str(decision.get('expectedOutcome', ''))[:80]}」。结果怎么样了？"
        )
        nudge = conv_store.create_nudge(
            kind="review_due",
            trigger_key=trigger_key_for(decision["id"]),
            trigger_ref={"decisionId": decision["id"], "title": title},
            why_now=why_now,
            message=message,
            scheduled_for=_iso(min(review_at, current)),
            dedupe_days=DEDUPE_DAYS,
            now=_iso(current),
        )
        if nudge is not None:
            created += 1
    # ---- 承诺回访：committed_to 且带 valid_to 的已确认 / 待确认理解，到期前一天与当天各提醒一次（去重 3 天）。
    try:
        from ..stores.ontology_store import OntologyStore

        onto = OntologyStore.instance()
        for claim in onto.list_claims(section="matters", trust_states=("confirmed", "working"), limit=500, include_hidden=False):
            if claim.get("predicate") != "committed_to" or not claim.get("validTo"):
                continue
            due = _parse(claim["validTo"])
            if due is None or due > horizon:
                continue
            if any(word and word in claim["content"] for word in quiet):
                continue
            days = (current - due).days
            why_now = f"你说过「{claim['content'][:40]}」，{'已经过了 ' + str(days) + ' 天' if days > 0 else ('今天' if due <= current else '明天') + '就是你自己定的期限'}"
            checked += 1
            nudge = conv_store.create_nudge(
                kind="commitment_due",
                trigger_key=f"commitment:{claim['id']}",
                trigger_ref={"claimId": claim["id"], "section": claim["section"]},
                why_now=why_now,
                message=f"「{claim['content'][:50]}」——进展怎么样？不想聊也可以先划掉。",
                scheduled_for=_iso(min(due, current)),
                dedupe_days=DEDUPE_DAYS,
                now=_iso(current),
            )
            if nudge is not None:
                created += 1
    except Exception as exc:  # noqa: BLE001 - 本体侧异常不影响其它提醒
        logger.debug("承诺提醒扫描失败：%s", type(exc).__name__)

    # ---- 每周回顾：只在周日（或 ZHIJUN_WEEKLY_ANYDAY=1 时任意日）触发一次；本周有 ≥ 3 条新确认理解或 ≥ 1 个判断；不含打卡语言。
    try:
        import os

        weekly_allowed = current.weekday() == 6 or os.environ.get("ZHIJUN_WEEKLY_ANYDAY", "").strip() == "1"
        weekly = weekly_review_candidate(conv_store=conv_store, growth=growth, now=current) if weekly_allowed else None
        if weekly is not None:
            nudge = conv_store.create_nudge(
                kind="weekly_review",
                trigger_key=f"weekly:{current.date().isocalendar()[0]}-{current.date().isocalendar()[1]}",
                trigger_ref={"summary": weekly["summary"], "weekStart": weekly["weekStart"]},
                why_now="一周过去了，你记下的东西攒了一些",
                message=weekly["message"],
                scheduled_for=_iso(current),
                dedupe_days=7,
                now=_iso(current),
            )
            if nudge is not None:
                created += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("每周回顾扫描失败：%s", type(exc).__name__)
    return {"created": created, "checked": checked}


def weekly_review_candidate(*, conv_store: ConversationStore, growth, now: datetime) -> dict | None:
    """本周的素材够不够开一次回顾：≥3 条新确认理解或 ≥1 个新判断；有张力就顺带点出来。"""
    from ..stores.ontology_store import OntologyStore

    onto = OntologyStore.instance()
    week_start = now - timedelta(days=7)
    confirmed_this_week = [
        c for c in onto.list_claims(trust_states=("confirmed",), limit=500)
        if (_parse(c.get("lastReaffirmed")) or week_start) >= week_start
    ]
    decisions_this_week = [d for d in growth.list_decisions() if (_parse(d.get("createdAt")) or week_start) >= week_start]
    if len(confirmed_this_week) < 3 and not decisions_this_week:
        return None
    tensions = onto.list_conflicts(status="pending", limit=3)
    parts = [f"这周你记下了 {len(confirmed_this_week)} 条关于自己的理解"]
    if decisions_this_week:
        parts.append(f"和 {len(decisions_this_week)} 个判断")
    summary = "、".join(parts)
    if tensions:
        t = tensions[0]
        summary += f"；其中「{t['claimA']['content'][:20]}」和「{t['claimB']['content'][:20]}」放在一起有点张力"
    message = f"{summary}——要不要花五分钟一起看看？不想看就划掉，下周不会催。"
    return {"summary": summary, "message": message, "weekStart": _iso(week_start)}


def today(*, conv_store: ConversationStore | None = None) -> dict:
    conv_store = conv_store or ConversationStore.instance()
    return {"items": conv_store.today_nudges(), "policy": conv_store.nudge_policy()}
