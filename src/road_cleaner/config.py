"""Configuration for Road Cleaner.

Every external dependency in this system sits behind a port with at least two
adapters: one that runs locally with no credentials, and one that talks to
Google Cloud. This module decides which is which.

``ROAD_CLEANER_MODE`` sets the defaults for every adapter at once. Individual
adapter settings override it, so you can run, say, a real Gemini analyst against
a local SQLite store while you are still developing.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
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


class MediaProviderKind(StrEnum):
    SCRIPTED = "scripted"
    VERTEX = "vertex"


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
    # The second switch in front of a real agency.
    #
    # `seeds/agencies.yaml` now holds the DOTs' actual public reporting forms, so
    # the dashboard can show where a report would really go. That makes DRY_RUN
    # the only thing standing between this system and an automated submission to
    # a government intake form, and one environment variable is not enough for
    # that. Both must be set before anything is transmitted; the channels check
    # it themselves rather than trusting callers.
    allow_live_filing: bool = Field(default=False, alias="ALLOW_LIVE_FILING")
    # A narrow hole in the wall above, for demonstrating that the last step is
    # real without arming the other seventy-one agencies to do it.
    #
    # `ALLOW_LIVE_FILING=true` is all-or-nothing, and "all" includes
    # contact@dot.ga.gov and info@azdot.gov -- two genuinely deliverable
    # addresses in the registry. Sending one honest end-to-end report should not
    # require unlocking those, so this names recipients one at a time instead.
    # Comma-separated; matched case-insensitively against the destination.
    #
    # This only ever *permits* an address that is already refused. It cannot
    # widen anything: an address not on the list is exactly as blocked as before.
    live_filing_allowlist: str = Field(default="", alias="LIVE_FILING_ALLOWLIST")
    # Who the demonstration send goes to. Separate from the allowlist above so
    # that being *reachable* and being *the demo recipient* stay two decisions:
    # this address still has to appear in the allowlist to receive anything, and
    # the button stays hidden until both are set.
    demo_send_to: str = Field(default="", alias="DEMO_SEND_TO")

    @property
    def live_filing_allowed(self) -> frozenset[str]:
        """The allowlist as addresses, lowercased and stripped of blanks."""
        return frozenset(
            entry.strip().lower()
            for entry in self.live_filing_allowlist.split(",")
            if entry.strip()
        )
    # Where this deployment answers, e.g. https://road-cleaner-....run.app
    #
    # Only used to turn a site-relative evidence path into a link a road crew can
    # open from an email. Left empty, reports simply do not mention the still --
    # a relative URL in somebody's inbox is worse than no URL.
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    data_dir: Path = Field(default=DEFAULT_DATA_DIR, alias="DATA_DIR")

    # ------------------------------------------------------- vision / ADK
    use_adk: bool = Field(default=False, alias="USE_ADK")
    vision_provider: str = Field(default=AUTO, alias="VISION_PROVIDER")
    gemini_model: str = Field(default="gemini-3.7-flash", alias="GEMINI_MODEL")
    # Gemma 4 IS serverless on Vertex -- `gemma-4-26b-a4b-it-maas` at
    # location=global, no GPU endpoint, verified 2026-08-22. Gemma *3* is not:
    # every 3.x id 404s, which is what the "-maas" suffix distinguishes.
    #
    # It writes the drill scaffold (free text -> structured hazard spec), a small
    # fast job well matched to a small fast model. It is deliberately kept off the
    # detection path, where Gemini's accuracy earns its cost.
    #
    # The prefilter flag stays default-off: it is a cost optimisation for running
    # thousands of frames an hour, not something a demo needs, and turning it on
    # puts a second model in front of every frame.
    gemma_prefilter_enabled: bool = Field(default=False, alias="GEMMA_PREFILTER_ENABLED")
    gemma_model: str = Field(default="gemma-4-26b-a4b-it-maas", alias="GEMMA_MODEL")
    # Vertex throttles hard. A full-speed run with no ceiling produced 165
    # consecutive 429s and no detections at all, so the analyzer holds a
    # semaphore and retries transient failures with backoff.
    vision_max_concurrency: int = Field(default=4, alias="VISION_MAX_CONCURRENCY")
    vision_max_retries: int = Field(default=5, alias="VISION_MAX_RETRIES")
    google_cloud_project: str | None = Field(default=None, alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(default="us-central1", alias="GOOGLE_CLOUD_LOCATION")
    google_genai_use_vertexai: bool = Field(default=True, alias="GOOGLE_GENAI_USE_VERTEXAI")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    # Path to a service account key. Optional: on Cloud Run the runtime service
    # account is picked up from the metadata server and this stays unset. It is
    # read here only so that setting it in .env works -- see `_resolve_auto_defaults`.
    google_application_credentials: Path | None = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS"
    )

    # ------------------------------------------------ simulation / media
    # Synthetic scenario generation: Veo renders a dashcam-perspective clip of a
    # hazard we actually detected, Chirp narrates the dispatch briefing, Lyria
    # scores the reel.
    #
    # Deliberately NOT an AUTO setting. Every other adapter flips with
    # ROAD_CLEANER_MODE, but generation bills per second of video -- having a
    # deploy to cloud silently switch Veo on is exactly the kind of surprise the
    # dry-run default exists to prevent. Turning this on is its own decision, and
    # generation only ever happens through `road-cleaner simulate`, never on a
    # request path or in the polling loop.
    media_provider: str = Field(default=MediaProviderKind.SCRIPTED, alias="MEDIA_PROVIDER")
    # The GA model id, not a "-preview" one. Every veo-*-preview id 404s on
    # invocation now, including ones whose metadata still reads back over REST --
    # being able to GET a publisher model is not the same as being able to call
    # it, which is a trap worth only falling into once.
    veo_model: str = Field(default="veo-3.1-fast-generate-001", alias="VEO_MODEL")
    lyria_model: str = Field(default="lyria-002", alias="LYRIA_MODEL")
    tts_voice: str = Field(default="en-US-Chirp3-HD-Achernar", alias="TTS_VOICE")
    # Veo and Lyria are region-pinned and were only verified in us-central1;
    # GOOGLE_CLOUD_LOCATION is free to be "global" for Gemini, which tolerates it.
    # Keeping these separate stops one being fixed at the cost of the other.
    vertex_media_location: str = Field(default="us-central1", alias="VERTEX_MEDIA_LOCATION")
    media_local_path: Path | None = Field(default=None, alias="MEDIA_LOCAL_PATH")
    # How long to wait on a Veo long-running operation before giving up.
    veo_timeout_seconds: int = Field(default=300, alias="VEO_TIMEOUT_SECONDS")
    veo_resolution: str = Field(default="1080p", alias="VEO_RESOLUTION")
    # Veo rewrites the prompt before rendering, and the rewrite reaches for
    # cinema -- bigger, more dramatic, more graded. It would be good to turn off
    # and **cannot be**: Veo 3 rejects the request outright with "Veo 3 prompt
    # enhancement cannot be disabled." The setting stays for the day a model
    # allows it; until then the counterweights are the explicit size anchors in
    # `scenario_prompt` and the `negative_prompt` field.
    veo_enhance_prompt: bool = Field(default=True, alias="VEO_ENHANCE_PROMPT")

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
    # Frames within this hash distance are treated as identical and skipped.
    # 0 means "only literally identical frames" -- which is the only thing an
    # average hash can honestly detect. See `adapters/camera/scene.phash`.
    phash_identical_threshold: int = Field(default=0, alias="PHASH_IDENTICAL_THRESHOLD")
    # Safety net: however many identical frames arrive, analyze one every this
    # many polls anyway. Without it, a static scene containing a hazard would be
    # skipped forever and never detected at all.
    max_consecutive_skips: int = Field(default=10, alias="MAX_CONSECUTIVE_SKIPS")

    # --------------------------------------------------------------- web
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")

    # ------------------------------------------------------------- accounts
    # Firebase Authentication, Google sign-in only. The point of insisting on
    # Google is that its addresses arrive already verified, and a verified
    # address is the whole basis on which this application is willing to send
    # somebody mail without asking a human first.
    #
    # None of these four are secret. The API key in particular ships inside the
    # client bundle by design -- it identifies the project to Firebase, it does
    # not authorise anything. It needs no Secret Manager entry, unlike
    # SMTP_PASSWORD. What actually protects an account is the ID token, which is
    # signed by Google and verified server-side in `web/auth.py`.
    firebase_project_id: str | None = Field(default=None, alias="FIREBASE_PROJECT_ID")
    firebase_api_key: str | None = Field(default=None, alias="FIREBASE_API_KEY")
    firebase_auth_domain: str | None = Field(default=None, alias="FIREBASE_AUTH_DOMAIN")
    firebase_app_id: str | None = Field(default=None, alias="FIREBASE_APP_ID")

    # ------------------------------------------------------------- dashcam
    # How long a confirmed find stays reportable before it is dropped. Looking
    # stops for this window: a hazard is one object, and continuing to spend
    # Vertex calls on the thing you have already found is waste. Unclicked, the
    # find is discarded -- nothing is written and nothing is sent.
    dashcam_report_window_seconds: int = Field(
        default=15, alias="DASHCAM_REPORT_WINDOW_SECONDS"
    )
    # Whether a reported dashcam finding is also mailed to the agency that owns
    # the road, on top of the copy that goes to whoever reported it.
    #
    # Off, and turning it on is deliberately not sufficient on its own: an
    # agency address still has to clear `guard_live_send` the ordinary way, via
    # LIVE_FILING_ALLOWLIST or ALLOW_LIVE_FILING. This flag opens the code path;
    # it does not open the wall. Flipping it can never, by itself, put mail in a
    # real maintenance desk's inbox.
    dashcam_notify_dot: bool = Field(default=False, alias="DASHCAM_NOTIFY_DOT")

    @property
    def auth_configured(self) -> bool:
        """Whether sign-in can work at all.

        All four or none: a project id with no API key gives a page that renders
        a sign-in button which cannot possibly succeed, and the honest thing is
        to not render it.
        """
        return all(
            (
                self.firebase_project_id,
                self.firebase_api_key,
                self.firebase_auth_domain,
                self.firebase_app_id,
            )
        )

    @property
    def firebase_web_config(self) -> dict[str, str]:
        """The public config the browser SDK needs. Empty when unconfigured."""
        if not self.auth_configured:
            return {}
        return {
            "apiKey": self.firebase_api_key or "",
            "authDomain": self.firebase_auth_domain or "",
            "projectId": self.firebase_project_id or "",
            "appId": self.firebase_app_id or "",
        }

    # --------------------------------------------------------- resolution
    @field_validator(
        "sqlite_path",
        "blob_local_path",
        "filing_outbox",
        "media_local_path",
        "google_application_credentials",
        mode="before",
    )
    @classmethod
    def _blank_path_is_unset(cls, value: object) -> object:
        """Treat ``KEY=`` in .env as "not set" rather than as the path ".".

        An empty string parses to ``Path("")``, which is ``Path(".")`` -- so a
        commented-out-by-emptying setting would silently resolve to the current
        directory instead of falling back to the default below.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
        # Generated media lives apart from `frames/`, which holds real camera
        # evidence. Two directories, not one, so nothing synthetic can be picked
        # up by anything walking the evidence store.
        if self.media_local_path is None:
            self.media_local_path = self.data_dir / "media"

        self._export_google_env()
        return self

    def _export_google_env(self) -> None:
        """Push the Google settings back into the process environment.

        pydantic-settings reads .env into *this object* and stops there, but two
        Google libraries only ever look at ``os.environ``:

        * ``google-auth`` for GOOGLE_APPLICATION_CREDENTIALS -- without this it
          ignores the .env value and uses whatever ADC is lying around, which may
          well be a different project's.
        * ``google-adk``, which builds its own client from the environment when an
          agent is given a bare model string. ``AdkReasoner`` takes ``use_vertex``
          but has no way to hand it to ``LlmAgent``, so if GOOGLE_GENAI_USE_VERTEXAI
          is absent here ADK quietly falls back to the Gemini Developer API --
          billing a different quota than the Vertex one we meant to use.

        Anything already in the real environment outranks .env in pydantic's own
        precedence, so it is already reflected in these fields and writing it back
        changes nothing.
        """
        exports: dict[str, str | None] = {
            "GOOGLE_GENAI_USE_VERTEXAI": "True" if self.google_genai_use_vertexai else "False",
            "GOOGLE_CLOUD_PROJECT": self.google_cloud_project,
            "GOOGLE_CLOUD_LOCATION": self.google_cloud_location,
            "GOOGLE_API_KEY": self.google_api_key,
            "GOOGLE_APPLICATION_CREDENTIALS": (
                str(self.google_application_credentials)
                if self.google_application_credentials is not None
                else None
            ),
        }
        for key, value in exports.items():
            if value:
                os.environ[key] = value

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

    def redirected_to(self, root: Path | str) -> Settings:
        """A copy of these settings with *every* on-disk path under `root`.

        Use this rather than setting DATA_DIR when a run must leave the real
        data directory alone. DATA_DIR alone is not enough: `.env` ships with
        SQLITE_PATH and BLOB_LOCAL_PATH spelled out, and an explicit setting
        wins over the data_dir default. A run launched with DATA_DIR=./elsewhere
        still wrote its database into ./data and destroyed a demo dataset.
        """
        root = Path(root)
        return self.model_copy(
            update={
                "data_dir": root,
                "sqlite_path": root / "road_cleaner.db",
                "blob_local_path": root / "frames",
                "media_local_path": root / "media",
                "filing_outbox": root / "outbox",
            }
        )

    def ensure_directories(self) -> None:
        """Create the local directories the app writes to. Safe to call repeatedly."""
        for path in (
            self.data_dir,
            self.blob_local_path,
            self.filing_outbox,
            self.media_local_path,
        ):
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
        if (
            self.gemma_prefilter_enabled
            and not self.gemma_model.endswith("-maas")
            and "/" not in self.gemma_model
        ):
            # Serverless Gemma on Vertex is the "-maas" family. Anything else has
            # to be a self-deployed Model Garden endpoint resource name, and a
            # bare "gemma-3-4b-it" is neither -- it 404s on every frame.
            missing.append(
                f"GEMMA_MODEL ({self.gemma_model!r} is neither a serverless '-maas' "
                "model nor a deployed endpoint path "
                "projects/<p>/locations/<r>/endpoints/<id>)"
            )
        if self.media_provider == MediaProviderKind.VERTEX and not self.google_cloud_project:
            missing.append("GOOGLE_CLOUD_PROJECT (Veo / Lyria)")
        # A path pointing at nothing is a typo, and it surfaces a long way from
        # here -- as a DefaultCredentialsError on the first model call.
        creds = self.google_application_credentials
        if creds is not None and not creds.is_file():
            missing.append(f"GOOGLE_APPLICATION_CREDENTIALS (no such file: {creds})")
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
