"""Regression cases for the three F1 audit corrections."""
import pytest
from pathlib import Path

from edge_api import create_app
from edge_api.config import Config
from edge_api.infrastructure.models import database


@pytest.mark.parametrize('path', ['/readings', '/buffer/sync'])
@pytest.mark.parametrize('fragment,repetitions', [('[', 60000), ('{"a":', 12000)], ids=['array', 'object'])
def test_r1_deep_json_returns_400(client, path, fragment, repetitions):
    response = client.post('/api/v1/edge' + path, data=fragment * repetitions,
                           content_type='application/json')
    assert response.status_code == 400


@pytest.mark.parametrize('key', ['', 'replace-with-at-least-32-random-bytes'], ids=['empty', 'example'])
def test_r2_startup_rejects_empty_and_example_admin_keys(tmp_path, monkeypatch, key):
    monkeypatch.setenv('EDGE_ADMIN_KEY', key)
    path = tmp_path / 'must-not-open.db'
    try:
        with pytest.raises(ValueError, match='EDGE_ADMIN_KEY'):
            create_app(Config(database_path=str(path), enable_availability_loop=False))
        assert not path.exists()
    finally:
        if not database.is_closed():
            database.close()


def test_r2_example_file_leaves_admin_key_empty():
    lines = Path('.env.example').read_text(encoding='utf-8').splitlines()
    values = [line.split('=', 1)[1] for line in lines if line.startswith('EDGE_ADMIN_KEY=')]
    assert values == ['']


@pytest.mark.parametrize('method,path,body', [
    ('GET', '/buffer', None), ('GET', '/health', None), ('GET', '/readings', None),
    ('GET', '/readings/discarded', None), ('GET', '/sensor-nodes', None),
    ('GET', '/sensor-nodes/offline', None), ('GET', '/calibration/esp32-01', None),
    ('GET', '/calibration/esp32-01/plausibility-range', None),
    ('GET', '/calibration/esp32-01/changes', None),
    ('POST', '/sensor-nodes', {'deviceCode': 'new-node'}),
    ('PUT', '/calibration/esp32-01/offset', {'temperatureOffset': 1, 'humidityOffset': 0}),
    ('POST', '/buffer/sync', {}), ('POST', '/sensor-nodes/esp32-01/credentials', None),
])
def test_r3_remote_admin_is_rejected_on_every_route(client, method, path, body):
    response = client.open('/api/v1/edge' + path, method=method, json=body,
                           environ_overrides={'REMOTE_ADDR': '192.168.137.50'})
    assert response.status_code == 403


@pytest.mark.parametrize('address', ['127.0.0.1', '127.0.0.2', '::1', '::ffff:127.0.0.1'])
def test_r3_loopback_admin_is_allowed(client, address):
    response = client.get('/api/v1/edge/buffer', environ_overrides={'REMOTE_ADDR': address})
    assert response.status_code == 200


@pytest.mark.parametrize('address,expected', [
    ('192.168.137.1', 200), ('2001:db8::10', 200), ('::ffff:192.168.137.1', 200),
    ('192.168.137.50', 403), ('2001:db8::11', 403),
])
def test_r3_explicit_allowlist_allows_only_named_ips(make_client, monkeypatch, address, expected):
    monkeypatch.setenv('EDGE_ADMIN_ALLOWED_IPS', '192.168.137.1, 2001:db8::10')
    client = make_client()
    response = client.get('/api/v1/edge/buffer', environ_overrides={'REMOTE_ADDR': address})
    assert response.status_code == expected


@pytest.mark.parametrize('address,forwarded,key,expected', [
    ('192.168.137.50', '127.0.0.1', 'valid', 403),
    ('192.168.137.50', '127.0.0.1', 'wrong', 403),
    ('127.0.0.1', '192.168.137.50', 'valid', 200),
    (None, '127.0.0.1', 'valid', 403),
])
def test_r3_source_check_ignores_forwarded_headers_and_precedes_key(client, address, forwarded, key, expected):
    headers = {'X-Edge-Admin-Key': client.admin_key if key == 'valid' else 'wrong', 'X-Forwarded-For': forwarded}
    response = client.get('/api/v1/edge/buffer', headers=headers, environ_overrides={'REMOTE_ADDR': address})
    assert response.status_code == expected


def test_r3_remote_node_reading_remains_allowed(client):
    response = client.post('/api/v1/edge/readings',
        json={'deviceCode': 'esp32-01', 'temperature': 5, 'humidity': 50},
        environ_overrides={'REMOTE_ADDR': '192.168.137.50'})
    assert response.status_code == 201


@pytest.mark.parametrize('allowed', ['*', '192.168.137.0/24', 'not-an-address'])
def test_r3_invalid_allowlist_fails_closed(tmp_path, monkeypatch, allowed):
    monkeypatch.setenv('EDGE_ADMIN_KEY', 'test-only-key-not-an-operational-secret')
    monkeypatch.setenv('EDGE_ADMIN_ALLOWED_IPS', allowed)
    try:
        with pytest.raises(ValueError, match='EDGE_ADMIN_ALLOWED_IPS'):
            create_app(Config(database_path=str(tmp_path / 'invalid-source.db'), enable_availability_loop=False))
    finally:
        if not database.is_closed():
            database.close()
