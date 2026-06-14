"""Configuration via environment variables + .env file."""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    browser_use_api_key: str = ""
    neon_database_url: str = ""
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    database_path: str = "fcn.db"
    session_secret: str = "change-me-in-production"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()