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
    from . import disclosure
    explicit = explicit_memory_request(text)
    # PRD V2 5.5：重话那一轮先接住、不抽取（用户说「记下来」可以豁免）；
    # 危机内容完全不抽取，explicit 也不豁免。放在最前面短路，任何其它策略都不能绕过它。
    if disclosure.extraction_blocked(text, explicit_request=explicit):
        return False
    return (bool(convs.get_conversation(cid)) and not memory_request_declined(text)
            and (automatic_allowed(ontology, convs, cid) or explicit)
            and check_action(scope_policy(scope_for(cid, convs)), "memory_extract", explicit=explicit)["allowed"])


def process_candidates(valid, entities, *, store, conversation_id, message_id, user_text,
                       routing_sources=None, input_origin=None, prev_assistant=None, request_message_id=None):
    from .extract import (admission, auto_confirmable, existing_candidate, explicit_memory_request, followup_memory_request,
                          memory_source_message, memory_request_declined, persist, _question_source)
    convs = ConversationStore.instance()
    message = convs.get_message(message_id)
    empty = {"created": [], "reaffirmed": [], "promoted": [], "suppressed": len(valid), "autoConfirmed": []}
    if (not message or message["conversationId"] != conversation_id or message["role"] != "user"
            or message["status"] != "complete" or message["content"] != user_text):
        return empty
    request = message
    if request_message_id:
        request = convs.get_message(request_message_id)
        if (not request or request["conversationId"] != conversation_id or request["role"] != "user"
                or request["status"] != "complete" or not followup_memory_request(request["content"])):
            return empty
        history = convs.list_messages(conversation_id)
        source = memory_source_message(history, request)
        if not source or source["id"] != message_id:
            return empty
        if any(m["role"] == "user" and m["seq"] > request["seq"] and memory_request_declined(m["content"]) for m in history):
            return empty
    # Recheck the request's permission after the model call; the source remains
    # the original assertion even when manual mode requires a later save command.
    if not extraction_allowed(store, convs, conversation_id, request["content"]):
        return empty
    ledger = MemoryStore(store)
    if request_message_id and any(row["message_id"] == message_id for row in ledger.admissions(conversation_id)):
        return empty  # replay/rephrased model output cannot create another candidate
    topic = topic_for(convs, conversation_id, request["id"])
    scope = scope_for(conversation_id, convs)
    # V3 M2（拍板 4）：important 模式且章程允许时，亲口说的高置信自述直接记住（可撤回）；manual 模式仍只出 working。
    auto_confirm_allowed = automatic_allowed(store, convs, conversation_id)
    # Every new extracted interpretation is a candidate, even in the legacy path.
    # [] still marks local-derived ancestry, never invents an external grant.
    sources = routing_sources if routing_sources is not None else []
    # Known identities must not consume the proposal slot before a new identity
    # is considered. Repeats now mature the record instead of being dropped:
    # append this message as evidence (once) and refresh lastReaffirmed; a
    # working record restated in the user's own words is confirmed. Assisted
    # text and quotes sliced from a question/hypothetical are not reaffirmation.
    novel, reaffirmed, promoted, auto_confirmed = [], [], [], []
    duplicate_count = tombstone_count = 0
    for claim in valid:
        if claim.subject in ("me", "我", "本人", "我自己", "用户"):
            existing = existing_candidate(store, claim, ME_ENTITY_ID, convs, scope)
            if existing is not None:
                duplicate_count += 1
                if (not input_origin and not _question_source(claim.quote, user_text)
                        and not any(e.get("messageId") == message_id for e in existing.get("evidence", []))):
                    evidence = [{"kind": "conversation_turn", "conversation_id": conversation_id, "message_id": message_id,
                                 "quote": claim.quote, "locator": {"routingSources": sources, "localOnly": True}}]
                    store.add_evidence(existing["id"], evidence, reaffirm=True)
                    reaffirmed.append(existing["id"])
                    if existing["trustState"] == "working" and auto_confirmable(claim, user_text, input_origin=input_origin, allowed=auto_confirm_allowed):
                        try:
                            store.transition(existing["id"], "confirm", surface="conversation", conversation_id=conversation_id,
                                             message_id=message_id, note="用户再次亲口说到，视为确认")
                            promoted.append(existing["id"])
                            auto_confirmed.append(existing["id"])
                        except OntologyConflictError:
                            pass
                continue
            if store.find_tombstone_by_hash(ME_ENTITY_ID, claim.predicate, claim.content, device_scope=scope):
                # A tombstone only yields to the user's own restatement (persist then supersedes it); model repeats stay suppressed.
                if not auto_confirmable(claim, user_text, input_origin=input_origin, allowed=auto_confirm_allowed):
                    tombstone_count += 1
                    continue
        novel.append(claim)
    long_term, contextual = admission(novel, user_text, input_origin, prev_assistant=prev_assistant)
    # PRD V2 6.1：心里的事要同一件事被提及两次以上才成候选，第一次只记一个计数。
    # 一次抱怨不是长期困扰。计数本身有副作用（这次提及要记下来），所以即使这条
    # 候选后面因为别的原因被丢掉，计数也照记——那次提及确实发生过。
    from . import burdens as burden_gate
    long_term = [c for c in long_term if burden_gate.admitted(store, c, message_id=message_id)]
    contextual = [c for c in contextual if c.section != "burdens"]
    explicit = explicit_memory_request(request["content"])
    selected = (long_term or (contextual[:1] if explicit else []))
    result = persist(selected, entities if selected else [], store=store, conversation_id=conversation_id,
                     message_id=message_id, routing_sources=sources, input_origin=input_origin,
                     user_text=user_text, auto_confirm_allowed=auto_confirm_allowed)
    result["reaffirmed"] = list(dict.fromkeys([*reaffirmed, *result["reaffirmed"]]))
    result["promoted"] = list(dict.fromkeys([*promoted, *result["promoted"]]))
    result["autoConfirmed"] = list(dict.fromkeys([*auto_confirmed, *result.get("autoConfirmed", [])]))
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
    result["filterReasons"] = {"existing": duplicate_count, "retracted": tombstone_count,
                              "admission": max(0, len(novel) - len(selected) - len(contextual)),
                              "contextOnly": len(contextual)}
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
