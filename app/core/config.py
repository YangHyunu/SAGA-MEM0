from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_env: str = "development"

    # LLM Providers
    openai_api_key: str = ""
    google_api_key: str = ""
    anthropic_api_key: str = ""
    default_model: str = "gpt-4o-mini"

    # Memory (mem0 + Qdrant)
    memory_backend: str = "mem0"
    mem0_collection_name: str = "yangban_memories"
    mem0_embedder_model: str = "text-embedding-3-small"
    qdrant_path: str = "./qdrant_db"
    qdrant_url: str | None = None

    # Curator
    curator_interval: int = 10
    curator_model: str = "gpt-4o-mini"

    # Characters
    charx_storage_dir: Path = Field(default=Path("./characters"))

    # Observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "yangban"
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"

    # Rate Limiting
    rate_limit_default: str = "60/minute"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


settings = Settings()
