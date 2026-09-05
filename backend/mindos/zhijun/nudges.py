"""提醒：事件触发、每日 ≤ N、必须说明「为何现在」、遵守章程的静默领域、可按主题永久静默。

P2 只做 ``review_due``：判断簿里到期 / 逾期且还没记结果的判断。扫描每小时由本体 worker 触发，
也可通过 ``POST /api/mindos/nudges/scan`` 立即执行。投递只在应用内（对话页顶部条）。
"""
from __future__ import annotations

import logging
import re
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


# 「不希望 / 不要 / 不用 / 别 … 提 / 聊 / 谈 / 碰 / 问」：用户在建档或对话里说过的静默边界。
_QUIET_PATTERN_RE = re.compile(r"(不希望|不要|不用|别)[^。；;]*?(提|聊|谈|碰|问)")
# 去掉句式后剩下的就是话题词；顺序有讲究：长模式先于短模式。
_QUIET_STRIP = (
    "我不希望", "不希望", "AI", "ai", "知君", "主动提起", "主动提", "主动", "这些话题", "这类话题", "的话题", "话题",
    "不用", "不要", "别", "提起", "提", "聊", "谈", "碰", "问", "。", "！", "!", "，", "；", ";", " ",
)
_QUIET_SPLIT_RE = re.compile(r"[和、或与，,]|以及")
_QUIET_FILL_RE = re.compile(r"^(了|的|这些|那些|这类|那类|一些|等|吧|呢|啊)+|(了|的|这些|那些|这类|那类|一些|等|吧|呢|啊)+$")


def quiet_words_from_text(content: str) -> list[str]:
    """从一句静默边界里抽话题词（2 到 8 字）：「我不希望AI主动提起健康和家里的矛盾这些话题」→ 健康 / 家里的矛盾。"""
    text = str(content or "").strip()
    if not text:
        return []
    # 先按分隔符切，再逐段剥句式；「家里的矛盾」里的「的」要保住，所以只剥首尾虚词。
    words: list[str] = []
    for piece in _QUIET_SPLIT_RE.split(text):
        piece = piece.strip()
        for pattern in _QUIET_STRIP:
            piece = piece.replace(pattern, "")
        piece = _QUIET_FILL_RE.sub("", piece).strip()
        if 2 <= len(piece) <= 8 and piece not in words:
            words.append(piece)
    return words


def _is_quiet_boundary(claim: dict) -> bool:
    if str(claim.get("predicate") or "") == "boundary":
        return True
    return bool(_QUIET_PATTERN_RE.search(str(claim.get("content") or "")))


def _quiet_words(charter: dict | None, onto=None, *, scope="global", conversations=None) -> list[str]:
    """静默领域：章程 quietDomains，加上本体 principles 分区里用户说过的「不要主动提」边界。"""
    words: list[str] = []
    if charter:
        words.extend(str(w).strip() for w in (charter.get("quietDomains") or []) if str(w).strip())
        for value in charter.get("quietDomains") or []:
            words.extend(quiet_words_from_text(value))
    try:
        if onto is None:
            from ..stores.ontology_store import OntologyStore

            onto = OntologyStore.instance()
        for claim in onto.list_claims(section="principles", trust_states=("confirmed", "working"), limit=200, include_hidden=False):
            from .alignment import visible
            if not visible(claim, conversations or ConversationStore.instance(), scope):
                continue
            if not _is_quiet_boundary(claim):
                continue
            for word in quiet_words_from_text(claim.get("content") or ""):
                if word not in words:
                    words.append(word)
    except Exception as exc:  # noqa: BLE001 - 本体不可用时只用章程
        logger.debug("从本体抽静默领域失败：%s", type(exc).__name__)
    return [w for w in words if w]


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
    from .charter_policy import scope_policy, check_action, assert_current, record_scope_or_none, basis
    horizon = current + timedelta(days=HORIZON_DAYS)
    created = 0
    checked = 0
    for decision in growth.list_decisions("open"):
        scope = record_scope_or_none(decision, conv_store, growth=growth)
        if scope is None:
            continue
        charter = scope_policy(scope, growth=growth)
        if not check_action(charter, "proactive")["allowed"]:
            continue
        quiet = _quiet_words(charter["charter"], scope=charter["scope"], conversations=conv_store)
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
        assert_current(charter, growth=growth)
        nudge = conv_store.create_nudge(
            kind="review_due",
            trigger_key=trigger_key_for(decision["id"]),
            trigger_ref={"decisionId": decision["id"], "title": title, "charterBasis": basis(charter)},
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
        from .alignment import visible

        onto = OntologyStore.instance()
        for claim in onto.list_claims(section="matters", trust_states=("confirmed", "working"), limit=500, include_hidden=False):
            scope = record_scope_or_none(claim, conv_store, growth=growth)
            if scope is None:
                continue
            charter = scope_policy(scope, growth=growth)
            if not visible(claim, conv_store, charter["scope"]):
                continue
            if not check_action(charter, "proactive")["allowed"]:
                continue
            quiet = _quiet_words(charter["charter"], onto, scope=charter["scope"], conversations=conv_store)
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
            assert_current(charter, growth=growth)
            nudge = conv_store.create_nudge(
                kind="commitment_due",
                trigger_key=f"commitment:{claim['id']}",
                trigger_ref={"claimId": claim["id"], "section": claim["section"], "charterBasis": basis(charter)},
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
            charter = scope_policy("global", growth=growth)
            assert_current(charter, growth=growth)
            nudge = conv_store.create_nudge(
                kind="weekly_review",
                trigger_key=f"weekly:{current.date().isocalendar()[0]}-{current.date().isocalendar()[1]}",
                trigger_ref={"summary": weekly["summary"], "weekStart": weekly["weekStart"], "charterBasis": basis(charter)},
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
    from .charter_policy import scope_policy, check_action, record_in_scope
    from .alignment import visible
    if not check_action(scope_policy("global", growth=growth), "proactive")["allowed"]:
        return None

    onto = OntologyStore.instance()
    week_start = now - timedelta(days=7)
    confirmed_this_week = [
        c for c in onto.list_claims(trust_states=("confirmed",), limit=500)
        if visible(c, conv_store, "global") and (_parse(c.get("lastReaffirmed")) or week_start) >= week_start
    ]
    decisions_this_week = [d for d in growth.list_decisions() if record_in_scope(d, conv_store, "global", growth=growth)
                           and (_parse(d.get("createdAt")) or week_start) >= week_start]
    if len(confirmed_this_week) < 3 and not decisions_this_week:
        return None
    tensions = [t for t in onto.list_conflicts(status="pending", limit=100)
                if visible(t["claimA"], conv_store, "global") and visible(t["claimB"], conv_store, "global")][:3]
    parts = [f"这周你记下了 {len(confirmed_this_week)} 条关于自己的理解"]
    if decisions_this_week:
        parts.append(f"和 {len(decisions_this_week)} 个判断")
    summary = "、".join(parts)
    if tensions:
        t = tensions[0]
        summary += f"；其中「{t['claimA']['content'][:20]}」和「{t['claimB']['content'][:20]}」放在一起有点张力"
    message = f"{summary}——要不要花五分钟一起看看？不想看就划掉，下周不会催。"
    return {"summary": summary, "message": message, "weekStart": _iso(week_start)}


def today(*, conv_store: ConversationStore | None = None, scope="global") -> dict:
    conv_store = conv_store or ConversationStore.instance()
    from .charter_policy import scope_policy, check_action
    if not check_action(scope_policy(scope), "proactive")["allowed"]:
        return {"items": [], "policy": conv_store.nudge_policy()}
    # Hidden reminders are not marked as delivered and do not consume the limit.
    items = conv_store.today_nudges(eligible=lambda n: ((n.get("triggerRef") or {}).get("charterBasis") or {}).get("scope", "global") == scope)
    return {"items": items, "policy": conv_store.nudge_policy()}
