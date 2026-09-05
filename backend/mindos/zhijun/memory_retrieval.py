"""Bounded, read-only claim recall from an already-permitted conversation context.

The caller must supply only messages allowed for this task, then check the returned
claims' scope, lifecycle and external grants. Search hints never become evidence.
No provider calls, model loading, downloads, storage writes or alignment updates.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from ..stores.ontology_store import SECTIONS, lexical_similarity, tokenize
from .memory_index import CACHE as _CACHE, scores as _index_scores


# Deliberately small recall vocabulary, not an intent/personality classifier.
_TOPICS = (
    ("自主", "自主权", "自己决定", "自己做主", "决定权", "独立决策", "掌控方向"),
    ("工作", "项目", "职业", "事业", "研发", "产品", "创业"),
    ("家庭", "家人", "孩子", "女儿", "儿子", "亲子"),
    ("冲突", "拒绝", "边界", "不同意", "额外请求", "表达需要"),
    ("公开表达", "分享", "演讲", "发言", "临场", "即兴", "准备时间"),
    ("犹豫", "纠结", "取舍", "选择", "权衡"),
    ("疲惫", "疲劳", "精力", "休息", "睡眠", "熬夜"),
)
_STOP = set("我 你 我们 你们 什么 怎么 如何 哪些 还有 这个 这些 那个 那些 事情 这样 那样 一个 一些 目前 现在 觉得 可以 就是 还是 没有 已经 自己 时候 问题 了解 认识 理解 告诉 需要 希望 知道 知君 继续 刚才 帮我 可能 觉得".split())
_STOP.update("会不会 不会 能不能 不能 是否 要不要 有没有 能否 可以吗 怎么办 为什么 怎样 多少 不确定 确定 担心".split())
_FOLLOWUP = re.compile(r"^(?:那(?:么)?(?:[，,？?]|$|怎么|如何|还有|这个|这件|你说|我呢|是否|要怎么|该怎么|应该|能不能|是不是|为什么|然后)|这样|接着|继续|还有|这个|这件|这些|它|刚才|前面|上面|你说的|再说|怎么办|怎么做|为什么会|比如呢|然后呢|(?:帮我)?看看(?:哪些|哪里|还|这|刚才))")
_NEW_TOPIC = re.compile(r"换个话题|换一(?:个)?话题|另外问|不说这个|先不聊这个|聊(?:点|聊)?别的|新话题")
_OVERVIEW = re.compile(
    r"(?:你(?:现在|目前)?(?:了解我(?:什么|多少|哪些)?|怎么看我(?:这个人)?|如何看我|眼中的我)|"
    r"我的本体(?:上|里)?(?:有哪些|有什么|是什么)|"
    r"你(?:目前)?对我(?:有什么|有哪些)(?:了解|理解)|"
    r"你(?:目前)?对我的(?:理解|认识|了解)|"
    r"关于我的理解(?:有哪些|有什么))(?=$|[，。？！?])"
)


def is_followup(content: str) -> bool:
    """Conservative explicit continuation, not merely any short user message."""
    text = str(content or "").strip()[:1000]
    text = re.sub(r"^(?:对|好|好的|是的|嗯|可以)[，,。！!\s]+", "", text)
    return not _NEW_TOPIC.search(text) and bool(_FOLLOWUP.search(text))


def is_self_overview(content: str, intent: str = "conversation") -> bool:
    """Shared explicit-overview detection for routing and provenance labels."""
    return intent == "self_overview" or bool(_OVERVIEW.search(str(content or "").strip()[:1000]))


def conversation_query(content: str, recent_messages: list[dict]) -> str:
    """Return <= 2,322 characters of current text and explicit follow-up hints.

    A self-contained new question does not inherit an unrelated earlier topic.
    ``recent_messages`` is chronological, excludes this turn, and is pre-filtered
    by the caller's permissions/history cutoff. System/tool messages are ignored.
    """
    from .memory_context import build_focus
    return build_focus(content, recent_messages)["query"]


def _tokens(text: str) -> set[str]:
    return tokenize(text) - _STOP


def _topics(text: str) -> set[int]:
    return {i for i, words in enumerate(_TOPICS) if any(word in text for word in words)}


def _date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def _eligible(ontology, conversations=None, scope=None, retrospective=False):
    """Apply scope and lifecycle before any result cap or embedding operation."""
    states = ("confirmed", "working", "superseded") if retrospective else ("confirmed", "working")
    rows = ontology.list_claims(trust_states=states, include_hidden=False, limit=-1)
    if scope is not None and conversations is None:
        return []
    known, messages, materials = {}, {}, {}
    if scope is not None:
        origins = {e["conversationId"] for c in rows for e in c.get("evidence", []) if e.get("conversationId")}
        message_ids = {e["messageId"] for c in rows for e in c.get("evidence", []) if e.get("messageId")}
        with conversations._connect() as db:
            for ids, table, fields, target in ((origins, "conversations", "id,device_scope", known),
                                               (message_ids, "messages", "id,conversation_id", messages)):
                values = sorted(ids)
                for offset in range(0, len(values), 400):
                    batch = values[offset:offset + 400]
                    found = db.execute(f"SELECT {fields} FROM {table} WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch)
                    target.update({r[0]: r[1] for r in found})
    now, result = datetime.now(timezone.utc), []
    for c in rows:
        if c.get("trustState") not in states or c.get("challenged"):
            continue
        deferred = _date(c.get("deferredUntil"))
        if deferred and deferred > now:
            continue
        if scope is not None:
            owner = c.get("deviceScope", "global")
            if owner != "global" and owner != scope:
                continue
            evidence = c.get("evidence", [])
            origins = {e["conversationId"] for e in evidence if e.get("conversationId")}
            if (origins and any(known.get(cid) != scope for cid in origins)) or (not origins and scope != owner):
                continue
            if any(e.get("messageId") and messages.get(e["messageId"]) != e.get("conversationId") for e in evidence):
                continue
            available = True
            for e in evidence:
                if not e.get("materialId"):
                    continue
                ident = e["materialId"]
                if ident not in materials:
                    from ..chat_imports import require_material
                    try:
                        require_material(ident, scope)
                        materials[ident] = True
                    except Exception:
                        materials[ident] = False
                available = available and materials[ident]
            if not available:
                continue
        start, end = _date(c.get("validFrom")), _date(c.get("validTo"))
        historical = c.get("trustState") == "superseded" or bool(end and end < now)
        if historical and not retrospective:
            continue
        if start and start > now and c.get("layer") != "aspirational" and not retrospective:
            continue
        result.append({**c, "temporalStatus": "historical" if historical else "future" if start and start > now else "current"})
    return result


def _search_fields(ontology, candidates, scope):
    # Only aliases attached to eligible records in this device's entity scope.
    entity_ids = {c.get(k) for c in candidates for k in ("subjectEntityId", "objectEntityId")} - {None, "ent_me"}
    aliases = {}
    if entity_ids:
        with ontology._connect() as db:
            values = sorted(entity_ids)
            for offset in range(0, len(values), 400):
                batch = values[offset:offset + 400]
                sql = "SELECT e.id,a.alias FROM entities e JOIN entity_aliases a ON a.entity_id=e.id WHERE e.status='active' AND e.id IN (" + ",".join("?" for _ in batch) + ")"
                if scope is not None:
                    sql += " AND e.device_scope=?"
                    batch = [*batch, scope]
                for row in db.execute(sql, batch):
                    aliases.setdefault(row[0], []).append(row[1])
    result = {}
    for c in candidates:
        details = c.get("contextual") or {}
        names = [c.get("objectName") or "", c.get("subjectName") or ""]
        names += [name for key in ("subjectEntityId", "objectEntityId") for name in aliases.get(c.get(key), [])]
        result[c["id"]] = {"content": str(c.get("content") or ""),
            "entities": " ".join(sorted({n for n in names if n and n != "我"})),
            "situation": str(details.get("situation") or "")[:1000],
            "exceptions": str(details.get("exceptions") or "")[:500],
            "time": " ".join(str(c.get(k) or "") for k in ("validFrom", "validTo"))}
    return result


def confirmed_background(ontology, *, conversations=None, scope=None, limit=4, budget=600):
    """Confirmed identity/role/background candidates, not personality predictions.

    Larger explicit budgets support caller-side authorization/backfill; the
    default final capsule remains at most four records and 600 characters.
    """
    limit, budget = max(0, min(int(limit), 120)), max(0, min(int(budget), 12000))
    result, used = [], 0
    for c in _eligible(ontology, conversations, scope):
        if (c.get("trustState") != "confirmed" or c.get("section") != "who"
                or c.get("subjectEntityId") != "ent_me"
                or c.get("predicate") not in ("is", "role", "background")
                or c.get("layer") not in ("observed", "self_declared") or c.get("scope") == "context_only"
                or (c.get("contextual") or {}).get("framing") in ("current", "context_only")):
            continue
        cost = len(c["content"])
        if used + cost > budget:
            continue
        if len(result) >= limit:
            break
        result.append({**c, "score": 0.0, "retrievalReason": "confirmed-background", "retrievalMethod": "identity-role"})
        used += cost
    return result


def retrieve_claims(ontology, content: str, recent_messages: list[dict], intent: str = "conversation", limit: int = 4,
                    *, conversations=None, scope=None, queries=None, focus=None, retrospective=False) -> list[dict]:
    """Return copied original Claims plus score/retrievalReason/retrievalMethod.

    Explicit self-overviews cover confirmed sections. Ordinary recall also admits
    working hypotheses (still labelled working). Alignment only nudges already
    relevant candidates, never grants relevance; repetition is not scored.
    """
    limit = max(0, min(int(limit), 120))
    if not limit:
        return []
    current = str(content or "").strip()[:1000]
    from .memory_context import build_focus
    focus = focus or build_focus(current, recent_messages)
    retrospective = retrospective or intent == "retrospective" or focus.get("mode") == "retrospective"
    candidates = _eligible(ontology, conversations, scope, retrospective)
    overview = is_self_overview(current, intent)
    if overview:
        result = []
        # Query each section separately: a large active section must not crowd out
        # a small/older one before diversification even starts.
        sections = [[c for c in candidates if c.get("section") == section and c.get("trustState") == "confirmed"] for section in SECTIONS]
        for position in range(limit):
            for claims in sections:
                if position < len(claims):
                    result.append({**claims[position], "score": 1.0, "retrievalReason": "overview", "retrievalMethod": "section-coverage"})
                    if len(result) == limit:
                        return result
        return result
    query = str(focus.get("query") or current)[:2322]
    extra = [q[:400] for q in (queries or [])[:3] if isinstance(q, str) and q.strip()]
    if extra:
        query = "\n".join([query, *extra])[:3525]
    current_tokens, query_tokens = _tokens(current), _tokens(query)
    if not query_tokens:
        return []
    current_topics, query_topics = _topics(current), _topics(query)
    ranked = []
    fields = _search_fields(ontology, candidates, scope)
    for claim in candidates:
        values = fields[claim["id"]]
        text = "\n".join(v for v in values.values() if v)[:1800]
        tokens, topics = _tokens(text), _topics(text)
        direct = lexical_similarity(current_tokens, tokens) + .22 * bool(current_topics & topics)
        if query != current and not current_topics and len(current_tokens) <= 2:
            direct = 0.0  # "还有哪些没确定" must not outrank its real topic via one generic word.
        contextual = lexical_similarity(query_tokens, tokens) + .22 * bool(query_topics & topics)
        score = max(direct, .8 * contextual)
        reason = "continuation" if query != current and .8 * contextual > direct else "current"
        matched = [key for key, value in values.items() if query_tokens & _tokens(value)]
        entity_match = any(len(name) >= 2 and name in query for name in values["entities"].split())
        if entity_match:
            score += .25
        ranked.append((score, reason, claim, matched))
    ranked.sort(key=lambda row: (-row[0], row[2]["id"]))
    namespace = (str(getattr(ontology, "_db_path", id(ontology))), scope)
    semantic = _index_scores(namespace, query,
        {c["id"]: "\n".join(v for v in fields[c["id"]].values() if v)[:1800] for c in candidates},
        {c["id"]: json.dumps({k: c.get(k) for k in ("trustState", "scope", "contextRef", "evidence", "selfAlignment", "privacyLevel", "updatedAt")}, ensure_ascii=False, sort_keys=True) for c in candidates})
    result = []
    for score, reason, claim, matched in ranked:
        embedding = semantic.get(claim["id"], 0)
        method = "lexical-topic"
        if embedding >= .70 and embedding * .5 > score:
            score, method = embedding * .5, "loaded-embedding"
            reason = "continuation" if query != current else "current"
        if score < .12:
            continue
        a = claim.get("selfAlignment") or {}
        if claim.get("trustState") == "confirmed" and a.get("framing") == "long_term":
            score += min(.05, max(0, a.get("level") or 0) * .01)
        result.append({**claim, "score": round(score, 5), "retrievalReason": reason, "retrievalMethod": method,
                       "matchedFields": matched})
    result.sort(key=lambda c: (-c["score"], c["id"]))
    return result[:limit]
