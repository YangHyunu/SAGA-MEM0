from fastapi import APIRouter

from app.api.v1.characters import router as characters_router
from app.api.v1.chat import router as chat_router
from app.api.v1.memory import router as memory_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(chat_router, tags=["chat"])
api_router.include_router(characters_router, tags=["characters"])
api_router.include_router(memory_router, tags=["memory"])
