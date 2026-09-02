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
    return {"created": created, "checked": checked}


def today(*, conv_store: ConversationStore | None = None) -> dict:
    conv_store = conv_store or ConversationStore.instance()
    return {"items": conv_store.today_nudges(), "policy": conv_store.nudge_policy()}
