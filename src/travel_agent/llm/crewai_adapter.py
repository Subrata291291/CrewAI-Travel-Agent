
from typing import Any

from crewai.llms.base_llm import BaseLLM

from src.travel_agent.llm.router import generate


class FallbackCrewAIModel(BaseLLM):
    model: str = "fallback/groq-openrouter-gemini-openai"
    provider: str = "custom"

    def __init__(self, **data: Any):
        data.setdefault("model", "fallback/groq-openrouter-gemini-openai")
        super().__init__(**data)

    def call(
        self,
        messages: Any,
        tools: list | None = None,
        callbacks: list | None = None,
        available_functions: dict | None = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> str:
        if tools or available_functions:
            raise NotImplementedError(
                "Tool calling is not supported by this initial adapter."
            )

        if response_model is not None:
            raise NotImplementedError(
                "Structured responses are not supported by this adapter yet."
            )

        if isinstance(messages, str):
            prompt = messages
        elif isinstance(messages, list):
            parts = []

            for message in messages:
                if isinstance(message, dict):
                    role = message.get("role", "user")
                    content = message.get("content", "")
                else:
                    role = getattr(message, "role", "user")
                    content = getattr(message, "content", "")

                if isinstance(content, str) and content.strip():
                    parts.append(f"{role}: {content}")

            prompt = "\n\n".join(parts)
        else:
            raise ValueError("Unsupported message format.")

        if not prompt.strip():
            raise ValueError("The LLM prompt is empty.")

        result = generate(prompt)
        return result.text
