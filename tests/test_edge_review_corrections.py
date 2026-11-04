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
