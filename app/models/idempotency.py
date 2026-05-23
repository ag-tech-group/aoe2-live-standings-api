"""Idempotency-key cache table (#61).

When a client sends `Idempotency-Key: <uuid>` on a write request, the
middleware in `app.middleware.idempotency` stores the (key,
request_fingerprint) → response so a retry with the same key returns
the cached response instead of re-executing the side effect. Rows
older than 24h are purged out-of-band (Cloud Run Job — follow-up).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IdempotencyKey(Base):
    """One row per `(key)` — the cached result of an earlier write.

    Fingerprint captures method + path + body hash so a client that
    reuses a key with a *different* request gets a 422, not a
    silently-replayed unrelated response.
    """

    __tablename__ = "idempotency_keys"

    # The client-supplied key. UUIDs are 36 chars; a 64-char column
    # gives headroom for any opaque token format up to that size.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)

    # SHA-256 of `${method} ${path} ${body_bytes}` — 64 hex chars.
    # Doesn't need to be cryptographic, just unique enough to detect
    # accidental key reuse across genuinely different requests.
    request_fingerprint: Mapped[str] = mapped_column(String(64))

    # Cached response payload — captured by the middleware.
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[bytes] = mapped_column(LargeBinary)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        # Drives the retention purge: WHERE created_at < now() - 24h.
        Index("ix_idempotency_keys_created_at", "created_at"),
    )
