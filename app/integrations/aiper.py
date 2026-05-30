"""Aiper cloud integration (HydroComm / pool cleaners).

Aiper wraps all REST calls in an AES-CBC encrypted body with the AES key
transported via RSA. This adapter implements that envelope, matching the
protocol used by the official mobile app and the community ha-aiper project.

The API is region-based:
    US:   https://apiamerica.aiper.com
    EU:   https://apieurope.aiper.com
    Asia: https://apiasia.aiper.com

On login the server returns canonical domain(s) for the account, so the
initial region only needs to be approximately right.

The REST API is used to authenticate and list devices, but HydroComm water
quality (pH/ORP/EC/TDS/chlorine/temperature) is *not* exposed over REST — it
lives in the device's AWS IoT shadow, read over MQTT in :mod:`aiper_shadow`.

Credentials dict shape:
    {"email": ..., "password": ..., "region": "eu"|"us"|"asia" (default "eu")}
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .aiper_shadow import OpenIdInfo, read_water_quality
from .base import DeviceMeasurement, PoolDevice, ProviderError

REGION_BASES: dict[str, str] = {
    "us": "https://apiamerica.aiper.com",
    "eu": "https://apieurope.aiper.com",
    "asia": "https://apiasia.aiper.com",
}

# RSA public key used by the official mobile app (DER, base64-encoded).
_RSA_PUB_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCIKoKPqwq1f60hm/2lpHDF/DT4J9YaptuTq78nsxdgnSBAvkIZ3E8d"
    "qbEBT/VETjJ9Yr28QtHX13E8QGByYxLzYPldHNXChgOWfSemTEC3TxPvlaSuM9eFUuhqSeGbgoKG7JJNlgjvsPO2cH"
    "EhPXJE4qWtKEZVOZBxEeCgAaLZxwIDAQAB"
)

# HydroComm water-quality keys → (DeviceMeasurement field, scale).
_FIELD_MAP = {
    "ph": ("ph", 1.0),
    "orp": ("orp", 1.0),
    "redox": ("orp", 1.0),
    "tds": ("tds", 1.0),
    "ec": ("ec", 1.0),  # stored as-is; TDS is derived from it below when absent
    "rcl": ("free_chlorine", 1.0),
    "free_chlorine": ("free_chlorine", 1.0),
    "fcl": ("free_chlorine", 1.0),
    "cl": ("free_chlorine", 1.0),
    "temp": ("temperature_c", 1.0),
    "temperature": ("temperature_c", 1.0),
    "water_temp": ("temperature_c", 1.0),
}

# Plausible ranges per canonical field. Aiper sometimes reports sentinel or
# uncalibrated values (e.g. a negative "rcl"); anything outside these bounds is
# dropped rather than stored, since bad readings drive bad (and unsafe) advice.
_PLAUSIBLE = {
    "ph": (0.0, 14.0),
    "free_chlorine": (0.0, 20.0),
    "orp": (0.0, 1500.0),
    "ec": (0.0, 20000.0),
    "tds": (0.0, 10000.0),
    "temperature_c": (-5.0, 60.0),
}


# ---------------------------------------------------------------------------
# AES/RSA crypto envelope
# ---------------------------------------------------------------------------

class _AiperCrypto:
    """AES-CBC + RSA-PKCS1v15 request encryption matching the mobile app."""

    def __init__(self) -> None:
        alphabet = bytes(range(40, 127))
        self.aes_key = bytes(secrets.choice(alphabet) for _ in range(16))
        self.iv = bytes(secrets.choice(alphabet) for _ in range(16))
        self._encrypt_key_header = self._build_encrypt_key_header()

    @property
    def header(self) -> str:
        return self._encrypt_key_header

    def _build_encrypt_key_header(self) -> str:
        key_json = json.dumps(
            {
                "key": self.aes_key.decode("utf-8", errors="replace"),
                "iv": self.iv.decode("utf-8", errors="replace"),
            },
            separators=(",", ":"),
        ).encode()
        der = base64.b64decode(_RSA_PUB_B64)
        pub = load_der_public_key(der)
        if not isinstance(pub, rsa.RSAPublicKey):
            raise TypeError("Not an RSA key")
        encrypted = pub.encrypt(key_json, padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _nonce() -> str:
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()-_=+[]{}"
        return "".join(secrets.choice(chars) for _ in range(4))

    @staticmethod
    def _zero_pad(data: bytes, block_size: int = 16) -> bytes:
        pad_len = block_size - (len(data) % block_size)
        if pad_len == block_size:
            return data
        return data + (b"\x00" * pad_len)

    def encrypt_body(self, body: dict) -> str:
        body = {**body, "nonce": self._nonce(), "timestamp": int(time.time() * 1000)}
        raw = json.dumps(body, separators=(",", ":")).encode()
        raw = self._zero_pad(raw)
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        encryptor = cipher.encryptor()
        ct = encryptor.update(raw) + encryptor.finalize()
        return json.dumps({"data": base64.b64encode(ct).decode()})

    def decrypt_response(self, raw: bytes | str) -> dict:
        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        encrypted = base64.b64decode(text)
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        decryptor = cipher.decryptor()
        pt = decryptor.update(encrypted) + decryptor.finalize()
        return json.loads(pt.rstrip(b"\x00").decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AiperClient(PoolDevice):
    provider_name = "aiper"

    def __init__(self, credentials: dict) -> None:
        super().__init__(credentials)
        region = (credentials.get("region") or "eu").lower()
        if region not in REGION_BASES:
            region = "eu"
        self.base_url = credentials.get("base_url") or REGION_BASES[region]
        self.base_url = self.base_url.rstrip("/")
        self._token: str | None = None
        self._openid: OpenIdInfo | None = None

    def _post(self, path: str, body: dict | None = None, token: str = "") -> dict:
        crypto = _AiperCrypto()
        headers = {
            "Content-Type": "application/json",
            "version": "3.0.0",
            "os": "android",
            "charset": "UTF-8",
            "Accept-Language": "en",
            "zoneId": "Europe/London",
            "encryptKey": crypto.header,
            "token": token or (self._token or ""),
        }
        data = crypto.encrypt_body(body) if body else None
        try:
            resp = httpx.post(
                f"{self.base_url}{path}",
                content=data,
                headers=headers,
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach Aiper: {exc}") from exc
        try:
            payload = crypto.decrypt_response(resp.content)
        except Exception as exc:
            raise ProviderError(f"Failed to decrypt Aiper response: {exc}") from exc
        return payload

    @staticmethod
    def _is_success(payload: dict) -> bool:
        code = str(payload.get("code", ""))
        return code in ("0", "200") or payload.get("successful") is True

    def _login(self) -> str:
        if self._token:
            return self._token
        email = self.credentials.get("email")
        password = self.credentials.get("password")
        if not email or not password:
            raise ProviderError("Aiper requires an email and password")

        payload = self._post("/login", {"email": email, "password": password}, token="")

        code = str(payload.get("code", ""))
        if code in ("401", "403"):
            raise ProviderError("Aiper rejected the email/password")
        if code == "5050":
            raise ProviderError(
                "Account not found in this region. "
                "Try a different region (US, EU, or Asia)."
            )
        if not self._is_success(payload):
            msg = payload.get("msg") or payload.get("message") or payload.get("mess") or "Unknown error"
            raise ProviderError(f"Aiper login failed: {msg}")

        data = payload.get("data") or {}
        token = data.get("token")
        if not token:
            raise ProviderError("Aiper login did not return a token")

        domains = data.get("domain") or []
        if domains:
            self.base_url = str(domains[0]).rstrip("/")

        self._token = token
        return token

    def _get(self, path: str, body: dict | None = None) -> dict:
        self._login()
        payload = self._post(path, body or {})
        code = str(payload.get("code", ""))
        if code in ("401", "403"):
            self._token = None
            self._login()
            payload = self._post(path, body or {})
        if not self._is_success(payload):
            raise ProviderError(f"Aiper API error for {path}: {payload.get('msg') or payload.get('message')}")
        return payload

    def verify(self) -> bool:
        self._login()
        return True

    def _ensure_openid(self) -> OpenIdInfo:
        """Fetch the Cognito/IoT identity needed to read device shadows."""
        if self._openid:
            return self._openid
        self._login()
        payload = self._get("/users/getOpenIdToken")
        data = payload.get("data") or {}
        identity_id = data.get("identityId")
        iot_endpoint = data.get("iotEndpoint")
        token = data.get("token")
        if not (identity_id and iot_endpoint and token):
            raise ProviderError("Aiper did not return the IoT identity needed to read readings")

        region = data.get("region")
        if not region and ".iot." in iot_endpoint:
            region = iot_endpoint.split(".iot.", 1)[1].split(".", 1)[0]
        if not region:
            raise ProviderError("Could not determine the Aiper IoT region")

        self._openid = OpenIdInfo(
            identity_id=identity_id,
            iot_endpoint=iot_endpoint,
            region=region,
            token=token,
        )
        return self._openid

    def _list_device_serials(self) -> list[str]:
        payload = self._get("/equipment/getEquipment")
        data = payload.get("data", [])
        if isinstance(data, dict):
            data = data.get("list", data.get("equipments", []))
        if not isinstance(data, list):
            data = []
        serials: list[str] = []
        for device in data:
            sn = device.get("sn") or device.get("device_id") or device.get("id")
            if sn:
                serials.append(str(sn))
        return serials

    def latest_measurements(self) -> list[DeviceMeasurement]:
        serials = self._list_device_serials()
        if not serials:
            raise ProviderError("No Aiper devices found on this account")

        openid = self._ensure_openid()
        results: list[DeviceMeasurement] = []
        for sn in serials:
            try:
                wqs = read_water_quality(openid, sn)
            except RuntimeError as exc:
                raise ProviderError(str(exc)) from exc
            measurement = self._parse_w2wqs(wqs, sn) if wqs else None
            if measurement:
                results.append(measurement)

        if not results:
            # Device(s) found but no readings came back in time — usually means
            # the monitor is offline or asleep. Don't store anything.
            raise ProviderError(
                "Connected, but no recent reading was available "
                "(the monitor may be offline or asleep). Try again shortly."
            )
        return results

    @staticmethod
    def _parse_w2wqs(wqs: dict, device_id: str) -> DeviceMeasurement | None:
        """Turn a W2WQS water-quality block into a normalised measurement."""
        # result != 0 means the sample is not valid/ready; skip it.
        result = wqs.get("result")
        if result is not None:
            try:
                if int(result) != 0:
                    return None
            except (TypeError, ValueError):
                pass

        measured: dict[str, float] = {}
        taken_at = dt.datetime.now(dt.timezone.utc)
        for key, value in wqs.items():
            k = str(key).lower()
            if k == "time" and value:
                taken_at = _parse_timestamp(value) or taken_at
                continue
            if k not in _FIELD_MAP or value is None:
                continue
            field, scale = _FIELD_MAP[k]
            try:
                scaled = float(value) * scale
            except (TypeError, ValueError):
                continue
            lo, hi = _PLAUSIBLE.get(field, (float("-inf"), float("inf")))
            if not (lo <= scaled <= hi):
                continue  # drop sentinel / uncalibrated values
            measured.setdefault(field, scaled)

        # TDS isn't reported directly; approximate it from EC when missing.
        if "tds" not in measured and "ec" in measured:
            measured["tds"] = round(measured["ec"] * 0.64, 1)

        if not measured:
            return None
        return DeviceMeasurement(taken_at=taken_at, external_id=device_id, **measured)


def _parse_timestamp(value) -> dt.datetime | None:
    """Best-effort parse of an epoch (s/ms) or ISO-8601 timestamp to UTC."""
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1_000_000_000_000 else value
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
