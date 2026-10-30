from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from edge_api.domain.model import RawReading, ReadingStatus, SensorReading
from edge_api.infrastructure.cloud_client import HttpCloudClient


def reading(at=None, identity=1):
    return SensorReading("MG-01", RawReading(11.5, 48, at or datetime(2026, 10, 30, 12, 0, 0, 123456)),
                         ReadingStatus.CALIBRATED, 11.0, 50.0, 1, identity)


def receipt(count=1, reused=False):
    return {"readingsAccepted": count, "measurementsCreated": 0 if reused else 2 * count,
            "measurementsReused": 2 * count if reused else 0}


def response(status=201, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body if body is not None else receipt())


def test_transport_preserves_calibrated_values_and_microseconds_and_does_not_follow_redirects(monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr(requests, "post", post)
    client = HttpCloudClient("http://core/", "test-secret")
    assert client.publish_reading(reading())
    args, kwargs = post.call_args
    assert args[0] == "http://core/api/v1/measurements"
    assert kwargs["json"] == {"deviceCode": "MG-01", "temperature": 11.0, "humidity": 50.0,
                              "capturedAt": "2026-10-30T12:00:00.123456Z"}
    assert kwargs["headers"]["X-Device-Credential"] == "test-secret"
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is False


def test_offsets_are_normalized_to_utc_without_losing_precision(monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr(requests, "post", post)
    at = datetime(2026, 10, 30, 7, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5)))
    assert HttpCloudClient("http://core", "secret").publish_reading(reading(at))
    assert post.call_args.kwargs["json"]["capturedAt"] == "2026-10-30T12:00:00.123456Z"


@pytest.mark.parametrize("url,credential", [("", "secret"), ("http://core", "")])
def test_missing_configuration_never_confirms_or_sends(url, credential, monkeypatch):
    post = Mock()
    monkeypatch.setattr(requests, "post", post)
    assert not HttpCloudClient(url, credential).publish_reading(reading())
    post.assert_not_called()


@pytest.mark.parametrize("status", [199, 301, 302, 400, 401, 403, 409, 500, 503])
def test_non_success_responses_preserve_pending_data_and_do_not_log_credentials(status, monkeypatch, caplog):
    monkeypatch.setattr(requests, "post", Mock(return_value=response(status)))
    assert not HttpCloudClient("http://core", "private-test-secret").publish_reading(reading())
    assert "private-test-secret" not in caplog.text


@pytest.mark.parametrize("body", [[], {}, {"readingsAccepted": 0, "measurementsCreated": 2, "measurementsReused": 0},
                                  {"readingsAccepted": 1, "measurementsCreated": 1, "measurementsReused": 0},
                                  {"readingsAccepted": True, "measurementsCreated": 2, "measurementsReused": 0}])
def test_a_success_status_without_a_matching_receipt_does_not_confirm(body, monkeypatch):
    monkeypatch.setattr(requests, "post", Mock(return_value=response(200, body)))
    assert not HttpCloudClient("http://core", "secret").publish_reading(reading())


@pytest.mark.parametrize("error", [requests.Timeout("timeout"), requests.ConnectionError("offline"), ValueError("not JSON")])
def test_transport_and_receipt_errors_leave_the_reading_pending(error, monkeypatch):
    post = Mock(side_effect=error)
    monkeypatch.setattr(requests, "post", post)
    assert not HttpCloudClient("http://core", "secret").publish_reading(reading())


def test_confirmed_duplicate_receipts_are_successful(monkeypatch):
    monkeypatch.setattr(requests, "post", Mock(return_value=response(200, receipt(reused=True))))
    assert HttpCloudClient("http://core", "secret").publish_reading(reading())


def test_large_batches_are_chunked_and_every_chunk_requires_confirmation(monkeypatch):
    sizes = []
    def post(url, **kwargs):
        size = len(kwargs["json"]["measurements"])
        sizes.append(size)
        return response(201, receipt(size))
    monkeypatch.setattr(requests, "post", post)
    readings = [reading(identity=i) for i in range(1001)]
    assert HttpCloudClient("http://core", "secret").publish_batch(readings)
    assert sizes == [500, 500, 1]


def test_partial_chunk_failure_keeps_the_whole_batch_for_idempotent_retry(monkeypatch):
    post = Mock(side_effect=[response(201, receipt(500)), response(503)])
    monkeypatch.setattr(requests, "post", post)
    readings = [reading(identity=i) for i in range(1001)]
    client = HttpCloudClient("http://core", "secret")
    assert not client.publish_batch(readings)
    assert post.call_count == 2
    sizes = []
    def replay(url, **kwargs):
        size = len(kwargs["json"]["measurements"])
        sizes.append(size)
        return response(200, receipt(size, reused=True))
    monkeypatch.setattr(requests, "post", replay)
    assert client.publish_batch(readings)
    assert sizes == [500, 500, 1]


def test_edge_response_keeps_the_capture_precision(client):
    result = client.post("/api/v1/edge/readings", json={"deviceCode": "esp32-01", "temperature": 5,
                         "humidity": 50, "capturedAt": "2026-10-01T11:59:59.123456Z"})
    assert result.status_code == 201
    assert result.get_json()["capturedAt"] == "2026-10-01T11:59:59.123456Z"
