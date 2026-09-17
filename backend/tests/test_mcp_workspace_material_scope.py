"""A first-party DE library grant does not widen an external Agent's grant."""
import json

import pytest

from tests.test_unified_mcp import env, Rag
from zhijun_mcp.models import AccessError


class WorkspaceRag(Rag):
    """DE sees the whole workspace; filters only narrow that authority."""

    def __init__(self):
        super().__init__()
        self.library = {"doc-a"}

    def item(self, mid):
        item = super().item(mid)
        item.update(title="Synthetic " + mid, text="WORKSPACE_BODY_" + mid)
        return item

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        selected = set(kwargs.get("material_ids") or self.library) & self.library
        items = [self.item(mid) for mid in sorted(selected)[:kwargs["top_k"]]]
        return {"status": "ok" if items else "no_results", "items": items, "returnedCount": len(items),
                "policyVersion": 2, "detectorRevision": "detector-2"}


def add_workspace_upload(env):
    env.rag = WorkspaceRag()
    env.service.rag = env.rag
    # This upload happens after the external grant was saved for doc-a.
    env.materials.items["doc-b"] = {"id": "doc-b", "title": "Synthetic doc-b", "version": 1}
    env.rag.library.add("doc-b")
    internal = env.rag.search("synthetic", interaction_id="internal-turn", top_k=5)
    assert {item["materialId"] for item in internal["items"]} == {"doc-a", "doc-b"}
    env.rag.calls.clear()


def test_new_workspace_upload_remains_outside_existing_nonempty_external_grant(env):
    add_workspace_upload(env)
    access = env.service.call(env.principal, "zhijun_get_access", {})
    assert access["materialIds"] == ["doc-a"]
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "synthetic"})
    assert [item["source"]["materialId"] for item in result["items"]] == ["doc-a"]
    assert env.rag.calls[0][2]["material_ids"] == ["doc-a"]
    reference = result["items"][0]["source"]["reference"]
    reread = env.service.call(env.principal, "zhijun_read_work_evidence", {"reference": reference})
    assert "WORKSPACE_BODY_doc-a" in json.dumps(reread)
    assert "doc-b" not in json.dumps([access, result, reread])
    # A DE evidence handle from the internal conversation is not an MCP handle.
    ungranted_ref = env.rag.item("doc-b")["evidenceRef"]
    calls = len(env.rag.calls)
    with pytest.raises(AccessError):
        env.service.call(env.principal, "zhijun_read_work_evidence", {"reference": ungranted_ref})
    assert len(env.rag.calls) == calls


def test_empty_acl_intersection_never_uses_de_all_workspace_default(env):
    add_workspace_upload(env)
    del env.materials.items["doc-a"]
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "synthetic"})
    assert result["items"] == []
    assert not env.rag.calls


def test_workspace_search_response_cannot_smuggle_ungranted_material(env):
    add_workspace_upload(env)
    original = env.rag.search

    def widened(query, **kwargs):
        # Simulate a broken/misconfigured downstream service ignoring the filter.
        result = original(query, **kwargs)
        result["items"].append(env.rag.item("doc-b"))
        result["returnedCount"] = len(result["items"])
        return result

    env.rag.search = widened
    with pytest.raises(AccessError, match="RAG_CONTRACT_INVALID"):
        env.service.call(env.principal, "zhijun_search_work_data", {"query": "synthetic"})
    assert not any(call[0] == "resolve" for call in env.rag.calls)
    assert env.store.audits()[-1]["result"] == "denied"


def test_workspace_resolve_cannot_substitute_ungranted_material(env):
    add_workspace_upload(env)
    result = env.service.call(env.principal, "zhijun_search_work_data", {"query": "synthetic"})
    reference = result["items"][0]["source"]["reference"]
    original = env.rag.evidence_resolve
    ungranted = env.rag.item("doc-b")

    def substituted(refs):
        response = original(refs)
        # Preserve the requested handle to exercise the material identity fence,
        # rather than failing only because the evidenceRef is unknown.
        response["items"][0].update(materialId="doc-b", text=ungranted["text"])
        return response

    env.rag.evidence_resolve = substituted
    with pytest.raises(AccessError, match="EVIDENCE_CONTEXT_CHANGED"):
        env.service.call(env.principal, "zhijun_read_work_evidence", {"reference": reference})
    assert env.store.audits()[-1]["result"] == "denied"
