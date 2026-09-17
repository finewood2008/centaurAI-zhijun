"""Owner-only UI facade. No management operation is registered as an MCP tool."""
from fastapi import APIRouter, Depends, HTTPException

from .models import AccessError, DecisionArgs, EnableChange, GrantChange, GrantSpec, StateChange
from .runtime import get_service


def build_router(guard, provider=get_service):
    router = APIRouter(prefix="/api/mindos/settings/external-agents", dependencies=[Depends(guard)])

    def run(fn):
        try:
            return fn(provider())
        except AccessError as exc:
            raise HTTPException(exc.status, {"code": exc.code}) from None
        except Exception:
            # Downstream exceptions can contain material names, network paths or credentials.
            raise HTTPException(503, {"code": "EXTERNAL_AGENTS_UNAVAILABLE"}) from None

    @router.get("")
    def status():
        try:
            service = provider()
        except AccessError:
            return {"available": False, "enabled": False, "grants": [], "endpoint": None}
        return {"available": True, "enabled": service.store.enabled(),
                "endpoint": service.store.audience, "grants": service.store.grants()}

    @router.put("/enabled")
    def enabled(body: EnableChange):
        return run(lambda s: (s.store.set_enabled(body.enabled), {"enabled": body.enabled})[1])

    @router.get("/preview")
    def preview():
        return run(lambda s: {"personal": s.personal.preview(), "materials": s.materials.catalog()})

    @router.post("/grants")
    def create(body: GrantSpec):
        return run(lambda s: s.save_grant(body))

    @router.put("/grants/{grantId}")
    def change(grantId: str, body: GrantChange):
        spec = GrantSpec.model_validate(body.model_dump(exclude={"expectedRevision"}))
        return run(lambda s: s.save_grant(spec, grantId, body.expectedRevision))

    @router.patch("/grants/{grantId}")
    def state(grantId: str, body: StateChange):
        return run(lambda s: s.store.change_state(grantId, body.expectedRevision, body.state))

    @router.get("/audit")
    def audit():
        return run(lambda s: {"items": s.store.audits()})

    @router.get("/requests/{requestId}")
    def request_preview(requestId: str):
        return run(lambda s: s.request_preview(requestId))

    @router.post("/requests/{requestId}")
    def decide(requestId: str, body: DecisionArgs):
        return run(lambda s: s.decide(requestId, body.decision))

    return router
