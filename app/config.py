"""Centralised, env-driven settings.

Pydantic-settings gives us typed validation at startup — we fail fast on
boot instead of discovering a missing key mid-flight over the wire.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8080

    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"

    llm_provider: Literal["groq", "xai"] = "groq"
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-70b-versatile"
    xai_api_key: str = ""
    xai_model: str = "grok-2-latest"

    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""

    aws_region: str = "us-east-1"
    dynamodb_table: str = "CAM_HardwareTokens"

    allow_unverified_tokens: bool = Field(
        default=False,
        description="Dev-only bypass for DynamoDB hardware-token verification.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
