"""判断簿 → 本体的单向钩子：复盘写下的经验变成「原则候选」（永远先是待确认的「你想成为的」）。

方向只有 growth → 候选 → 用户确认 → claims；本体不反写判断簿。≥ 2 次复盘写下相近经验 → 标 promotion_ready 置顶。
"""
from __future__ import annotations

import logging

from ..stores.ontology_store import ME_ENTITY_ID, OntologyConflictError, OntologyError, OntologyStore, lexical_similarity, tokenize

logger = logging.getLogger(__name__)

SIMILAR_LESSON = 0.6


def on_review(review: dict, decision: dict | None, *, store: OntologyStore | None = None) -> dict:
    store = store or OntologyStore.instance()
    created: list[str] = []
    reaffirmed: list[str] = []
    for lesson in (review or {}).get("lessons") or []:
        text = str(lesson or "").strip().replace("\n", " ")
        if len(text) < 4:
            continue
        content = text[:120]
        evidence = [{"kind": "review", "decision_id": review.get("decisionId"), "quote": text[:300]}]
        existing = store.find_active_by_hash(ME_ENTITY_ID, "holds_principle", content)
        if existing is None:
            for candidate in store.list_claims(section="principles", trust_states=("working", "confirmed"), limit=500):
                if lexical_similarity(tokenize(content), tokenize(candidate["content"])) >= SIMILAR_LESSON:
                    existing = candidate
                    break
        if existing is not None:
            store.add_evidence(existing["id"], evidence, reaffirm=True)
            if existing["trustState"] == "working" and store.evidence_source_count(existing["id"]) >= 2:
                store.set_promotion_ready(existing["id"], True)
            reaffirmed.append(existing["id"])
            continue
        try:
            claim = store.create_claim(
                {
                    "subject_entity_id": ME_ENTITY_ID,
                    "predicate": "holds_principle",
                    "content": content,
                    "section": "principles",
                    "layer": "aspirational",
                    "confidence": 0.6,
                },
                evidence,
                trust_state="working",
                trust_origin="model",
                surface="decision_panel",
                note=f"来自判断「{(decision or {}).get('title', '')}」的复盘经验",
            )
            created.append(claim["id"])
        except (OntologyConflictError, OntologyError) as exc:
            logger.debug("复盘经验候选写入被拒：%s", exc)
    if created:
        try:
            from .jobs import enqueue_projection

            enqueue_projection(store=store)
        except Exception:  # noqa: BLE001
            pass
    return {"created": created, "reaffirmed": reaffirmed}
