"""Unit tests for the PoolLab / LabCOM adapter (no network)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.integrations.base import ProviderError
from app.integrations.poollab import PoolLabClient

BASE_TS = 1_750_000_000  # 2025-06-15T15:06:40Z


def _row(offset_s: int, parameter: str, value: str, serial: str = "PL2-001", **extra):
    return {
        "id": offset_s,
        "parameter": parameter,
        "unit": "ppm",
        "value": value,
        "device_serial": serial,
        "operator_name": "",
        "timestamp": BASE_TS + offset_s,
        **extra,
    }


def _cloud_payload(measurements, account_id=42):
    return {
        "data": {
            "CloudAccount": {
                "id": 7,
                "email": "owner@example.com",
                "Accounts": [
                    {
                        "id": account_id,
                        "forename": "Garden",
                        "surname": "Pool",
                        "Measurements": measurements,
                    }
                ],
            }
        }
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _patch_post(monkeypatch, response, capture=None):
    def fake_post(url, json=None, headers=None, timeout=None):
        if capture is not None:
            capture.update({"url": url, "json": json, "headers": headers})
        return response

    monkeypatch.setattr(httpx, "post", fake_post)


def test_verify_sends_raw_token_header(monkeypatch):
    sent = {}
    _patch_post(monkeypatch, FakeResponse(payload=_cloud_payload([])), capture=sent)
    assert PoolLabClient({"api_key": " token-123 "}).verify() is True
    # LabCOM expects the bare token, no "Bearer" prefix.
    assert sent["headers"]["Authorization"] == "token-123"
    assert "CloudAccount" in sent["json"]["query"]


def test_missing_api_key_rejected():
    with pytest.raises(ProviderError, match="API key"):
        PoolLabClient({}).verify()


def test_invalid_token_maps_to_provider_error(monkeypatch):
    _patch_post(monkeypatch, FakeResponse(status_code=401))
    with pytest.raises(ProviderError, match="rejected"):
        PoolLabClient({"api_key": "bad"}).verify()


def test_graphql_errors_map_to_provider_error(monkeypatch):
    _patch_post(
        monkeypatch,
        FakeResponse(payload={"errors": [{"message": "boom"}], "data": None}),
    )
    with pytest.raises(ProviderError, match="boom"):
        PoolLabClient({"api_key": "k"}).verify()


def test_session_grouping_merges_a_test_run(monkeypatch):
    rows = [
        _row(0, "PL pH", "7.2"),
        _row(300, "PL Chlorine Free", "1.4"),
        _row(900, "PL T-Alka", "110"),
    ]
    _patch_post(monkeypatch, FakeResponse(payload=_cloud_payload(rows)))
    (m,) = PoolLabClient({"api_key": "k"}).latest_measurements()
    assert m.ph == 7.2
    assert m.free_chlorine == 1.4
    assert m.total_alkalinity == 110
    assert m.external_id == "42:PL2-001"
    # The session is stamped with the last test's time.
    assert m.taken_at == datetime.fromtimestamp(BASE_TS + 900, tz=timezone.utc)


def test_gap_over_an_hour_starts_a_new_session(monkeypatch):
    rows = [
        _row(0, "PL pH", "7.2"),
        _row(2 * 3600, "PL Chlorine Free", "1.4"),
    ]
    _patch_post(monkeypatch, FakeResponse(payload=_cloud_payload(rows)))
    first, second = PoolLabClient({"api_key": "k"}).latest_measurements()
    assert first.ph == 7.2 and first.free_chlorine is None
    assert second.free_chlorine == 1.4 and second.ph is None


def test_retest_in_same_session_wins(monkeypatch):
    rows = [
        _row(0, "PL pH", "7.9"),
        _row(600, "PL pH", "7.4"),  # re-tested after dosing prep
    ]
    _patch_post(monkeypatch, FakeResponse(payload=_cloud_payload(rows)))
    (m,) = PoolLabClient({"api_key": "k"}).latest_measurements()
    assert m.ph == 7.4


def test_skips_tutorial_overrange_and_unknown_rows(monkeypatch):
    rows = [
        _row(0, "PL pH", "7.0", serial="tutorial"),
        _row(10, "PL pH", "7.0", operator_name="Tutorial"),
        _row(20, "PL Chlorine Free", "OVERRANGE"),
        _row(30, "PL Urea", "1.0"),  # parameter the app doesn't track
        _row(40, "PL Cyanuric Acid", "45"),
    ]
    _patch_post(monkeypatch, FakeResponse(payload=_cloud_payload(rows)))
    (m,) = PoolLabClient({"api_key": "k"}).latest_measurements()
    assert m.cyanuric_acid == 45
    assert m.ph is None
    assert m.free_chlorine is None


def test_devices_are_grouped_separately(monkeypatch):
    rows = [
        _row(0, "PL pH", "7.2", serial="PL2-001"),
        _row(60, "PL pH", "7.6", serial="PL2-002"),
    ]
    _patch_post(monkeypatch, FakeResponse(payload=_cloud_payload(rows)))
    measurements = PoolLabClient({"api_key": "k"}).latest_measurements()
    assert {m.external_id for m in measurements} == {"42:PL2-001", "42:PL2-002"}
    assert sorted(m.ph for m in measurements) == [7.2, 7.6]
