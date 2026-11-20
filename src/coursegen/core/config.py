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
