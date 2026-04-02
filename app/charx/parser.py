import io
import json
import zipfile
from pathlib import Path

import structlog

from app.charx.schemas import CharacterCard

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

ZIP_MAGIC = b"PK\x03\x04"


def find_zip_offset(data: bytes) -> int:
    offset = data.find(ZIP_MAGIC)
    if offset == -1:
        raise ValueError("ZIP magic bytes (PK\\x03\\x04) not found in file")
    return offset


def parse_charx(file_path: str | Path, user_name: str = "User") -> CharacterCard:
    path = Path(file_path)
    raw = path.read_bytes()

    offset = find_zip_offset(raw)
    logger.info(
        "charx_zip_offset_found",
        file=path.name,
        offset=offset,
        total_bytes=len(raw),
    )

    zip_data = raw[offset:]
    with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
        if "card.json" not in zf.namelist():
            raise FileNotFoundError("card.json not found in charx archive")

        card_raw = zf.read("card.json")

    card_dict = json.loads(card_raw)

    data = card_dict.get("data", card_dict)

    card = CharacterCard.model_validate(data)

    card = _replace_user_placeholder(card, user_name)

    logger.info(
        "charx_parsed",
        character=card.name,
        lorebook_entries=len(card.character_book.entries) if card.character_book else 0,
    )

    return card


def _replace_user_placeholder(card: CharacterCard, user_name: str) -> CharacterCard:
    replacements = {
        "description": card.description.replace("{{user}}", user_name),
        "first_mes": card.first_mes.replace("{{user}}", user_name),
        "scenario": card.scenario.replace("{{user}}", user_name),
        "system_prompt": card.system_prompt.replace("{{user}}", user_name),
        "mes_example": card.mes_example.replace("{{user}}", user_name),
        "personality": card.personality.replace("{{user}}", user_name),
    }

    greetings = [g.replace("{{user}}", user_name) for g in card.alternate_greetings]
    replacements["alternate_greetings"] = greetings

    if card.character_book:
        entries = []
        for entry in card.character_book.entries:
            new_content = entry.content.replace("{{user}}", user_name)
            entries.append(entry.model_copy(update={"content": new_content}))
        new_book = card.character_book.model_copy(update={"entries": entries})
        replacements["character_book"] = new_book

    return card.model_copy(update=replacements)
