"""Five read-only tools with retrieval-time filtering and delivery-time fencing."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
import math
from pydantic import ValidationError

from .models import AccessError, GrantSpec, Principal, TOOLS
from .store import opaque


class ExternalAgentService:
    def __init__(self, store, personal, rag, materials, *, public_origin):
        self.store, self.personal, self.rag, self.materials = store, personal, rag, materials
        self.public_origin = public_origin.rstrip("/")

    def save_grant(self, spec: GrantSpec, grant_id=None, expected_revision=None):
        # materials.describe MUST apply the bound account's current box ACL.
        available = self.materials.describe(spec.materialIds) if spec.materialIds else {}
        if not set(spec.materialIds).issubset(available):
            raise AccessError("MATERIAL_UNAVAILABLE")
        eligible = {c["id"] for c in self.personal.preview() if c["section"] in spec.sections}
        if not set(spec.acknowledgedLegacyIds).issubset(eligible):
            raise AccessError("PERSONAL_SCOPE_CHANGED", 409)
        return self.store.save_grant(spec, grant_id, expected_revision)

    def call(self, principal: Principal, tool: str, arguments: dict):
        try:
            return self._call(principal, tool, arguments)
        except Exception:
            # Fixed outcome only; error strings/arguments can include private data.
            if tool in TOOLS:
                self.store.receipt(principal, tool, [], result="denied", delivery="none")
            raise

    def _call(self, principal: Principal, tool: str, arguments: dict):
        if tool not in TOOLS:
            raise AccessError("TOOL_NOT_AVAILABLE", 404)
        try:
            args = TOOLS[tool].model_validate(arguments)
        except ValidationError:
            raise AccessError("INVALID_ARGUMENTS", 400) from None
        grant = self.store.check(principal)
        if tool == "zhijun_get_access":
            payload = {"type": "access", "sections": grant["sections"],
                       "materialIds": list(self._allowed(grant)), "expiresAt": grant["expiresAt"],
                       "capabilities": list(TOOLS), "readOnly": True}
        elif tool == "zhijun_get_personal_context":
            payload = {"type": "personal", "items": self.personal.read(grant, args.sections, args.limit)}
        elif tool == "zhijun_search_work_data":
            payload = self.search(principal, grant, args)
        elif tool == "zhijun_read_work_evidence":
            record = self.store.record("evidence_refs", args.reference, grant)
            payload = {"type": "work", "items": self.resolve(grant, [record["payload"]])}
        else:
            pending = self.store.record("requests", args.requestId, grant)
            payload = {"type": "request", "requestId": args.requestId, "status": pending["state"]}
            if pending["state"] == "ready":
                payload["items"] = self.resolve(grant, pending["payload"]["evidence"])
        # No body may escape a revoked/changed grant, even if revoked during I/O.
        # All mutating management paths share this workspace service/store lock.
        with self.store.lock, (self.personal.ontology._lock if tool == "zhijun_get_personal_context" else nullcontext()):
            self.store.check(principal, grant["revision"])
            if tool == "zhijun_get_personal_context":
                # Personal state may also change during processing.
                payload["items"] = self.personal.read(grant, args.sections, args.limit)
            resources = [{"id": i.get("id") or i["source"]["materialId"], "version": i["version"],
                          "delivery": i.get("delivery", "context")}
                         for i in payload.get("items", [])]
            receipt = self.store.receipt(principal, tool, resources, result=payload.get("status", "delivered"))
            self.store.check(principal, grant["revision"])
            return {**payload, "receipt": receipt, "version": 1,
                    "updatedAt": datetime.fromtimestamp(self.store.clock(), timezone.utc).isoformat()}

    def _allowed(self, grant):
        if not grant["materialIds"]:
            return {}
        return {key: value for key, value in self.materials.describe(grant["materialIds"]).items()
                if key in grant["materialIds"]}

    def search(self, principal, grant, args):
        allowed = self._allowed(grant)
        # The underlying client treats [] as all materials. NEVER pass [] through.
        if not allowed:
            return {"type": "work", "items": []}
        evidence, pending = [], []
        ids = sorted(allowed)
        for offset in range(0, len(ids), 100):
            self.store.check(principal, grant["revision"])
            batch = ids[offset:offset + 100]
            interaction_id = opaque("mcp_")
            result = self.rag.search(args.query, interaction_id=interaction_id,
                                     material_ids=batch, top_k=args.limit)
            from mindos.data_agent_rag import _search_result
            try:
                state, _ = _search_result(result, batch, interaction_id)
            except ValueError:
                raise AccessError("RAG_CONTRACT_INVALID", 502) from None
            if result.get("detectionNotice") or state == "sensitive_check_unavailable":
                # Do not silently release unchecked originals or partial failures.
                raise AccessError("SENSITIVE_CHECK_INCOMPLETE", 409)
            if result.get("status") == "sensitive_confirmation_required":
                confirmation = result.get("confirmation") or {}
                if not confirmation.get("confirmToken"):
                    raise AccessError("RAG_CONTRACT_INVALID", 502)
                pending.append({"token": confirmation["confirmToken"],
                                "expiresAt": confirmation.get("expiresAt"),
                                "policyVersion": confirmation["policyVersion"],
                                "detectorRevision": confirmation["detectorRevision"],
                                "versions": {key: allowed[key]["version"] for key in batch},
                                "previews": [{"materialId": hit["materialId"],
                                              "text": str(hit.get("redactedPreview", ""))[:500]}
                                    for hit in confirmation.get("hits", [])[:20] if hit.get("materialId") in batch]})
            else:
                evidence.extend(self._handles(result.get("items", []), allowed, batch))
        if pending:
            from .personal import timestamp
            expiries = [timestamp(p["expiresAt"]) for p in pending]
            if any(e is None or not math.isfinite(e) or e <= self.store.clock() for e in expiries):
                raise AccessError("CONFIRMATION_EXPIRED", 409)
            request_id = self.store.put_record("requests", grant,
                {"pending": pending, "evidence": evidence, "limit": args.limit, "query": args.query},
                expires=min(self.store.clock() + 600, *expiries))
            return {"type": "work", "status": "pending", "requestId": request_id,
                    "confirmationUrl": self.public_origin + "/external-agents/requests/" + request_id}
        evidence.sort(key=lambda item: item.get("score", 0), reverse=True)
        unique = {item["ref"]: item for item in evidence}
        return {"type": "work", "items": self.resolve(grant, list(unique.values())[:args.limit])}

    @staticmethod
    def _handles(items, allowed, batch, *, allow_sensitive=False, expected_fence=None):
        if not isinstance(items, list) or len(items) > 20:
            raise AccessError("RAG_CONTRACT_INVALID", 502)
        from mindos.data_agent_rag import _items
        try:
            items = _items(items, list(batch))
        except ValueError:
            raise AccessError("RAG_CONTRACT_INVALID", 502) from None
        result = []
        for item in items:
            if expected_fence is not None and (item["policyVersion"], item["detectorRevision"]) != expected_fence:
                raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
            if item["verificationStatus"] != "verified" or (item["containsSensitive"] and not allow_sensitive):
                raise AccessError("SENSITIVE_CONFIRMATION_REQUIRED", 409)
            mid = item.get("materialId")
            if mid not in batch or mid not in allowed or item.get("materialVersion") != allowed[mid]["version"]:
                raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
            ref = item.get("evidenceRef")
            if not isinstance(ref, str) or not ref.startswith("erv2_") or len(ref) > 2048:
                raise AccessError("RAG_CONTRACT_INVALID", 502)
            score = item.get("score", 0)
            result.append({"ref": ref, "materialId": mid, "version": item["materialVersion"],
                           "policyVersion": item["policyVersion"], "detectorRevision": item["detectorRevision"],
                           "containsSensitive": item["containsSensitive"], "verificationStatus": item["verificationStatus"],
                           "score": score if isinstance(score, (float, int)) and math.isfinite(score) else 0})
        return result

    def resolve(self, grant, handles):
        allowed = self._allowed(grant)
        for handle in handles:
            if handle["materialId"] not in allowed or allowed[handle["materialId"]]["version"] != handle["version"]:
                raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
        outputs = []
        for offset in range(0, len(handles), 10):
            batch = handles[offset:offset + 10]
            result = self.rag.evidence_resolve([i["ref"] for i in batch])
            from mindos.data_agent_rag import _resolved_items
            try:
                verified = _resolved_items(result, list(allowed), [{**i, "evidenceRef": i["ref"], "materialVersion": i["version"]} for i in batch])
            except ValueError:
                raise AccessError("EVIDENCE_CONTEXT_CHANGED", 409) from None
            expected = {i["ref"]: i for i in batch}
            if len(verified) != len(expected):
                raise AccessError("EVIDENCE_UNAVAILABLE", 409)
            seen = set()
            for item in verified:
                handle = expected.get(item.get("evidenceRef"))
                if (not handle or item["evidenceRef"] in seen or item.get("materialId") != handle["materialId"]
                        or item.get("materialVersion") != handle["version"]
                        or item.get("verificationStatus") != "verified"):
                    raise AccessError("EVIDENCE_CONTEXT_CHANGED", 409)
                seen.add(item["evidenceRef"])
                reference = self.store.put_record("evidence_refs", grant, handle)
                locator = item.get("locator") or {}
                # Only numeric page/line locators; never return storage paths/URLs.
                location = {k: v for k, v in locator.items() if k in ("page", "line", "startLine", "endLine")
                            and type(v) is int and v >= 0}
                outputs.append({"type": "work", "text": str(item.get("text", ""))[:8000],
                    "title": str(item.get("title", ""))[:300], "version": handle["version"],
                    "delivery": handle.get("delivery", "policy"),
                    "updatedAt": allowed[handle["materialId"]].get("updatedAt"),
                    "source": {"type": "work_evidence", "reference": reference,
                               "materialId": handle["materialId"], "location": location}})
        # Fence account ACL and material revision a second time before delivery.
        current = self._allowed(grant)
        if any(h["materialId"] not in current or current[h["materialId"]]["version"] != h["version"] for h in handles):
            raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
        return outputs

    def request_preview(self, request_id):
        with self.store.db() as db:
            row = db.execute("SELECT grant_id FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise AccessError("REFERENCE_UNAVAILABLE", 404)
        grant = self.store.grant(row[0])
        if not self.store.enabled() or grant["state"] != "active" or grant["expiresAt"] <= self.store.clock():
            raise AccessError("GRANT_INACTIVE")
        record = self.store.record("requests", request_id, grant)
        allowed = self._allowed(grant)
        materials, previews = {}, []
        for pending in record["payload"].get("pending", []):
            for mid, version in pending["versions"].items():
                if mid not in allowed or allowed[mid]["version"] != version:
                    raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
                materials[mid] = {"id": mid, "title": str(allowed[mid].get("title", ""))[:300], "version": version}
            previews.extend(pending.get("previews", []))
        return {"requestId": request_id, "agentName": grant["agentName"], "state": record["state"],
                "expiresAt": record["expires"], "query": record["payload"].get("query", ""),
                "materials": list(materials.values()), "previews": previews}

    def decide(self, request_id, decision):
        # This method is wired ONLY to authenticated owner management, never tools.
        with self.store.lock:
            with self.store.db() as db:
                row = db.execute("SELECT grant_id FROM requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise AccessError("REFERENCE_UNAVAILABLE", 404)
            grant = self.store.grant(row[0])
            principal = Principal(**self.store.subject.model_dump(), agentId=grant["agentId"],
                grantId=grant["id"], audience=self.store.audience, expiresAt=int(self.store.clock()) + 900)
            self.store.check(principal)
            record = self.store.record("requests", request_id, grant)
            if record["state"] != "pending":
                raise AccessError("REQUEST_ALREADY_DECIDED", 409)
            if decision == "cancel":
                self.store.update_request(request_id, "cancelled", {})
                return {"status": "cancelled"}
            payload, allowed = record["payload"], self._allowed(grant)
            for pending in payload["pending"]:
                if any(mid not in allowed or allowed[mid]["version"] != version for mid, version in pending["versions"].items()):
                    self.store.update_request(request_id, "expired", {})
                    raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
            # Mark first. A crash/uncertain Confirm never causes a replay of user approval.
            self.store.update_request(request_id, "processing", {})
            try:
                evidence = payload["evidence"]
                for pending in payload["pending"]:
                    result = self.rag.confirm(pending["token"], desensitize=decision == "masked",
                                              idempotency_key=opaque("confirm_"))
                    if result.get("status") != "ok" or result.get("detectionNotice"):
                        raise AccessError("SENSITIVE_CHECK_INCOMPLETE", 409)
                    if type(result.get("desensitized")) is not bool or (decision == "masked" and not result["desensitized"]):
                        raise AccessError("DELIVERY_MODE_MISMATCH", 409)
                    if (result.get("policyVersion"), result.get("detectorRevision")) != (pending["policyVersion"], pending["detectorRevision"]):
                        raise AccessError("MATERIAL_CONTEXT_CHANGED", 409)
                    actual_mode = "masked" if result["desensitized"] else "original"
                    evidence.extend({**h, "delivery": actual_mode} for h in
                        self._handles(result.get("items", []), allowed, pending["versions"], allow_sensitive=actual_mode == "original",
                                      expected_fence=(pending["policyVersion"], pending["detectorRevision"])))
                self.store.check(principal, grant["revision"])
                evidence.sort(key=lambda item: item.get("score", 0), reverse=True)
                evidence = list({item["ref"]: item for item in evidence}.values())[:payload["limit"]]
                # Resolve now for policy/version validation; no body stored in request DB.
                self.resolve(grant, evidence)
                self.store.receipt(principal, "user_confirmation",
                    [{"id": request_id, "version": grant["revision"]}] +
                    [{"id": h["materialId"], "version": h["version"]} for h in evidence],
                    delivery=decision, result="approved")
                self.store.update_request(request_id, "ready", {"evidence": evidence})
            except Exception:
                self.store.update_request(request_id, "failed", {})
                raise
            return {"status": "ready"}
