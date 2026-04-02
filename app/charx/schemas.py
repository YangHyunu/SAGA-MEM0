from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LorebookEntry(BaseModel):
    model_config = {"str_strip_whitespace": True}

    keys: list[str] = Field(default_factory=list)
    content: str
    constant: bool = False
    insertion_order: int = 100
    enabled: bool = True
    name: str = ""
    comment: str = ""


class CharacterBook(BaseModel):
    entries: list[LorebookEntry] = Field(default_factory=list)


class CharacterCard(BaseModel):
    model_config = {"str_strip_whitespace": True, "populate_by_name": True}

    name: str
    description: str = ""
    first_mes: str = ""
    alternate_greetings: list[str] = Field(default_factory=list)
    character_book: Optional[CharacterBook] = None
    personality: str = ""
    scenario: str = ""
    mes_example: str = ""
    system_prompt: str = ""
    extensions: dict = Field(default_factory=dict)
