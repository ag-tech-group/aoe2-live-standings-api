"""Team management endpoints, scoped to a tournament — owner-gated writes.

Team *standings* (the computed read view) stay in ``tournaments.py`` at
``/tournaments/{slug}/teams/standings``. This router is the write surface:
creating, editing, and deleting teams and their members. Every route is
gated by ``require_tournament_owner``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, audit
from app.auth import get_current_user_id, require_tournament_owner
from app.database import get_async_session
from app.limiting import limiter
from app.models import Team, TeamMember, Tournament
from app.schemas import TeamCreate, TeamMemberCreate, TeamRead, TeamUpdate

router = APIRouter(prefix="/tournaments/{tournament_slug}/teams", tags=["teams"])


async def get_owned_team(
    team_id: int,
    tournament: Tournament = Depends(require_tournament_owner),
    session: AsyncSession = Depends(get_async_session),
) -> Team:
    """Resolve ``{team_id}`` to a team in the owner-gated tournament, or 404.

    Builds on ``require_tournament_owner`` — the caller is already
    confirmed an owner of ``{tournament_slug}`` — and additionally scopes
    the team to that tournament, so a ``team_id`` belonging to another
    tournament is unreachable here.
    """
    team = (
        await session.execute(
            select(Team).where(Team.id == team_id, Team.tournament_id == tournament.id)
        )
    ).scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


@router.post("", status_code=201)
@limiter.limit("10/minute")
async def create_team(
    request: Request,
    payload: TeamCreate,
    tournament: Tournament = Depends(require_tournament_owner),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> TeamRead:
    """Create a team within the tournament — owner-gated.

    The new team starts with no members. Team names are not unique: a
    tournament may hold several teams sharing a name.
    """
    team = Team(tournament_id=tournament.id, name=payload.name, initials=payload.initials)
    session.add(team)
    await session.commit()
    audit(
        AuditAction.TEAM_CREATE,
        actor_user_id=actor_user_id,
        tournament_slug=tournament.slug,
        tournament_id=tournament.id,
        target_team_id=team.id,
        name=payload.name,
    )
    return TeamRead.model_validate(team)


@router.patch("/{team_id}")
@limiter.limit("20/minute")
async def update_team(
    request: Request,
    payload: TeamUpdate,
    team: Team = Depends(get_owned_team),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> TeamRead:
    """Edit a team's name or initials — owner-gated.

    PATCH semantics: only the fields present in the request body change.
    """
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(team, field, value)
    await session.commit()
    audit(
        AuditAction.TEAM_UPDATE,
        actor_user_id=actor_user_id,
        tournament_id=team.tournament_id,
        target_team_id=team.id,
        changes=changes,
    )
    return TeamRead.model_validate(team)


@router.delete("/{team_id}", status_code=204)
@limiter.limit("10/minute")
async def delete_team(
    request: Request,
    team: Team = Depends(get_owned_team),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a team — owner-gated.

    The team's ``TeamMember`` rows cascade with it. Polled player data is
    untouched; only the team grouping is removed.
    """
    audit_payload = {
        "tournament_id": team.tournament_id,
        "target_team_id": team.id,
    }
    await session.delete(team)
    await session.commit()
    audit(AuditAction.TEAM_DELETE, actor_user_id=actor_user_id, **audit_payload)


@router.post("/{team_id}/members", status_code=204)
@limiter.limit("20/minute")
async def add_team_member(
    request: Request,
    payload: TeamMemberCreate,
    team: Team = Depends(get_owned_team),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Add a profile to a team — owner-gated.

    409 if the profile is already on the team. Team membership is separate
    from the tournament roster — this does not add the profile to
    ``TournamentPlayer``.
    """
    existing = (
        await session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team.id,
                TeamMember.profile_id == payload.profile_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Player already on the team")

    session.add(TeamMember(team_id=team.id, profile_id=payload.profile_id))
    await session.commit()
    audit(
        AuditAction.TEAM_MEMBER_ADD,
        actor_user_id=actor_user_id,
        tournament_id=team.tournament_id,
        target_team_id=team.id,
        target_profile_id=payload.profile_id,
    )


@router.delete("/{team_id}/members/{profile_id}", status_code=204)
@limiter.limit("20/minute")
async def remove_team_member(
    request: Request,
    profile_id: int,
    team: Team = Depends(get_owned_team),
    actor_user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Remove a profile from a team — owner-gated.

    404 if the profile isn't on the team.
    """
    member = (
        await session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team.id,
                TeamMember.profile_id == profile_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Player not found on this team")

    await session.delete(member)
    await session.commit()
    audit(
        AuditAction.TEAM_MEMBER_REMOVE,
        actor_user_id=actor_user_id,
        tournament_id=team.tournament_id,
        target_team_id=team.id,
        target_profile_id=profile_id,
    )
