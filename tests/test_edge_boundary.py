"""Security and validation regressions for the node/administration boundary."""
import os
import runpy
import secrets
import sqlite3

import pytest

from edge_api import create_app
from edge_api.config import Config
from edge_api.infrastructure.models import database

BASE = '/api/v1/edge'


@pytest.fixture
def boundary(tmp_path, monkeypatch, cloud, clock):
    key = secrets.token_urlsafe(32)
    monkeypatch.setenv('EDGE_ADMIN_KEY', key)
    monkeypatch.setenv('EDGE_ADMIN_ID', 'bench-operator')
    config = Config(database_path=str(tmp_path / 'boundary.db'), enable_availability_loop=False)
    app = create_app(config, cloud=cloud, clock=clock)
    client = app.test_client()
    admin = {'X-Edge-Admin-Key': key}
    tokens = {}
    for code in ['node-a', 'node-b']:
        response = client.post(BASE + '/sensor-nodes', headers=admin,
                               json={'deviceCode': code, 'samplingIntervalSeconds': 10})
        assert response.status_code == 201
        # Baseline has no issued credential; keep exercising its open endpoints.
        tokens[code] = response.json.get('nodeCredential', 'pre-f1-no-issued-credential')
    yield app, client, admin, tokens
    database.close()


def send(client, tokens, code='node-a', **values):
    return client.post(BASE + '/readings', headers={'X-Node-Credential': tokens[code]},
                       json={'deviceCode': code, 'temperature': 5, 'humidity': 50, **values})


@pytest.mark.parametrize('credential', [None, 'invalid-node-credential'])
def test_ac01_missing_or_invalid_node_credential_cannot_write(boundary, credential):
    _, c, admin, _ = boundary
    headers = {} if credential is None else {'X-Node-Credential': credential}
    response = c.post(BASE + '/readings', headers=headers,
                      json={'deviceCode': 'node-a', 'temperature': 5, 'humidity': 50})
    assert response.status_code == 401
    assert c.get(BASE + '/readings', headers=admin).json == []


def test_ac01_credential_is_bound_to_one_device_and_never_disclosed(boundary):
    _, c, admin, tokens = boundary
    response = c.post(BASE + '/readings', headers={'X-Node-Credential': tokens['node-a']},
                      json={'deviceCode': 'node-b', 'temperature': 5, 'humidity': 50})
    assert response.status_code == 403
    assert c.get(BASE + '/readings', headers=admin).json == []
    assert send(c, tokens).status_code == 201
    assert 'nodeCredential' not in c.get(BASE + '/calibration/node-a', headers=admin).json


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/sensor-nodes', {'deviceCode': 'intruder'}),
    ('PUT', '/calibration/node-a/offset', {'temperatureOffset': 1, 'humidityOffset': 0}),
    ('POST', '/buffer/sync', {}),
    ('GET', '/buffer', None), ('GET', '/readings', None),
    ('GET', '/readings/discarded', None), ('GET', '/calibration/node-a', None),
    ('GET', '/calibration/node-a/plausibility-range', None),
    ('GET', '/sensor-nodes', None), ('GET', '/sensor-nodes/offline', None),
    ('GET', '/health', None),
    ('GET', '/calibration/node-a/changes', None),
    ('POST', '/sensor-nodes/node-a/credentials', None),
])
def test_ac02_administration_denies_anonymous_and_node_identity(boundary, method, path, body):
    _, c, _, tokens = boundary
    assert c.open(BASE + path, method=method, json=body).status_code == 401
    assert c.open(BASE + path, method=method, json=body,
                  headers={'X-Node-Credential': tokens['node-a']}).status_code == 403


def test_admin_key_is_not_a_node_credential_and_mixed_identities_are_denied(boundary):
    _, c, admin, tokens = boundary
    body = {'deviceCode': 'node-a', 'temperature': 5, 'humidity': 50}
    assert c.post(BASE + '/readings', json=body, headers=admin).status_code == 403
    assert c.post(BASE + '/readings', json=body,
                  headers={**admin, 'X-Node-Credential': tokens['node-a']}).status_code == 401
    assert c.get(BASE + '/buffer', headers={'X-Edge-Admin-Key': 'incorrect'}).status_code == 401


@pytest.mark.parametrize('raw', ['NaN', 'Infinity', '-Infinity', '1e999', 'true', '"x"', 'null', '100000'])
def test_ac03_invalid_offset_returns_400_and_preserves_profile(boundary, raw):
    _, c, admin, _ = boundary
    before = c.get(BASE + '/calibration/node-a', headers=admin).json
    r = c.put(BASE + '/calibration/node-a/offset', headers=admin, content_type='application/json',
              data='{"temperatureOffset":' + raw + ',"humidityOffset":0}')
    assert r.status_code == 400
    assert c.get(BASE + '/calibration/node-a', headers=admin).json == before


@pytest.mark.parametrize('body', [{'temperatureOffset': 10.01, 'humidityOffset': 0},
                                {'temperatureOffset': 0, 'humidityOffset': -20.01}])
def test_ac03_offset_limits_apply_on_registration_too(boundary, body):
    _, c, admin, _ = boundary
    assert c.post(BASE + '/sensor-nodes', headers=admin, json={'deviceCode': 'bad', **body}).status_code == 400
    assert c.get(BASE + '/calibration/bad', headers=admin).status_code == 404


@pytest.mark.parametrize('raw', ['[]', 'null', '{', '{"batchSize":"x"}', '{"batchSize":true}',
                                '{"batchSize":1.5}', '{"batchSize":501}'])
def test_malformed_sync_requests_return_400(boundary, raw):
    _, c, admin, _ = boundary
    assert c.post(BASE + '/buffer/sync', headers=admin, data=raw, content_type='application/json').status_code == 400


@pytest.mark.parametrize('interval', ['x', True, 1.5, None, 2**70])
def test_malformed_sampling_interval_returns_400(boundary, interval):
    _, c, admin, _ = boundary
    assert c.post(BASE + '/sensor-nodes', headers=admin,
                  json={'deviceCode': 'bad', 'samplingIntervalSeconds': interval}).status_code == 400


def test_overflowing_date_returns_400(boundary):
    _, c, _, tokens = boundary
    assert send(c, tokens, capturedAt='0001-01-01T00:00:00+14:00').status_code == 400


@pytest.mark.parametrize('raw', ['NaN', 'Infinity', '-Infinity', '1e999'])
def test_nonfinite_json_is_rejected_even_in_unused_fields(boundary, raw):
    _, c, _, tokens = boundary
    body = '{"deviceCode":"node-a","temperature":5,"humidity":50,"ignored":' + raw + '}'
    assert c.post(BASE + '/readings', headers={'X-Node-Credential': tokens['node-a']},
                  data=body, content_type='application/json').status_code == 400


@pytest.mark.parametrize('temperature,humidity,offset', [(5, 99.5, (0, 2.5)), (79.5, 50, (2.5, 0)),
                                                       (-39.5, 50, (-2.5, 0)), (5, .5, (0, -2.5))])
def test_ac04_calibrated_values_outside_physical_range_are_not_published(boundary, cloud, temperature, humidity, offset):
    _, c, admin, tokens = boundary
    assert c.put(BASE + '/calibration/node-a/offset', headers=admin,
                  json={'temperatureOffset': offset[0], 'humidityOffset': offset[1]}).status_code == 200
    r = send(c, tokens, temperature=temperature, humidity=humidity)
    assert r.status_code == 202
    assert r.json['status'] == 'DISCARDED'
    assert cloud.published == []


def test_implausible_reading_does_not_restore_offline(boundary, clock):
    _, c, admin, tokens = boundary
    send(c, tokens)
    clock.advance(seconds=11)
    assert len(c.get(BASE + '/sensor-nodes/offline', headers=admin).json) == 2
    assert send(c, tokens, temperature=900).status_code == 202
    codes = [n['deviceCode'] for n in c.get(BASE + '/sensor-nodes/offline', headers=admin).json]
    assert 'node-a' in codes


def test_implausible_reading_does_not_hide_silence_before_offline_check(boundary, clock):
    _, c, admin, tokens = boundary
    send(c, tokens)
    clock.advance(seconds=11)
    assert send(c, tokens, temperature=900).status_code == 202
    codes = [n['deviceCode'] for n in c.get(BASE + '/sensor-nodes/offline', headers=admin).json]
    assert 'node-a' in codes


def test_calibration_changes_record_authenticated_actor_time_and_old_values(boundary, clock):
    _, c, admin, _ = boundary
    clock.advance(seconds=1)
    assert c.put(BASE + '/calibration/node-a/offset', headers=admin,
                  json={'temperatureOffset': 1, 'humidityOffset': 2, 'actor': 'forged'}).status_code == 200
    r = c.get(BASE + '/calibration/node-a/changes', headers=admin)
    assert r.status_code == 200
    change = r.json[-1]
    assert change['actor'] == 'bench-operator'
    assert change['changedAt'] == clock.now.isoformat() + 'Z'
    assert (change['oldTemperatureOffset'], change['newTemperatureOffset']) == (0, 1)


def test_node_credentials_are_hashed_rotatable_and_survive_restart(boundary, cloud, clock):
    app, c, admin, tokens = boundary
    r = c.post(BASE + '/sensor-nodes/node-a/credentials', headers=admin)
    assert r.status_code == 200
    next_token = r.json['nodeCredential']
    assert 'no-store' in r.headers['Cache-Control']
    assert send(c, tokens).status_code == 401
    tokens['node-a'] = next_token
    database.close()
    stored = sqlite3.connect(app.config['services'].config.database_path)
    dump = '\n'.join(stored.iterdump()); stored.close()
    secret_was_not_stored = next_token not in dump
    assert secret_was_not_stored
    restarted = create_app(app.config['services'].config, cloud=cloud, clock=clock).test_client()
    assert send(restarted, tokens).status_code == 201


def test_body_limit_is_enforced(tmp_path, monkeypatch, cloud, clock):
    monkeypatch.setenv('EDGE_ADMIN_KEY', secrets.token_urlsafe(32))
    monkeypatch.setenv('EDGE_MAX_CONTENT_BYTES', '128')
    c = create_app(Config(database_path=str(tmp_path/'limit.db'), enable_availability_loop=False), cloud, clock).test_client()
    assert c.post(BASE + '/sensor-nodes', headers={'X-Edge-Admin-Key': os.environ['EDGE_ADMIN_KEY']},
                  json={'deviceCode': 'x', 'padding': 'a'*500}).status_code == 413


def test_startup_requires_admin_key_and_defaults_to_loopback(tmp_path, monkeypatch, cloud, clock):
    monkeypatch.delenv('EDGE_ADMIN_KEY', raising=False)
    with pytest.raises(ValueError, match='EDGE_ADMIN_KEY'):
        create_app(Config(database_path=str(tmp_path/'no-key.db'), enable_availability_loop=False), cloud, clock)
    monkeypatch.setenv('EDGE_ADMIN_KEY', secrets.token_urlsafe(32))
    assert Config().bind_host == '127.0.0.1'


def test_registration_rejects_non_string_device_code(boundary):
    _, c, admin, _ = boundary
    assert c.post(BASE + '/sensor-nodes', headers=admin, json={'deviceCode': ['not-a-code']}).status_code == 400


def test_server_entrypoint_uses_loopback_and_explicit_bind_override(tmp_path, monkeypatch):
    monkeypatch.setenv('EDGE_ADMIN_KEY', secrets.token_urlsafe(32))
    monkeypatch.setenv('EDGE_DATABASE_PATH', str(tmp_path/'entrypoint.db'))
    monkeypatch.delenv('EDGE_BIND_HOST', raising=False)
    calls = []
    monkeypatch.setattr('flask.Flask.run', lambda self, **kw: calls.append(kw['host']))
    runpy.run_path('run.py', run_name='__main__')
    monkeypatch.setenv('EDGE_BIND_HOST', '127.0.0.2')
    runpy.run_path('run.py', run_name='__main__')
    assert calls == ['127.0.0.1', '127.0.0.2']


def test_migration_preserves_legacy_evidence_and_needs_explicit_node_provisioning(tmp_path, monkeypatch, cloud, clock):
    from edge_api.infrastructure.models import (CalibrationProfileModel, SensorReadingModel,
        LocalBufferEntryModel, OfflineNodeModel, SyncStateModel)
    path = str(tmp_path/'legacy.db')
    if not database.is_closed(): database.close()
    database.init(path); database.connect()
    tables = [CalibrationProfileModel, SensorReadingModel, LocalBufferEntryModel, OfflineNodeModel, SyncStateModel]
    database.create_tables(tables)
    profile = CalibrationProfileModel.create(sensor_node_id='legacy', temperature_offset=0, humidity_offset=0,
        temperature_min=-40, temperature_max=80, humidity_min=0, humidity_max=100, sampling_interval_seconds=10,
        calibrated_at=clock.now, updated_at=clock.now)
    reading = SensorReadingModel.create(sensor_node_id='legacy', calibration_profile=profile,
        raw_temperature=5, raw_humidity=50, calibrated_temperature=5, calibrated_humidity=50,
        captured_at=clock.now, status='CALIBRATED')
    LocalBufferEntryModel.create(sensor_reading=reading, gateway_id='old', enqueued_at=clock.now)
    OfflineNodeModel.create(sensor_node_id='legacy', last_seen_at=clock.now, expected_sampling_interval=10,
                            detected_at=clock.now)
    SyncStateModel.create(gateway_id='old', last_sync_at=clock.now)
    before = {t._meta.table_name: list(database.execute_sql('SELECT * FROM '+t._meta.table_name)) for t in tables}
    database.close()
    key = secrets.token_urlsafe(32); monkeypatch.setenv('EDGE_ADMIN_KEY', key)
    config = Config(database_path=path, enable_availability_loop=False)
    c = create_app(config, cloud, clock).test_client()
    after = {t._meta.table_name: list(database.execute_sql('SELECT * FROM '+t._meta.table_name)) for t in tables}
    assert before == after
    assert database.pragma('user_version') == 1
    assert c.post(BASE+'/readings', json={'deviceCode':'legacy','temperature':5,'humidity':50}).status_code == 401
    admin = {'X-Edge-Admin-Key':key}
    issued = c.post(BASE+'/sensor-nodes/legacy/credentials', headers=admin)
    assert issued.status_code == 200
    c = create_app(config, cloud, clock).test_client()
    assert c.get(BASE+'/buffer', headers=admin).json['pendingCount'] == 1
    r = c.post(BASE+'/readings', headers={'X-Node-Credential':issued.json['nodeCredential']},
               json={'deviceCode':'legacy','temperature':5,'humidity':50})
    assert r.status_code == 201
    assert c.get(BASE+'/buffer', headers=admin).json['pendingCount'] == 2
