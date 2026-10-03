from datetime import datetime, timedelta

import pytest

from edge_api import create_app
from edge_api.config import Config


class FakeCloud:
    def __init__(self):
        self.online = True
        self.published = []
        self.batches = []

    def publish_reading(self, reading):
        if not self.online:
            return False
        self.published.append(reading.id)
        return True

    def publish_batch(self, readings):
        if not self.online:
            return False
        self.batches.append([r.raw.captured_at for r in readings])
        return True


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def cloud():
    return FakeCloud()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(tmp_path, cloud, clock):
    def _make(**overrides):
        config = Config(
            database_path=str(tmp_path / "edge-test.db"),
            gateway_id="gw-test",
            enable_availability_loop=False,
            **overrides,
        )
        return create_app(config, cloud=cloud, clock=clock).test_client()

    return _make


@pytest.fixture
def client(make_client):
    c = make_client()
    c.post("/api/v1/edge/sensor-nodes", json={"deviceCode": "esp32-01", "samplingIntervalSeconds": 60})
    return c


def post_reading(client, temperature=5.0, humidity=60.0, captured_at=None, device="esp32-01"):
    body = {"deviceCode": device, "temperature": temperature, "humidity": humidity}
    if captured_at:
        body["capturedAt"] = captured_at.isoformat() + "Z"
    return client.post("/api/v1/edge/readings", json=body)
