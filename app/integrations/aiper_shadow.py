"""Read an Aiper device's water-quality readings from its AWS IoT shadow.

Aiper's HydroComm reports pH/ORP/EC/TDS/chlorine/temperature only over MQTT,
not REST. The readings live in the device's AWS IoT *shadow*. To fetch them we:

1. Exchange the account's Cognito OpenID token for temporary AWS credentials.
2. Open an MQTT-over-WebSocket connection to AWS IoT, signing the WebSocket URL
   with SigV4 (hand-rolled with the stdlib — no AWS SDK dependency).
3. Subscribe to the device's shadow ``get/accepted`` topic plus its custom
   report topics, publish an empty ``shadow/get``, and take the first message
   that carries a ``W2WQS`` (water-quality) block.

This is a short connect-get-disconnect cycle, run synchronously. If the device
is offline or nothing arrives in time we return ``None`` and the caller surfaces
a clear "no reading available" error rather than storing anything.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger("pool_tracking.aiper_shadow")

# XOR key the device uses on its custom (non-AWS) report topics.
_XOR_KEY = bytes([0x12, 0x34, 0x56, 0x78])

_IOT_SERVICE = "iotdevicegateway"


@dataclass
class OpenIdInfo:
    """Cognito identity + IoT endpoint, from Aiper's /users/getOpenIdToken."""

    identity_id: str
    iot_endpoint: str
    region: str
    token: str


# ---------------------------------------------------------------------------
# AWS credentials
# ---------------------------------------------------------------------------

def fetch_aws_credentials(info: OpenIdInfo) -> dict:
    """Exchange the OpenID token for temporary AWS credentials via Cognito."""
    url = f"https://cognito-identity.{info.region}.amazonaws.com/"
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
    }
    body = {
        "IdentityId": info.identity_id,
        "Logins": {"cognito-identity.amazonaws.com": info.token},
    }
    resp = httpx.post(url, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    creds = (resp.json() or {}).get("Credentials") or {}
    if not creds.get("AccessKeyId"):
        raise RuntimeError("Cognito did not return AWS credentials")
    return creds


# ---------------------------------------------------------------------------
# SigV4 WebSocket presigning
# ---------------------------------------------------------------------------

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _presign_ws_path(endpoint: str, region: str, creds: dict) -> str:
    """Build the SigV4-signed ``/mqtt?...`` path for an AWS IoT WebSocket."""
    access_key = creds["AccessKeyId"]
    secret_key = creds["SecretKey"]
    session_token = creds.get("SessionToken", "")

    now = dt.datetime.now(dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    canonical_uri = "/mqtt"
    credential_scope = f"{datestamp}/{region}/{_IOT_SERVICE}/aws4_request"

    # Query params must be in sorted order for the canonical request; these
    # four already are. The security token is appended *after* signing.
    canonical_qs = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={quote(access_key + '/' + credential_scope, safe='')}"
        f"&X-Amz-Date={amzdate}"
        "&X-Amz-SignedHeaders=host"
    )
    canonical_headers = f"host:{endpoint}\n"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(
        ["GET", canonical_uri, canonical_qs, canonical_headers, "host", payload_hash]
    )

    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amzdate,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, _IOT_SERVICE),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    canonical_qs += f"&X-Amz-Signature={signature}"
    if session_token:
        canonical_qs += f"&X-Amz-Security-Token={quote(session_token, safe='')}"
    return f"{canonical_uri}?{canonical_qs}"


# ---------------------------------------------------------------------------
# Message decoding
# ---------------------------------------------------------------------------

def _xor_decode(data: bytes) -> str:
    """Decode an XOR+base64 payload from a device report topic."""
    decoded = base64.b64decode(data)
    return bytes(b ^ _XOR_KEY[i % 4] for i, b in enumerate(decoded)).decode("utf-8")


def _parse_message(payload: bytes) -> dict | None:
    """Parse one MQTT payload (plain JSON or XOR+base64) into a dict."""
    for decoder in (lambda p: p.decode("utf-8"), _xor_decode):
        try:
            data = json.loads(decoder(payload))
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - try the next decoder
            continue
    return None


def _find_w2wqs(data: dict) -> dict | None:
    """Locate the W2WQS water-quality block in a shadow or report message."""
    # Classic shadow: {"state": {"reported": {... "W2WQS": {...}}}}
    state = data.get("state")
    if isinstance(state, dict) and isinstance(state.get("reported"), dict):
        data = state["reported"]
    # Documents shadow: {"current": {"state": {"reported": {...}}}}
    current = data.get("current")
    if isinstance(current, dict):
        cur_state = current.get("state")
        if isinstance(cur_state, dict) and isinstance(cur_state.get("reported"), dict):
            data = cur_state["reported"]

    if isinstance(data.get("W2WQS"), dict):
        return data["W2WQS"]
    # Some firmwares wrap it as {"type": "W2WQS", "data": {...}}.
    if data.get("type") == "W2WQS" and isinstance(data.get("data"), dict):
        return data["data"]
    return None


# ---------------------------------------------------------------------------
# Shadow read
# ---------------------------------------------------------------------------

def read_water_quality(
    info: OpenIdInfo, sn: str, *, timeout: float = 20.0
) -> dict | None:
    """Return the latest W2WQS block for device ``sn``, or ``None`` if none.

    Connects to AWS IoT, requests the device shadow, and waits up to ``timeout``
    seconds for a message carrying water-quality data. Returns ``None`` if the
    device is offline or nothing arrives in time.
    """
    import paho.mqtt.client as mqtt

    creds = fetch_aws_credentials(info)
    ws_path = _presign_ws_path(info.iot_endpoint, info.region, creds)

    result: dict[str, dict | None] = {"wqs": None}
    seen: list[str] = []  # "topic -> top-level keys" for each message, for logging
    got_data = threading.Event()
    got_shadow = threading.Event()  # the classic shadow reply arrived (any content)
    connected = threading.Event()

    get_topic = f"$aws/things/{sn}/shadow/get"
    # Subscribe broadly: the classic shadow usually holds the reading, but the
    # device may also push it on its custom report topics, so listen to both.
    sub_topics = [
        f"$aws/things/{sn}/shadow/get/accepted",
        f"$aws/things/{sn}/shadow/get/rejected",
        f"$aws/things/{sn}/shadow/update/accepted",
        f"$aws/things/{sn}/shadow/update/documents",
        f"aiper/things/{sn}/shadow/report",
        f"aiper/things/{sn}/app/report",
    ]

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            logger.warning("Aiper IoT connect failed for %s: %s", sn, reason_code)
            return
        client.subscribe([(t, 1) for t in sub_topics])
        connected.set()

    def on_subscribe(client, userdata, mid, reason_codes, properties=None, *args):
        # 0x80 here would mean the IoT policy denied a topic; log only if so.
        denied = [str(rc) for rc in reason_codes if getattr(rc, "is_failure", False)]
        if denied:
            logger.warning("Aiper IoT denied subscriptions for %s: %s", sn, denied)
        # Publish the shadow-get only after the subscription is confirmed; shadow
        # responses aren't retained, so requesting before SUBACK can lose them.
        client.publish(get_topic, "", qos=1)

    def on_message(client, userdata, message):
        data = _parse_message(message.payload)
        if not data:
            seen.append(f"{message.topic} -> <unparseable {len(message.payload)}B>")
            return
        reported = data
        state = data.get("state")
        if isinstance(state, dict) and isinstance(state.get("reported"), dict):
            reported = state["reported"]
        seen.append(f"{message.topic} -> {sorted(reported.keys())[:12]}")
        if message.topic.endswith("/shadow/get/accepted"):
            got_shadow.set()
        wqs = _find_w2wqs(data)
        if wqs is not None:
            result["wqs"] = wqs
            got_data.set()

    client = mqtt.Client(
        client_id=info.identity_id,
        transport="websockets",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.ws_set_options(path=ws_path, headers={"Host": info.iot_endpoint})
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        client.connect(info.iot_endpoint, 443, keepalive=60)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not connect to Aiper's device cloud: {exc}") from exc

    client.loop_start()
    try:
        if not connected.wait(timeout=min(timeout, 10.0)):
            logger.warning("Aiper IoT connection timed out for %s", sn)
            return None
        # The classic shadow reply is authoritative and usually arrives in <1s.
        # If it carries water quality we're done; if it doesn't (e.g. a cleaner
        # robot, not a monitor), wait only a short grace for a live report rather
        # than blocking the whole timeout.
        if not got_data.wait(timeout=min(timeout, 8.0)):
            if got_shadow.is_set():
                got_data.wait(timeout=4.0)
            else:
                got_data.wait(timeout=max(0.0, timeout - 8.0))
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    if result["wqs"] is None:
        # Not an error: many Aiper devices (cleaner robots) have no water-quality
        # sensors. Log what we saw so it's clear where any readings would live.
        logger.info(
            "No water-quality data for %s. Messages seen: %s",
            sn, "; ".join(seen) if seen else "(none)",
        )
    return result["wqs"]
