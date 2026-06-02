"""Community Hype voting request/response schemas — the FE contract (#210).

Voters submit a full ballot (replace semantics) per category, read back
their own ballot, and read aggregate tallies. ``coins >= 0`` and
"no duplicate target within a category" are enforced here; the
server-authoritative checks that need live tournament state (per-category
``sum <= budget``, target-exists-in-this-tournament) run in the handler.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Bound a single ballot's entry count per category — far above the
# realistic target count (≤24) but small enough to keep payloads sane and
# cap per-request work. Foreign / unknown target ids are rejected by the
# handler regardless of this.
_MAX_BALLOT_ENTRIES = 256
# Mirrors the ``voter_token`` column width (String(64)); the FE generates
# it, typically a random UUID persisted in localStorage.
_MAX_VOTER_TOKEN_LENGTH = 64
# Cloudflare Turnstile response tokens are short-lived and bounded; cap
# generously. Verified server-side in #211.
_MAX_TURNSTILE_TOKEN_LENGTH = 2048


class FanVoteAllocation(BaseModel):
    """One ballot line: Hype (``coins``) on one target.

    ``target_id`` is the stable surrogate id — a ``tournament_player_id``
    for the ``players`` category, a ``team_id`` for ``teams`` (#187-safe).
    Used both in the submitted ballot and the ``/me`` read-back.
    """

    target_id: int = Field(ge=1)
    coins: int = Field(ge=0)


class FanVoteBallotSubmit(BaseModel):
    """Body for ``PUT /fan-votes`` — the caller's entire ballot (replace).

    An empty category list clears that category; both categories are
    replaced atomically. ``voter_token`` is the convenience key the FE
    persists to re-edit a ballot (not a security identity).
    ``turnstile_token`` is the Cloudflare Turnstile response — accepted
    here as part of the published contract, verified server-side in #211
    (optional now so the endpoint functions before the secret is wired).
    The per-category budget cap and target validity are enforced
    server-side in the handler.
    """

    voter_token: str = Field(min_length=1, max_length=_MAX_VOTER_TOKEN_LENGTH)
    turnstile_token: str | None = Field(default=None, max_length=_MAX_TURNSTILE_TOKEN_LENGTH)
    players: list[FanVoteAllocation] = Field(default_factory=list, max_length=_MAX_BALLOT_ENTRIES)
    teams: list[FanVoteAllocation] = Field(default_factory=list, max_length=_MAX_BALLOT_ENTRIES)

    @field_validator("players", "teams")
    @classmethod
    def _no_duplicate_targets(cls, value: list[FanVoteAllocation]) -> list[FanVoteAllocation]:
        target_ids = [allocation.target_id for allocation in value]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("a target_id may appear at most once per category")
        return value


class FanVoteBallotRead(BaseModel):
    """The caller's current ballot — ``GET /fan-votes/me`` and the PUT echo.

    Only targets with ``coins > 0`` are stored, so these lists carry the
    voter's live allocations; a cleared or never-set category is an empty
    list. The FE prefills the wallet UI from this.
    """

    players: list[FanVoteAllocation]
    teams: list[FanVoteAllocation]


class FanVoteTallyEntry(BaseModel):
    """Aggregate Hype for one target: total ``coins`` and distinct ``backers``."""

    target_id: int
    coins: int
    backers: int


class FanVoteTallies(BaseModel):
    """Aggregate-on-read tallies for both categories (``GET /fan-votes/tallies``).

    Per target: ``SUM(coins)`` and ``COUNT(DISTINCT voter_token)``. Derived
    from the ballot rows on every read — no counters, so reallocation can
    never drift them. Each list is ordered by ``coins`` descending (board
    order).
    """

    players: list[FanVoteTallyEntry]
    teams: list[FanVoteTallyEntry]
