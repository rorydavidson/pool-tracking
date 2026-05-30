"""Aiper "HydroComm" smart pool monitor integration.

Aiper exposes a cloud REST API (plus an AWS IoT MQTT control plane the mobile
app uses for live device shadows). There is no official public API, so this
adapter follows the community-documented REST flow (cf. the ha-aiper project):
authenticate with email/password for a bearer token, list the account's
devices, and read each device's latest reported water-quality properties.

The HydroComm reports pH, ORP, EC, TDS, free chlorine and temperature, which
map cleanly onto our canonical measurement fields.

Credentials dict shape:
    {"email": ..., "password": ..., "base_url": <optional override>}
Because the exact host/paths are undocumented and may change, the base URL and
the property field names are overridable via the credentials dict / env.
"""
from __future__ import annotations

import datetime as dt

import httpx

from .base import DeviceMeasurement, PoolDevice, ProviderError

DEFAULT_BASE_URL = "https://api.aiper.com"

# Maps Aiper-reported property keys to (DeviceMeasurement field, scale).
# Aiper reports several aliases across firmware versions; we accept any of them.
_FIELD_MAP = {
    "ph": ("ph", 1.0),
    "orp": ("orp", 1.0),
    "redox": ("orp", 1.0),
    "tds": ("tds", 1.0),
    "ec": ("tds", 0.64),  # EC (µS/cm) → approximate TDS ppm
    "free_chlorine": ("free_chlorine", 1.0),
    "fcl": ("free_chlorine", 1.0),
    "cl": ("free_chlorine", 1.0),
    "temperature": ("temperature_c", 1.0),
    "temp": ("temperature_c", 1.0),
    "water_temp": ("temperature_c", 1.0),
}


class AiperClient(PoolDevice):
    provider_name = "aiper"

    def __init__(self, credentials: dict) -> None:
        super().__init__(credentials)
        self.base_url = (credentials.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._token: str | None = None

    def _login(self) -> str:
        if self._token:
            return self._token
        email = self.credentials.get("email")
        password = self.credentials.get("password")
        if not email or not password:
            raise ProviderError("Aiper requires an email and password")
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/auth/login",
                json={"email": email, "password": password},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach Aiper: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("Aiper rejected the email/password")
        if resp.status_code >= 400:
            raise ProviderError(f"Aiper login failed ({resp.status_code})")
        data = resp.json()
        token = (
            data.get("access_token")
            or data.get("token")
            or (data.get("data") or {}).get("access_token")
        )
        if not token:
            raise ProviderError("Aiper login did not return a token")
        self._token = token
        return token

    def _get(self, path: str) -> dict:
        token = self._login()
        try:
            resp = httpx.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Aiper request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Aiper API error ({resp.status_code}) for {path}")
        return resp.json()

    def verify(self) -> bool:
        self._login()
        return True

    def latest_measurements(self) -> list[DeviceMeasurement]:
        payload = self._get("/v1/devices")
        devices = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(devices, dict):
            devices = devices.get("devices", [])
        results: list[DeviceMeasurement] = []
        for device in devices or []:
            device_id = device.get("device_id") or device.get("id") or device.get("sn")
            if not device_id:
                continue
            props_payload = self._get(f"/v1/devices/{device_id}/properties")
            props = (
                props_payload.get("data", props_payload)
                if isinstance(props_payload, dict)
                else props_payload
            )
            results.append(self._parse_properties(props, str(device_id)))
        return results

    @staticmethod
    def _parse_properties(props: dict, device_id: str) -> DeviceMeasurement:
        # Properties may be a flat dict or {"properties": {...}}.
        if isinstance(props, dict) and "properties" in props:
            props = props["properties"]
        measured: dict[str, float] = {}
        taken_at = dt.datetime.now(dt.timezone.utc)
        for key, value in (props or {}).items():
            k = str(key).lower()
            if k in ("updated_at", "timestamp", "reported_at") and value:
                try:
                    taken_at = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError:
                    pass
                continue
            if k not in _FIELD_MAP or value is None:
                continue
            field, scale = _FIELD_MAP[k]
            try:
                measured.setdefault(field, float(value) * scale)
            except (TypeError, ValueError):
                continue
        return DeviceMeasurement(taken_at=taken_at, external_id=device_id, **measured)
