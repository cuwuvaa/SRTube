import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    bot_token: SecretStr = SecretStr("")
    bot_token_file: Path | None = None
    environment: Literal["legacy", "development", "production"] = "legacy"
    data_dir: Path = Path("data")
    tracks_dir: Path | None = None
    renderer_socket: Path | None = None
    ffmpeg_threads: int = Field(default=1, ge=1, le=2)
    min_free_bytes: int = Field(default=512_000_000, ge=50_000_000)
    max_duration_seconds: int = Field(default=1200, ge=10, le=7200)
    max_upload_bytes: int = Field(default=49_000_000, ge=1_000_000, le=49_000_000)
    max_download_bytes: int = Field(default=200_000_000, ge=1_000_000)
    max_workers: int = Field(default=1, ge=1, le=4)
    max_queue: int = Field(default=8, ge=0, le=100)
    cache_hours: int = Field(default=2, ge=1, le=168)
    max_cache_bytes: int = Field(default=1_000_000_000, ge=100_000_000)
    preview_seconds: int = Field(default=20, ge=5, le=30)
    download_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    render_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    youtube_cookies_file: Path | None = None
    youtube_proxy: SecretStr = SecretStr("")
    youtube_pot_provider_url: str = ""
    youtube_player_clients: str = "default"
    youtube_min_interval_seconds: float = Field(default=5, ge=0, le=60)
    youtube_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    admin_user_ids: str = ""
    allowed_user_ids: str = ""

    @field_validator("cache_hours")
    @classmethod
    def limit_cache_age(cls, value: int) -> int:
        # Keep older environment files valid while enforcing the new two-hour cap.
        return min(value, 2)

    @field_validator("youtube_player_clients")
    @classmethod
    def valid_clients(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_]+(?:,[a-z_]+)*", value):
            raise ValueError("YOUTUBE_PLAYER_CLIENTS: ожидается список клиентов через запятую")
        return value

    @field_validator("youtube_pot_provider_url")
    @classmethod
    def valid_provider(cls, value: str) -> str:
        from urllib.parse import urlsplit

        if value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("YOUTUBE_POT_PROVIDER_URL: ожидается http(s) URL без логина и пароля")
            if any(character in value for character in (";", ",", "\n", "\r")):
                raise ValueError("YOUTUBE_POT_PROVIDER_URL: недопустимые разделители")
        return value.rstrip("/")

    @field_validator("youtube_cookies_file", "bot_token_file", "tracks_dir", "renderer_socket", mode="before")
    @classmethod
    def empty_path(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def allowed_ids(self) -> set[int]:
        return self.parse_ids(self.allowed_user_ids)

    @property
    def admin_ids(self) -> set[int]:
        return self.parse_ids(self.admin_user_ids)

    @staticmethod
    def parse_ids(value: str) -> set[int]:
        ids = {int(item) for item in re.split(r"[,\s]+", value.strip()) if item}
        if any(not 0 < user_id < 2**52 for user_id in ids):
            raise ValueError("Telegram user ID должен быть положительным целым числом меньше 2^52")
        return ids

    @property
    def cookies_path(self) -> Path:
        return self.youtube_cookies_file or self.data_dir / "auth" / "youtube.cookies.txt"

    def prepare(self) -> None:
        import os

        os.umask(0o077)
        self.data_dir = self.data_dir.expanduser().resolve()
        if self.data_dir in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
            raise ValueError("DATA_DIR должен быть отдельной папкой приложения")
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.tracks_dir = (self.tracks_dir or self.data_dir / "tracks").expanduser().resolve()
        if self.environment == "production" and (
            self.tracks_dir.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(self.tracks_dir)
        ):
            raise ValueError("Production state и tracks должны быть отдельными папками/томами")
        if self.bot_token_file:
            self.bot_token_file = self.bot_token_file.expanduser().resolve()
            if self.bot_token_file.is_file():
                if self.bot_token_file.stat().st_size > 256:
                    raise ValueError("BOT_TOKEN_FILE: недопустимый размер")
                self.bot_token = SecretStr(self.bot_token_file.read_text().strip())
        if self.environment == "production" and (not self.renderer_socket or self.max_workers != 1):
            raise ValueError("Production требует RENDERER_SOCKET и MAX_WORKERS=1")
        _ = self.allowed_ids  # Validate before accepting updates.
        _ = self.admin_ids
        if self.youtube_cookies_file is not None:
            self.youtube_cookies_file = self.youtube_cookies_file.expanduser().resolve()
