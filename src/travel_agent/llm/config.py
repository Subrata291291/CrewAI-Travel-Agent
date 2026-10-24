
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load secrets from the project's .env file.
load_dotenv()


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    model: str
    base_url: str | None = None


PROVIDER_ORDER = [
    name.strip().lower()
    for name in os.getenv(
        "LLM_PROVIDER_ORDER",
        "groq,openrouter,gemini,openai",
    ).split(",")
    if name.strip()
]


def get_provider_config(name: str) -> ProviderConfig:
    """Load one provider's settings without exposing its API key."""

    settings = {
        "groq": {
            "key": "GROQ_API_KEY",
            "model": "GROQ_MODEL",
            "url": "https://api.groq.com/openai/v1",
        },
        "openrouter": {
            "key": "OPENROUTER_API_KEY",
            "model": "OPENROUTER_MODEL",
            "url": "https://openrouter.ai/api/v1",
        },
        "gemini": {
            "key": "GEMINI_API_KEY",
            "model": "GEMINI_MODEL",
            "url": None,
        },
        "openai": {
            "key": "OPENAI_API_KEY",
            "model": "OPENAI_MODEL",
            "url": None,
        },
    }

    name = name.strip().lower()

    if name not in settings:
        raise ValueError(f"Unsupported LLM provider: {name}")

    item = settings[name]
    api_key = os.getenv(item["key"], "").strip()
    model = os.getenv(item["model"], "").strip()

    return ProviderConfig(
        name=name,
        api_key=api_key,
        model=model,
        base_url=item["url"],
    )


def validate_configuration() -> list[str]:
    """Return safe configuration warnings; never print secrets."""

    warnings = []

    for name in PROVIDER_ORDER:
        try:
            config = get_provider_config(name)
        except ValueError as exc:
            warnings.append(str(exc))
            continue

        if not config.api_key:
            warnings.append(f"{name}: API key is missing.")
        if not config.model:
            warnings.append(f"{name}: model ID is missing.")

    return warnings
