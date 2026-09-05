"""Conservative ancestry checks for generic model/export consumers.

Per-service file/alignment consent still belongs to their dedicated flows. A
generic context, consolidation or export cannot reuse those grants implicitly.
Resolve old review evidence too; fixing only newly created claims is not enough.
"""
from __future__ import annotations

import json

from ..stores.conversation_store import ConversationStore
from ..stores.growth_store import GrowthStore
from ..stores.ontology_store import OntologyStore


class SourcePolicy:
    def __init__(self, ontology=None, conversations=None, growth=None):
        self.ontology = ontology or OntologyStore.instance()
        self.conversations = conversations or ConversationStore.instance()
        self.growth = growth or GrowthStore.instance()
        self._cache = {}

    def conversation_local(self, cid):
        if not cid:
            return False
        key = ('conversation', cid)
        if key not in self._cache:
            from . import alignment
            from ..chat_imports import protected_conversation
            # Missing ancestry is not permission to send a derivative.
            self._cache[key] = (not self.conversations.get_conversation(cid)
                or alignment.protected(cid, self.conversations, self.ontology)
                or protected_conversation(cid, self.conversations))
        return self._cache[key]

    def decision_local(self, decision, seen=None):
        if not decision:
            return True
        seen = set(seen or ())
        key = ('decision', decision['id'])
        if key in seen:
            return True
        seen.add(key)
        with self.ontology._connect() as db:
            if db.execute('SELECT 1 FROM learning_episodes WHERE decision_id=?', (decision['id'],)).fetchone():
                return True
        refs = [*(decision.get('evidenceRefs') or []), *((decision.get('outcome') or {}).get('evidenceRefs') or [])]
        for raw in refs:
            try:
                ref = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                continue
            if not isinstance(ref, dict):
                continue
            if ref.get('kind') == 'local_only_decision' or ref.get('materialId') or ref.get('routingSources'):
                return True
            if self.conversation_local(ref.get('conversationId')):
                return True
            if ref.get('claimId') and self.claim_local(self.ontology.get_claim(ref['claimId']), seen):
                return True
        return False

    def claim_local(self, claim, seen=None):
        if not claim:
            return True
        seen = set(seen or ())
        key = ('claim', claim['id'])
        if key in seen:
            return True
        seen.add(key)
        if claim.get('privacyLevel') not in ('public', 'private'):
            return True
        for ev in claim.get('evidence') or []:
            if (ev.get('locator') or {}).get('localOnly') or ev.get('materialId'):
                return True
            if self.conversation_local(ev.get('conversationId')):
                return True
            if ev.get('decisionId') and self.decision_local(self.growth.get_decision(ev['decisionId']), seen):
                return True
        # Canonical edits copy evidence, but also resolve legacy incomplete copies.
        parent = claim.get('supersedesId')
        return bool(parent and self.claim_local(self.ontology.get_claim(parent), seen))
