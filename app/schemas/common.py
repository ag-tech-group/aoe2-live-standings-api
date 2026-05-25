"""Shared schema types and helpers used across response models."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel


class ListEnvelope[T](BaseModel):
    """Response shape for every list endpoint: ``{ last_polled_at, items }``.

    ``last_polled_at`` is the max ``updated_at`` across the rows the list
    covers (including nested rows where applicable). ``null`` when the
    list is empty. Once the polling worker lands, normal write traffic
    drives this value through the existing ``updated_at`` columns — no
    separate poller-status table needed.
    """

    last_polled_at: datetime | None
    items: list[T]


def compute_last_polled_at(timestamps: Iterable[datetime | None]) -> datetime | None:
    """Return the max non-null timestamp, or ``None`` if every input is null/empty.

    Used by routers to derive the response-level ``last_polled_at`` from the
    ``updated_at`` columns on whatever rows the response is about to return.
    """
    present = [t for t in timestamps if t is not None]
    return max(present) if present else None
