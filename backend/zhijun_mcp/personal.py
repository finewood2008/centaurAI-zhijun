"""Explicit stores only: this adapter never chooses a global/default workspace."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from mindos.zhijun.source_policy import SourcePolicy
from .models import SECTIONS


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return float("inf")


class PersonalContext:
    def __init__(self, ontology, conversations, growth, access):
        self.ontology, self.conversations, self.growth, self.access = ontology, conversations, growth, access
        # Capture ALL historical claims, not just today's confirmed ones. A later
        # promotion of an old draft must not bypass the first-consent migration.
        with ontology._connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM claims")]
        access.initialize_legacy(ids)
        # Persist explicit old denials independently of legacy review retention.
        with ontology._connect() as db:
            rows = db.execute("SELECT target_id,before_json,after_json,note FROM review_events WHERE target_type='claim'").fetchall()
        import json
        for row in rows:
            try:
                before, after = json.loads(row["before_json"] or "null"), json.loads(row["after_json"] or "null")
                if isinstance(after, dict) and after.get("exportAllowed") is False and (
                        row["note"] == "导出开关" or isinstance(before, dict) and "exportAllowed" in before):
                    access.preserve_denial(row["target_id"])
            except (ValueError, TypeError):
                continue

    def denied(self, claim, excluded=(), seen=None):
        if not claim:
            return True
        seen = set(seen or ())
        cid = claim["id"]
        if cid in seen or cid in excluded or cid in self.access.denied_ids():
            return True
        seen.add(cid)
        if SourcePolicy(self.ontology, self.conversations, self.growth).claim_local(claim):
            return True
        # Any explicit historical off is a persistent deny in V1; do not infer
        # consent from a default false value or a subsequent legacy export toggle.
        for event in self.ontology.review_events(cid, limit=10000):
            after = event.get("after") or {}
            before = event.get("before") or {}
            if (event.get("note") == "导出开关" or "exportAllowed" in before) and after.get("exportAllowed") is False:
                self.access.preserve_denial(cid)
                return True
        parent = claim.get("supersedesId")
        if parent and self.denied(self.ontology.get_claim(parent), excluded, seen):
            return True
        for evidence in claim.get("evidence") or []:
            locator = evidence.get("locator") or {}
            parents = [locator.get("claimId"), *(locator.get("claimIds") or [])]
            for parent_id in parents:
                if parent_id and self.denied(self.ontology.get_claim(parent_id), excluded, seen):
                    return True
            if evidence.get("decisionId"):
                decision = self.growth.get_decision(evidence["decisionId"])
                if not decision:
                    return True
                import json
                for raw in decision.get("evidenceRefs") or []:
                    try:
                        ref = json.loads(raw) if isinstance(raw, str) else raw
                    except (ValueError, TypeError):
                        return True
                    if isinstance(ref, dict) and ref.get("claimId") and self.denied(
                            self.ontology.get_claim(ref["claimId"]), excluded, seen):
                        return True
        return False

    def candidates(self, sections=SECTIONS, excluded=()):
        from mindos.zhijun.alignment import visible
        now = self.access.clock()
        policy = SourcePolicy(self.ontology, self.conversations, self.growth)
        # Device-only claims and unresolved/challenged claims cannot be exported.
        claims = self.ontology.list_claims(trust_states=("confirmed",), limit=10000,
                                           include_hidden=False, device_scope="global")
        result = []
        for claim in claims:
            start, end = timestamp(claim.get("validFrom")), timestamp(claim.get("validTo"))
            if (claim.get("section") not in sections or claim.get("scope") != "long_term"
                    or claim.get("supersededById") or claim.get("retractedAt")
                    or (start is not None and (not math.isfinite(start) or start > now))
                    or (end is not None and (not math.isfinite(end) or end <= now))
                    or claim.get("privacyLevel") not in ("public", "private")
                    or policy.claim_local(claim) or not visible(claim, self.conversations, "global")
                    or self.denied(claim, excluded)):
                continue
            result.append(claim)
        return result

    def preview(self):
        legacy = self.access.legacy_ids()
        return [{**self.public(c), "requiresLegacyConfirmation": c["id"] in legacy}
                for c in self.candidates()]

    def read(self, grant, sections, limit):
        sections = set(sections or grant["sections"]) & set(grant["sections"])
        legacy = self.access.legacy_ids() - set(grant["acknowledgedLegacyIds"])
        return [self.public(c) for c in self.candidates(sections, set(grant["excludedClaimIds"]) | legacy)][:limit]

    @staticmethod
    def public(claim):
        version = claim.get("updatedAt") or claim.get("lastReaffirmed") or claim.get("createdAt")
        return {"id": claim["id"], "type": "personal", "section": claim["section"],
                "nature": claim["layer"], "content": claim["content"][:4000],
                "scope": claim["scope"], "validFrom": claim.get("validFrom"),
                "validTo": claim.get("validTo"), "version": version, "updatedAt": version,
                "source": {"type": "ontology_claim", "reference": claim["id"]}}
