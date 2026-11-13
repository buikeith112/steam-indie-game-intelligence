"""Runtime configuration loaded from environment variables / .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    steam_api_key: str | None = None
    database_url: str = "postgresql+psycopg://steam:steam@localhost:5432/steam_intel"
    itad_api_key: str | None = None

    # Request budgets (requests per second) per host family.
    store_rps: float = 0.6        # store.steampowered.com (appdetails, histogram)
    webapi_rps: float = 2.0       # api.steampowered.com (Query, GetItems, reviews, CCU)
    steamspy_rps: float = 1.0     # steamspy.com

    user_agent: str = "steam-indie-game-intelligence/0.1 (research project)"
    store_review_text: bool = False

    # Analytical scope
    min_release_year: int = 2018
    min_reviews: int = 10
    review_sample_per_type: int = 100   # helpful + recent samples per game


@lru_cache
def get_settings() -> Settings:
    return Settings()
