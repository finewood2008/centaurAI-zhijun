"""Domain scope is explicit in a fixed private worker; legacy Web is separate."""
import os
from fastapi import HTTPException


def _device_scope_of(request=None):
    if os.environ.get("ZHIJUN_WORKSPACE_ID"):
        from zhijun_worker.capabilities import current
        runtime = current()
        if runtime is None:
            raise HTTPException(503, {"code": "WORKER_NOT_RUNNING"})
        if request is not None and getattr(request.state, "zhijun_workspace", None) is not runtime[0]:
            raise HTTPException(401, {"code": "WORKER_PROOF_REQUIRED"})
        # This is the only scope in a physically isolated, immutable owner process.
        return "global"
    from .uploads import _device_scope_of as legacy_scope
    return legacy_scope(request)
