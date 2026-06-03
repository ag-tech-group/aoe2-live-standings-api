from app.routers.civilizations import router as civilizations_router
from app.routers.leaderboards import router as leaderboards_router
from app.routers.live import router as live_router
from app.routers.matches import router as matches_router
from app.routers.me import router as me_router
from app.routers.owners import router as owners_router
from app.routers.players import router as players_router
from app.routers.stream import router as stream_router
from app.routers.teams import router as teams_router
from app.routers.tournaments import router as tournaments_router

__all__ = [
    "civilizations_router",
    "leaderboards_router",
    "live_router",
    "matches_router",
    "me_router",
    "owners_router",
    "players_router",
    "stream_router",
    "teams_router",
    "tournaments_router",
]
