import shutil
from pathlib import Path

import structlog
from fastapi import APIRouter, Request, UploadFile

from app.charx.lorebook import LorebookEngine
from app.charx.parser import parse_charx
from app.charx.schemas import CharacterCard
from app.core.config import settings
from app.core.limiter import limiter

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

router = APIRouter()


@router.post("/characters/upload")
@limiter.limit(settings.rate_limit_default)
async def upload_character(request: Request, file: UploadFile) -> dict:
    storage_dir = settings.charx_storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)

    dest = storage_dir / (file.filename or "unknown.charx")
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    user_name = request.headers.get("x-user-name", "User")
    card = parse_charx(dest, user_name=user_name)

    if card.character_book:
        lorebook = LorebookEngine(card.character_book.entries)
        request.app.state.lorebook = lorebook
        request.app.state.active_character = card

    logger.info(
        "character_uploaded",
        name=card.name,
        file=dest.name,
        lorebook_entries=len(card.character_book.entries) if card.character_book else 0,
    )

    return {
        "name": card.name,
        "file": dest.name,
        "lorebook_entries": len(card.character_book.entries) if card.character_book else 0,
    }


@router.get("/characters")
@limiter.limit(settings.rate_limit_default)
async def list_characters(request: Request) -> list[dict]:
    storage_dir = settings.charx_storage_dir
    if not storage_dir.exists():
        return []

    characters: list[dict] = []
    for path in storage_dir.glob("*.charx"):
        try:
            card = parse_charx(path)
            characters.append({
                "name": card.name,
                "file": path.name,
                "description": card.description[:200] if card.description else "",
            })
        except Exception:
            logger.exception("character_parse_failed", file=path.name)
            characters.append({"name": path.stem, "file": path.name, "error": True})

    return characters
