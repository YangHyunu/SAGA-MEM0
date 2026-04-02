import structlog

from app.core.config import settings
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.google import GoogleProvider
from app.providers.openai import OpenAIProvider

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_providers: dict[str, LLMProvider] = {}


def _get_or_create_provider(provider_name: str) -> LLMProvider:
    if provider_name not in _providers:
        if provider_name == "openai":
            _providers["openai"] = OpenAIProvider()
        elif provider_name == "google":
            _providers["google"] = GoogleProvider()
        elif provider_name == "anthropic":
            _providers["anthropic"] = AnthropicProvider()
        else:
            raise ValueError(f"Unknown provider: {provider_name}")
    return _providers[provider_name]


def route_to_provider(model: str) -> tuple[LLMProvider, str]:
    provider_name = detect_provider(model)
    provider = _get_or_create_provider(provider_name)
    logger.info("llm_provider_routed", model=model, provider=provider_name)
    return provider, provider_name


def detect_provider(model: str) -> str:
    model_lower = model.lower()
    if model_lower.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "openai"
    if model_lower.startswith(("gemini-", "gemma-")):
        return "google"
    if model_lower.startswith(("claude-",)):
        return "anthropic"
    return "openai"
