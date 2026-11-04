"""Regression cases for the three F1 audit corrections."""
import pytest


@pytest.mark.parametrize('path', ['/readings', '/buffer/sync'])
@pytest.mark.parametrize('fragment,repetitions', [('[', 60000), ('{"a":', 12000)], ids=['array', 'object'])
def test_r1_deep_json_returns_400(client, path, fragment, repetitions):
    response = client.post('/api/v1/edge' + path, data=fragment * repetitions,
                           content_type='application/json')
    assert response.status_code == 400
