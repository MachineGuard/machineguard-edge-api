"""Opt-in contract tests against a real isolated Core API; no FakeCloud or fabricated incidents."""
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
import secrets
from conftest import AuthenticatedEdgeClient

from edge_api import create_app
from edge_api.config import Config
from edge_api.infrastructure.cloud_client import HttpCloudClient

pytestmark = pytest.mark.live_core


@pytest.fixture
def live(tmp_path):
    base = os.getenv("MACHINEGUARD_CORE_URL", "").rstrip("/")
    email = os.getenv("MACHINEGUARD_CORE_ADMIN_EMAIL", "")
    password = os.getenv("MACHINEGUARD_CORE_ADMIN_PASSWORD", "")
    if not base or not email or not password:
        pytest.skip("Set MACHINEGUARD_CORE_URL and isolated Core admin credentials to run live contract tests")
    login = requests.post(base + "/api/v1/auth/login", json={"email": email, "password": password}, timeout=15)
    assert login.status_code == 200, "Core test login failed"
    headers = {"Authorization": "Bearer " + login.json()["accessToken"]}
    issued = []

    def core(method, path, body=None, expected=200, params=None):
        response = requests.request(method, base + path, json=body, headers=headers, params=params,
                                    timeout=20, allow_redirects=False)
        assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
        return response.json() if response.content else None

    suffix = uuid.uuid4().hex[:10]
    code = "e2e-" + suffix
    zone = core("POST", "/api/v1/environmental-monitoring/zones", {"name": "E2E " + suffix}, 201)["id"]
    point = core("POST", f"/api/v1/environmental-monitoring/zones/{zone}/points", {"name": "Point"}, 201)["id"]
    sensor = core("POST", f"/api/v1/environmental-monitoring/zones/{zone}/points/{point}/sensors",
                  {"deviceCode": code, "samplingIntervalSeconds": 60}, 201)["id"]
    for variable, minimum, maximum in [("TEMPERATURE", 2, 8), ("HUMIDITY", 30, 75)]:
        core("PUT", f"/api/v1/environmental-monitoring/zones/{zone}/thresholds/{variable}",
             {"minimumValue": minimum, "maximumValue": maximum})

    def provision():
        result = core("POST", "/api/v1/gateways", {"name": "E2E " + suffix, "sensorNodeIds": [sensor]}, 201)
        issued.append(result["gateway"]["id"])
        return result

    gateway = provision()
    config = Config(database_path=str(tmp_path / "live-edge.db"), gateway_id=gateway["gateway"]["id"], admin_key=secrets.token_urlsafe(32),
                    cloud_base_url=base, device_credential=gateway["credential"], enable_availability_loop=False,enable_delivery_worker=False)
    client = AuthenticatedEdgeClient(create_app(config).test_client(), config.admin_key)
    assert client.post("/api/v1/edge/sensor-nodes", json={"deviceCode": code}).status_code == 201
    assert client.put(f"/api/v1/edge/calibration/{code}/offset",
                      json={"temperatureOffset": -0.5, "humidityOffset": 2}).status_code == 200
    started = datetime.now(timezone.utc)

    def send(client, temperature=11.5, seconds_ago=120):
        captured = (started - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")
        result = client.post("/api/v1/edge/readings", json={"deviceCode": code, "temperature": temperature,
                             "humidity": 48, "capturedAt": captured})
        assert result.status_code == 201
        return result.get_json()

    def history_count():
        return core("GET", "/api/v1/environmental-monitoring/measurements/history", params={
            "monitoringZoneId": zone, "from": (started - timedelta(hours=1)).isoformat(),
            "to": (started + timedelta(minutes=1)).isoformat()})["totalElements"]

    yield SimpleNamespace(core=core, config=config, edge=client, gateway=gateway, zone=zone, sensor=sensor,
                          send=send, history_count=history_count, provision=provision)
    for identity in issued:
        requests.post(base + f"/api/v1/gateways/{identity}/revoke", headers=headers, timeout=15)


def test_real_gateway_reading_creates_alert_and_completes_incident_attention(live):
    assert live.send(live.edge)["status"] == "SYNCED"
    latest = live.core("GET", "/api/v1/environmental-monitoring/measurements/latest",
                       params={"monitoringZoneId": live.zone})
    assert {m["environmentalVariable"]: float(m["measuredValue"]) for m in latest} == {"TEMPERATURE": 11.0, "HUMIDITY": 50.0}
    alerts = live.core("GET", "/api/v1/alerts", params={"monitoringZoneId": live.zone})
    assert len(alerts) == 1
    alert = alerts[0]
    incident = alert["incidentId"]
    live.core("POST", f"/api/v1/alerts/{alert['id']}/acknowledgements", {"note": "Attending E2E"}, 201)
    live.core("POST", f"/api/v1/incidents/{incident}/corrective-actions", {"description": "Repaired E2E cooling"}, 201)
    live.core("POST", f"/api/v1/incidents/{incident}/resolve", expected=409)
    assert live.send(live.edge, temperature=5.5, seconds_ago=60)["status"] == "SYNCED"
    assert live.core("GET", f"/api/v1/incidents/{incident}")["incident"]["status"] == "ACKNOWLEDGED"
    live.core("POST", f"/api/v1/incidents/{incident}/resolve")
    assert live.core("GET", f"/api/v1/incidents/{incident}")["alert"]["status"] == "CLOSED"
    excursions = live.core("GET", "/api/v1/traceability/excursions", params={"monitoringZoneId": live.zone})
    assert len(excursions) == 1 and excursions[0]["incidentId"] == incident
    assert live.history_count() == 4


class LoseFirstConfirmation(HttpCloudClient):
    lose_next = True

    def _post(self, path, body, expected_readings=1):
        confirmed = super()._post(path, body, expected_readings)
        if confirmed and self.lose_next:
            self.lose_next = False
            return False  # Core accepted it, but the sender did not receive its confirmation.
        return confirmed


def test_real_core_response_loss_is_recovered_without_duplicate_measurements(live):
    cloud = LoseFirstConfirmation(live.config.cloud_base_url, live.config.device_credential)
    client = AuthenticatedEdgeClient(create_app(live.config, cloud=cloud).test_client(), live.config.admin_key, live.edge.tokens)
    assert live.send(client)["status"] == "CALIBRATED"
    assert client.get("/api/v1/edge/buffer").get_json()["pendingCount"] == 1
    assert live.history_count() == 2
    synced = client.post("/api/v1/edge/buffer/sync").get_json()
    assert synced == {"synced": 1, "pending": 0, "bufferSynced": True}
    assert live.history_count() == 2
    assert len(live.core("GET", "/api/v1/alerts", params={"monitoringZoneId": live.zone})) == 1


def test_revoked_gateway_keeps_pending_data_and_reprovisioning_recovers_it(live):
    live.core("POST", f"/api/v1/gateways/{live.gateway['gateway']['id']}/revoke")
    assert live.send(live.edge)["status"] == "CALIBRATED"
    assert live.edge.get("/api/v1/edge/buffer").get_json()["pendingCount"] == 1
    assert live.history_count() == 0
    renewed = live.provision()
    restarted = AuthenticatedEdgeClient(create_app(replace(live.config, device_credential=renewed["credential"])).test_client(),
                                         live.config.admin_key, live.edge.tokens)
    assert restarted.post("/api/v1/edge/buffer/sync").get_json()["pending"] == 0
    assert live.history_count() == 2


def test_edge_boundary_rejections_never_reach_real_core(live):
    code = next(iter(live.edge.tokens))
    raw_client = live.edge.client
    payload = {'deviceCode': code, 'temperature': 5, 'humidity': 50}
    assert raw_client.post('/api/v1/edge/readings', json=payload).status_code == 401
    headers = {'X-Node-Credential': live.edge.tokens[code]}
    assert raw_client.post('/api/v1/edge/readings', json={**payload, 'deviceCode': 'other'}, headers=headers).status_code == 403
    assert raw_client.post('/api/v1/edge/buffer/sync', json={}, headers=headers).status_code == 403
    invalid = '{"deviceCode":"' + code + '","temperature":Infinity,"humidity":50}'
    assert raw_client.post('/api/v1/edge/readings', data=invalid, content_type='application/json', headers=headers).status_code == 400
    # The fixture has humidity offset +2: plausible raw input becomes implausible.
    assert live.edge.post('/api/v1/edge/readings', json={**payload, 'humidity': 99.5}).status_code == 202
    assert live.history_count() == 0
    assert live.edge.get('/api/v1/edge/buffer').json['pendingCount'] == 0
    assert live.core('GET', '/api/v1/alerts', params={'monitoringZoneId': live.zone}) == []


def test_sample_retry_without_node_clock_survives_restart_and_reaches_core_once(live):
    code = next(iter(live.edge.tokens))
    headers = {'X-Node-Credential': live.edge.tokens[code]}
    payload = {'deviceCode': code, 'sampleId': 'clockless-1', 'temperature': 11.5, 'humidity': 48}
    first = live.edge.client.post('/api/v1/edge/readings', json=payload, headers=headers)
    assert first.status_code == 201 and first.json['status'] == 'SYNCED'
    live.edge.put(f'/api/v1/edge/calibration/{code}/offset', json={'temperatureOffset': 1, 'humidityOffset': 0})
    restarted = create_app(live.config).test_client()
    again = restarted.post('/api/v1/edge/readings', json=payload, headers=headers)
    assert again.status_code == 201 and again.json == first.json
    assert live.history_count() == 2
    assert len(live.core('GET', '/api/v1/alerts', params={'monitoringZoneId': live.zone})) == 1
    future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    invalid = restarted.post('/api/v1/edge/readings', headers=headers,
        json={**payload, 'sampleId': 'clockless-2', 'capturedAt': future})
    assert invalid.status_code == 400 and live.history_count() == 2
