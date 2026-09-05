"""把用户自己记下的历史判断带进当下：商量时找「过去类似的判断」。

只用判断簿里的原文（标题 / 选择 / 结果 / 经验），按词面相似度排序；已复盘、已记结果的优先，因为它们带着结果。
"""
from __future__ import annotations

from ..stores.ontology_store import lexical_similarity, tokenize

STATUS_WEIGHT = {"reviewed": 0.25, "outcome_recorded": 0.15, "open": 0.0}


def local_only_decision(decision: dict | None) -> bool:
    from .source_policy import SourcePolicy
    return SourcePolicy().decision_local(decision)


def similar_decisions(user_text: str, *, k: int = 3, growth=None, exclude_id: str | None = None) -> list[dict]:
    if growth is None:
        from ..stores.growth_store import GrowthStore

        growth = GrowthStore.instance()
    q = tokenize(user_text)
    from .source_policy import SourcePolicy
    policy = SourcePolicy(growth=growth)
    scored: list[tuple[float, dict]] = []
    for decision in growth.list_decisions():
        # Protected derived choices stay in the judgment book, not generic model
        # context. Direct review is handled locally and retains this restriction.
        if policy.decision_local(decision):
            continue
        if exclude_id and decision["id"] == exclude_id:
            continue
        haystack = " ".join(str(decision.get(key) or "") for key in ("title", "context", "choice", "rationale"))
        sim = lexical_similarity(q, tokenize(haystack)) if q else 0.0
        score = sim + STATUS_WEIGHT.get(decision.get("status"), 0.0)
        if sim <= 0.0:
            continue
        scored.append((score, decision))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    picked = [d for score, d in scored[: max(0, k)] if score > 0.05]
    return picked
