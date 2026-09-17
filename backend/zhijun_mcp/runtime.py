"""Composition inside the independently managed, identity-bound Zhijun worker."""
import os
import threading
from urllib.parse import urlsplit

from .models import AccessError, Subject
from .store import AccessStore

_runtime = None
_lock = threading.RLock()


class MaterialAccess:
    def __init__(self, capabilities):
        self.capabilities = capabilities

    def describe(self, ids):
        if not ids:
            return {}
        result = self.capabilities.call("external_agents.materials.describe", {"materialIds": list(ids)})
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise AccessError("MATERIAL_ACCESS_UNAVAILABLE", 503)
        items = {}
        for item in result["items"]:
            if (not isinstance(item, dict) or item.get("id") not in ids
                    or type(item.get("version")) is not int or item["version"] < 1):
                raise AccessError("MATERIAL_ACCESS_INVALID", 502)
            items[item["id"]] = item
        return items

    def catalog(self):
        result = self.capabilities.call("external_agents.materials.list", {"limit": 1000})
        # This is an OWNER-only preview, never an external tool.
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise AccessError("MATERIAL_ACCESS_UNAVAILABLE", 503)
        return [{k: item.get(k) for k in ("id", "title", "version", "updatedAt")}
                for item in result["items"][:1000]]


def get_service():
    global _runtime
    from zhijun_worker import capabilities as ports
    bound = ports.current()
    if not bound:
        raise AccessError("MCP_WORKSPACE_REQUIRED", 503)
    workspace, capabilities = bound
    resource = os.environ.get("ZHIJUN_MCP_RESOURCE_URL", "")
    parsed = urlsplit(resource)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/mcp"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise AccessError("MCP_NOT_PROVISIONED", 503)
    with _lock:
        if _runtime and _runtime[0] is workspace:
            return _runtime[1]
        from mindos.stores.ontology_store import OntologyStore
        from mindos.stores.conversation_store import ConversationStore
        from mindos.stores.growth_store import GrowthStore
        from zhijun_worker.data_agent_rag_v2 import configured_client
        from .personal import PersonalContext
        from .service import ExternalAgentService
        subject = Subject(accountId=workspace.account_id, boxId=workspace.device_id,
            workspaceId=workspace.workspace_id, ownershipEpoch=workspace.ownership_epoch)
        store = AccessStore(workspace.data_root / "db" / "external_agents.db", subject, resource)
        personal = PersonalContext(OntologyStore.instance(), ConversationStore.instance(), GrowthStore.instance(), store)
        service = ExternalAgentService(store, personal, configured_client(), MaterialAccess(capabilities),
                                       public_origin=f"https://{parsed.netloc}")
        _runtime = (workspace, service)
        return service


def invoke(envelope):
    from .models import Principal
    from pydantic import ValidationError
    if not isinstance(envelope, dict) or set(envelope) != {"principal", "tool", "arguments"}:
        raise AccessError("INVALID_REQUEST", 400)
    try:
        principal = Principal.model_validate(envelope["principal"])
        return get_service().call(principal, envelope["tool"], envelope["arguments"])
    except ValidationError:
        raise AccessError("INVALID_REQUEST", 400) from None


def invoke_owner(envelope):
    """Only Gateway-signed box-browser calls, after account ticket verification."""
    from .models import GrantSpec, DecisionArgs
    service = get_service()
    if (type(envelope) is not dict or set(envelope) != {"subject", "action", "arguments"}
            or envelope["subject"] != service.store.subject.model_dump()):
        raise AccessError()
    action, args = envelope["action"], envelope["arguments"]
    if action == "status" and args == {}:
        return {"enabled": service.store.enabled()}
    if not service.store.enabled():
        raise AccessError("MCP_DISABLED")
    if action == "preview" and args == {}:
        return {"personal": service.personal.preview(), "materials": service.materials.catalog()}
    if action == "create_grant":
        return service.save_grant(GrantSpec.model_validate(args))
    if action == "revoke_grant" and set(args) == {"grantId", "revision"}:
        return service.store.change_state(args["grantId"], args["revision"], "revoked")
    if action in {"request_preview", "decide"}:
        if action == "decide" and set(args) == {"requestId", "decision"}:
            decision = DecisionArgs(decision=args["decision"])
            return service.decide(args["requestId"], decision.decision)
        if action == "request_preview" and set(args) == {"requestId"}:
            return service.request_preview(args["requestId"])
    raise AccessError("OWNER_ACTION_INVALID", 400)
