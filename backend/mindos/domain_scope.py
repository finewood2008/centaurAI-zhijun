"""Domain scope is explicit in a fixed private worker; legacy Web is separate."""
import os
from fastapi import HTTPException


def _device_scope_of(request=None):
    from .uploads import _device_scope_of as legacy_scope
    return legacy_scope(request)
