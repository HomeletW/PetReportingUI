from functools import lru_cache
import os
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Station(Strict):
    id: str
    camera: str
    zone: str
    kind: Literal["feeder", "litterbox"]
    enabled: bool = True


class Frigate(Strict):
    base_url: str = "https://frigate.example.invalid"
    allowed_host: str = "frigate.example.invalid"
    username: str = ""
    password: SecretStr = SecretStr("")
    # Enable only after checking the installed instance's OpenAPI schema and playback.
    bbox_verified: bool = False
    clip_verified: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_snapshot_bytes: int = Field(default=10_000_000, ge=1024)
    max_clip_bytes: int = Field(default=100_000_000, ge=1024)

    @model_validator(mode="after")
    def origin(self):
        p = urlparse(self.base_url)
        if (
            p.scheme != "https"
            or p.hostname != self.allowed_host
            or p.username
            or p.password
            or p.query
            or p.fragment
            or p.path not in ("", "/")
        ):
            raise ValueError("Frigate must use its allowlisted HTTPS origin")
        return self


class Settings(Strict):
    household_id: str = "home"
    source_id: str = "frigate-home"
    timezone: str = "America/Toronto"
    database_url: SecretStr = SecretStr("")
    internal_api_token: SecretStr = SecretStr("")
    public_base_url: str = "http://localhost:8092"
    media_root: Path = Path("/data/media")
    pet_id: str = "coconut"
    pet_name: str = "Coconut"
    stations: list[Station] = Field(
        default_factory=lambda: [
            Station(id="feeder", camera="feeder_camera", zone="feeder", kind="feeder"),
            Station(id="litterbox", camera="litterbox_camera", zone="litterbox", kind="litterbox"),
        ]
    )
    lease_seconds: int = Field(default=300, ge=30, le=3600)
    report_retention_days: int = Field(default=90, ge=1)
    normalized_record_retention_days: int = Field(default=365, ge=1)
    draft_media_grace_days: int = Field(default=7, ge=1)
    notifications_enabled: bool = False
    frigate: Frigate = Field(default_factory=Frigate)

    @model_validator(mode="after")
    def validate_settings(self):
        ZoneInfo(self.timezone)
        p = urlparse(self.public_base_url)
        if (
            p.scheme not in ("http", "https")
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError("Invalid public base URL")
        if len({s.id for s in self.stations}) != len(self.stations):
            raise ValueError("Duplicate station IDs")
        url = self.database_url.get_secret_value()
        if url and not url.startswith("postgresql+psycopg://"):
            raise ValueError("Use a postgresql+psycopg:// connection URL")
        token = self.internal_api_token.get_secret_value()
        if token and len(token) < 32:
            raise ValueError("Internal API token must be at least 32 characters")
        return self


@lru_cache
def settings() -> Settings:
    data = {}
    for path in [
        os.getenv("PETREPORT_CONFIG", "/config/config.yaml"),
        os.getenv("PETREPORT_SECRETS", "/config/secrets.yaml"),
    ]:
        if Path(path).is_file():
            overlay = yaml.safe_load(Path(path).read_text()) or {}
            for key, value in overlay.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key] = {**data[key], **value}
                else:
                    data[key] = value
    return Settings.model_validate(data)
