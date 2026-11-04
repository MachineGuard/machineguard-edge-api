from datetime import datetime, timedelta
import secrets

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


class AuthenticatedEdgeClient:
    """Legacy behavior scenarios use the same real authorization boundary as nodes."""
    def __init__(self, client, admin_key, tokens=None):
        self.client, self.admin_key = client, admin_key
        self.tokens = tokens if tokens is not None else {}

    def open(self, path, method='GET', **kwargs):
        if 'headers' not in kwargs:
            if method == 'POST' and path == '/api/v1/edge/readings':
                code = kwargs.get('json', {}).get('deviceCode')
                token = self.tokens.get(code, next(iter(self.tokens.values()), 'not-issued'))
                kwargs['headers'] = {'X-Node-Credential': token}
            else:
                kwargs['headers'] = {'X-Edge-Admin-Key': self.admin_key}
        response = self.client.open(path, method=method, **kwargs)
        if method == 'POST' and path == '/api/v1/edge/sensor-nodes' and response.status_code == 201:
            self.tokens[response.json['deviceCode']] = response.json['nodeCredential']
        return response

    def get(self, path, **kwargs): return self.open(path, 'GET', **kwargs)
    def post(self, path, **kwargs): return self.open(path, 'POST', **kwargs)
    def put(self, path, **kwargs): return self.open(path, 'PUT', **kwargs)


@pytest.fixture
def cloud():
    return FakeCloud()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(tmp_path, cloud, clock):
    admin_key = secrets.token_urlsafe(32)
    tokens = {}
    def _make(**overrides):
        config = Config(
            database_path=str(tmp_path / "edge-test.db"),
            gateway_id="gw-test",
            enable_availability_loop=False,
            admin_key=admin_key,
            **overrides,
        )
        return AuthenticatedEdgeClient(create_app(config, cloud=cloud, clock=clock).test_client(), admin_key, tokens)

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
