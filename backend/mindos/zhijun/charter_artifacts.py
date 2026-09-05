"""Local lineage receipts for optional draft helpers, outside chat/profile data.

Once suggestions were shown, edited text cannot be reliably classified as copied
or independently written. Preserve their possible ancestry conservatively, without
claiming that merely viewing a suggestion is user agreement.
"""
import json

from ..stores.alignment_store import digest


def _key(cid, task, revision):
    return "charter_helper_receipt:" + digest([cid, task, revision])


def remember(ontology, cid, task, revision, preview):
    with ontology._lock:
        return _remember(ontology, cid, task, revision, preview)


def _remember(ontology, cid, task, revision, preview):
    old = recall(ontology, cid, task, revision) or {}
    sources = [*old.get("routingSources", []), *[s["ref"] for s in preview["sources"]]]
    receipt = {"conversationId": cid, "task": task, "revision": revision,
               "charterBasis": preview.get("charterBasis"),
               "routingSources": list({digest(s): s for s in sources}.values()),
               "possibleAssistance": True}
    ontology.meta_set(_key(cid, task, revision), json.dumps(receipt, ensure_ascii=False))
    # Editors intentionally retain touched candidate text across background draft
    # revisions. Its ancestry must survive even if the next draft omits that text.
    previous = recall_lineage(ontology, cid, task) or {}
    aggregate = {**receipt, "sourceRevisions": list(dict.fromkeys([*previous.get("sourceRevisions", []), revision])),
                 "routingSources": list({digest(s): s for s in [*previous.get("routingSources", []), *sources]}.values())}
    ontology.meta_set(_key(cid, task, "all-revisions"), json.dumps(aggregate, ensure_ascii=False))
    return receipt


def recall(ontology, cid, task, revision):
    value = ontology.meta_get(_key(cid, task, revision))
    return json.loads(value) if value else None


def recall_lineage(ontology, cid, task):
    return recall(ontology, cid, task, "all-revisions")
