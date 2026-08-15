"""Ports: the interfaces every external dependency is reached through.

Each of these has at least two implementations in `adapters/` -- one that runs
locally with no credentials, one that talks to a real service. Nothing in
`domain/` or `agents/` imports an adapter directly; `container.py` is the only
place the choice is made.
"""

from road_cleaner.ports.blob_store import BlobNotFoundError, BlobStore
from road_cleaner.ports.camera_source import CameraFetchError, CameraSource
from road_cleaner.ports.case_repository import CaseRepository
from road_cleaner.ports.clock import Clock, FrozenClock, SystemClock
from road_cleaner.ports.event_bus import EventBus, Handler
from road_cleaner.ports.filing_channel import FilingChannel, FilingError, FilingResult
from road_cleaner.ports.reasoning import JurisdictionVerdict, Reasoner
from road_cleaner.ports.vision import ClearanceCheck, VisionAnalyzer

__all__ = [
    "BlobNotFoundError",
    "BlobStore",
    "CameraFetchError",
    "CameraSource",
    "CaseRepository",
    "ClearanceCheck",
    "Clock",
    "EventBus",
    "FilingChannel",
    "FilingError",
    "FilingResult",
    "FrozenClock",
    "Handler",
    "JurisdictionVerdict",
    "Reasoner",
    "SystemClock",
    "VisionAnalyzer",
]
