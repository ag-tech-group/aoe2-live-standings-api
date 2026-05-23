"""Shared slowapi limiter instance.

Defined in its own module so router files can apply per-route limits
via ``@limiter.limit(...)`` without creating a circular import with
``app.main`` (which mounts the routers).

The catch-all default ``60/minute`` applies to any route that doesn't
carry its own decorator; write routes on the management API override
this with tighter per-endpoint caps (see #60).
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
