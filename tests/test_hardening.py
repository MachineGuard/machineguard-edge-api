from datetime import timedelta
from conftest import post_reading


def test_credential_expiry_is_unknown_without_trusted_metadata(client):
    state=client.get('/api/v1/edge/health').json['delivery']
    assert state['credentialExpiry']['status']=='UNKNOWN'
    assert state['credentialExpiry']['expiresAt'] is None


def test_trusted_expiry_warning_and_expiration_preserve_pending(client,clock,cloud):
    services=client.client.application.config['services']
    services.config.device_credential_expires_at=(clock()+timedelta(seconds=60)).isoformat()+'Z'
    services.config.credential_warning_seconds=120
    cloud.online=False;post_reading(client)
    health=client.get('/api/v1/edge/health').json['delivery']
    assert health['credentialExpiry']['status']=='EXPIRING'
    assert health['pending']==1
    clock.advance(seconds=61)
    state=client.get('/api/v1/edge/health').json['delivery']
    assert state['credentialExpiry']['status']=='EXPIRED' and state['pending']==1
    assert state['quarantined']==0


def test_invalid_expiry_metadata_fails_at_startup(make_client):
    import pytest
    from edge_api.config import Config
    for value in ['not-a-time','2026-11-04T12:00:00']:
        config=Config(admin_key='x'*32)
        config.device_credential_expires_at=value
        with pytest.raises(ValueError):config.validate()
