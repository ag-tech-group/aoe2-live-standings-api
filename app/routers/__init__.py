from app.routers.leaderboards import router as leaderboards_router
from app.routers.live import router as live_router
from app.routers.matches import router as matches_router
from app.routers.players import router as players_router

__all__ = [
    "leaderboards_router",
    "live_router",
    "matches_router",
    "players_router",
]
