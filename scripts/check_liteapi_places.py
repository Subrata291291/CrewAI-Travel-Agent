
import sys
from pathlib import Path

# Add the project root to Python's module search path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.travel_agent.integrations.liteapi_client import (
    LiteAPIClient,
    LiteAPIError,
)


def main() -> None:
    try:
        client = LiteAPIClient()
        places = client.search_places("Goa, India")

        if not places:
            print("Connection succeeded, but no places were returned.")
            return

        print(f"Place lookup succeeded. Results: {len(places)}")

        for place in places[:5]:
            print("-" * 40)
            print("Name:", place.get("displayName", "Unknown"))
            print("Place ID:", place.get("placeId", "Missing"))
            print(
                "Address:",
                place.get("formattedAddress", "Not provided"),
            )

    except (ValueError, LiteAPIError) as exc:
        print(f"LiteAPI test failed: {exc}")


if __name__ == "__main__":
    main()
