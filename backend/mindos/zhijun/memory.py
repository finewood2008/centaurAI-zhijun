"""Admit memories sparingly; retain event context in one opt-in local outline."""
from __future__ import annotations

import re

from ..stores.conversation_store import ConversationStore
from ..stores.memory_store import MemoryStore
from ..stores.ontology_store import ME_ENTITY_ID, OntologyConflictError, OntologyError
from .alignment import scope_for, visible

# Explicit topic changes are reliable; never infer topics with another model call.
_TOPIC_CHANGE = re.compile(r"^(?:那[，,]?|好[的吧]?[，,。]?|嗯[，,]?|现在)?\s*(?:换[个一]个?话题|换一件事|另外一件事|再说另一件事|聊聊另一件事|说点别的)")


def topic_for(convs, cid, message_id=None):
    """Conservative topic: one conversation until the user explicitly switches.

    An explicit memory request starts a new review opportunity, not a new profile.
    It remains stable as the user continues talking and after refresh/restart.
    """
    from .extract import explicit_memory_request
    anchor = cid
    for message in convs.list_messages(cid):
        if message["role"] == "user":
            text = message["content"].strip()
            if anchor == cid or _TOPIC_CHANGE.search(text) or explicit_memory_request(text):
                anchor = message["id"]
        if message_id and message["id"] == message_id:
            break
    return anchor


def automatic_allowed(ontology, convs, cid):
    from .charter_policy import scope_policy, check_action
    scope = scope_for(cid, convs)
    return (bool(convs.get_conversation(cid)) and MemoryStore(ontology).policy(scope)["mode"] == "important"
            and check_action(scope_policy(scope), "memory_auto")["allowed"])


def extraction_allowed(ontology, convs, cid, text):
    from .extract import explicit_memory_request, memory_request_declined
    from .charter_policy import scope_policy, check_action
    explicit = explicit_memory_request(text)
    return (bool(convs.get_conversation(cid)) and not memory_request_declined(text)
            and (automatic_allowed(ontology, convs, cid) or explicit)
            and check_action(scope_policy(scope_for(cid, convs)), "memory_extract", explicit=explicit)["allowed"])


def process_candidates(valid, entities, *, store, conversation_id, message_id, user_text,
                       routing_sources=None, input_origin=None, prev_assistant=None):
    from .extract import admission, explicit_memory_request, persist
    convs = ConversationStore.instance()
    message = convs.get_message(message_id)
    empty = {"created": [], "reaffirmed": [], "promoted": [], "suppressed": len(valid)}
    if (not message or message["conversationId"] != conversation_id or message["role"] != "user"
            or message["status"] != "complete" or message["content"] != user_text):
        return empty
    # Recheck after a possibly slow model call; a new preference is effective now.
    if not extraction_allowed(store, convs, conversation_id, user_text):
        return empty
    ledger = MemoryStore(store)
    topic = topic_for(convs, conversation_id, message_id)
    long_term, contextual = admission(valid, user_text, input_origin, prev_assistant=prev_assistant)
    explicit = explicit_memory_request(user_text)
    # Every new extracted interpretation is a candidate, even in the legacy path.
    # [] still marks local-derived ancestry, never invents an external grant.
    sources = routing_sources if routing_sources is not None else []
    selected = (long_term or (contextual[:1] if explicit else []))
    result = persist(selected, entities if selected else [], store=store, conversation_id=conversation_id,
                     message_id=message_id, routing_sources=sources, input_origin=input_origin)
    for claim_id in result["created"]:
        ledger.register(claim_id, conversation_id, topic, message_id, explicit)
    # Contextual notes do not produce ontology candidates. One local outline per
    # topic accumulates source-linked updates; opening it does not save a fact.
    if contextual and not explicit:
        entries = [{"content": c.content, "quote": c.quote, "messageId": message_id,
                    "sources": sources, "replyAssistance": input_origin,
                    "privacyLevel": c.privacy_level, "layer": c.layer} for c in contextual]
        outline = ledger.merge_draft(conversation_id, topic, entries)
        result["draftId"] = outline["id"]
    result["suppressed"] += max(0, len(valid) - len(selected) - len(contextual))
    return result


def _usable_claim(claim, convs, scope, cid, *, include_deferred=False):
    if (not claim or claim["trustState"] != "working" or claim.get("challenged")
            or (claim.get("deferredUntil") and not include_deferred)):
        return False
    if not visible(claim, convs, scope):
        return False
    for evidence in claim.get("evidence", []):
        message = convs.get_message(evidence.get("messageId") or "") or {}
        if (evidence.get("conversationId") == cid and message.get("conversationId") == cid
                and message.get("role") == "user" and message.get("status") == "complete"
                and evidence.get("quote") and evidence["quote"] in message.get("content", "")):
            return True
    return False


def pending(ontology, convs, cid):
    """Explicitly opened queue: all topics, independent of automatic reminders."""
    scope = scope_for(cid, convs)
    if not convs.get_conversation(cid):
        return {"items": [], "total": 0}
    items = []
    for entry in MemoryStore(ontology).admissions(cid):
        claim = ontology.get_claim(entry["claim_id"])
        if _usable_claim(claim, convs, scope, cid, include_deferred=True):
            items.append({"topicId": entry["topic_id"], "claim": claim})
    return {"items": items, "total": len(items)}


REMINDER_USER_TURN_GAP = 3


def attention(ontology, convs, cid):
    # Reserving/replacing is one operation, including concurrent tabs and workers.
    with ontology._lock:
        return _attention(ontology, convs, cid)


def _attention(ontology, convs, cid):
    ledger = MemoryStore(ontology)
    topic, scope = topic_for(convs, cid), scope_for(cid, convs)
    policy = ledger.policy(scope)
    automatic = automatic_allowed(ontology, convs, cid)
    admissions = ledger.admissions(cid, topic)
    claims = [ontology.get_claim(a["claim_id"]) for a in admissions if automatic or a["explicit"]]
    claims = [c for c in claims if _usable_claim(c, convs, scope, cid)]
    messages = convs.list_messages(cid)
    user_messages = [m for m in messages if m["role"] == "user" and m["status"] == "complete"
                     and (m.get("meta", {}).get("replyAssistance") or {}).get("kind") != "control"]
    user_turn = len(user_messages)
    message_seq = max((m["seq"] for m in user_messages), default=0)
    source_seq = {a["claim_id"]: (convs.get_message(a["message_id"]) or {}).get("seq", 0) for a in admissions}
    selected = ledger.slot(cid, topic)
    # Alignments and memory candidates draw from the same durable topic slot.
    alignments = []
    alignment_seq = {}
    if automatic:
        for message in reversed(convs.list_messages(cid)):
            if topic_for(convs, cid, message["id"]) != topic:
                break
            if (message.get("meta") or {}).get("kind") == "alignment_proposal":
                for ref in message["meta"].get("alignmentSources", []):
                    claim = ontology.get_claim(ref.get("claimId", ""))
                    proposal = (claim or {}).get("selfAlignment", {}).get("proposal")
                    if proposal and proposal.get("conversationId") == cid and visible(claim, convs, scope):
                        alignments.append(claim)
                        alignment_seq[claim["id"]] = max((m["seq"] for m in user_messages if m["seq"] < message["seq"]), default=0)
    if selected and not selected["shown_user_turn"] and user_turn:
        selected = ledger.initialize_clock(cid, topic, message_seq=message_seq, user_turn=user_turn)
    if selected is None:
        if claims:
            selected = ledger.reserve(cid, topic, "claim", claims[0]["id"], message_seq=message_seq, user_turn=user_turn)
        elif alignments:
            selected = ledger.reserve(cid, topic, "alignment", alignments[0]["id"], message_seq=message_seq, user_turn=user_turn)
    elif selected["consumed"] and user_turn - selected["shown_user_turn"] >= REMINDER_USER_TURN_GAP:
        fresh = [("claim", c) for c in claims if source_seq.get(c["id"], 0) > selected["shown_message_seq"]]
        fresh += [("alignment", c) for c in alignments if alignment_seq.get(c["id"], 0) > selected["shown_message_seq"]]
        # A dismissed proposal itself is not fresh evidence, even if a later
        # assistant repeats it. Historical unseen items stay in the manual queue.
        fresh = [(kind, c) for kind, c in fresh if (kind, c["id"]) != (selected["kind"], selected["target_id"])]
        if fresh:
            kind, item = fresh[0]
            selected = ledger.renew(cid, topic, selected, kind, item["id"], message_seq=message_seq, user_turn=user_turn)
    candidate = alignment = None
    if selected and not selected["consumed"]:
        choices = claims if selected["kind"] == "claim" else alignments
        found = next((c for c in choices if c["id"] == selected["target_id"]), None)
        if found:
            if selected["kind"] == "claim":
                candidate = found
            else:
                alignment = found
        else:
            ledger.consume(cid, topic, selected["kind"], selected["target_id"])
    draft = ledger.draft(cid, topic)
    return {"topicId": topic, "policy": policy, "candidate": candidate, "alignment": alignment,
            "draft": public_draft(draft), "pendingCount": pending(ontology, convs, cid)["total"]}


def public_draft(draft):
    if not draft:
        return None
    return {**draft, "savedContent": draft["summary"][:120],
            "entries": [{k: e[k] for k in ("content", "quote", "messageId")} for e in draft["entries"]]}


def review_draft(ontology, convs, cid, draft_id, revision, action):
    ledger = MemoryStore(ontology)
    # Serialize this draft's save with all candidate writes. The deterministic
    # content hash recovers a crash between create_claim and finish_draft.
    with ontology._lock:
        draft = ledger.draft(cid, draft_id=draft_id)
        if not draft:
            raise OntologyError("小结不存在")
        desired = "saved" if action == "save" else "dismissed"
        if draft["status"] == desired and draft["revision"] == revision + 1:
            return {"draft": public_draft(draft), "claim": ontology.get_claim(draft["claimId"]) if draft["claimId"] else None}
        if draft["status"] != "draft" or draft["revision"] != revision:
            raise OntologyConflictError("小结已更新，请重新核对后保存")
        if action == "dismiss":
            return {"draft": public_draft(ledger.finish_draft(cid, draft_id, revision, desired))}
        evidence = []
        for entry in draft["entries"]:
            message = convs.get_message(entry["messageId"])
            if not message or message["conversationId"] != cid or entry["quote"] not in message["content"]:
                raise OntologyConflictError("小结来源已经变化，请重新核对")
            locator = {"localOnly": True, "routingSources": entry["sources"], "memoryDraftId": draft_id}
            if entry.get("replyAssistance"):
                locator["replyAssistance"] = entry["replyAssistance"]
            evidence.append({"kind": "conversation_turn", "conversation_id": cid,
                             "message_id": message["id"], "quote": entry["quote"], "locator": locator})
        # The exact saved excerpt is shown in the drawer. Full source snippets
        # remain attached; no freeform model summary is promoted to personality.
        content = draft["summary"][:120]
        existing = ontology.find_active_by_hash(ME_ENTITY_ID, "happened", content, device_scope=scope_for(cid, convs))
        if existing and not any((e.get("locator") or {}).get("memoryDraftId") == draft_id for e in existing.get("evidence", [])):
            raise OntologyConflictError("已有相同的事件记录；未重复保存或修改原记录")
        claim = existing or ontology.create_claim({"subject_entity_id": ME_ENTITY_ID,
            "section": "matters", "predicate": "happened", "content": content,
            "layer": "aspirational" if any(e.get("layer") == "aspirational" for e in draft["entries"]) else "self_declared",
            "scope": "context_only", "context_ref": cid,
            "device_scope": scope_for(cid, convs), "confidence": 0.5,
            "privacy_level": "sensitive" if any(e.get("privacyLevel") == "sensitive" for e in draft["entries"]) else "private"},
            evidence, trust_state="confirmed", trust_origin="user_confirm", surface="conversation",
            conversation_id=cid, note="用户主动保存事件小结；仅用于这件事，不确认长期倾向或贴合度")
        draft = ledger.finish_draft(cid, draft_id, revision, desired, claim["id"])
        return {"draft": public_draft(draft), "claim": claim}
