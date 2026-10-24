
from crewai import Agent

from src.travel_agent.llm.crewai_adapter import FallbackCrewAIModel


def create_travel_researcher() -> Agent:
    return Agent(
        role="Travel Research Specialist",
        goal=(
            "Understand the traveler's requirements and prepare "
            "a clear, accurate travel-planning brief."
        ),
        backstory=(
            "You are a careful travel research specialist. "
            "You identify missing details, distinguish preferences "
            "from verified facts, and never invent prices, availability, "
            "or booking confirmations."
        ),
        llm=FallbackCrewAIModel(),
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )
