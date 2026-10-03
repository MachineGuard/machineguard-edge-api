import logging
from datetime import datetime
from typing import Protocol

import requests

from ..domain.model import SensorReading

logger = logging.getLogger(__name__)


class CloudGateway(Protocol):
    def publish_reading(self, reading: SensorReading) -> bool: ...

    def publish_batch(self, readings: list[SensorReading]) -> bool: ...


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


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

    def _post(self, path: str, body) -> bool:
        if not self.base_url:
            return False
        try:
            response = requests.post(f"{self.base_url}{path}", json=body, headers=self.headers, timeout=self.timeout)
            return response.status_code < 300
        except requests.RequestException as exc:
            logger.warning("Sin conectividad con la API central: %s", exc)
            return False

    def publish_reading(self, reading: SensorReading) -> bool:
        return self._post("/api/v1/measurements", _payload(reading))

    def publish_batch(self, readings: list[SensorReading]) -> bool:
        return self._post("/api/v1/measurements/batch", {"measurements": [_payload(r) for r in readings]})
