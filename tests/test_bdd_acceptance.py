"""Pruebas BDD (pytest-bdd) de Edge Processing.

Cada escenario de tests/features corresponde a un criterio de aceptación de las
historias del Sprint 1 (TS02, TS03 y US03). FakeCloud simula la disponibilidad de la
API central y FakeClock controla el tiempo del gateway (ver conftest.py).
"""
from datetime import timedelta

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("features")


@pytest.fixture
def edge(make_client):
    """Estado del escenario: cliente HTTP del gateway y última respuesta."""
    return {"client": make_client(), "response": None, "readings": []}


def _send_reading(edge, clock, device, temperature=5.0, humidity=60.0, minutes_ago=None):
    body = {"deviceCode": device, "temperature": temperature, "humidity": humidity}
    if minutes_ago is not None:
        body["capturedAt"] = (clock.now - timedelta(minutes=minutes_ago)).isoformat() + "Z"
    edge["response"] = edge["client"].post("/api/v1/edge/readings", json=body)
    if edge["response"].status_code in (201, 202):
        edge["readings"].append(edge["response"].get_json()["id"])
    return edge["response"]


def _iso_minutes_ago(clock, minutes):
    return (clock.now - timedelta(minutes=minutes)).isoformat() + "Z"


# ---------- Contexto del gateway ----------

@given(parsers.parse('que el gateway Edge tiene registrado el nodo "{device}" '
                     'con un intervalo de muestreo de {seconds:d} segundos'))
def node_registered(edge, device, seconds):
    response = edge["client"].post("/api/v1/edge/sensor-nodes",
                                   json={"deviceCode": device, "samplingIntervalSeconds": seconds})
    assert response.status_code == 201, response.get_json()


@given(parsers.parse("que el Local Buffer del gateway tiene capacidad para {capacity:d} mediciones"))
def buffer_capacity(edge, make_client, capacity):
    # Reinicia el gateway con otra capacidad; usa la misma base SQLite, así que el nodo sigue registrado.
    edge["client"] = make_client(buffer_capacity=capacity)


@given("que la API central está disponible")
def cloud_available(cloud):
    cloud.online = True


@given("que la API central no está disponible")
def cloud_unavailable(cloud):
    cloud.online = False


@given("que se restablece la conexión con la API central")
@when("se restablece la conexión con la API central")
def cloud_restored(cloud):
    cloud.online = True


@given(parsers.parse("que pasan {seconds:d} segundos"))
@when(parsers.parse("pasan {seconds:d} segundos"))
def time_passes(clock, seconds):
    clock.advance(seconds=seconds)


# ---------- TS02: calibración y filtrado ----------

@given(parsers.parse('que el nodo "{device}" tiene un offset de calibración de {temperature:g} °C '
                     'y {humidity:g} % de humedad'))
@when(parsers.parse('se cambia el offset de calibración del nodo "{device}" a {temperature:g} °C '
                    'y {humidity:g} % de humedad'))
def set_offset(edge, device, temperature, humidity):
    response = edge["client"].put(f"/api/v1/edge/calibration/{device}/offset",
                                  json={"temperatureOffset": temperature, "humidityOffset": humidity})
    assert response.status_code == 200, response.get_json()


@when(parsers.parse('el nodo "{device}" envía una lectura de {temperature:g} °C y {humidity:g} % de humedad'))
def send_reading(edge, clock, device, temperature, humidity):
    _send_reading(edge, clock, device, temperature, humidity)


@then(parsers.parse("la medición calibrada es de {temperature:g} °C y {humidity:g} % de humedad"))
def calibrated_measurement(edge, temperature, humidity):
    body = edge["response"].get_json()
    assert body["calibratedTemperature"] == pytest.approx(temperature)
    assert body["calibratedHumidity"] == pytest.approx(humidity)


@then(parsers.parse('la lectura queda con el estado "{status}"'))
def reading_status(edge, status):
    assert edge["response"].get_json()["status"] == status


@then(parsers.parse("hay {count:d} lecturas descartadas"))
def discarded_readings(edge, count):
    assert len(edge["client"].get("/api/v1/edge/readings/discarded").get_json()) == count


@then(parsers.parse("la primera lectura conserva una temperatura calibrada de {temperature:g} °C"))
def first_reading_unchanged(edge, temperature):
    stored = {r["id"]: r for r in edge["client"].get("/api/v1/edge/readings").get_json()}
    assert stored[edge["readings"][0]]["calibratedTemperature"] == pytest.approx(temperature)


@then(parsers.parse("se publicaron {count:d} mediciones en la API central"))
def published_count(cloud, count):
    assert len(cloud.published) == count


@then(parsers.parse("la respuesta tiene el código {status:d}"))
def response_status(edge, status):
    assert edge["response"].status_code == status, edge["response"].get_json()


# ---------- TS03: Local Buffer y sincronización ----------

@given(parsers.parse('que el nodo "{device}" envió una lectura capturada hace {minutes:d} minutos'))
@when(parsers.parse('el nodo "{device}" envía una lectura capturada hace {minutes:d} minutos'))
def send_captured_reading(edge, clock, device, minutes):
    response = _send_reading(edge, clock, device, minutes_ago=minutes)
    assert response.status_code == 201, response.get_json()


@given("que el gateway sincronizó el Local Buffer")
@when("el gateway sincroniza el Local Buffer")
def sync_buffer(edge):
    edge["response"] = edge["client"].post("/api/v1/edge/buffer/sync")
    assert edge["response"].status_code == 200


@then(parsers.parse("el Local Buffer tiene {count:d} mediciones pendientes"))
def pending_count(edge, count):
    assert edge["client"].get("/api/v1/edge/buffer").get_json()["pendingCount"] == count


@then(parsers.parse('el Local Buffer tiene el estado "{status}"'))
def buffer_status(edge, status):
    assert edge["client"].get("/api/v1/edge/buffer").get_json()["status"] == status


@then(parsers.parse("la medición pendiente más antigua fue capturada hace {minutes:d} minutos"))
def oldest_pending(edge, clock, minutes):
    buffer = edge["client"].get("/api/v1/edge/buffer").get_json()
    assert buffer["oldestPendingAt"] == _iso_minutes_ago(clock, minutes)


@then(parsers.parse("se sincronizan {count:d} mediciones"))
def synced_count(edge, count):
    assert edge["response"].get_json()["synced"] == count


@then(parsers.parse("el lote enviado conserva las marcas temporales de hace {first:d}, {second:d} y {third:d} "
                    "minutos en orden cronológico"))
def batch_keeps_original_timestamps(cloud, clock, first, second, third):
    expected = [clock.now - timedelta(minutes=m) for m in (first, second, third)]
    assert cloud.batches[0] == expected


@then(parsers.parse("la API central recibió {count:d} lotes"))
def batches_received(cloud, count):
    assert len(cloud.batches) == count


# ---------- US03: estado de los Sensor Nodes ----------

@then(parsers.parse('el nodo "{device}" tiene el estado "{status}"'))
def node_status(edge, device, status):
    nodes = {n["deviceCode"]: n for n in edge["client"].get("/api/v1/edge/sensor-nodes").get_json()}
    assert nodes[device]["status"] == status


@given(parsers.parse('que el nodo "{device}" aparece entre los nodos Offline'))
@then(parsers.parse('el nodo "{device}" aparece entre los nodos Offline'))
def node_is_offline(edge, device):
    offline = edge["client"].get("/api/v1/edge/sensor-nodes/offline").get_json()
    assert device in [n["deviceCode"] for n in offline]


@then("no hay nodos Offline")
def no_offline_nodes(edge):
    assert edge["client"].get("/api/v1/edge/sensor-nodes/offline").get_json() == []
