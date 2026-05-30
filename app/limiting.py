"""Shared slowapi limiter instance.

Defined in its own module so router files can apply per-route limits
via ``@limiter.limit(...)`` without creating a circular import with
``app.main`` (which mounts the routers).

The catch-all default applies to any route that doesn't carry its own
decorator — in practice, every public read endpoint (/standings, /live,
/players, /tournaments, etc.). Write routes on the management API
override this with tighter per-endpoint caps (see #60).

**Why 300/minute and not 60:** public reads are CDN-cached
(``s-maxage=15``) so steady-state viewer traffic coalesces at the edge
and never hits this limit. The case the cap has to absorb is the
*cold-CDN burst* — a deploy purges the cache, and dozens or hundreds of
viewers behind a single NAT IP (corporate office, campus, ISP CGNAT)
all miss simultaneously, generating a one-time spike from one IP. At
60/minute that spike trips 429s for everyone past the first ~15
viewers; at 300/minute the burst is absorbed and the next 15-second
window the edge is hot again. A runaway crawler still gets backstopped
— 300/minute is a small fraction of what a real scraper would attempt.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["300/minute"])
