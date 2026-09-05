"""Read-only candidate adapters; resolving, consent and final budgets live in ContextPlan."""
from __future__ import annotations

import json
import heapq
import os
import sys

from fastapi import HTTPException

from ..stores.ontology_store import lexical_similarity, tokenize
from ..stores.alignment_store import digest
from .charter_policy import record_in_scope
from .context_lookup import strip_citation_markers

_STOP = set("我 你 我们 现在 这个 那个 什么 怎么 如何 哪些 还有 可以 希望 需要 知君 帮我 自己 觉得 事情 问题 之前 相关".split())


def claim_ref(router, claim):
    # Match Router's source digest at the read, not after ranking or rendering.
    version = digest({k: claim.get(k) for k in (
        "content", "trustState", "scope", "contextual", "selfAlignment", "evidence", "privacyLevel",
        "section", "layer", "subjectEntityId", "subjectName", "predicate", "objectEntityId", "objectName",
        "validFrom", "validTo")})
    kind = "claim" if claim["trustState"] in ("confirmed", "working") else "claim_history"
    return router.ref(kind, claim["id"], version=version)


def message_ref(router, message):
    content = message["content"]
    meta = message.get("meta") or {}
    marker = "[用户从 AI 候选起草后发送，不等于独立自述或长期画像确认]\n"
    if (meta.get("replyAssistance") or {}).get("kind") == "assisted" and content.startswith(marker):
        content = content[len(marker):]
    return router.ref("message", message["id"], version=digest([content, meta, message.get("status", "complete")]))


def relevance(queries, text):
    tokens = tokenize(text) - _STOP
    return max((lexical_similarity(tokenize(q) - _STOP, tokens) for q in queries), default=0.0)


def bound_matter(router, include_inactive=False):
    """Only an explicit conversation binding supplies an ongoing-matter source."""
    from ..stores.matters_store import MattersStore, matter_text, source_version
    binding = MattersStore(router.onto, router.convs).binding(router.cid, router.scope)
    matter = binding["matter"]
    snapshot = {"matterId": (matter or {}).get("id"), "revision": binding["bindingRevision"]}
    if not matter or (matter["status"] != "active" and not include_inactive):
        return snapshot, None
    return snapshot, {"ref": router.ref("matter", matter["id"], version=source_version(matter)),
        "category": "matter", "title": ("正在推进 · " if matter["status"] == "active" else "回顾事项 · ") + matter["title"], "text": matter_text(matter), "score": 1.2,
        "query": "\n".join(matter[key] for key in ("title", "goal", "context", "nextStep", "outcome") if matter.get(key))[:1200]}


def artifact_candidates(router, matter_id, queries):
    from ..stores.matters_store import MattersStore, source_version
    items = MattersStore(router.onto, router.convs).artifacts(matter_id, router.scope)
    candidates = []
    for item in items:
        score = relevance(queries, item["title"] + "\n" + item["markdown"])
        if score >= .12:
            candidates.append({"ref": router.ref("artifact", item["id"], version=source_version(item)),
                "category": "artifact", "title": "已保存成果 · " + item["title"], "text": item["markdown"], "score": score})
    return sorted(candidates, key=lambda item: -item["score"])[:3]


def _batches(store, sql, parameters):
    """Scan all scoped rows without retaining an unbounded database result."""
    with store._connect() as db:
        cursor = db.execute(sql, parameters)
        while rows := cursor.fetchmany(256):
            yield rows


def history_candidates(router, queries, *, cutoff=None):
    # Historical original words are separate evidence, not a message's taint
    # closure. Scope is filtered before text leaves the storage query.
    cutoff = router.mode.get("cutoff", 0) if cutoff is None else cutoff
    before = getattr(router, "context_before_seq", None)
    batches = _batches(router.convs, """SELECT m.id,m.content,m.conversation_id,m.seq FROM messages m
            JOIN conversations c ON c.id=m.conversation_id
            WHERE c.device_scope=? AND m.role='user' AND m.status='complete'
            AND (m.conversation_id!=? OR m.seq>?)
            AND (m.conversation_id!=? OR ? IS NULL OR m.seq<?)
            ORDER BY m.created_at DESC,m.id""", (router.scope, router.cid, cutoff, router.cid, before, before))
    results = []
    for rows in batches:
        for row in rows:
            score = relevance(queries, row["content"])
            if score < .12:
                continue
            message = router.convs.get_message(row["id"])
            if not message or message["status"] != "complete":
                continue
            score = relevance(queries, message["content"])
            if score < .12:
                continue
            expression = ((message or {}).get("meta") or {}).get("replyAssistance", {})
            if expression.get("kind") == "control":
                continue
            assisted = expression.get("kind") == "assisted"
            results.append({"ref": message_ref(router, message), "score": score,
                "category": "history", "title": "历史用户原话" if not assisted else "用户发送的辅助表达（不是独立自述）",
                "text": message["content"], "message": message,
                "conversationId": row["conversation_id"], "seq": row["seq"]})
        results = sorted(results, key=lambda c: (-c["score"], c["ref"]["id"]))[:120]
    return results


def claim_text(claim):
    """Keep situation/exception qualifiers whole; budgets drop the whole item."""
    state = {"confirmed": "已确认理解", "working": "待验证推测", "superseded": "历史已替代理解", "retracted": "历史已撤回理解"}.get(claim["trustState"], "记录")
    parts = [state + "：" + claim["content"], "记录层次：" + str(claim.get("layer", "")),
             "适用范围：" + ("仅适用于当时情境" if claim.get("scope") == "context_only" else "仍需结合当前情境")]
    if claim["trustState"] not in ("confirmed", "working"):
        parts.append("仅用于回顾，不代表当前用户")
    contextual = claim.get("contextual") or {}
    for key, label in (("situation", "具体情境"), ("exceptions", "例外或未知"), ("framing", "时间与认同类型")):
        if contextual.get(key):
            parts.append(label + "：" + str(contextual[key]))
    alignment = claim.get("selfAlignment") or {}
    if alignment.get("level") is not None:
        from ..stores.alignment_store import LEVELS
        parts.append("用户校准等级（不是真假分数）：" + LEVELS[alignment["level"]])
        frames = {"context_only": "仅适用于当时情境", "aspirational": "理想方向，不表示已经做到", "long_term": "用户目前认同的长期倾向"}
        parts.append("校准适用范围：" + frames.get(alignment.get("framing"), "尚未明确"))
    if alignment.get("reason"):
        parts.append("校准说明：" + str(alignment["reason"]))
    for key, label in (("validFrom", "起始时间"), ("validTo", "适用截止")):
        if claim.get(key):
            parts.append(label + "：" + str(claim[key]))
    quotes = [str(e.get("quote")) for e in claim.get("evidence", []) if e.get("quote")]
    if quotes:
        parts.append("对应原话（选取，不增加独立证据）：\n" + "\n".join(quotes[:3]))
    return "\n".join(parts)


def summary_candidates(router, queries):
    # Immutable revision identities let the router check their full source chain.
    before = getattr(router, "context_before_seq", None)
    batches = _batches(router.convs, """SELECT s.* FROM conversation_summaries s
            JOIN conversations c ON c.id=s.conversation_id WHERE c.device_scope=?
            AND s.revision=(SELECT MAX(newer.revision) FROM conversation_summaries newer
                            WHERE newer.conversation_id=s.conversation_id
                            AND (newer.conversation_id!=? OR ? IS NULL OR newer.up_to_seq<?))
            ORDER BY s.created_at DESC,s.conversation_id""", (router.scope, router.cid, before, before))
    results = []
    for rows in batches:
        for row in rows:
            text = strip_citation_markers(row["summary"] + "\n" + "\n".join(json.loads(row["key_points_json"] or "[]")))
            score = relevance(queries, text)
            if score >= .12:
                results.append({"ref": router.ref("summary", f"{row['conversation_id']}:{row['revision']}"),
                    "score": score * .85, "category": "summary", "title": "历史对话摘要（派生内容，不是新增证据）",
                    "text": text.strip()})
        results = sorted(results, key=lambda c: (-c["score"], c["ref"]["id"]))[:120]
    return results


def decision_candidates(router, queries):
    from ..stores.growth_store import GrowthStore
    from ..stores.learning_store import LearningStore
    growth = GrowthStore.instance()
    results = []
    for decision in growth.list_decisions():
        if not record_in_scope(decision, router.convs, router.scope, growth=growth):
            continue
        decision_title = strip_citation_markers(decision["title"])
        outcome = decision.get("outcome") or {}
        review = decision.get("review") or {}
        parts = ["当时的判断：" + decision_title, "当时情境：" + decision.get("context", ""),
                 "当时选择：" + decision.get("choice", ""), "当时理由：" + decision.get("rationale", ""),
                 "事前预期（不是实际结果）：" + decision.get("expectedOutcome", "")]
        if outcome:
            parts.append("用户记录的实际结果：" + str(outcome.get("result", "")) + "；" + str(outcome.get("notes", "")))
        if review:
            parts.append("用户复盘：" + str(review.get("reflection", "")) + "；" + "；".join(review.get("lessons") or []))
        text = strip_citation_markers("\n".join(parts))
        score = relevance(queries, text)
        if score >= .12:
            results.append({"ref": router.ref("decision", decision["id"], version=digest(decision)), "score": score,
                "category": "decision", "title": decision_title, "text": text, "decision": decision})
        episode = LearningStore(router.onto).get(decision["id"])
        if episode and router.convs.get_conversation(episode["conversationId"]):
            expectation = episode.get("expectation") or {}
            text = "事前观察预期（不是事实）：" + json.dumps(expectation, ensure_ascii=False)
            if episode.get("proposal"):
                text += "\n尚待核对的解释提议：" + json.dumps(episode["proposal"], ensure_ascii=False)
            if episode.get("resolution"):
                text += "\n用户处理记录：" + json.dumps(episode["resolution"], ensure_ascii=False)
            text = strip_citation_markers(text)
            score = relevance(queries, text)
            if score >= .12:
                results.append({"ref": router.ref("episode", decision["id"], version=digest(episode)), "score": score,
                    "category": "episode", "title": "情境观察 · " + decision_title, "text": text})
    return sorted(results, key=lambda c: (-c["score"], c["ref"]["id"]))[:120]


def _material_item(router, record, snapshot, body, snippet, score, *, offset=None):
    # A QA/card snippet must be proven against the current extracted snapshot.
    # Do not send a convenient summary or stale index hit in its place.
    offset = body.find(snippet) if offset is None else offset
    if not snippet or offset < 0 or body[offset:offset + len(snippet)] != snippet:
        return None
    ident, version = record["materialId"], record["versionNumber"]
    source_version = digest([{"materialId": ident, "version": version}, snapshot["snapshot_id"], digest(body)])
    return {"ref": router.ref("material", ident, materialVersion=version, version=source_version), "score": score,
        "category": "material", "title": record["fileName"], "text": snippet,
        "material": {"materialId": ident, "version": version, "title": record["fileName"],
            "snapshotId": snapshot["snapshot_id"], "chunkKey": f"{ident}::snapshot:{snapshot['snapshot_id']}:{offset}",
            "locator": {"kind": "text", "offset": offset, "length": len(snippet)}, "partial": len(snippet) < len(body)}}


def material_candidates(router, queries):
    if os.environ.get("ZHIJUN_MATERIAL_EVIDENCE", "1").lower() in ("0", "false", "no"):
        return []
    from ..chat_imports import read_ref, require_material
    results = []
    qa = sys.modules.get("mindos.qa")
    encoder = getattr(sys.modules.get("embedder"), "_text_model", None)
    if qa is not None and encoder is not None:
        # Reuse QA retrieval only when already warm; never download/load a model
        # as an accidental side effect of typing or opening a route preview.
        for query in queries[:3]:
            try:
                hits = qa.build_evidence(query, limit=12, device_scope=router.scope)
            except Exception:
                continue
            for hit in hits or []:
                if getattr(hit, "source_type", "") != "material" or not hit.material_id:
                    continue
                try:
                    record = require_material(hit.material_id, router.scope)
                    record, snapshot, body = read_ref({"materialId": hit.material_id, "version": record["versionNumber"]}, router.scope)
                    item = _material_item(router, record, snapshot, body, hit.snippet.strip(), float(hit.score))
                    if item:
                        results.append(item)
                except (HTTPException, OSError, ValueError, KeyError):
                    continue
    # Ready snapshots remain usable before vector indexing and without an encoder.
    from ..services import ingestion
    for raw in ingestion.JobStore.instance().list(device_scope=router.scope):
        try:
            record = require_material(raw["material_id"], router.scope)
            record, snapshot, body = read_ref({"materialId": record["materialId"], "version": record["versionNumber"]}, router.scope)
        except (HTTPException, OSError, ValueError, KeyError):
            continue
        chunks = ((offset, body[offset:offset + 600]) for offset in range(0, len(body), 500))
        ranked = heapq.nsmallest(2, ((relevance(queries, text), offset, text) for offset, text in chunks), key=lambda x: (-x[0], x[1]))
        for score, offset, snippet in ranked:
            if score >= .12:
                item = _material_item(router, record, snapshot, body, snippet, score, offset=offset)
                if item:
                    results.append(item)
    return sorted(results, key=lambda c: (-c["score"], c["ref"]["id"], (c.get("material") or {}).get("locator", {}).get("offset", 0)))[:80]
