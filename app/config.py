from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str | None = os.getenv("DATABASE_URL")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    kaggle_username: str | None = os.getenv("KAGGLE_USERNAME")
    kaggle_key: str | None = os.getenv("KAGGLE_KEY")
    access_token: str | None = os.getenv("APP_ACCESS_TOKEN")

    @property
    def kaggle_auth(self) -> tuple[str, str] | None:
        if self.kaggle_username and self.kaggle_key:
            return (self.kaggle_username, self.kaggle_key)
        return None


settings = Settings()
