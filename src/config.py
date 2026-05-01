"""
Configuration module using pydantic-settings.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "X-v2 Collector"
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    environment: str = Field(default="development", env="ENVIRONMENT")

    # Redis — used to publish tweets to the existing trading-bot stream
    redis_host: str = Field(default="localhost", env="REDIS_HOST")
    redis_port: int = Field(default=6379, env="REDIS_PORT")
    redis_db: int = Field(default=0, env="REDIS_DB")
    redis_url: str = Field(default="", env="REDIS_URL")

    # Twitter/X account burner credentials
    twitter_username: str = Field(default="", env="TWITTER_USERNAME")
    twitter_password: str = Field(default="", env="TWITTER_PASSWORD")
    twitter_email: str = Field(default="", env="TWITTER_EMAIL")

    # Accounts to scrape (comma-separated)
    x_accounts_to_track: List[str] = Field(
        default=["Deltaone", "financialjuice"],
        env="X_ACCOUNTS_TO_TRACK"
    )

    # Timing
    poll_interval_min: int = Field(default=45, env="POLL_INTERVAL_MIN")
    poll_interval_max: int = Field(default=55, env="POLL_INTERVAL_MAX")

    # Engine thresholds
    twikit_failure_threshold: int = Field(default=3, env="TWIKIT_FAILURE_THRESHOLD")

    # Session persistence
    cookies_path: str = Field(default="/app/data/cookies.json", env="COOKIES_PATH")

    # Health API
    health_port: int = Field(default=8001, env="HEALTH_PORT")
    health_host: str = Field(default="0.0.0.0", env="HEALTH_HOST")

    # Redis Stream config (must match existing v1 collector)
    stream_key: str = Field(default="tweets:raw", env="REDIS_STREAM_KEY")
    stream_maxlen: int = Field(default=100000, env="REDIS_STREAM_MAXLEN")

    @property
    def resolved_redis_url(self) -> str:
        if self.redis_url:
            return self.redis_url
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


# Global instance
settings = Settings()


def get_settings() -> Settings:
    return settings
