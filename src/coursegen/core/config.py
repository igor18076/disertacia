from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    postgres_host: str = Field(default="postgres", min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(default="coursegen", min_length=1)
    postgres_user: str = Field(default="coursegen", min_length=1)
    postgres_password: SecretStr = Field(min_length=1)
    qdrant_url: AnyHttpUrl = AnyHttpUrl("http://qdrant:6333")
    probe_timeout_seconds: int = Field(default=3, ge=2, le=30)
    document_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    document_max_pages: int = Field(default=2000, ge=1)
    document_max_text_chars: int = Field(default=8_000_000, ge=1)
    chunk_strategy: Literal["fixed", "section", "hierarchical"] = "section"
    chunk_size: int = Field(default=800, ge=8, le=4096)
    chunk_overlap: float = Field(default=0.12, ge=0, le=0.5)
    worker_poll_seconds: float = Field(default=1, gt=0, le=60)
    embedding_model_dir: Path = Path("models/rubert-tiny2-int8")
    search_batch_size: int = Field(default=16, ge=1, le=128)

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
