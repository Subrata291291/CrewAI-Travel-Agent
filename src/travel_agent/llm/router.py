
from dataclasses import dataclass

from google import genai
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from src.travel_agent.llm.config import (
    PROVIDER_ORDER,
    get_provider_config,
)


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str


class AllProvidersFailed(RuntimeError):
    """Raised when every eligible provider fails."""

    pass


def is_recoverable_error(exc: Exception) -> bool:
    """Decide whether moving to the next provider is appropriate."""

    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True

    if isinstance(exc, APIStatusError):
        status = exc.status_code

        # Authentication, permission, quota, rate-limit and
        # server errors can justify using another provider.
        return status in (401, 403, 408, 409, 429) or status >= 500

    # Gemini SDK errors can expose an HTTP status code.
    status = getattr(exc, "code", None)
    if status is None:
        status = getattr(exc, "status_code", None)

    if isinstance(status, int):
        return status in (401, 403, 408, 409, 429) or status >= 500

    # Unknown errors are not silently treated as recoverable.
    return False


def call_provider(name: str, prompt: str) -> LLMResult:
    """Make exactly one request to the selected provider."""

    config = get_provider_config(name)

    if not config.api_key:
        raise ValueError(f"{name}: API key is missing.")

    if not config.model:
        raise ValueError(f"{name}: model ID is missing.")

    if name in ("groq", "openrouter", "openai"):
        client_args = {
            "api_key": config.api_key,
            "max_retries": 0,
            "timeout": 30.0,
        }

        if config.base_url:
            client_args["base_url"] = config.base_url

        client = OpenAI(**client_args)

        response = client.chat.completions.create(
            model=config.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful AI travel planning assistant. "
                        "Never claim a booking is confirmed unless a booking "
                        "system has actually confirmed it."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )

        answer = response.choices[0].message.content

        if not answer:
            raise RuntimeError(f"{name}: provider returned empty content.")

        return LLMResult(
            text=answer,
            provider=name,
            model=config.model,
        )

    if name == "gemini":
        client = genai.Client(
            api_key=config.api_key,
            http_options={"timeout": 30_000},
        )

        response = client.models.generate_content(
            model=config.model,
            contents=prompt,
        )

        answer = response.text

        if not answer:
            raise RuntimeError("gemini: provider returned empty content.")

        return LLMResult(
            text=answer,
            provider=name,
            model=config.model,
        )

    raise ValueError(f"Unsupported provider: {name}")


def generate(prompt: str) -> LLMResult:
    """Try providers in order, without retrying any provider."""

    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty.")

    failures = []

    for name in PROVIDER_ORDER:
        try:
            result = call_provider(name, prompt)

            # Log only provider/model names, never prompts or secrets.
            print(f"LLM success: provider={result.provider}, model={result.model}")

            return result

        except Exception as exc:
            # Do not print exception text: some SDK errors may include
            # request details. Log only the error class.
            print(
                f"LLM provider failed: {name}; "
                f"error_type={type(exc).__name__}"
            )

            failures.append((name, exc))

            if not is_recoverable_error(exc):
                raise RuntimeError(
                    f"Non-recoverable error from {name}; "
                    "fallback stopped. Inspect the error type safely."
                ) from None

    attempted = ", ".join(name for name, _ in failures)

    raise AllProvidersFailed(
        f"All configured LLM providers failed. Attempted: {attempted}"
    )
