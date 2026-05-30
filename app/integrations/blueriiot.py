"""Blueriiot "Blue Connect" integration (api.riiotlabs.com).

The Blue Connect cloud has no official public API. This adapter follows the
flow reverse-engineered by the community (e.g. python-blueconnect,
BlueRiiot2MQTT): log in with email/password to obtain temporary AWS
credentials, then call the ``execute-api`` endpoints with AWS Signature V4.

Blue Connect probes measure temperature, pH, ORP (redox) and conductivity /
salinity — not free chlorine directly — so only those fields are populated.
Credentials dict shape: ``{"email": ..., "password": ...}``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from urllib.parse import urlsplit

import httpx

from .base import DeviceMeasurement, PoolDevice, ProviderError

BASE_URL = "https://api.riiotlabs.com/prod"
AWS_REGION = "eu-west-1"
AWS_SERVICE = "execute-api"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_headers(
    method: str,
    url: str,
    creds: dict,
    body: str = "",
) -> dict[str, str]:
    """Build AWS Signature V4 headers for an execute-api request."""
    parts = urlsplit(url)
    host = parts.netloc
    canonical_uri = parts.path or "/"
    canonical_query = parts.query  # assumes already sorted/encoded by caller

    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = _sha256(body.encode("utf-8"))
    headers = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-security-token": creds["session_token"],
        "x-amz-content-sha256": payload_hash,
    }
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{date_stamp}/{AWS_REGION}/{AWS_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical_request.encode("utf-8"))]
    )

    k_date = _hmac(f"AWS4{creds['secret_key']}".encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, AWS_REGION)
    k_service = _hmac(k_region, AWS_SERVICE)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


# Maps Blue Connect measurement names to (DeviceMeasurement field, scale).
_FIELD_MAP = {
    "temperature": ("temperature_c", 1.0),
    "ph": ("ph", 1.0),
    "orp": ("orp", 1.0),
    "salinity": ("salt", 1000.0),  # reported in g/L → ppm
    "tds": ("tds", 1.0),
    "conductivity": ("tds", 1.0),  # fallback proxy if no explicit tds
}


class BlueRiiotClient(PoolDevice):
    provider_name = "blueriiot"

    def __init__(self, credentials: dict) -> None:
        super().__init__(credentials)
        self._aws_creds: dict | None = None

    def _login(self) -> dict:
        if self._aws_creds:
            return self._aws_creds
        email = self.credentials.get("email")
        password = self.credentials.get("password")
        if not email or not password:
            raise ProviderError("Blueriiot requires an email and password")
        try:
            resp = httpx.post(
                f"{BASE_URL}/user/login",
                json={"email": email, "password": password},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach Blueriiot: {exc}") from exc
        if resp.status_code == 401 or resp.status_code == 403:
            raise ProviderError("Blueriiot rejected the email/password")
        if resp.status_code >= 400:
            raise ProviderError(f"Blueriiot login failed ({resp.status_code})")
        data = resp.json()
        creds = data.get("credentials") or {}
        self._aws_creds = {
            "access_key": creds.get("access_key") or creds.get("AccessKeyId"),
            "secret_key": creds.get("secret_key") or creds.get("SecretKey"),
            "session_token": creds.get("session_token") or creds.get("SessionToken"),
        }
        if not all(self._aws_creds.values()):
            raise ProviderError("Blueriiot login did not return usable credentials")
        return self._aws_creds

    def _get(self, path: str) -> dict:
        creds = self._login()
        url = f"{BASE_URL}{path}"
        headers = _sigv4_headers("GET", url, creds)
        try:
            resp = httpx.get(url, headers=headers, timeout=30)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Blueriiot request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Blueriiot API error ({resp.status_code}) for {path}")
        return resp.json()

    def verify(self) -> bool:
        self._login()
        return True

    def latest_measurements(self) -> list[DeviceMeasurement]:
        pools = self._get("/swimming_pool")
        if isinstance(pools, dict):
            pools = pools.get("data", [])
        results: list[DeviceMeasurement] = []
        for pool in pools or []:
            pool_id = pool.get("swimming_pool_id") or pool.get("swimming_pool", {}).get(
                "swimming_pool_id"
            )
            if not pool_id:
                continue
            devices = self._get(f"/swimming_pool/{pool_id}/blue")
            if isinstance(devices, dict):
                devices = devices.get("data", [])
            for device in devices or []:
                serial = device.get("blue_device_serial") or device.get("blue_device_id")
                if not serial:
                    continue
                m = self._get(
                    f"/swimming_pool/{pool_id}/blue/{serial}"
                    "/lastMeasurements?mode=blue_and_strip"
                )
                results.append(self._parse_measurements(m, serial))
        return results

    @staticmethod
    def _parse_measurements(payload: dict, serial: str) -> DeviceMeasurement:
        items = payload.get("data", []) if isinstance(payload, dict) else []
        taken_at = dt.datetime.now(dt.timezone.utc)
        measured: dict[str, float] = {}
        for item in items:
            name = (item.get("name") or "").lower()
            value = item.get("value")
            if value is None or name not in _FIELD_MAP:
                continue
            field, scale = _FIELD_MAP[name]
            measured.setdefault(field, float(value) * scale)
            ts = item.get("timestamp") or item.get("measured_at")
            if ts:
                try:
                    taken_at = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
        return DeviceMeasurement(taken_at=taken_at, external_id=serial, **measured)
