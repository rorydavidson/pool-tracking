"""Third-party pool device integrations (Aiper, Blueriiot, PoolLab)."""
from __future__ import annotations

from ..models import Provider
from .aiper import AiperClient
from .base import DeviceMeasurement, PoolDevice, ProviderError
from .blueriiot import BlueRiiotClient
from .poollab import PoolLabClient


def get_client(provider: Provider, credentials: dict) -> PoolDevice:
    """Construct the device client for a provider from decrypted credentials."""
    if provider == Provider.aiper:
        return AiperClient(credentials)
    if provider == Provider.blueriiot:
        return BlueRiiotClient(credentials)
    if provider == Provider.poollab:
        return PoolLabClient(credentials)
    raise ProviderError(f"Unsupported provider: {provider}")


__all__ = [
    "AiperClient",
    "BlueRiiotClient",
    "PoolLabClient",
    "DeviceMeasurement",
    "PoolDevice",
    "ProviderError",
    "get_client",
]
