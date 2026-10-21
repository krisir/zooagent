from fastapi import APIRouter

from .actions_ui import router as actions_router
from .applications import router as applications_router
from .chat import router as chat_router
from .health import router as health_router
from .models import router as data_router
from .settings import router as settings_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(applications_router)
api_router.include_router(data_router)
api_router.include_router(actions_router)
api_router.include_router(chat_router)
api_router.include_router(settings_router)

__all__ = ["api_router"]
