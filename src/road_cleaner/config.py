"""Configuration for Road Cleaner.

Every external dependency in this system sits behind a port with at least two
adapters: one that runs locally with no credentials, and one that talks to
Google Cloud. This module decides which is which.

``ROAD_CLEANER_MODE`` sets the defaults for every adapter at once. Individual
adapter settings override it, so you can run, say, a real Gemini analyst against
a local SQLite store while you are still developing.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
SEEDS_DIR = REPO_ROOT / "seeds"


class Mode(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class CameraSourceKind(StrEnum):
    FIXTURE = "fixture"
    VENDOR511 = "vendor511"


class VisionProvider(StrEnum):
    SCRIPTED = "scripted"
    GEMINI = "gemini"


class RepositoryKind(StrEnum):
    SQLITE = "sqlite"
    FIRESTORE = "firestore"


class BlobStoreKind(StrEnum):
    LOCAL = "local"
    GCS = "gcs"


class EventBusKind(StrEnum):
    MEMORY = "memory"
    PUBSUB = "pubsub"


class FilingChannelKind(StrEnum):
    DRY_RUN = "dry_run"
    OPEN311 = "open311"
    EMAIL = "email"


# Sentinel meaning "whatever ROAD_CLEANER_MODE implies". Lets us tell apart
# "user did not set this" from "user explicitly chose the mode default".
AUTO = "auto"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- core
    mode: Mode = Field(default=Mode.LOCAL, alias="ROAD_CLEANER_MODE")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    data_dir: Path = Field(default=DEFAULT_DATA_DIR, alias="DATA_DIR")

    # ------------------------------------------------------- vision / ADK
    use_adk: bool = Field(default=False, alias="USE_ADK")
    vision_provider: str = Field(default=AUTO, alias="VISION_PROVIDER")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    gemma_prefilter_enabled: bool = Field(default=False, alias="GEMMA_PREFILTER_ENABLED")
    gemma_model: str = Field(default="gemma-3-4b-it", alias="GEMMA_MODEL")
    google_cloud_project: str | None = Field(default=None, alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(default="us-central1", alias="GOOGLE_CLOUD_LOCATION")
    google_genai_use_vertexai: bool = Field(default=True, alias="GOOGLE_GENAI_USE_VERTEXAI")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")

    # ----------------------------------------------------- camera sources
    camera_source: str = Field(default=AUTO, alias="CAMERA_SOURCE")
    ga_511_base_url: str = Field(default="https://511ga.org", alias="GA_511_BASE_URL")
    fl_511_base_url: str = Field(default="https://fl511.com", alias="FL_511_BASE_URL")
    nc_511_base_url: str = Field(default="https://www.drivenc.gov", alias="NC_511_BASE_URL")
    ga_511_api_key: str | None = Field(default=None, alias="GA_511_API_KEY")
    fl_511_api_key: str | None = Field(default=None, alias="FL_511_API_KEY")
    nc_511_api_key: str | None = Field(default=None, alias="NC_511_API_KEY")
    # Every state on this vendor platform publishes the same throttle.
    state_api_rate_limit: int = Field(default=10, alias="STATE_API_RATE_LIMIT")
    state_api_rate_window_seconds: int = Field(default=60, alias="STATE_API_RATE_WINDOW_SECONDS")

    # ----------------------------------------------------------- storage
    repository: str = Field(default=AUTO, alias="REPOSITORY")
    sqlite_path: Path | None = Field(default=None, alias="SQLITE_PATH")
    blob_store: str = Field(default=AUTO, alias="BLOB_STORE")
    blob_local_path: Path | None = Field(default=None, alias="BLOB_LOCAL_PATH")
    gcs_bucket: str | None = Field(default=None, alias="GCS_BUCKET")
    firestore_database: str = Field(default="(default)", alias="FIRESTORE_DATABASE")

    # --------------------------------------------------------- event bus
    event_bus: str = Field(default=AUTO, alias="EVENT_BUS")
    pubsub_topic_frames: str = Field(default="frame-captured", alias="PUBSUB_TOPIC_FRAMES")
    pubsub_topic_hazards: str = Field(default="hazard-confirmed", alias="PUBSUB_TOPIC_HAZARDS")
    pubsub_topic_dlq: str = Field(default="road-cleaner-dlq", alias="PUBSUB_TOPIC_DLQ")

    # ------------------------------------------------------------ filing
    filing_channel: str = Field(default=AUTO, alias="FILING_CHANNEL")
    filing_outbox: Path | None = Field(default=None, alias="FILING_SANDBOX_INBOX")
    filing_from_address: str = Field(
        default="road-cleaner@example.invalid", alias="FILING_FROM_ADDRESS"
    )
    open311_endpoint: str | None = Field(default=None, alias="OPEN311_ENDPOINT")
    open311_api_key: str | None = Field(default=None, alias="OPEN311_API_KEY")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")

    # ----------------------------------------------------- confidence gate
    gate_min_confidence: float = Field(default=0.55, alias="GATE_MIN_CONFIDENCE")
    gate_corroboration_min_confidence: float = Field(
        default=0.50, alias="GATE_CORROBORATION_MIN_CONFIDENCE"
    )
    gate_min_frame_gap_seconds: int = Field(default=90, alias="GATE_MIN_FRAME_GAP_SECONDS")
    gate_max_frame_gap_seconds: int = Field(default=1800, alias="GATE_MAX_FRAME_GAP_SECONDS")
    gate_duplicate_radius_meters: float = Field(default=500.0, alias="GATE_DUPLICATE_RADIUS_METERS")
    gate_watch_margin: float = Field(default=0.15, alias="GATE_WATCH_MARGIN")

    # -------------------------------------------------------- watch tiers
    poll_seconds_busy: int = Field(default=120, alias="POLL_SECONDS_BUSY")
    poll_seconds_quiet: int = Field(default=600, alias="POLL_SECONDS_QUIET")
    poll_seconds_active: int = Field(default=60, alias="POLL_SECONDS_ACTIVE")
    auditor_interval_seconds: int = Field(default=3600, alias="AUDITOR_INTERVAL_SECONDS")
    # Frames whose perceptual hash differs by <= this are treated as identical
    # and never reach the vision model. Calibrated at 1: repeats score 0, a
    # newly-appeared hazard scores 2. See `adapters/camera/scene.phash`.
    phash_identical_threshold: int = Field(default=1, alias="PHASH_IDENTICAL_THRESHOLD")

    # --------------------------------------------------------------- web
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")

    # --------------------------------------------------------- resolution
    @model_validator(mode="after")
    def _resolve_auto_defaults(self) -> Settings:
        cloud = self.mode is Mode.CLOUD

        def pick(current: str, local_value: str, cloud_value: str) -> str:
            return (cloud_value if cloud else local_value) if current == AUTO else current

        self.camera_source = pick(
            self.camera_source, CameraSourceKind.FIXTURE, CameraSourceKind.VENDOR511
        )
        self.vision_provider = pick(
            self.vision_provider, VisionProvider.SCRIPTED, VisionProvider.GEMINI
        )
        self.repository = pick(self.repository, RepositoryKind.SQLITE, RepositoryKind.FIRESTORE)
        self.blob_store = pick(self.blob_store, BlobStoreKind.LOCAL, BlobStoreKind.GCS)
        self.event_bus = pick(self.event_bus, EventBusKind.MEMORY, EventBusKind.PUBSUB)
        # Filing stays dry-run in both modes. Going live is a deliberate,
        # separate decision -- see `dry_run` and the CLI's --confirm flag.
        self.filing_channel = pick(
            self.filing_channel, FilingChannelKind.DRY_RUN, FilingChannelKind.DRY_RUN
        )

        # Paths default under data_dir so a single DATA_DIR move relocates everything.
        if self.sqlite_path is None:
            self.sqlite_path = self.data_dir / "road_cleaner.db"
        if self.blob_local_path is None:
            self.blob_local_path = self.data_dir / "frames"
        if self.filing_outbox is None:
            self.filing_outbox = self.data_dir / "outbox"
        return self

    # ----------------------------------------------------------- helpers
    @property
    def state_api_keys(self) -> dict[str, str | None]:
        return {"GA": self.ga_511_api_key, "FL": self.fl_511_api_key, "NC": self.nc_511_api_key}

    @property
    def state_base_urls(self) -> dict[str, str]:
        return {
            "GA": self.ga_511_base_url,
            "FL": self.fl_511_base_url,
            "NC": self.nc_511_base_url,
        }

    def ensure_directories(self) -> None:
        """Create the local directories the app writes to. Safe to call repeatedly."""
        for path in (self.data_dir, self.blob_local_path, self.filing_outbox):
            if path is not None:
                path.mkdir(parents=True, exist_ok=True)
        if self.sqlite_path is not None:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def missing_for_live(self) -> list[str]:
        """What is still missing before this config could run against real services.

        Used by ``road-cleaner doctor`` so the gap between "runs locally" and
        "runs for real" is always visible rather than discovered at runtime.
        """
        missing: list[str] = []
        if self.vision_provider == VisionProvider.GEMINI or self.use_adk:
            if self.google_genai_use_vertexai and not self.google_cloud_project:
                missing.append("GOOGLE_CLOUD_PROJECT (Vertex AI)")
            if not self.google_genai_use_vertexai and not self.google_api_key:
                missing.append("GOOGLE_API_KEY (Gemini Developer API)")
        if self.camera_source == CameraSourceKind.VENDOR511:
            missing.extend(
                f"{state}_511_API_KEY"
                for state, key in self.state_api_keys.items()
                if not key
            )
        if self.repository == RepositoryKind.FIRESTORE and not self.google_cloud_project:
            missing.append("GOOGLE_CLOUD_PROJECT (Firestore)")
        if self.blob_store == BlobStoreKind.GCS and not self.gcs_bucket:
            missing.append("GCS_BUCKET")
        if self.event_bus == EventBusKind.PUBSUB and not self.google_cloud_project:
            missing.append("GOOGLE_CLOUD_PROJECT (Pub/Sub)")
        if self.filing_channel == FilingChannelKind.OPEN311 and not self.open311_endpoint:
            missing.append("OPEN311_ENDPOINT")
        if self.filing_channel == FilingChannelKind.EMAIL and not self.smtp_host:
            missing.append("SMTP_HOST")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings. Tests use this after changing the environment."""
    get_settings.cache_clear()
