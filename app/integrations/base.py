"""Common interface for third-party pool monitoring devices.

Each provider's cloud API is undocumented and reverse-engineered by the
community, so adapters are deliberately isolated behind this interface: they
normalise whatever the vendor returns into a :class:`DeviceMeasurement` with
the same units the rest of the app uses (ppm, mV, °C). If a vendor changes
their API, only the adapter needs updating.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class ProviderError(RuntimeError):
    """Raised when a device API call fails or credentials are rejected."""


@dataclass
class DeviceMeasurement:
    """A normalised reading from a device, in the app's canonical units."""

    taken_at: datetime
    external_id: str | None = None  # device serial / measurement id, for de-duping

    ph: float | None = None
    free_chlorine: float | None = None  # ppm
    total_chlorine: float | None = None  # ppm
    total_alkalinity: float | None = None  # ppm
    cyanuric_acid: float | None = None  # ppm
    calcium_hardness: float | None = None  # ppm
    salt: float | None = None  # ppm
    orp: float | None = None  # mV
    ec: float | None = None  # µS/cm
    tds: float | None = None  # ppm
    temperature_c: float | None = None


class PoolDevice(ABC):
    """A user's account on a provider's cloud, exposing one or more devices."""

    provider_name: str

    def __init__(self, credentials: dict) -> None:
        self.credentials = credentials

    @abstractmethod
    def verify(self) -> bool:
        """Authenticate and confirm the credentials work. May raise ProviderError."""

    @abstractmethod
    def latest_measurements(self) -> list[DeviceMeasurement]:
        """Return the latest measurement for each device on the account."""
