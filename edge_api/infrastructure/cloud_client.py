import logging
from datetime import datetime, timezone
from typing import Protocol

import requests

from ..domain.model import SensorReading

logger = logging.getLogger(__name__)


class CloudGateway(Protocol):
    def publish_reading(self, reading: SensorReading) -> bool: ...

    def publish_batch(self, readings: list[SensorReading]) -> bool: ...


def _iso(dt: datetime) -> str:
    utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z")


def _payload(reading: SensorReading) -> dict:
    return {
        "deviceCode": reading.sensor_node_id,
        "temperature": reading.calibrated_temperature,
        "humidity": reading.calibrated_humidity,
        "capturedAt": _iso(reading.raw.captured_at),
    }


class HttpCloudClient:
    """Publica ReadingCaptured hacia la RESTful API central usando la credencial de dispositivo del gateway."""

    def __init__(self, base_url: str, device_credential: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"X-Device-Credential": device_credential, "Content-Type": "application/json"}

    def _post(self, path: str, body, expected_readings: int = 1) -> bool:
        if not self.base_url or not self.headers["X-Device-Credential"]:
            return False
        try:
            response = requests.post(f"{self.base_url}{path}", json=body, headers=self.headers,
                                     timeout=self.timeout, allow_redirects=False)
            if not 200 <= response.status_code < 300:
                logger.warning("Core rejected ingestion with HTTP %s; readings remain pending", response.status_code)
                return False
            receipt = response.json()
            if not isinstance(receipt, dict):
                return False
            counts = [receipt.get(key) for key in ("readingsAccepted", "measurementsCreated", "measurementsReused")]
            return (all(type(value) is int and value >= 0 for value in counts)
                    and counts[0] == expected_readings and counts[1] + counts[2] == 2 * expected_readings)
        except (requests.RequestException, ValueError):
            # Do not log request headers or exceptions that might contain credentials.
            logger.warning("Core ingestion could not be confirmed; readings remain pending")
            return False

    def publish_reading(self, reading: SensorReading) -> bool:
        return self._post("/api/v1/measurements", _payload(reading))

    def publish_batch(self, readings: list[SensorReading]) -> bool:
        for offset in range(0, len(readings), 500):
            batch = readings[offset:offset + 500]
            if not self._post("/api/v1/measurements/batch", {"measurements": [_payload(r) for r in batch]}, len(batch)):
                return False
        return True
