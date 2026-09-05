"""Review lessons remain contextual candidates, never inferred lifetime principles."""
from __future__ import annotations

from ..stores.ontology_store import OntologyConflictError, OntologyStore
from .source_policy import SourcePolicy


def on_review(review: dict, decision: dict | None, *, store: OntologyStore | None = None) -> dict:
    store = store or OntologyStore.instance()
    created: list[str] = []
    from .charter_policy import scope_policy, check_action, assert_current, record_scope_or_none, basis
    from ..stores.conversation_store import ConversationStore
    scope = record_scope_or_none(decision or {}, ConversationStore.instance())
    if scope is None:
        return {"created": [], "reaffirmed": [], "reason": "charter_scope_uncertain"}
    policy = scope_policy(scope)
    if not check_action(policy, "memory_auto")["allowed"]:
        return {"created": [], "reaffirmed": [], "reason": "charter_memory_manual"}
    local_only = SourcePolicy(store).decision_local(decision)
    for lesson in (review or {}).get("lessons") or []:
        text = str(lesson or "").strip().replace("\n", " ")
        if len(text) < 4:
            continue
        content = text[:120]
        evidence = [{"kind": "review", "decision_id": review.get("decisionId"), "quote": text[:300],
                     "locator": {"localOnly": local_only, "reviewId": review.get("id"),
                                 "charterBasis": basis(policy),
                                 "context": (decision or {}).get("context", "")[:1000]}}]
        try:
            assert_current(policy)
            claim = store.create_claim(
                {
                    "content": content,
                    "section": "ways",
                    "layer": "hypothesis",
                    "confidence": 0.6,
                    "scope": "context_only",
                    "context_ref": review.get("decisionId"),
                    "privacy_level": "restricted" if local_only else "private",
                },
                evidence,
                trust_state="working",
                trust_origin="model",
                surface="decision_panel",
                note=f"这次判断的经验，尚未验证适用于其他情境：{(decision or {}).get('title', '')}",
            )
            created.append(claim["id"])
        except OntologyConflictError:
            # Retrying must not inflate evidence or promote a lifetime trait.
            continue
    return {"created": created, "reaffirmed": []}
