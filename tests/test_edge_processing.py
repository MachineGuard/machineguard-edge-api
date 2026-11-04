from datetime import timedelta

from conftest import post_reading


def test_register_node_creates_profile_and_rejects_duplicates(client):
    duplicate = client.post("/api/v1/edge/sensor-nodes", json={"deviceCode": "esp32-01"})
    assert duplicate.status_code == 409
    profile = client.get("/api/v1/edge/calibration/esp32-01").get_json()
    assert profile["samplingIntervalSeconds"] == 60
    assert profile["temperatureOffset"] == 0.0


def test_valid_reading_is_calibrated_and_published_online(client, cloud):
    client.put("/api/v1/edge/calibration/esp32-01/offset", json={"temperatureOffset": -0.5, "humidityOffset": 2})
    response = post_reading(client, temperature=5.0, humidity=60.0)
    body = response.get_json()
    assert response.status_code == 201
    assert body["calibratedTemperature"] == 4.5
    assert body["calibratedHumidity"] == 62.0
    assert body["status"] == "SYNCED"
    assert len(cloud.published) == 1


def test_implausible_reading_is_discarded_and_never_published(client, cloud):
    response = post_reading(client, temperature=300.0)
    assert response.status_code == 202
    assert response.get_json()["status"] == "DISCARDED"
    assert cloud.published == []
    discarded = client.get("/api/v1/edge/readings/discarded").get_json()
    assert len(discarded) == 1


def test_reading_keeps_capture_timestamp_of_the_node(client, clock):
    captured = clock.now - timedelta(minutes=10)
    body = post_reading(client, captured_at=captured).get_json()
    assert body["capturedAt"] == captured.isoformat() + "Z"


def test_future_timestamp_and_unknown_node_are_rejected(client, clock):
    assert post_reading(client, captured_at=clock.now + timedelta(hours=1)).status_code == 400
    assert post_reading(client, device="ghost").status_code == 403
    assert client.post("/api/v1/edge/readings", json={"deviceCode": "esp32-01"}).status_code == 400


def test_offline_cloud_buffers_readings_and_sync_is_chronological_and_idempotent(client, cloud, clock):
    cloud.online = False
    t0 = clock.now - timedelta(minutes=30)
    # llegan desordenadas a propósito
    for offset in (20, 0, 10):
        post_reading(client, temperature=5.0 + offset / 10, captured_at=t0 + timedelta(minutes=offset))

    buffer = client.get("/api/v1/edge/buffer").get_json()
    assert buffer["pendingCount"] == 3
    assert buffer["status"] == "PENDING"
    assert buffer["oldestPendingAt"] == t0.isoformat() + "Z"

    cloud.online = True
    result = client.post("/api/v1/edge/buffer/sync").get_json()
    assert result == {"synced": 3, "pending": 0, "bufferSynced": True}
    assert cloud.batches[0] == sorted(cloud.batches[0])

    again = client.post("/api/v1/edge/buffer/sync").get_json()
    assert again["synced"] == 0 and again["bufferSynced"] is False
    assert len(cloud.batches) == 1
    assert client.get("/api/v1/edge/buffer").get_json()["lastSyncAt"] is not None


def test_sync_keeps_pending_when_cloud_is_still_down(client, cloud):
    cloud.online = False
    post_reading(client)
    result = client.post("/api/v1/edge/buffer/sync").get_json()
    assert result["synced"] == 0 and result["pending"] == 1


def test_new_readings_are_enqueued_while_buffer_has_pending_to_preserve_order(client, cloud):
    cloud.online = False
    post_reading(client)
    cloud.online = True
    body = post_reading(client).get_json()
    assert body["status"] == "CALIBRATED"
    assert cloud.published == []
    assert client.get("/api/v1/edge/buffer").get_json()["pendingCount"] == 2


def test_full_buffer_drops_oldest_pending_readings(make_client, cloud, clock):
    client = make_client(buffer_capacity=2)
    client.post("/api/v1/edge/sensor-nodes", json={"deviceCode": "esp32-01"})
    cloud.online = False
    t0 = clock.now - timedelta(minutes=10)
    for i in range(3):
        post_reading(client, captured_at=t0 + timedelta(minutes=i))
    buffer = client.get("/api/v1/edge/buffer").get_json()
    assert buffer["pendingCount"] == 2
    assert buffer["status"] == "FULL"
    assert buffer["oldestPendingAt"] == (t0 + timedelta(minutes=1)).isoformat() + "Z"
    assert len(client.get("/api/v1/edge/readings/discarded").get_json()) == 1


def test_offset_update_only_affects_later_readings(client):
    first = post_reading(client, temperature=5.0).get_json()
    client.put("/api/v1/edge/calibration/esp32-01/offset", json={"temperatureOffset": 1.0, "humidityOffset": 0})
    second = post_reading(client, temperature=5.0).get_json()
    assert first["calibratedTemperature"] == 5.0
    assert second["calibratedTemperature"] == 6.0
    stored_first = [r for r in client.get("/api/v1/edge/readings").get_json() if r["id"] == first["id"]][0]
    assert stored_first["calibratedTemperature"] == 5.0


def test_node_goes_offline_after_sampling_interval_and_is_restored_on_next_reading(client, clock):
    post_reading(client)
    clock.advance(seconds=30)
    assert client.get("/api/v1/edge/sensor-nodes/offline").get_json() == []

    clock.advance(seconds=60)
    offline = client.get("/api/v1/edge/sensor-nodes/offline").get_json()
    assert [n["deviceCode"] for n in offline] == ["esp32-01"]
    assert client.get("/api/v1/edge/sensor-nodes").get_json()[0]["status"] == "OFFLINE"
    # una segunda consulta no duplica el registro
    assert len(client.get("/api/v1/edge/sensor-nodes/offline").get_json()) == 1

    post_reading(client)
    assert client.get("/api/v1/edge/sensor-nodes/offline").get_json() == []
    assert client.get("/api/v1/edge/sensor-nodes").get_json()[0]["status"] == "ONLINE"


def test_plausibility_range_endpoint(client):
    body = client.get("/api/v1/edge/calibration/esp32-01/plausibility-range").get_json()
    assert body["temperatureRange"] == {"min": -40.0, "max": 80.0}
    assert body["humidityRange"] == {"min": 0.0, "max": 100.0}
