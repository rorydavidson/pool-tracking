"""PoolLab / LabCOM cloud integration (backend.labcom.cloud).

Water-i.d.'s PoolLab photometers (1.0 and 2.0) sync measurements via the
LabCOM app to the LabCOM cloud, which exposes a GraphQL API. Authentication
is a static API token the user generates on the LabCOM website (Settings →
API), sent as a raw ``Authorization`` header (no ``Bearer`` prefix).

A photometer reports one parameter per measurement row (a pH test, then a
chlorine test, ...), unlike probes which report a full snapshot. Rows taken
close together on the same device are grouped into a single "test session"
:class:`DeviceMeasurement` so the dashboard's latest reading shows the whole
water test, not just the last tested parameter. The full history is returned;
``store_measurements`` de-dupes on ``(external_id, taken_at)``.

Credentials dict shape: ``{"api_key": ...}``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from .base import DeviceMeasurement, PoolDevice, ProviderError

GRAPHQL_URL = "https://backend.labcom.cloud/graphql"

# Consecutive rows from the same device more than this far apart start a new
# test session (individual photometer tests are a few minutes apart).
SESSION_GAP = timedelta(minutes=60)

VERIFY_QUERY = "query { CloudAccount { id email } }"

MEASUREMENTS_QUERY = """
query {
  CloudAccount {
    id
    email
    Accounts {
      id
      forename
      surname
      Measurements {
        id
        parameter
        unit
        value
        device_serial
        operator_name
        timestamp
      }
    }
  }
}
"""

# Maps normalised LabCOM parameter names (lowercased, "pl " prefix stripped,
# punctuation collapsed) to DeviceMeasurement fields. LabCOM values are
# already in the app's canonical units (ppm / mg/l, °C); pH is unitless.
_PARAMETER_MAP = {
    "ph": "ph",
    "chlorine free": "free_chlorine",
    "free chlorine": "free_chlorine",
    "chlorine total": "total_chlorine",
    "total chlorine": "total_chlorine",
    "t alka": "total_alkalinity",
    "alkalinity": "total_alkalinity",
    "alkalinity m": "total_alkalinity",
    "total alkalinity": "total_alkalinity",
    "cyanuric acid": "cyanuric_acid",
    "ca hardness": "calcium_hardness",
    "calcium hardness": "calcium_hardness",
    "salt": "salt",
    "salinity": "salt",
    "temperature": "temperature_c",
}


def _normalise(parameter: str) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", parameter.lower()).strip()
    return name[3:] if name.startswith("pl ") else name


class PoolLabClient(PoolDevice):
    provider_name = "poollab"

    def _post(self, query: str) -> dict:
        """Run a GraphQL query and return the CloudAccount object."""
        api_key = (self.credentials.get("api_key") or "").strip()
        if not api_key:
            raise ProviderError("PoolLab requires a LabCOM API key")
        try:
            resp = httpx.post(
                GRAPHQL_URL,
                json={"query": query},
                headers={"Authorization": api_key},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach LabCOM: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("LabCOM rejected the API key")
        if resp.status_code == 429:
            raise ProviderError("LabCOM rate limit reached; try again in a minute")
        if resp.status_code >= 400:
            raise ProviderError(f"LabCOM API error ({resp.status_code})")
        data = resp.json()
        if data.get("errors"):
            message = (data["errors"][0] or {}).get("message", "unknown error")
            raise ProviderError(f"LabCOM query failed: {message}")
        account = (data.get("data") or {}).get("CloudAccount")
        if not account:
            raise ProviderError("LabCOM returned no account data")
        return account

    def verify(self) -> bool:
        self._post(VERIFY_QUERY)
        return True

    def latest_measurements(self) -> list[DeviceMeasurement]:
        cloud = self._post(MEASUREMENTS_QUERY)
        results: list[DeviceMeasurement] = []
        for account in cloud.get("Accounts") or []:
            rows = self._parse_rows(account.get("Measurements") or [])
            results.extend(self._sessions(account.get("id"), rows))
        return results

    @staticmethod
    def _parse_rows(rows: list[dict]) -> list[tuple[datetime, str, float, str]]:
        """Filter to usable rows as (taken_at, field, value, device_serial)."""
        parsed = []
        for row in rows:
            # LabCOM injects demo measurements into every new account.
            if (row.get("device_serial") or "").lower() == "tutorial":
                continue
            if (row.get("operator_name") or "").lower() == "tutorial":
                continue
            field = _PARAMETER_MAP.get(_normalise(row.get("parameter") or ""))
            if field is None:
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue  # "OVERRANGE" / "UNDERRANGE"
            ts = row.get("timestamp")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            taken_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            parsed.append((taken_at, field, value, row.get("device_serial") or ""))
        return parsed

    @staticmethod
    def _sessions(
        account_id: int | None, rows: list[tuple[datetime, str, float, str]]
    ) -> list[DeviceMeasurement]:
        """Group a pool's rows into per-device test sessions."""
        by_serial: dict[str, list[tuple[datetime, str, float, str]]] = {}
        for row in sorted(rows):
            by_serial.setdefault(row[3], []).append(row)

        results = []
        for serial, serial_rows in by_serial.items():
            session: list[tuple[datetime, str, float, str]] = []
            for row in serial_rows:
                if session and row[0] - session[-1][0] > SESSION_GAP:
                    results.append(PoolLabClient._build(account_id, serial, session))
                    session = []
                session.append(row)
            if session:
                results.append(PoolLabClient._build(account_id, serial, session))
        return results

    @staticmethod
    def _build(
        account_id: int | None, serial: str, session: list[tuple[datetime, str, float, str]]
    ) -> DeviceMeasurement:
        fields: dict[str, float] = {}
        for _taken_at, field, value, _serial in session:
            fields[field] = value  # time-ordered, so a re-test wins
        external_id = f"{account_id}:{serial}" if serial else str(account_id)
        return DeviceMeasurement(taken_at=session[-1][0], external_id=external_id, **fields)
